"""All REST calls for the dashboard live in this file. The dashboard only talks to the
FastAPI app server, never to MQTT or the databases directly.

Endpoints used
  /api/state    {timestamp, source, live, lastMessageAgeSec, runStartedAt,
                 zone:{zoneId,temperature_c,humidity_pct,co2_ppm,noise_db,riskLevel},
                 machines:[{machineId,name,status,availabilityState,inMaintenance,utilization,availability}],
                 activeAlerts}
  /api/history  {signal, points:[{timestamp,sourceTimestamp,value}]}   (also lathe_status / drill_status)
  /api/alerts   [{id,severity,description,asset,createdAt,clearedAt,active,triggerValue}]
  /api/graph    {nodes:[{id,label,properties,alertActive}], edges:[{source,target,type}]}
"""
import os

import requests

API_BASE = os.getenv("API_BASE", "http://localhost:8000")
STATUS_SIGNAL = {"machine1": "lathe_status", "machine2": "drill_status"}


class ApiError(Exception):
    pass


class Client:
    def __init__(self, base=API_BASE):
        self.base = base.rstrip("/")

    def _get(self, path, **params):
        try:
            r = requests.get(self.base + path, params=params or None, timeout=3)
            r.raise_for_status()
            return r.json()
        except Exception as e:  # network, HTTP or JSON errors all surface the same way
            raise ApiError(f"{path} failed: {e}") from e

    def state(self):
        return self._get("/api/state")

    def history(self, signal, minutes=30):
        return self._get("/api/history", signal=signal, minutes=minutes)

    def alerts(self):
        return self._get("/api/alerts")

    def graph(self):
        return self._get("/api/graph")
