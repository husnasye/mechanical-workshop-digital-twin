import argparse
import json
import math
import os
import sys
import time
from datetime import datetime, timezone

import pandas as pd
import paho.mqtt.client as mqtt

BASE = "workshop"
MACHINES = {"lathe": ("machine1", "lathe_status", "LATHE-STAT-01"),
            "drill": ("machine2", "drill_status", "DRILL-STAT-01")}


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def clean(v):
    if v is None:
        return None
    if isinstance(v, float) and math.isnan(v):
        return None
    return v.item() if hasattr(v, "item") else v


def connect(host, port, client_id, retries=30):
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=client_id)
    for attempt in range(1, retries + 1):
        try:
            client.connect(host, port, keepalive=60)
            client.loop_start()
            print(f"[replay] connected to mqtt://{host}:{port}")
            return client
        except (ConnectionRefusedError, OSError) as e:
            print(f"[replay] broker not ready ({e}), retry {attempt}/{retries} ...")
            time.sleep(2)
    sys.exit("[replay] could not connect to the MQTT broker")


def publish(client, topic, payload, verbose):
    client.publish(topic, json.dumps(payload), qos=1)
    if verbose:
        print(f"  -> {topic} {json.dumps(payload['measurements'])}")


def make_msg(prefix, seq, asset_id, sensor_id, src_ts, measurements):
    return {
        "message_id": f"{prefix}-{src_ts}",
        "seq": seq,
        "asset_id": asset_id,
        "sensor_id": sensor_id,
        "source_timestamp": src_ts,
        "event_timestamp": now_iso(),
        "measurements": measurements,
    }


def main():
    ap = argparse.ArgumentParser(description="Replay workshop_data.csv as live MQTT telemetry")
    ap.add_argument("--csv", default="workshop_data.csv")
    ap.add_argument("--maintenance", default="maintenance_events.csv")
    ap.add_argument("--host", default=os.getenv("MQTT_HOST", "localhost"))
    ap.add_argument("--port", type=int, default=int(os.getenv("MQTT_PORT", 1883)))
    ap.add_argument("--interval", type=float, default=5.0, help="seconds between rows (default 5)")
    ap.add_argument("--start-row", type=int, default=0)
    ap.add_argument("--loop", action="store_true", help="start again when the CSV ends")
    ap.add_argument("--fail", choices=list(MACHINES), help="force this machine DOWN (demo what-if)")
    ap.add_argument("--fail-at", type=int, default=10, help="tick at which the forced failure starts")
    ap.add_argument("--fail-for", type=int, default=12, help="number of ticks the forced failure lasts")
    ap.add_argument("--quiet", action="store_true")
    a = ap.parse_args()

    df = pd.read_csv(a.csv)
    df = df.drop(columns=["test_case", "env_source"], errors="ignore")
    maint = pd.read_csv(a.maintenance) if os.path.exists(a.maintenance) else pd.DataFrame()
    sent_maint = set()

    client = connect(a.host, a.port, client_id=f"replay-{os.getpid()}")
    seq, tick, row = 0, 0, a.start_row
    print(f"[replay] {len(df)} rows, starting at row {row}, one row every {a.interval}s")

    try:
        while True:
            if row >= len(df):
                if not a.loop:
                    break
                row, sent_maint = 0, set()
                print("[replay] end of file, looping")
            r = df.iloc[row]
            ts = r["timestamp"]
            verbose = not a.quiet
            if verbose:
                print(f"[tick {tick:4d} | row {row:4d} | {ts}]")

            seq += 1
            publish(client, f"{BASE}/zone1/environment", make_msg(
                "env", seq, "zone1", "ENV-01", ts,
                {"temperature_c": clean(r["temperature_c"]),
                 "humidity_pct": clean(r["humidity_pct"]),
                 "co2_ppm": clean(r["co2_ppm"])}), verbose)

            seq += 1
            publish(client, f"{BASE}/zone1/noise", make_msg(
                "noise", seq, "zone1", "NOISE-01", ts,
                {"noise_db": clean(r["noise_db"])}), verbose)

            forced = a.fail and a.fail_at <= tick < a.fail_at + a.fail_for
            for name, (machine_id, col, sensor_id) in MACHINES.items():
                status = clean(r[col])
                if forced and name == a.fail:
                    status = "DOWN"
                seq += 1
                publish(client, f"{BASE}/{machine_id}/status", make_msg(
                    f"{machine_id}-status", seq, machine_id, sensor_id, ts,
                    {"status": status}), verbose)

            for _, m in maint.iterrows():
                for edge, col in (("STARTED", "maintenance_start"), ("COMPLETED", "maintenance_end")):
                    key = (m["event_id"], edge)
                    if key not in sent_maint and ts >= m[col]:
                        sent_maint.add(key)
                        seq += 1
                        publish(client, f"{BASE}/{m['machine_id']}/maintenance", make_msg(
                            f"maint-{edge.lower()}", seq, m["machine_id"], "CMMS", m[col],
                            {"event_id": m["event_id"], "event_type": m["event_type"], "state": edge,
                             "start": m["maintenance_start"], "end": m["maintenance_end"],
                             "reason": m["reason"]}), verbose)

            row += 1
            tick += 1
            time.sleep(a.interval)
    except KeyboardInterrupt:
        print("\n[replay] stopped by user")
    finally:
        client.loop_stop()
        client.disconnect()
        print(f"[replay] done, {seq} messages published")


if __name__ == "__main__":
    main()