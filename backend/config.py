"""Settings for the app server. Everything can be overridden with environment variables
(docker-compose sets them), so the same code runs locally and in a container."""
import os

MQTT_HOST = os.getenv("MQTT_HOST", "localhost")
MQTT_PORT = int(os.getenv("MQTT_PORT", "1883"))
MQTT_TOPIC = os.getenv("MQTT_TOPIC", "workshop/#")

MEMGRAPH_URI = os.getenv("MEMGRAPH_URI") or \
    f"bolt://{os.getenv('MEMGRAPH_HOST', 'localhost')}:{os.getenv('MEMGRAPH_PORT', '7687')}"
DB_PATH = os.getenv("SQLITE_PATH") or os.getenv("DB_PATH", "data/twin.db")

# Duplicate check: a message is a duplicate if the same message_id (or the same original
# reading) arrived on the same topic within the last DEDUP_SECONDS. Real duplicates
# (broker redelivery, a repeated CSV row) arrive seconds apart; a time window instead of
# "ever seen" lets the replay script be run again later for a new demo.
DEDUP_SECONDS = int(os.getenv("DEDUP_SECONDS", "60"))

# KPIs are computed over this rolling window of live (replay) time.
KPI_WINDOW_MIN = int(os.getenv("KPI_WINDOW_MIN", "60"))

# The twin counts as "live" while messages keep arriving. A gap longer than this ends
# the current run; the next message starts a new run (fresh KPIs, history and alerts).
# The replay sends a row every 10 s, so 25 s allows one late row before declaring it stopped.
LIVE_TIMEOUT_SEC = int(os.getenv("LIVE_TIMEOUT_SEC", "25"))

WORKSHOP_ID = "WS-01"
ZONE_ID = "zone1"
MACHINES = {"machine1": "Lathe", "machine2": "Drill"}

# Which signals each MQTT topic type must carry.
SIGNALS_BY_KIND = {
    "environment": ["temperature_c", "humidity_pct", "co2_ppm"],
    "noise": ["noise_db"],
    "status": ["status"],
}

UNITS = {"temperature_c": "°C", "humidity_pct": "%", "co2_ppm": "ppm", "noise_db": "dB(A)", "status": ""}

# Physically plausible ranges. Anything outside is treated as a faulty sensor reading
# and rejected (e.g. 150 °C, or CO2 = 0 ppm from a sensor dropout).
VALID_RANGES = {
    "temperature_c": (-10, 60),
    "humidity_pct": (0, 100),
    "co2_ppm": (250, 5000),
    "noise_db": (30, 130),
}

VALID_STATUSES = {"RUNNING", "IDLE", "DOWN"}

# Used only if the graph has no threshold properties on the Zone node.
DEFAULT_THRESHOLDS = {"tempMax": 30.0, "co2Max": 1200.0, "noiseMax": 85.0}

# Default sensor for each topic when a message does not name one.
DEFAULT_SENSORS = {
    ("zone1", "environment"): "ENV-01",
    ("zone1", "noise"): "NOISE-01",
    ("machine1", "status"): "LATHE-STAT-01",
    ("machine2", "status"): "DRILL-STAT-01",
    ("machine1", "maintenance"): "CMMS",
    ("machine2", "maintenance"): "CMMS",
}
DATA_SOURCE_ID = "CSV-01"
