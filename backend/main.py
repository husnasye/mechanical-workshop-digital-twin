"""FastAPI app server for the Mechanical Workshop Digital Twin.

Run locally:  uvicorn main:app --reload --port 8000   (Swagger UI at /docs)
"""
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Query

import config
from graph import Graph
from mqtt_consumer import Consumer
from storage import STATUS_SIGNAL, Storage
from twin import Twin

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)-6s %(message)s")
logging.getLogger("neo4j").setLevel(logging.ERROR)

storage = Storage(config.DB_PATH)
graph = Graph(config.MEMGRAPH_URI)
twin = Twin(storage, graph)
consumer = Consumer(config.MQTT_HOST, config.MQTT_PORT, config.MQTT_TOPIC, twin.handle)

SIGNALS = ["temperature_c", "humidity_pct", "co2_ppm", "noise_db", *STATUS_SIGNAL.values()]


@asynccontextmanager
async def lifespan(app):
    graph.run([])  # create the static graph now if Memgraph is up (retried on each event otherwise)
    consumer.start()
    yield
    consumer.stop()


app = FastAPI(title="Workshop Digital Twin API", version="1.0", lifespan=lifespan,
              description="App server: MQTT ingestion, cleaning, Memgraph + SQLite twin, analytics.")


@app.get("/health", tags=["system"])
def health():
    return {"mqttConnected": consumer.connected, "memgraphSeeded": graph.seeded, "queueBacklog": consumer.backlog(),
            "live": twin.live(), "runStartedAt": twin.run_started,
            "processed": twin.stats, "stored": storage.counts()}


@app.get("/api/state", tags=["twin"])
def state():
    s = twin.state()
    if s is None:
        raise HTTPException(503, "No telemetry received yet. Start the replay script.")
    return s


@app.get("/api/history", tags=["twin"])
def history(signal: str = Query(..., enum=SIGNALS), minutes: int = Query(30, ge=1, le=1440)):
    rows = storage.history(signal, minutes, since=twin.run_started)  # current run only
    return {"signal": signal, "points": [
        {"timestamp": r["event_ts"], "sourceTimestamp": r["source_ts"],
         "value": r["value"] if r["value_text"] is None else r["value_text"]} for r in rows]}


@app.get("/api/alerts", tags=["twin"])
def alerts(limit: int = Query(100, ge=1, le=1000)):
    return [{"id": a["alert_id"], "severity": a["severity"], "description": a["description"], "asset": a["asset"],
             "createdAt": a["created_at"], "clearedAt": a["cleared_at"], "active": bool(a["active"]),
             "triggerValue": a["trigger_value"]} for a in storage.recent_alerts(limit, since=twin.run_started)]


@app.get("/api/kpis", tags=["analytics"])
def kpis():
    out = []
    for mid, name in config.MACHINES.items():
        k = twin.kpis_for(mid)
        out.append({"machineId": mid, "name": name, "utilization": k["utilization"],
                    "availability": k["availability"], "samples": k["samples"]})
    return {"windowMinutes": config.KPI_WINDOW_MIN, "machines": out}


@app.get("/api/graph", tags=["twin"])
def graph_view():
    try:
        return graph.export()
    except Exception as e:
        raise HTTPException(503, f"Memgraph unavailable: {e}")


@app.get("/api/predict", tags=["analytics"])
def predict():
    if twin.prediction is None:
        raise HTTPException(503, "Prediction model not available yet.")
    return twin.prediction


@app.get("/api/rejected", tags=["system"])
def rejected(limit: int = Query(50, ge=1, le=500)):
    """Messages that failed validation, newest first, with the reason."""
    return storage.rejected(limit)
