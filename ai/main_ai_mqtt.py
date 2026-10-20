#!/usr/bin/env python3
"""
main_ai_mqtt.py -- live AI processing of REAL-TIME data from MQTT (HW2, req 2).

Subscribes to the same batched sensor stream the forwarder consumes, keeps a
rolling window per device per measurement, and runs the analyses named in
config.yaml as the data arrives. Results are published to cps/hw1/<device>/ai
(so any live consumer can subscribe) and also written to InfluxDB as
ai_<measurement> (so they appear in Grafana without changing the forwarder).

Shares ai/processing.py with main_ai_influx.py, so the live analysis is
identical to the retrospective one. Nothing is hardcoded -- measurements,
fields, analyses, window length and sample rate all come from config.yaml
("avoid hard coding and use a yaml file to specify the sensor measurements").

    python3 main_ai_mqtt.py                 # uses ./config.yaml
    python3 main_ai_mqtt.py --config other.yaml
"""

from __future__ import annotations
import argparse
import json
import os
import ssl
import sys
import time
from collections import deque

import yaml
import paho.mqtt.client as mqtt
from influxdb import InfluxDBClient

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import processing as P

def default_config_path() -> str:
    """Locate config.yaml for both interpreted and Nuitka --onefile runs.

    In a Nuitka onefile, __file__ is a temporary extraction dir, so the config
    bundled there is not the one the user edits. Prefer config next to the real
    executable (sys.argv[0]) and the current directory; fall back to __file__."""
    here = os.path.dirname(os.path.abspath(__file__))
    exe = os.path.dirname(os.path.abspath(sys.argv[0])) if sys.argv and sys.argv[0] else here
    for cand in (os.path.join(exe, "config.yaml"),
                 os.path.join(os.getcwd(), "config.yaml"),
                 os.path.join(here, "config.yaml")):
        if os.path.isfile(cand):
            return cand
    return os.path.join(here, "config.yaml")



def load_config(path: str) -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


def esc(v) -> str:
    return str(v).replace(",", r"\,").replace(" ", r"\ ").replace("=", r"\=")


class LiveProcessor:
    """Holds a rolling window of recent samples per (device, measurement) and
    runs the configured analyses once per window_seconds of fresh data."""

    def __init__(self, cfg: dict):
        self.sample_hz = float(cfg.get("sample_hz", 50))
        self.window_s = float(cfg.get("window_seconds", 4))
        self.maxlen = int(self.sample_hz * self.window_s)
        self.prefix = cfg.get("output", {}).get("measurement_prefix", "ai_")
        self.pub_fmt = cfg["mqtt"].get("pub_topic_fmt", "cps/hw1/{device}/ai")

        # measurement name -> (fields, analyses)
        self.spec = {m["name"]: (m["fields"], m["analysis"])
                     for m in cfg["measurements"]}
        # (device, measurement) -> {field: deque}
        self.windows: dict[tuple, dict[str, deque]] = {}
        self.last_run: dict[tuple, float] = {}

        inf = cfg["influx"]
        try:
            self.influx = InfluxDBClient(host=inf["host"], port=inf["port"],
                                         database=inf["database"])
            self.influx.ping()
            self.influx_ok = True
        except Exception as exc:                       # noqa: BLE001
            print(f"[warn] InfluxDB unavailable ({exc}); publishing to MQTT only")
            self.influx_ok = False

    def ingest(self, measurement: str, device: str, values: dict):
        if measurement not in self.spec:
            return
        fields, _ = self.spec[measurement]
        key = (device, measurement)
        win = self.windows.setdefault(
            key, {f: deque(maxlen=self.maxlen) for f in fields})
        n = 0
        for f in fields:
            arr = values.get(f)
            if arr:
                win[f].extend(float(x) for x in arr)
                n = max(n, len(arr))
        return key

    def maybe_run(self, client, key):
        device, measurement = key
        fields, analyses = self.spec[measurement]
        win = self.windows[key]
        if min((len(win[f]) for f in fields), default=0) < self.maxlen:
            return                                     # window not full yet
        now = time.time()
        if now - self.last_run.get(key, 0) < self.window_s:
            return                                     # already ran this window
        self.last_run[key] = now

        window = {f: list(win[f]) for f in fields}
        results = P.run_analyses(analyses, window, sample_hz=self.sample_hz)
        if not results:
            return

        # 1) publish to the live ai topic
        topic = self.pub_fmt.format(device=device)
        payload = {"device_id": device, "stream": "ai", "source": measurement,
                   "ts": now, "results": results}
        client.publish(topic, json.dumps(payload), qos=0)

        # 2) persist to InfluxDB so Grafana can show it
        if self.influx_ok:
            out_m = f"{self.prefix}{measurement}"
            flds = ",".join(f"{k}={float(v)}" for k, v in results.items())
            line = f"{out_m},device_id={esc(device)},src=mqtt {flds} {int(now*1e9)}"
            try:
                self.influx.write_points([line], protocol="line", time_precision="n")
            except Exception as exc:                   # noqa: BLE001
                print(f"[warn] influx write failed: {exc}")

        code = results.get("activity_code")
        extra = f" activity={code:.0f}" if code is not None else ""
        print(f"[ai] {device}/{measurement}: "
              + " ".join(f"{k}={v:.3f}" for k, v in list(results.items())[:3])
              + extra)


def main():
    try:
        sys.stdout.reconfigure(line_buffering=True)   # usable logs under pipes/systemd
    except Exception:
        pass
    ap = argparse.ArgumentParser(description="Live AI processing from MQTT")
    ap.add_argument("--config", default=default_config_path())
    args = ap.parse_args()
    cfg = load_config(args.config)
    mq = cfg["mqtt"]
    proc = LiveProcessor(cfg)

    client = mqtt.Client(client_id="cps-ai-mqtt")
    if mq.get("tls"):
        client.tls_set(ca_certs=mq.get("cafile"), cert_reqs=ssl.CERT_REQUIRED,
                       tls_version=ssl.PROTOCOL_TLS_CLIENT)

    def on_connect(c, u, flags, rc):
        print(f"connected to mqtt://{mq['host']}:{mq['port']} (rc={rc}), "
              f"sub {mq['sub_topic']}")
        c.subscribe(mq["sub_topic"])

    def on_message(c, u, msg):
        try:
            suffix = msg.topic.rsplit("/", 1)[-1]
            if suffix not in proc.spec:
                return                                 # not a measurement we process
            p = json.loads(msg.payload)
            key = proc.ingest(suffix, p["device_id"], p.get("values", {}))
            if key:
                proc.maybe_run(c, key)
        except Exception as exc:                       # noqa: BLE001
            print(f"[warn] bad message on {msg.topic}: {exc}")

    client.on_connect = on_connect
    client.on_message = on_message
    client.connect(mq["host"], mq["port"], keepalive=60)
    print(f"window = {proc.window_s}s ({proc.maxlen} samples @ {proc.sample_hz} Hz)")
    try:
        client.loop_forever()
    except KeyboardInterrupt:
        print("\nstopping")
        client.disconnect()


if __name__ == "__main__":
    main()
