"""SQLite history store: every accepted reading, every rejected message, alerts, maintenance.

Memgraph holds the *current* twin state; SQLite holds the time series behind it."""
import json
import os
import sqlite3
import threading
from datetime import datetime, timedelta, timezone

from cleaning import iso

SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    message_id TEXT,
    dedup_key  TEXT,
    topic      TEXT,
    event_ts   TEXT
);
CREATE INDEX IF NOT EXISTS idx_messages_topic ON messages(topic);
CREATE TABLE IF NOT EXISTS readings (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id  TEXT,
    asset_id    TEXT,
    sensor_id   TEXT,
    signal      TEXT,
    value       REAL,
    value_text  TEXT,
    unit        TEXT,
    source      TEXT,
    source_ts   TEXT,
    event_ts    TEXT,
    ingested_ts TEXT
);
CREATE INDEX IF NOT EXISTS idx_readings_signal_ts ON readings(signal, event_ts);
CREATE TABLE IF NOT EXISTS rejected (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    received_ts TEXT,
    topic       TEXT,
    message_id  TEXT,
    reason      TEXT,
    payload     TEXT
);
CREATE TABLE IF NOT EXISTS alerts (
    alert_id      TEXT PRIMARY KEY,
    rule          TEXT,
    asset         TEXT,
    severity      TEXT,
    description   TEXT,
    trigger_value REAL,
    active        INTEGER,
    created_at    TEXT,
    cleared_at    TEXT
);
CREATE TABLE IF NOT EXISTS maintenance (
    event_id   TEXT PRIMARY KEY,
    machine_id TEXT,
    event_type TEXT,
    start_ts   TEXT,
    end_ts     TEXT,
    reason     TEXT
);
"""

# Signal names used by the dashboard for machine status history.
STATUS_SIGNAL = {"machine1": "lathe_status", "machine2": "drill_status"}


def now_iso():
    return iso(datetime.now(timezone.utc))


def since_iso(minutes):
    return iso(datetime.now(timezone.utc) - timedelta(minutes=minutes))


class Storage:
    def __init__(self, path):
        if os.path.dirname(path):
            os.makedirs(os.path.dirname(path), exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.lock = threading.Lock()
        with self.lock:
            self.db.executescript(SCHEMA)
            self.db.commit()

    def _q(self, sql, args=()):
        with self.lock:
            return [dict(r) for r in self.db.execute(sql, args).fetchall()]

    # ---------------------------------------------------------------- writes
    def recent_ids(self, topic, seconds):
        """(message_id, dedup_key, event_ts) received on a topic in the last `seconds`, oldest first.
        Warms the duplicate window after a restart."""
        rows = self._q("SELECT message_id, dedup_key, event_ts FROM messages WHERE topic=? AND event_ts>=? "
                       "ORDER BY rowid", (topic, since_iso(seconds / 60)))
        return [(r["message_id"], r["dedup_key"], r["event_ts"]) for r in rows]

    def save_event(self, ev):
        ingested = now_iso()
        with self.lock:
            self.db.execute("INSERT INTO messages VALUES (?,?,?,?)",
                            (ev["message_id"], ev["dedup_key"], ev["topic"], ev["event_ts"]))
            if ev["kind"] == "maintenance":
                v = ev["values"]
                self.db.execute("INSERT OR REPLACE INTO maintenance VALUES (?,?,?,?,?,?)",
                                (v["event_id"], ev["asset_id"], v["event_type"], v["start"], v["end"], v["reason"]))
            else:
                for signal, value in ev["values"].items():
                    name = STATUS_SIGNAL[ev["asset_id"]] if signal == "status" else signal
                    num, text = (None, value) if signal == "status" else (value, None)
                    self.db.execute(
                        "INSERT INTO readings (message_id, asset_id, sensor_id, signal, value, value_text, unit,"
                        " source, source_ts, event_ts, ingested_ts) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                        (ev["message_id"], ev["asset_id"], ev["sensor_id"], name, num, text, ev["units"].get(signal),
                         ev["source"], ev["source_ts"], ev["event_ts"], ingested))
            self.db.commit()

    def save_rejected(self, topic, message_id, reason, payload):
        if isinstance(payload, bytes):
            payload = payload.decode("utf-8", "replace")
        with self.lock:
            self.db.execute("INSERT INTO rejected (received_ts, topic, message_id, reason, payload) VALUES (?,?,?,?,?)",
                            (now_iso(), topic, message_id, reason, payload[:2000]))
            self.db.commit()

    def open_alert(self, alert):
        with self.lock:
            self.db.execute("INSERT INTO alerts VALUES (?,?,?,?,?,?,?,?,?)",
                            (alert["id"], alert["rule"], alert["asset"], alert["severity"], alert["description"],
                             alert["value"], 1, alert["createdAt"], None))
            self.db.commit()

    def clear_alert(self, alert_id, cleared_at):
        with self.lock:
            self.db.execute("UPDATE alerts SET active=0, cleared_at=? WHERE alert_id=?", (cleared_at, alert_id))
            self.db.commit()

    # ---------------------------------------------------------------- reads
    def next_alert_number(self):
        rows = self._q("SELECT COUNT(*) AS n FROM alerts")
        return rows[0]["n"] + 1

    def active_alerts(self):
        return self._q("SELECT * FROM alerts WHERE active=1")

    def recent_alerts(self, limit=100):
        return self._q("SELECT * FROM alerts ORDER BY created_at DESC LIMIT ?", (limit,))

    def history(self, signal, minutes):
        return self._q("SELECT event_ts, source_ts, value, value_text FROM readings "
                       "WHERE signal=? AND event_ts>=? ORDER BY event_ts", (signal, since_iso(minutes)))

    def status_counts(self, machine_id, minutes):
        rows = self._q("SELECT value_text AS status, COUNT(*) AS n FROM readings "
                       "WHERE signal=? AND event_ts>=? GROUP BY value_text",
                       (STATUS_SIGNAL[machine_id], since_iso(minutes)))
        return {r["status"]: r["n"] for r in rows}

    def latest(self):
        """Latest value per signal, used to restore the twin state after a restart."""
        return self._q("SELECT r.* FROM readings r JOIN (SELECT signal, MAX(id) AS id FROM readings GROUP BY signal) m "
                       "ON r.id = m.id")

    def rejected(self, limit=50):
        rows = self._q("SELECT * FROM rejected ORDER BY id DESC LIMIT ?", (limit,))
        for r in rows:
            try:
                r["payload"] = json.loads(r["payload"])
            except (TypeError, ValueError):
                pass
        return rows

    def counts(self):
        return {t: self._q(f"SELECT COUNT(*) AS n FROM {t}")[0]["n"]
                for t in ("messages", "readings", "rejected", "alerts")}
