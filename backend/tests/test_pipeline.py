"""Offline tests: cleaning rules, alert open/clear and KPI arithmetic (no broker or Memgraph needed).

Run: python -m pytest -q   (from the server folder)
"""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from datetime import datetime, timezone  # noqa: E402

from analytics import machine_kpis  # noqa: E402
from cleaning import iso  # noqa: E402
from config import DEFAULT_THRESHOLDS  # noqa: E402
from graph import Graph  # noqa: E402
from storage import Storage  # noqa: E402
from twin import Twin  # noqa: E402


class FakeGraph(Graph):
    """Records Cypher instead of sending it to Memgraph."""
    def __init__(self):
        super().__init__("bolt://unused")
        self.sent = []

    def run(self, statements):
        self.sent.extend(statements)
        return True

    def thresholds(self):
        return dict(DEFAULT_THRESHOLDS)


SEQ = [0]


def msg(kind, asset, values, src_ts, **extra):
    SEQ[0] += 1
    body = {"message_id": f"{kind}-{SEQ[0]:06d}", "seq": SEQ[0], "asset_id": asset,
            "source_timestamp": src_ts, "event_timestamp": iso(datetime.now(timezone.utc)), "measurements": values}
    body.update(extra)
    return f"workshop/{asset}/{kind}", json.dumps(body).encode()


def make_twin(tmp_path):
    return Twin(Storage(str(tmp_path / "t.db")), FakeGraph())


def env(t, **over):
    v = {"temperature_c": 26.0, "humidity_pct": 45.0, "co2_ppm": 900.0}
    v.update(over)
    return msg("environment", "zone1", v, t)


def test_valid_message_accepted(tmp_path):
    tw = make_twin(tmp_path)
    ev = tw.handle(*env("2023-07-10T08:50:02Z"))
    assert ev is not None
    assert ev["source"] == "CSV-01"  # no "source" in the payload: tagged with the DataSource id
    assert tw.zone["temperature_c"] == 26.0


def test_planted_bad_rows_rejected(tmp_path):
    tw = make_twin(tmp_path)
    assert tw.handle(*env("2023-07-10T09:10:02Z", humidity_pct=None)) is None        # bad_missing_value
    assert tw.handle(*env("2023-07-10T09:33:22Z", temperature_c=150.0)) is None      # bad_out_of_range
    assert tw.handle(*env("2023-07-10T09:45:02Z", co2_ppm=0.0)) is None              # bad_sensor_dropout
    assert tw.handle(*env("2023-07-10T09:40:02Z")) is not None
    assert tw.handle(*env("2023-07-10T09:40:02Z")) is None                           # bad_duplicate
    reasons = [r["reason"] for r in tw.store.rejected()]
    assert any("missing value: humidity_pct" in r for r in reasons)
    assert any("out of range: temperature_c" in r for r in reasons)
    assert any("out of range: co2_ppm" in r for r in reasons)
    assert any("duplicate" in r for r in reasons)


def test_same_message_id_rejected(tmp_path):
    tw = make_twin(tmp_path)
    topic, payload = env("2023-07-10T08:50:02Z")
    assert tw.handle(topic, payload) is not None
    assert tw.handle(topic, payload) is None


def test_replay_can_be_rerun_after_window(tmp_path, monkeypatch):
    """The same message 61 s later (a second replay run) is accepted, also on a quiet topic."""
    import twin as twin_module
    clock = [1_000_000.0]
    monkeypatch.setattr(twin_module.time, "time", lambda: clock[0])
    tw = make_twin(tmp_path)
    topic, payload = env("2023-07-10T08:50:02Z")
    assert tw.handle(topic, payload) is not None
    clock[0] += 30
    assert tw.handle(topic, payload) is None       # still inside the 60 s window
    clock[0] += 31
    assert tw.handle(topic, payload) is not None   # window passed


def test_maintenance_message(tmp_path):
    tw = make_twin(tmp_path)
    ev = tw.handle(*msg("maintenance", "machine1",
                        {"event_id": "MT-001", "event_type": "corrective", "state": "STARTED",
                         "start": "2023-07-10T09:53:22Z", "end": "2023-07-10T10:03:22Z", "reason": "Spindle"},
                        "2023-07-10T09:53:22Z", sensor_id="CMMS"))
    assert ev["values"]["state"] == "STARTED"
    assert any("HAS_MAINTENANCE" in q for q, _ in tw.graph.sent)


