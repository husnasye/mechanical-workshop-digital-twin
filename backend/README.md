# App server (FastAPI)

Subscribes to `workshop/#` on Mosquitto, cleans each message, stores history in SQLite,
updates the live twin in Memgraph, computes KPIs and alerts, and serves REST for the dashboard.

| File | Role |
|---|---|
| `config.py` | Env-var settings, valid ranges, default thresholds |
| `cleaning.py` | Required fields, timestamps, ranges, duplicates, status normalisation, units |
| `storage.py` | SQLite tables: `messages`, `readings`, `rejected`, `alerts`, `maintenance` |
| `analytics.py` | Utilization, availability, alert rules, zone risk |
| `graph.py` | Memgraph (Bolt) MERGE/SET statements and `/api/graph` export |
| `twin.py` | The pipeline: clean -> store -> analytics -> graph |
| `mqtt_consumer.py` | paho-mqtt subscriber thread |
| `main.py` | FastAPI endpoints (Swagger at `/docs`) |

## Endpoints
`/api/state`, `/api/history?signal=&minutes=`, `/api/alerts`, `/api/kpis`, `/api/graph`,
`/api/predict`, `/api/rejected`, `/health`.

## Environment variables
`MQTT_HOST` (localhost), `MQTT_PORT` (1883), `MEMGRAPH_URI` (bolt://localhost:7687),
`DB_PATH` (data/twin.db), `KPI_WINDOW_MIN` (60).

## Expected MQTT payload
Topics: `workshop/zone1/environment`, `workshop/zone1/noise`, `workshop/machine1/status`,
`workshop/machine2/status`, optional `workshop/machine{1,2}/maintenance`.

```json
{"message_id": "env-000123", "seq": 123, "asset_id": "zone1", "sensor_id": "ENV-01",
 "source": "real", "source_timestamp": "2023-07-10T09:15:00Z", "event_timestamp": "2026-11-21T14:02:11Z",
 "measurements": {"temperature_c": 27.4, "humidity_pct": 48.0, "co2_ppm": 910}}
```
Status topics use `"measurements": {"status": "RUNNING"}`. Maintenance uses
`{"event_id", "event_type", "start", "end", "reason"}`.

## Tests
`docker build -t workshop-twin-server . && docker run --rm workshop-twin-server python -m pytest -q`
