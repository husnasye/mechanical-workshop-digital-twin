"""The processing pipeline: clean -> store -> update state -> analytics -> write back to the graph."""
import logging
import threading
import time
from collections import deque
from datetime import datetime, timedelta, timezone

from analytics import NOISE_AVG_SAMPLES, evaluate_rules, machine_kpis, zone_risk
from cleaning import Rejected, clean, iso, parse_ts
from config import DEDUP_SECONDS, KPI_WINDOW_MIN, LIVE_TIMEOUT_SEC, MACHINES, ZONE_ID
from storage import STATUS_SIGNAL

log = logging.getLogger("twin")
ZONE_SIGNALS = ("temperature_c", "humidity_pct", "co2_ppm", "noise_db")


def availability_state(status, in_maintenance=False):
    """Can the machine be used right now?  IDLE -> Available, RUNNING -> Occupied,
    DOWN or under maintenance -> Out of service."""
    if in_maintenance or status == "DOWN":
        return "Out of service"
    return "Occupied" if status == "RUNNING" else "Available"


class Twin:
    def __init__(self, storage, graph, predictor=None):
        self.store = storage
        self.graph = graph
        self.predictor = predictor
        self.lock = threading.Lock()
        self.zone = {s: None for s in ZONE_SIGNALS}
        self.machines = {m: "IDLE" for m in MACHINES}
        self.source = None
        self.timestamp = None
        self.stats = {"accepted": 0, "rejected": 0}
        self.prediction = None
        self.recent = {}  # topic -> deque of (message_id, dedup_key, received epoch seconds)
        self.noise_window = deque(maxlen=NOISE_AVG_SAMPLES)
        self.maintenance = {m: False for m in MACHINES}
        self.last_rx = None      # wall-clock time of the last accepted message
        self.run_started = None  # event time of the first message of the current run
        # True while Memgraph may be missing writes (at startup, or after a failed write).
        # The next successful write is followed by a resync of all alerts from SQLite.
        self.graph_dirty = True
        self._restore()

    def live(self):
        return self.last_rx is not None and time.time() - self.last_rx <= LIVE_TIMEOUT_SEC

    def _start_run(self, ev):
        """First message after a quiet gap: start a fresh run. Alerts left open by the previous
        run are closed, so the dashboard starts clean when the replay is started again."""
        stmts = []
        self.run_started = ev["event_ts"]
        self.noise_window.clear()
        self.maintenance = {m: False for m in MACHINES}
        closed = self.store.active_alerts()
        for a in closed:
            self.store.clear_alert(a["alert_id"], ev["event_ts"])
            stmts.append(self.graph.alert_clear(a["alert_id"], ev["event_ts"]))
        # reset the same flags in Memgraph so the graph matches the twin's fresh state
        stmts += [
            ("MATCH (n) WHERE n:Zone OR n:Machine SET n.alertStatus=false", {}),
            ("MATCH (m:Machine) SET m.maintenanceActive=false", {}),
            # a maintenance event still open when the last run stopped will never get its COMPLETED message
            ("MATCH (e:MaintenanceEvent {state:'STARTED'}) SET e.state='INTERRUPTED', e.interruptedAt=$ts",
             {"ts": ev["event_ts"]}),
        ]
        stmts += [self._availability_stmt(mid) for mid in MACHINES]
        log.info("RUN      new run started at %s (closed %d open alert(s))", self.run_started, len(closed))
        return stmts

    def _is_duplicate(self, topic, message_id, dedup_key):
        """Same message_id or same original reading on this topic within DEDUP_SECONDS?"""
        now = time.time()
        if topic not in self.recent:  # after a restart, warm up from SQLite
            q = deque()
            for mid, key, ts in self.store.recent_ids(topic, DEDUP_SECONDS):
                q.append((mid, key, parse_ts(ts).timestamp()))
            self.recent[topic] = q
        q = self.recent[topic]
        while q and q[0][2] < now - DEDUP_SECONDS:
            q.popleft()
        return any(mid == message_id or key == dedup_key for mid, key, _ in q)

    def _write_graph(self, stmts):
        """Write this event to Memgraph. If earlier writes were skipped, repair the alerts afterwards
        so the graph never keeps an alert that SQLite has closed (or misses one it opened)."""
        if not self.graph.run(stmts):
            self.graph_dirty = True
            return
        if self.graph_dirty and self.graph.run(self.graph.alerts_resync(self.store.all_alerts())):
            self.graph_dirty = False
            log.info("GRAPH    alerts resynced from SQLite")

    def _restore(self):
        """Rebuild the in-memory latest state from SQLite after a restart."""
        for r in self.store.latest():
            if r["signal"] in self.zone:
                self.zone[r["signal"]] = r["value"]
            for mid, sig in STATUS_SIGNAL.items():
                if r["signal"] == sig:
                    self.machines[mid] = r["value_text"]
            if self.timestamp is None or r["event_ts"] > self.timestamp:
                self.timestamp, self.source = r["event_ts"], r["source"]

    # ---------------------------------------------------------------- pipeline
    def handle(self, topic, payload):
        with self.lock:
            try:
                ev = clean(topic, payload, lambda mid, key: self._is_duplicate(topic, mid, key))
            except Rejected as r:
                self.stats["rejected"] += 1
                self.store.save_rejected(topic, r.message_id, r.reason, payload)
                log.warning("REJECTED %-28s %s", topic, r.reason)
                return None
            stmts = [] if self.live() else self._start_run(ev)
            self.last_rx = time.time()
            self.store.save_event(ev)
            self.recent[topic].append((ev["message_id"], ev["dedup_key"], time.time()))
            self.stats["accepted"] += 1
            stmts += self._apply(ev)
            self._write_graph(stmts)
            note = f" ({'; '.join(ev['notes'])})" if ev["notes"] else ""
            log.info("ACCEPTED %-28s seq=%s %s%s", topic, ev.get("seq"), ev["values"], note)
            return ev

    def _apply(self, ev):
        """Update the in-memory twin and return the Cypher statements that mirror it in Memgraph."""
        stmts = []
        self.timestamp, self.source = ev["event_ts"], ev["source"]
        if ev["kind"] == "maintenance":
            mid = ev["asset_id"]
            self.maintenance[mid] = ev["values"]["state"] == "STARTED"
            return [self.graph.maintenance_upsert(ev), self._availability_stmt(mid)]

        for signal, value in ev["values"].items():
            if ev["asset_id"] == ZONE_ID:
                self.zone[signal] = value
            else:
                self.machines[ev["asset_id"]] = value
            stmts.append(self.graph.reading_upsert(ev, signal, value))

        # rule alerts: open when a rule starts firing, clear when it recovers
        active = {a["rule"]: a for a in self.store.active_alerts()}
        smoothed = {}
        if "noise_db" in ev["values"]:
            self.noise_window.append(ev["values"]["noise_db"])
            smoothed["noise_db"] = sum(self.noise_window) / len(self.noise_window)
        rules = evaluate_rules(ev, self.graph.thresholds(), ZONE_ID, MACHINES, smoothed)
        for key, rule in rules.items():
            if rule["firing"] and key not in active:
                alert = {"id": f"AL-{self.store.next_alert_number():03d}", "rule": key, "asset": rule["asset"],
                         "severity": rule["severity"], "description": rule["description"],
                         "value": rule["value"], "createdAt": ev["event_ts"]}
                self.store.open_alert(alert)
                stmts.append(self.graph.alert_open(alert))
                log.warning("ALERT    %s %s (value %s)", alert["id"], alert["description"], rule["value"])
            elif rule["clear"] and key in active:
                self.store.clear_alert(active[key]["alert_id"], ev["event_ts"])
                stmts.append(self.graph.alert_clear(active[key]["alert_id"], ev["event_ts"]))
                log.info("CLEARED  %s %s", active[key]["alert_id"], active[key]["description"])

        active_now = self.store.active_alerts()
        alerting_assets = {a["asset"] for a in active_now}
        risk = zone_risk(active_now, self.machines.values(), ZONE_ID)

        if ev["asset_id"] == ZONE_ID:
            stmts.append(self.graph.zone_update(ev["values"], risk, ZONE_ID in alerting_assets, ev["event_ts"]))
        else:
            mid = ev["asset_id"]
            k = self.kpis_for(mid)
            end = datetime.now(timezone.utc)
            start = iso(end - timedelta(minutes=KPI_WINDOW_MIN))
            stmts += [
                self.graph.machine_update(mid, self.machines[mid], k["utilization"], k["availability"],
                                          mid in alerting_assets, ev["event_ts"]),
                self.graph.kpi_upsert(mid, "utilization", k["utilization"], start, iso(end)),
                self.graph.kpi_upsert(mid, "availability", k["availability"], start, iso(end)),
                self._availability_stmt(mid),
                # machine status changes can move the zone risk too
                ("MATCH (z:Zone {zoneId:$zone}) SET z.riskLevel=$risk", {"risk": risk}),
            ]
        overall = {"LOW": "OK", "MEDIUM": "WARNING", "HIGH": "CRITICAL"}[risk]
        stmts.append(self.graph.workshop_update(overall))

        if self.predictor is not None:
            self.prediction = self.predictor.predict(self)
            if self.prediction:
                stmts.append(("MATCH (m:Machine {machineId:$mid}) SET m.predictedStatus=$p, m.downRiskPct=$r",
                              {"mid": self.prediction["machineId"], "p": self.prediction["predictedStatus"],
                               "r": self.prediction["downRiskPct"]}))
        return stmts

    def _availability_stmt(self, mid):
        state = availability_state(self.machines[mid], self.maintenance[mid])
        return ("MATCH (m:Machine {machineId:$mid}) SET m.availabilityState=$s", {"mid": mid, "s": state})

    # ---------------------------------------------------------------- read models for the API
    def kpis_for(self, mid):
        """Utilization / availability (uptime) over the KPI window, limited to the current run."""
        return machine_kpis(self.store.status_counts(mid, KPI_WINDOW_MIN, since=self.run_started))

    def state(self):
        if self.timestamp is None or any(v is None for v in self.zone.values()):
            return None  # no complete zone reading yet
        active = self.store.active_alerts()
        machines = []
        for mid, name in MACHINES.items():
            k = self.kpis_for(mid)
            machines.append({"machineId": mid, "name": name, "status": self.machines[mid],
                             "availabilityState": availability_state(self.machines[mid], self.maintenance[mid]),
                             "inMaintenance": self.maintenance[mid],
                             "utilization": k["utilization"], "availability": k["availability"]})
        zone = {"zoneId": ZONE_ID, **self.zone, "riskLevel": zone_risk(active, self.machines.values(), ZONE_ID)}
        age = None if self.last_rx is None else round(time.time() - self.last_rx, 1)
        return {"timestamp": self.timestamp, "source": self.source, "live": self.live(),
                "lastMessageAgeSec": age, "runStartedAt": self.run_started, "zone": zone,
                "machines": machines, "activeAlerts": len(active)}