def test_status_normalised(tmp_path):
    tw = make_twin(tmp_path)
    ev = tw.handle(*msg("status", "machine1", {"status": "running"}, "2023-07-10T10:08:22Z"))  # bad_status_format
    assert ev["values"]["status"] == "RUNNING"
    assert tw.handle(*msg("status", "machine1", {"status": "BROKEN"}, "2023-07-10T10:08:32Z")) is None


def test_alert_opens_and_clears(tmp_path):
    tw = make_twin(tmp_path)
    tw.handle(*env("2023-07-10T10:00:00Z", temperature_c=31.0))
    active = tw.store.active_alerts()
    assert [a["rule"] for a in active] == ["temp_high"]
    tw.handle(*msg("noise", "zone1", {"noise_db": 60.0}, "2023-07-10T10:00:00Z"))  # other signal: no change
    assert len(tw.store.active_alerts()) == 1
    tw.handle(*env("2023-07-10T10:00:10Z", temperature_c=29.0))
    assert tw.store.active_alerts() == []
    assert tw.store.recent_alerts()[0]["cleared_at"] is not None


def test_alert_hysteresis(tmp_path):
    """Temp 31 opens the alert; 29.8 and 30.5 keep it (0.5 degree margin); 29.4 clears it."""
    tw = make_twin(tmp_path)
    for i, t in enumerate([31.0, 29.8, 30.5, 29.4]):
        tw.handle(*env(f"2023-07-10T10:00:{i * 10:02d}Z", temperature_c=t))
        if i < 3:
            assert len(tw.store.active_alerts()) == 1
    assert tw.store.active_alerts() == []
    assert len(tw.store.recent_alerts()) == 1  # one alert, not one per crossing


def test_noise_uses_one_minute_average(tmp_path):
    """A single 90 dB spike after quiet samples does not alert; sustained noise does."""
    tw = make_twin(tmp_path)
    for i, n in enumerate([60, 60, 60, 60, 60, 90] + [88] * 6):
        tw.handle(*msg("noise", "zone1", {"noise_db": float(n)}, f"2023-07-10T10:{i // 6:02d}:{(i % 6) * 10:02d}Z"))
        if i == 5:
            assert tw.store.active_alerts() == []
    assert [a["rule"] for a in tw.store.active_alerts()] == ["noise_high"]


def test_lathe_down_alert_and_availability(tmp_path):
    tw = make_twin(tmp_path)
    t = "2023-07-10T09:{:02d}:{:02d}Z"
    statuses = ["RUNNING"] * 6 + ["IDLE"] * 2 + ["DOWN"] * 2  # 10 samples
    for i, s in enumerate(statuses):
        tw.handle(*msg("status", "machine1", {"status": s}, t.format(i // 6, (i % 6) * 10)))
    k = tw.kpis_for("machine1")
    assert k == {"utilization": 60.0, "availability": 80.0, "samples": 10}
    assert any(a["rule"] == "machine1_down" for a in tw.store.active_alerts())
    tw.handle(*msg("status", "machine1", {"status": "RUNNING"}, t.format(5, 0)))
    assert not tw.store.active_alerts()


def test_kpi_formula():
    assert machine_kpis({}) == {"utilization": 0.0, "availability": 100.0, "samples": 0}
    assert machine_kpis({"RUNNING": 487, "IDLE": 173, "DOWN": 60}) == \
        {"utilization": 67.6, "availability": 91.7, "samples": 720}


def test_state_shape_matches_dashboard(tmp_path):
    tw = make_twin(tmp_path)
    assert tw.state() is None
    tw.handle(*env("2023-07-10T08:50:02Z"))
    tw.handle(*msg("noise", "zone1", {"noise_db": 60.0}, "2023-07-10T08:50:02Z"))
    s = tw.state()
    assert set(s) == {"timestamp", "source", "zone", "machines", "activeAlerts"}
    assert set(s["zone"]) == {"zoneId", "temperature_c", "humidity_pct", "co2_ppm", "noise_db", "riskStatus"}
    assert set(s["machines"][0]) == {"machineId", "name", "status", "utilization", "availability"}
