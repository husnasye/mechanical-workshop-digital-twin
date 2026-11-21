FROM python:3.11-slim

WORKDIR /app
ENV PYTHONUNBUFFERED=1 MQTT_HOST=mosquitto MQTT_PORT=1883

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY replay.py workshop_data.csv maintenance_events.csv ./

# Any replay.py options can be appended, e.g.
#   docker compose run --rm simulator --interval 1 --start-row 360
ENTRYPOINT ["python", "replay.py"]
