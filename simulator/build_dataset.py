"""
Build workshop_data.csv for the Mechanical Workshop Digital Twin.

Real part   : temperature, humidity, CO2 from the Indoor Fire Dataset (industrial hall,
              Mendeley Data, DOI 10.17632/yghykzm4km.1, CC BY 4.0), node sensornode0012,
              2023-07-10 08:50-10:50 UTC, 10 s sampling.
Synthetic   : noise_db, lathe_status, drill_status, a CO2 build-up event,
              and a handful of deliberately bad rows for testing the cleaning step.

Usage: python build_dataset.py <path to source csv> [--out-dir .] [--seed 42]
"""
import argparse
import numpy as np
import pandas as pd

NODE = "sensornode0012"
START, END = "2023-07-10 08:50:00", "2023-07-10 10:49:59"

AMBIENT_DB, LATHE_DB, DRILL_DB = 58.0, 79.0, 84.0   # rough sound levels at the zone sensor


def load_real(path):
    df = pd.read_csv(path, usecols=["Date", "Sensor_ID", "CO2_Room", "Temperature_Room", "Humidity_Room"])
    df = df[df.Sensor_ID == NODE].copy()
    df["Date"] = pd.to_datetime(df["Date"], utc=True)
    df = df.set_index("Date").sort_index().loc[START:END].reset_index()
    return pd.DataFrame({
        "timestamp": df["Date"].dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "temperature_c": df["Temperature_Room"].round(1),
        "humidity_pct": df["Humidity_Room"].round(1),
        "co2_ppm": df["CO2_Room"].round(0),
    })


def state_timeline(n, blocks):
    """blocks: list of (start_row, status). Returns a list of n statuses."""
    out, blocks = [], sorted(blocks)
    for i in range(n):
        cur = [s for r, s in blocks if r <= i][-1]
        out.append(cur)
    return out


def add_machines(df, rng):
    n = len(df)  # ~655 rows, 10 s each (~2 h)
    # Lathe: warm-up idle, long running job, short break, failure at ~row 380 for 60 rows (10 min), restored
    lathe = state_timeline(n, [(0, "IDLE"), (18, "RUNNING"), (200, "IDLE"), (230, "RUNNING"),
                               (380, "DOWN"), (440, "IDLE"), (455, "RUNNING"), (610, "IDLE")])
    # Drill: used in bursts
    drill_blocks, r = [(0, "IDLE")], 30
    while r < n:
        run = int(rng.integers(12, 36))
        drill_blocks += [(r, "RUNNING"), (r + run, "IDLE")]
        r += run + int(rng.integers(20, 60))
    drill = state_timeline(n, drill_blocks)
    df["lathe_status"], df["drill_status"] = lathe, drill
    return df


def add_noise(df, rng):
    def db_sum(levels):
        return 10 * np.log10(sum(10 ** (l / 10) for l in levels))
    vals = []
    for lathe, drill in zip(df.lathe_status, df.drill_status):
        levels = [AMBIENT_DB]
        if lathe == "RUNNING":
            levels.append(LATHE_DB)
        if drill == "RUNNING":
            levels.append(DRILL_DB + rng.normal(0, 2))   # drilling load varies
        vals.append(db_sum(levels) + rng.normal(0, 1.2))
    df["noise_db"] = np.round(vals, 1)
    return df


def add_co2_buildup(df):
    """Synthetic 'extraction fan failure': CO2 climbs ~500 ppm over 8 min, then decays."""
    start, rise, hold, fall = 500, 48, 12, 48
    add = np.zeros(len(df))
    for i in range(rise):
        add[start + i] = 520 * (i + 1) / rise
    add[start + rise:start + rise + hold] = 520
    for i in range(fall):
        add[start + rise + hold + i] = 520 * (1 - (i + 1) / fall)
    mask = add > 0
    df.loc[mask, "co2_ppm"] = (df.loc[mask, "co2_ppm"] + add[mask]).round(0)
    df.loc[mask, "env_source"] = "real+synthetic"
    df.loc[mask, "test_case"] = "co2_buildup"
    return df


def add_bad_rows(df):
    df = df.copy()
    df.loc[120, "humidity_pct"] = np.nan;  df.loc[120, "test_case"] = "bad_missing_value"
    df.loc[260, "temperature_c"] = 150.0;  df.loc[260, "test_case"] = "bad_out_of_range"
    df.loc[330, "co2_ppm"] = 0;            df.loc[330, "test_case"] = "bad_sensor_dropout"
    df.loc[470, "lathe_status"] = "running"; df.loc[470, "test_case"] = "bad_status_format"
    dup = df.loc[[300]].assign(test_case="bad_duplicate")
    df = pd.concat([df.iloc[:301], dup, df.iloc[301:]], ignore_index=True)
    return df


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("source")
    ap.add_argument("--out-dir", default=".")
    ap.add_argument("--seed", type=int, default=42)
    a = ap.parse_args()
    rng = np.random.default_rng(a.seed)

    df = load_real(a.source)
    df["env_source"], df["test_case"] = "real", ""
    df = add_machines(df, rng)
    df = add_noise(df, rng)
    df = add_co2_buildup(df)
    lathe_down = df.index[df.lathe_status == "DOWN"]
    df.loc[lathe_down, "test_case"] = df.loc[lathe_down, "test_case"].replace("", "lathe_failure")
    maint = pd.DataFrame([{
        "event_id": "MT-001", "machine_id": "machine1",
        "maintenance_start": df.timestamp[lathe_down[0]], "maintenance_end": df.timestamp[lathe_down[-1] + 1],
        "event_type": "corrective", "reason": "Spindle overheating, bearing replaced",
    }])
    df = add_bad_rows(df)

    cols = ["timestamp", "temperature_c", "humidity_pct", "co2_ppm", "noise_db",
            "lathe_status", "drill_status", "env_source", "test_case"]
    df[cols].to_csv(f"{a.out_dir}/workshop_data.csv", index=False)

    maint.to_csv(f"{a.out_dir}/maintenance_events.csv", index=False)
    print(f"wrote {len(df)} rows + {len(maint)} maintenance event(s)")


if __name__ == "__main__":
    main()
