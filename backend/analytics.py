"""Derived properties: KPIs, rule-based alerts and zone risk status.

Pure functions only (no I/O), so they are easy to unit test and to check by hand.
"""


def machine_kpis(status_counts):
    """status_counts: {'RUNNING': n, 'IDLE': n, 'DOWN': n} for samples inside the KPI window.

    Every sample covers the same replay interval, so counting samples is the same as
    measuring time. The whole window is treated as scheduled (shift) time.
      utilization  = running time / scheduled time * 100
      availability = (scheduled time - downtime) / scheduled time * 100
    """
    scheduled = sum(status_counts.values())
    if scheduled == 0:
        return {"utilization": 0.0, "availability": 100.0, "samples": 0}
    running = status_counts.get("RUNNING", 0)
    down = status_counts.get("DOWN", 0)
    return {
        "utilization": round(running / scheduled * 100, 1),
        "availability": round((scheduled - down) / scheduled * 100, 1),
        "samples": scheduled,
    }


# rule key -> (asset, signal, threshold property, severity, description)
ZONE_RULES = {
    "temp_high": ("temperature_c", "tempMax", "high", "Temperature above {t:g} °C"),
    "co2_high": ("co2_ppm", "co2Max", "high", "CO2 above {t:g} ppm"),
    "noise_high": ("noise_db", "noiseMax", "medium", "Noise 1-min average above {t:g} dB"),
}

# Noise is judged on a moving average of the last 6 samples (1 minute of data at 10 s),
# like an equivalent continuous sound level, instead of single machine-noise spikes.
NOISE_AVG_SAMPLES = 6


# Hysteresis: an alert opens above the threshold but only clears once the value is this
# far back below it, so a signal hovering at the limit does not open/close alerts every sample.
CLEAR_MARGIN = {"temp_high": 0.5, "co2_high": 50.0, "noise_high": 2.0}


def evaluate_rules(event, thresholds, zone_id, machine_names, smoothed=None):
    """Return {rule_key: rule} for every rule this event can decide.

    A rule is only re-evaluated when its own signal arrives, so a noise message never
    clears a temperature alert. rule = {asset, severity, description, value, firing, clear}.
    firing -> open an alert if none is active; clear -> close the active alert.
    smoothed: optional {signal: value} that replaces the raw value (noise moving average).
    """
    out = {}
    values = {**event["values"], **(smoothed or {})}
    if event["asset_id"] == zone_id:
        for key, (signal, tprop, severity, text) in ZONE_RULES.items():
            if signal in event["values"]:
                t = float(thresholds[tprop])
                v = round(values[signal], 1)
                out[key] = {"asset": zone_id, "severity": severity, "description": text.format(t=t),
                            "value": v, "firing": v > t, "clear": v <= t - CLEAR_MARGIN[key]}
    elif "status" in values:
        mid = event["asset_id"]
        down = values["status"] == "DOWN"
        out[f"{mid}_down"] = {"asset": mid, "severity": "high",
                              "description": f"{machine_names[mid]} is DOWN",
                              "value": None, "firing": down, "clear": not down}
    return out


def zone_risk(active_alerts, machine_statuses, zone_id):
    """LOW / MEDIUM / HIGH from how many safety rules are breaking right now.
    HIGH: two or more zone alerts at once. MEDIUM: one zone alert or a machine DOWN."""
    zone_alerts = sum(1 for a in active_alerts if a["asset"] == zone_id)
    if zone_alerts >= 2:
        return "HIGH"
    if zone_alerts == 1 or "DOWN" in machine_statuses:
        return "MEDIUM"
    return "LOW"
