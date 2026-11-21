# EEEE 489 Group 8 – Mechanical Workshop Digital Twin

A software-only digital twin of a mechanical workshop (one zone, a lathe and a drill).
A replay script streams `workshop_data.csv` over MQTT, a FastAPI app server cleans the data,
updates a Memgraph knowledge graph and a SQLite history, computes KPIs and alerts, and a
Streamlit dashboard shows everything live.

```
simulator (replay.py) --MQTT--> Mosquitto --MQTT--> backend (FastAPI) --Bolt--> Memgraph
                                                          |  \--> SQLite (history)
                                                          +--REST--> dashboard (Streamlit)
```

## Quick start (only Docker Desktop needed)

1. Install [Docker Desktop](https://www.docker.com/products/docker-desktop/) and start it.
2. Get the code:
   ```bash
   git clone -b zafar-server https://github.com/husnasye/eeee489-group8-final-project.git
   cd eeee489-group8-final-project
   ```
3. Build and start the stack (the first run downloads about 4 GB of images, mostly Memgraph):
   ```bash
   docker compose up -d --build
   ```
4. Wait about 40 seconds for Memgraph to finish starting, then stream the data:
   ```bash
   docker compose run --rm simulator --interval 1 --start-row 360
   ```
   Press Ctrl+C to stop. `--start-row 360` jumps to just before the lathe failure.

## Where to look

| What | Address |
|---|---|
| Dashboard | http://localhost:8501 |
| API docs (Swagger) | http://localhost:8000/docs |
| Health / message counts | http://localhost:8000/health |
| Rejected messages | http://localhost:8000/api/rejected |
| Memgraph Lab (graph view) | http://localhost:3000 → "Connect now" |

Useful Cypher in Memgraph Lab: `MATCH (n)-[r]->(m) RETURN n, r, m;`

## Replay options

| Option | What it does |
|---|---|
| `--interval 1` | one CSV row per second (default 5) |
| `--start-row 360` | start just before the lathe failure |
| `--fail lathe --fail-at 10 --fail-for 12` | force the lathe DOWN on demand |
| `--loop` | start again when the CSV ends |
| `--quiet` | do not print every message |

You can also run it with Python instead of Docker:
`pip install -r simulator/requirements.txt`, then `cd simulator` and `python replay.py --interval 1`.

## Useful commands

```bash
docker compose ps                 # what is running
docker compose logs -f backend    # watch messages being accepted / rejected / alerts
docker compose down               # stop everything (keeps the data)
docker compose down -v            # stop and wipe Memgraph + SQLite data (fresh start)
docker compose up -d --build      # rebuild after pulling new code
```

## Folders

| Folder | Owner | Contents |
|---|---|---|
| `simulator/` | Muzaffar | dataset, `replay.py`, Dockerfile |
| `mosquitto/` | Muzaffar | broker config |
| `backend/` | Zafar | FastAPI app server, cleaning, analytics, Memgraph + SQLite, tests |
| `dashboard/` | Husna | Streamlit dashboard |
| `docker-compose.yml` | Muzaffar | all services |

## Troubleshooting

- **Dashboard says "No telemetry received yet"**: start the simulator (step 4).
- **Port already in use**: something else is using 1883, 3000, 7687, 8000 or 8501. Stop it, or stop an older copy of the stack with `docker compose down`.
- **Graph tab empty right after start**: Memgraph takes about 30–40 seconds to boot. The backend keeps recording to SQLite and fills the graph once Memgraph is up.
