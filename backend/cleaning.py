"""Validation, cleaning and enrichment of one raw MQTT message.

clean() either returns a tidy event dict or raises Rejected(reason). The caller logs
every rejection with its reason in the SQLite `rejected` table.
"""
import json
import math
from datetime import datetime, timezone

from config import DEFAULT_SENSORS, MACHINES, SIGNALS_BY_KIND, UNITS, VALID_RANGES, VALID_STATUSES, ZONE_ID


class Rejected(Exception):
    def __init__(self, reason, message_id=None):
        super().__init__(reason)
        self.reason = reason
        self.message_id = message_id


def parse_ts(value):
    """ISO-8601 string -> timezone-aware UTC datetime."""
    if not isinstance(value, str) or not value:
        raise ValueError("empty timestamp")
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def iso(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _number(value):
    if value is None or value == "":
        return None
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(v) else v


def clean(topic, raw, is_duplicate):
    """topic: e.g. 'workshop/zone1/environment'. raw: bytes or str.
    is_duplicate(message_id, dedup_key) -> bool, supplied by storage."""
    # 1. topic -> asset + message kind
    parts = topic.split("/")
    if len(parts) != 3 or parts[0] != "workshop":
        raise Rejected(f"unknown topic '{topic}'")
    topic_asset, kind = parts[1], parts[2]
    if kind not in SIGNALS_BY_KIND and kind != "maintenance":
        raise Rejected(f"unknown message type '{kind}'")
    if topic_asset != ZONE_ID and topic_asset not in MACHINES:
        raise Rejected(f"unknown asset '{topic_asset}'")

    # 2. JSON
    try:
        msg = json.loads(raw)
        if not isinstance(msg, dict):
            raise ValueError("not an object")
    except ValueError as e:
        raise Rejected(f"invalid JSON: {e}")

    # 3. required fields
    message_id = msg.get("message_id")
    if not message_id:
        raise Rejected("missing field: message_id")
    for field in ("event_timestamp", "source_timestamp"):
        if not msg.get(field):
            raise Rejected(f"missing field: {field}", message_id)
    asset_id = msg.get("asset_id") or topic_asset  # enrichment: fill from topic
    if asset_id != topic_asset:
        raise Rejected(f"asset_id '{asset_id}' does not match topic asset '{topic_asset}'", message_id)

    # 4. timestamps
    try:
        event_ts = parse_ts(msg["event_timestamp"])
        source_ts = parse_ts(msg["source_timestamp"])
    except ValueError as e:
        raise Rejected(f"bad timestamp: {e}", message_id)

    # 5. duplicates: same message_id, or the same original reading replayed twice
    dedup_key = f"{asset_id}|{kind}|{iso(source_ts)}"
    if is_duplicate(message_id, dedup_key):
        raise Rejected(f"duplicate message (id {message_id} / source time {iso(source_ts)})", message_id)

    event = {
        "message_id": message_id,
        "seq": msg.get("seq"),
        "topic": topic,
        "kind": kind,
        "asset_id": asset_id,
        "sensor_id": msg.get("sensor_id") or DEFAULT_SENSORS.get((asset_id, kind)),
        "source": msg.get("source", "unknown"),
        "source_ts": iso(source_ts),
        "event_ts": iso(event_ts),
        "dedup_key": dedup_key,
        "values": {},
        "units": {},
        "notes": [],
    }

    body = msg.get("measurements")
    if not isinstance(body, dict):
        body = {k: v for k, v in msg.items() if k not in event}  # flat payload fallback

    if kind == "maintenance":
        required = ("event_id", "event_type", "start", "end")
        missing = [k for k in required if not body.get(k)]
        if missing:
            raise Rejected(f"missing maintenance field(s): {', '.join(missing)}", message_id)
        try:
            start, end = parse_ts(body["start"]), parse_ts(body["end"])
        except ValueError as e:
            raise Rejected(f"bad maintenance timestamp: {e}", message_id)
        event["values"] = {"event_id": body["event_id"], "event_type": body["event_type"],
                           "start": iso(start), "end": iso(end), "reason": body.get("reason", ""),
                           "state": str(body.get("state", "COMPLETED")).upper()}
        return event

    # 6. measurements: presence, type, range, status normalisation
    for signal in SIGNALS_BY_KIND[kind]:
        value = body.get(signal)
        if signal == "status":
            if value is None or str(value).strip() == "":
                raise Rejected("missing value: status", message_id)
            norm = str(value).strip().upper()
            if norm not in VALID_STATUSES:
                raise Rejected(f"invalid status '{value}'", message_id)
            if norm != value:
                event["notes"].append(f"status normalised '{value}' -> '{norm}'")
            event["values"]["status"] = norm
        else:
            num = _number(value)
            if num is None:
                raise Rejected(f"missing value: {signal}", message_id)
            lo, hi = VALID_RANGES[signal]
            if not lo <= num <= hi:
                raise Rejected(f"out of range: {signal}={num:g} (valid {lo}..{hi})", message_id)
            event["values"][signal] = num
        event["units"][signal] = UNITS[signal]  # enrichment: units
    return event
