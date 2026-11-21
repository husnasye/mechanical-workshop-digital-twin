"""Memgraph access (Bolt protocol via the neo4j Python driver).

The graph holds the live twin: static assets plus their latest properties, the latest
Reading per signal, KPI nodes, active/cleared Alerts and maintenance events.
If Memgraph is down the server keeps running (SQLite still records everything) and
retries on the next event.
"""
import logging
import time

from neo4j import GraphDatabase

from config import DATA_SOURCE_ID, DEFAULT_SENSORS, DEFAULT_THRESHOLDS, MACHINES, WORKSHOP_ID, ZONE_ID

log = logging.getLogger("graph")

# The business id property of each label (used as node id in /api/graph).
ID_KEYS = {"Workshop": "workshopId", "Zone": "zoneId", "Machine": "machineId", "Sensor": "sensorId",
           "DataSource": "sourceId", "Reading": "readingId", "Alert": "alertId",
           "MaintenanceEvent": "eventId", "KPI": "kpiId"}

# Static part of the twin. MERGE makes this safe to run again on every start, and it
# also works on top of the team's seed.cypher as long as the ids match.
STATIC = [
    ("MERGE (w:Workshop {workshopId:$ws}) ON CREATE SET w.name='Mechanical Workshop', w.overallStatus='OK'", {}),
    ("MERGE (z:Zone {zoneId:$zone}) ON CREATE SET z.name='Machining zone', z.tempMax=$tempMax, z.co2Max=$co2Max, "
     "z.noiseMax=$noiseMax, z.riskStatus='LOW', z.alertStatus=false", DEFAULT_THRESHOLDS),
    ("MATCH (w:Workshop {workshopId:$ws}), (z:Zone {zoneId:$zone}) MERGE (w)-[:HAS_ZONE]->(z)", {}),
    ("MERGE (d:DataSource {sourceId:$src}) ON CREATE SET d.fileName='workshop_data.csv', d.dataOrigin='real+synthetic', "
     "d.replayRate='5 s'", {}),
    ("MERGE (s:Sensor {sensorId:'ENV-01'}) ON CREATE SET s.sensorType='environment', s.model='SHT31 + SCD41'", {}),
    ("MERGE (s:Sensor {sensorId:'NOISE-01'}) ON CREATE SET s.sensorType='noise', s.model='Class 2 sound level meter'", {}),
    ("MATCH (z:Zone {zoneId:$zone}), (s:Sensor) WHERE s.sensorId IN ['ENV-01','NOISE-01'] MERGE (z)-[:HAS_SENSOR]->(s)", {}),
]
for _mid, _name in MACHINES.items():
    _sid = DEFAULT_SENSORS[(_mid, "status")]
    STATIC += [
        (f"MERGE (m:Machine {{machineId:'{_mid}'}}) ON CREATE SET m.machineType='{_name}', m.name='{_name}', "
         "m.operationalStatus='IDLE', m.utilization=0.0, m.availability=100.0, m.maintenanceActive=false", {}),
        (f"MERGE (s:Sensor {{sensorId:'{_sid}'}}) ON CREATE SET s.sensorType='machine status', "
         "s.model='PLC run/fault signal (current sensing)'", {}),
        (f"MATCH (z:Zone {{zoneId:$zone}}), (m:Machine {{machineId:'{_mid}'}}), (s:Sensor {{sensorId:'{_sid}'}}) "
         "MERGE (z)-[:CONTAINS]->(m) MERGE (m)-[:HAS_SENSOR]->(s)", {}),
    ]
STATIC.append(("MATCH (d:DataSource {sourceId:$src}), (s:Sensor) MERGE (d)-[:FEEDS]->(s)", {}))

ZONE_PROP = {"temperature_c": "temperature", "humidity_pct": "humidity", "co2_ppm": "co2", "noise_db": "noise"}


