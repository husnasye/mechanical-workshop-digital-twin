"""All REST calls for the dashboard live in this file.

Mock mode returns made-up data in the response shapes we ASSUME Zafar's API
will use. When his real JSON arrives, compare it with the mock_* functions
below and rename fields here (or in app.py) - nothing else needs to change.

Assumed shapes
  /api/state    {timestamp, source, zone:{zoneId,temperature_c,humidity_pct,co2_ppm,noise_db,riskStatus},
                 machines:[{machineId,name,status,utilization,availability}], activeAlerts}
  /api/history  {signal, points:[{timestamp,value}]}   (also lathe_status / drill_status)
  /api/alerts   [{id,severity,description,asset,createdAt,active}]
  /api/kpis     {windowMinutes, machines:[{machineId,name,utilization,availability}]}
  /api/graph    {nodes:[{id,label,properties,alertActive}], edges:[{source,target,type}]}
  /api/predict  {machineId, predictedStatus, downRiskPct}   (optional)
"""
import os
import random
from datetime import datetime, timedelta, timezone

import requests

API_BASE = os.getenv("API_BASE", "http://localhost:8000")
DEFAULT_MODE = os.getenv("DT_MODE", "mock")
STATUS_SIGNAL = {"machine1": "lathe_status", "machine2": "drill_status"}


class ApiError(Exception):
    pass


class Client:
    def __init__(self, mode="mock", base=API_BASE, scenario="normal"):
        self.live = mode == "live"
        self.base = base.rstrip("/")
        self.scenario = scenario

    def _get(self, path, **params):
        try:
            r = requests.get(self.base + path, params=params or None, timeout=3)
            r.raise_for_status()
            return r.json()
        except Exception as e:  # network, HTTP or JSON errors all surface the same way
            raise ApiError(f"{path} failed: {e}") from e

    def state(self):
        return self._get("/api/state") if self.live else mock_state(self.scenario)

    def history(self, signal, minutes=30):
        if self.live:
            return self._get("/api/history", signal=signal, minutes=minutes)
        return mock_history(self.scenario, signal, minutes)

    def alerts(self):
        return self._get("/api/alerts") if self.live else mock_alerts(self.scenario)

    def kpis(self):
        return self._get("/api/kpis") if self.live else mock_kpis(self.scenario)

    def graph(self):
        return self._get("/api/graph") if self.live else mock_graph(self.scenario)

    def predict(self):
        try:
            return self._get("/api/predict") if self.live else mock_predict(self.scenario)
        except ApiError:
            return None  # optional endpoint


# ---------------------------------------------------------------- mock data
NORMAL = {"temperature_c": 26.5, "humidity_pct": 48.0, "co2_ppm": 820.0, "noise_db": 68.0}
SPREAD = {"temperature_c": 0.6, "humidity_pct": 2.0, "co2_ppm": 40.0, "noise_db": 3.0}
ALERT_VALUES = {"temperature_c": 31.8, "co2_ppm": 1340.0, "noise_db": 88.0}


def _now():
    return datetime.now(timezone.utc)


def _iso(t):
    return t.isoformat(timespec="seconds").replace("+00:00", "Z")


def _zone_values(s):
    v = dict(NORMAL)
    if s == "alert":
        v.update(ALERT_VALUES)
    return v


def _machines(s):
    m = [
        {"machineId": "machine1", "name": "Lathe", "status": "RUNNING", "utilization": 78.4, "availability": 96.0},
        {"machineId": "machine2", "name": "Drill", "status": "RUNNING", "utilization": 64.2, "availability": 98.1},
    ]
    if s == "lathe_down":
        m[0].update(status="DOWN", utilization=52.3, availability=71.4)
    return m


def mock_alerts(s):
    t = _now()

    def a(i, sev, desc, asset, mins, active=True):
        return {"id": f"AL-{i:03d}", "severity": sev, "description": desc, "asset": asset,
                "createdAt": _iso(t - timedelta(minutes=mins)), "active": active}

    out = [a(1, "high", "Temperature above 30 °C", "zone1", 95, False)]
    if s == "alert":
        out += [a(2, "high", "Temperature above 30 °C", "zone1", 4),
                a(3, "high", "CO2 above 1200 ppm", "zone1", 3),
                a(4, "medium", "Noise above 85 dB", "zone1", 2)]
    if s == "lathe_down":
        out.append(a(5, "high", "Lathe is DOWN", "machine1", 2))
    return out


def mock_state(s):
    active = sum(1 for a in mock_alerts(s) if a["active"])
    risk = {"normal": "LOW", "alert": "HIGH", "lathe_down": "MEDIUM"}[s]
    zone = {"zoneId": "zone1", **_zone_values(s), "riskStatus": risk}
    return {"timestamp": _iso(_now()), "source": "synthetic", "zone": zone,
            "machines": _machines(s), "activeAlerts": active}


