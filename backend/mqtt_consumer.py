"""MQTT subscriber: listens on workshop/# and hands every message to the twin pipeline.

paho-mqtt runs its network loop in a background thread; connect_async + loop_start
keeps retrying, so the server can start before Mosquitto is ready. Incoming messages
go into an in-process queue and a separate worker thread processes them in order, so a
slow database can never block the network thread and cause messages to be lost."""
import logging
import queue
import threading

import paho.mqtt.client as mqtt

log = logging.getLogger("mqtt")


class Consumer:
    def __init__(self, host, port, topic, handler):
        self.topic = topic
        self.handler = handler
        self.connected = False
        self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="twin-app-server")
        self.client.on_connect = self._on_connect
        self.client.on_disconnect = self._on_disconnect
        self.client.on_message = self._on_message
        self.client.reconnect_delay_set(min_delay=1, max_delay=10)
        self.host, self.port = host, port
        self.inbox = queue.Queue()
        self.worker = threading.Thread(target=self._work, name="twin-worker", daemon=True)

    def start(self):
        self.worker.start()
        self.client.connect_async(self.host, self.port, keepalive=30)
        self.client.loop_start()
        log.info("connecting to MQTT broker %s:%s", self.host, self.port)

    def stop(self):
        self.client.loop_stop()
        self.client.disconnect()
        self.inbox.put(None)

    def backlog(self):
        return self.inbox.qsize()

    def _on_connect(self, client, userdata, flags, reason_code, properties):
        if reason_code.is_failure:
            log.warning("MQTT connect failed: %s", reason_code)
            return
        self.connected = True
        client.subscribe(self.topic, qos=1)
        log.info("subscribed to %s", self.topic)

    def _on_disconnect(self, client, userdata, flags, reason_code, properties):
        self.connected = False
        log.warning("MQTT disconnected (%s), retrying", reason_code)

    def _on_message(self, client, userdata, msg):
        self.inbox.put((msg.topic, msg.payload))  # network thread only queues

    def _work(self):
        while True:
            item = self.inbox.get()
            if item is None:
                return
            topic, payload = item
            try:
                self.handler(topic, payload)
            except Exception:  # never let one bad message kill the worker
                log.exception("error while processing %s", topic)
