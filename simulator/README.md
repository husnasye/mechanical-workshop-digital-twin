# Simulator (replay.py)

Replays `workshop_data.csv` as live MQTT telemetry, one row every 5 s by default.

## Run
```bash
docker compose up -d mosquitto          # from the repo root
pip install -r simulator/requirements.txt
cd simulator
python replay.py
```

Watch the messages arrive (needs mosquitto-clients, or use the container):
```bash
docker exec -it mosquitto mosquitto_sub -t 'workshop/#' -v
```

## Useful options
| Option | What it does |
|---|---|
| `--interval 1` | 1 row per second (faster testing) |
| `--start-row 360` | jump to just before the lathe failure (row ~381) |
| `--fail lathe --fail-at 10 --fail-for 12` | force the lathe DOWN on demand (what-if demo) |
| `--loop` | restart at the top when the CSV ends |
| `--host mosquitto` | broker hostname if running inside Docker |
| `--quiet` | don't print every message |

## Topics
| Topic | measurements |
|---|---|
| `workshop/zone1/environment` | `temperature_c`, `humidity_pct`, `co2_ppm` |
| `workshop/zone1/noise` | `noise_db` |
| `workshop/machine1/status` (lathe) | `status`: RUNNING / IDLE / DOWN |
| `workshop/machine2/status` (drill) | `status` |
| `workshop/machine1/maintenance` | `event_id`, `event_type`, `state` (STARTED/COMPLETED), `start`, `end`, `reason` |

## Message format (every topic)
```json
{
  "message_id": "env-2023-07-10T09:39:32Z",
  "seq": 1,
  "asset_id": "zone1",
  "sensor_id": "ENV-01",
  "source": "real",
  "source_timestamp": "2023-07-10T09:39:32Z",
  "event_timestamp": "2026-11-21T12:57:20.876Z",
  "measurements": {"temperature_c": 29.6, "humidity_pct": 42.4, "co2_ppm": 913.0}
}
```
- `source`: `real`, `real+synthetic` (CO2 spike rows) or `synthetic`
- `source_timestamp`: original dataset time; `event_timestamp`: time it was published
- Missing values arrive as `null`. A repeated `message_id` means a duplicate.
- The CSV's `test_case` column is never published; it's our answer key for testing.

## Data
- `workshop_data.csv`: 721 rows, 10 s apart (2023-07-10 08:50-10:50 UTC).
  Temperature, humidity and CO2 are real readings from the Indoor Fire Dataset with
  Distributed Multi-Sensor Nodes (Industrial Hall), Mendeley Data, DOI 10.17632/yghykzm4km.1,
  CC BY 4.0, node `sensornode0012`. Noise, machine states, the CO2 build-up and 5 bad rows are synthetic.
- `build_dataset.py`: rebuilds the CSV from the original dataset.