def mock_history(s, signal, minutes):
    n, now, target = max(2, int(minutes) * 2), _now(), _zone_values(s)
    pts = []
    for k in range(n - 1, -1, -1):  # k = steps back from now (30 s each)
        ts = now - timedelta(seconds=30 * k)
        slot = int(ts.timestamp() // 30)
        if signal in STATUS_SIGNAL.values():
            lathe = signal == "lathe_status"
            idle = slot % (40 if lathe else 50) >= (37 if lathe else 45)
            v = "IDLE" if idle else "RUNNING"
            if lathe and s == "lathe_down" and k < 30:
                v = "DOWN"
        else:
            rng = random.Random(f"{signal}{slot}")
            v = NORMAL[signal] + rng.uniform(-1, 1) * SPREAD[signal]
            if s == "alert" and k < 20 and signal in ALERT_VALUES:
                v = NORMAL[signal] + (target[signal] - NORMAL[signal]) * (1 - k / 20) + rng.uniform(-1, 1) * SPREAD[signal] * 0.3
            v = round(v, 1)
        pts.append({"timestamp": _iso(ts), "value": v})
    return {"signal": signal, "points": pts}


def mock_kpis(s):
    return {"windowMinutes": 60, "machines": [
        {k: m[k] for k in ("machineId", "name", "utilization", "availability")} for m in _machines(s)]}


def mock_predict(s):
    risk = {"normal": 12, "alert": 64, "lathe_down": 91}[s]
    return {"machineId": "machine1", "predictedStatus": "OK" if risk < 50 else "AT_RISK", "downRiskPct": risk}


def mock_graph(s):
    alerts = [a for a in mock_alerts(s) if a["active"]]
    hot = {a["asset"] for a in alerts}
    z = _zone_values(s)
    nodes = [
        ("WS-01", "Workshop", {"name": "Mechanical Workshop"}),
        ("zone1", "Zone", {"riskStatus": mock_state(s)["zone"]["riskStatus"], "temperature_c": z["temperature_c"]}),
        ("machine1", "Machine", {"type": "Lathe", "status": _machines(s)[0]["status"]}),
        ("machine2", "Machine", {"type": "Drill", "status": "RUNNING"}),
        ("ENV-01", "Sensor", {"sensorType": "environment"}),
        ("NOISE-01", "Sensor", {"sensorType": "noise"}),
        ("CSV-01", "DataSource", {"dataOrigin": "synthetic", "replayRate": "5 s"}),
        ("R-ENV", "Reading", {"signal": "temperature_c", "value": z["temperature_c"]}),
        ("R-NOISE", "Reading", {"signal": "noise_db", "value": z["noise_db"]}),
        ("KPI-M1", "KPI", {"metricName": "utilization", "value": _machines(s)[0]["utilization"]}),
        ("KPI-M2", "KPI", {"metricName": "utilization", "value": _machines(s)[1]["utilization"]}),
    ]
    edges = [
        ("WS-01", "zone1", "HAS_ZONE"), ("zone1", "machine1", "CONTAINS"), ("zone1", "machine2", "CONTAINS"),
        ("zone1", "ENV-01", "HAS_SENSOR"), ("zone1", "NOISE-01", "HAS_SENSOR"),
        ("CSV-01", "ENV-01", "FEEDS"), ("CSV-01", "NOISE-01", "FEEDS"),
        ("ENV-01", "R-ENV", "PRODUCES"), ("NOISE-01", "R-NOISE", "PRODUCES"),
        ("R-ENV", "zone1", "OBSERVES"), ("R-NOISE", "zone1", "OBSERVES"),
        ("machine1", "KPI-M1", "HAS_KPI"), ("machine2", "KPI-M2", "HAS_KPI"),
    ]
    out_nodes = [{"id": i, "label": l, "properties": p, "alertActive": i in hot} for i, l, p in nodes]
    out_edges = [{"source": a, "target": b, "type": t} for a, b, t in edges]
    for al in alerts:
        out_nodes.append({"id": al["id"], "label": "Alert", "properties": {"description": al["description"], "severity": al["severity"]}, "alertActive": True})
        out_edges.append({"source": al["asset"], "target": al["id"], "type": "TRIGGERS"})
    if s == "lathe_down":
        out_nodes.append({"id": "MT-001", "label": "MaintenanceEvent", "properties": {"reason": "Spindle fault"}, "alertActive": False})
        out_edges.append({"source": "machine1", "target": "MT-001", "type": "HAS_MAINTENANCE"})
    return {"nodes": out_nodes, "edges": out_edges}