def _asset(asset_id):
    """Cypher pattern for a zone or machine id (labels are fixed constants, not user input)."""
    return ("Zone", "zoneId") if asset_id == ZONE_ID else ("Machine", "machineId")


class Graph:
    def __init__(self, uri):
        self.uri = uri
        self.driver = None
        self.seeded = False
        self.down_until = 0.0
        self._thresholds = (0.0, dict(DEFAULT_THRESHOLDS))

    # ---------------------------------------------------------------- connection
    def _session(self):
        if self.driver is None:
            # short timeouts: a slow or missing Memgraph must not stall the pipeline
            self.driver = GraphDatabase.driver(self.uri, auth=("", ""), connection_timeout=2,
                                               max_transaction_retry_time=1, connection_acquisition_timeout=2)
        return self.driver.session()

    def run(self, statements):
        """Run [(cypher, params)] in one write transaction. Returns False if Memgraph is unreachable.
        After a failure, graph writes are skipped for 5 s (SQLite still records every event)."""
        base = {"ws": WORKSHOP_ID, "zone": ZONE_ID, "src": DATA_SOURCE_ID}
        if time.time() < self.down_until:
            return False
        try:
            with self._session() as s:
                if not self.seeded:
                    s.execute_write(lambda tx: [tx.run(q, {**base, **p}).consume() for q, p in STATIC])
                    self.seeded = True
                    log.info("static graph ensured in Memgraph")
                s.execute_write(lambda tx: [tx.run(q, {**base, **p}).consume() for q, p in statements])
            return True
        except Exception as e:  # network or Cypher error: keep serving, retry next time
            log.warning("Memgraph write failed, skipping graph writes for 5 s: %s", e)
            self.driver = None
            self.down_until = time.time() + 5
            return False

    def read(self, query, **params):
        with self._session() as s:
            return [r.data() for r in s.run(query, params)]

    def thresholds(self):
        """Alert thresholds come from the Zone node (cached for 30 s)."""
        ts, cached = self._thresholds
        if time.time() - ts < 30:
            return cached
        try:
            rows = self.read("MATCH (z:Zone {zoneId:$z}) RETURN z.tempMax AS tempMax, z.co2Max AS co2Max, "
                             "z.noiseMax AS noiseMax", z=ZONE_ID)
            if rows:
                cached = {k: (v if v is not None else DEFAULT_THRESHOLDS[k]) for k, v in rows[0].items()}
        except Exception as e:
            log.warning("could not read thresholds, using defaults: %s", e)
        self._thresholds = (time.time(), cached)
        return cached

    # ---------------------------------------------------------------- statement builders
    @staticmethod
    def zone_update(values, risk, alert_status, ts):
        props = {ZONE_PROP[k]: v for k, v in values.items() if k in ZONE_PROP}
        return ("MATCH (z:Zone {zoneId:$zone}) SET z += $props, z.riskStatus=$risk, z.alertStatus=$alert, "
                "z.lastUpdate=$ts", {"props": props, "risk": risk, "alert": alert_status, "ts": ts})

    @staticmethod
    def machine_update(mid, status, util, avail, alert_status, ts):
        return ("MATCH (m:Machine {machineId:$mid}) SET m.operationalStatus=$status, m.utilization=$util, "
                "m.availability=$avail, m.alertStatus=$alert, m.lastUpdate=$ts",
                {"mid": mid, "status": status, "util": util, "avail": avail, "alert": alert_status, "ts": ts})

    @staticmethod
    def workshop_update(overall):
        return ("MATCH (w:Workshop {workshopId:$ws}) SET w.overallStatus=$s", {"s": overall})

    @staticmethod
    def reading_upsert(ev, signal, value):
        """Keep one Reading node per signal holding the latest value (full history is in SQLite)."""
        label, key = _asset(ev["asset_id"])
        return (f"MERGE (r:Reading {{readingId:$rid}}) SET r.signal=$signal, r.value=$value, r.unit=$unit, "
                "r.eventTimestamp=$ets, r.sourceTimestamp=$sts, r.messageId=$msg, r.source=$source "
                "WITH r MERGE (s:Sensor {sensorId:$sensor}) "  # MERGE: an unknown sensor id still gets a node
                f"WITH r, s MATCH (a:{label} {{{key}:$asset}}) "
                "MERGE (s)-[:PRODUCES]->(r) MERGE (r)-[:OBSERVES]->(a)",
                {"rid": f"R-{ev['asset_id']}-{signal}", "signal": signal, "value": value,
                 "unit": ev["units"].get(signal, ""), "ets": ev["event_ts"], "sts": ev["source_ts"],
                 "msg": ev["message_id"], "source": ev["source"], "sensor": ev["sensor_id"], "asset": ev["asset_id"]})

    @staticmethod
    def kpi_upsert(mid, metric, value, start, end):
        return ("MERGE (k:KPI {kpiId:$kid}) SET k.metricName=$metric, k.value=$value, k.unit='%', "
                "k.windowStart=$start, k.windowEnd=$end WITH k MATCH (m:Machine {machineId:$mid}) "
                "MERGE (m)-[:HAS_KPI]->(k)",
                {"kid": f"KPI-{mid}-{metric}", "metric": metric, "value": value, "start": start, "end": end, "mid": mid})

    @staticmethod
    def alert_open(a):
        label, key = _asset(a["asset"])
        return (f"MERGE (al:Alert {{alertId:$id}}) SET al.description=$desc, al.severity=$sev, al.active=true, "
                f"al.createdAt=$created, al.rule=$rule WITH al MATCH (x:{label} {{{key}:$asset}}) "
                "MERGE (x)-[:TRIGGERS]->(al)",
                {"id": a["id"], "desc": a["description"], "sev": a["severity"], "created": a["createdAt"],
                 "rule": a["rule"], "asset": a["asset"]})

    @staticmethod
    def alert_clear(alert_id, ts):
        return ("MATCH (al:Alert {alertId:$id}) SET al.active=false, al.clearedAt=$ts", {"id": alert_id, "ts": ts})

    @staticmethod
    def maintenance_upsert(ev):
        v = ev["values"]
        return ("MERGE (e:MaintenanceEvent {eventId:$id}) SET e.machineId=$mid, e.eventType=$type, e.startTime=$start, "
                "e.endTime=$end, e.reason=$reason, e.state=$state WITH e MATCH (m:Machine {machineId:$mid}) "
                "SET m.maintenanceActive=($state='STARTED') MERGE (m)-[:HAS_MAINTENANCE]->(e)",
                {"id": v["event_id"], "mid": ev["asset_id"], "type": v["event_type"], "start": v["start"],
                 "end": v["end"], "reason": v["reason"], "state": v["state"]})

    # ---------------------------------------------------------------- export for the dashboard
    def export(self):
        nodes = self.read("MATCH (n) RETURN id(n) AS nid, labels(n)[0] AS label, properties(n) AS props")
        edges = self.read("MATCH (a)-[r]->(b) RETURN id(a) AS a, type(r) AS type, id(b) AS b")
        ids, hidden, out_nodes = {}, set(), []
        for n in nodes:
            props = n["props"]
            if n["label"] == "Alert" and not props.get("active"):
                hidden.add(n["nid"])  # cleared alerts stay in Memgraph but are not drawn
                continue
            ids[n["nid"]] = props.get(ID_KEYS.get(n["label"], ""), str(n["nid"]))
            out_nodes.append({"id": ids[n["nid"]], "label": n["label"], "properties": props,
                              "alertActive": bool(props.get("alertStatus")) or
                              (n["label"] == "Alert" and bool(props.get("active")))})
        out_edges = [{"source": ids[e["a"]], "target": ids[e["b"]], "type": e["type"]}
                     for e in edges if e["a"] in ids and e["b"] in ids]
        return {"nodes": out_nodes, "edges": out_edges}
