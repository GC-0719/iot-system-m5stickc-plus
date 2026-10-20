#!/usr/bin/env python3
"""
main_ai_mqtt.py -- live AI processing of REAL-TIME data from MQTT (HW2, req 2).

Processes the live sensor stream and writes derived results back as ai_<stream>.
Shares ai/processing.py with main_ai_influx.py so the analysis is identical, and
is driven entirely by config.yaml -- no measurement/field/analysis is named in
code ("avoid hard coding and use a yaml file ...").

Reliability (per the HW2 slide notes: the link may be unreliable, have long
delays, deliver packets out of timestamp order, or lose packets entirely):
  * QoS 1 + a persistent session, so the broker redelivers messages missed while
    this process is restarting.
  * Samples are buffered BY THEIR OWN TIMESTAMP (start_ts + i/freq), not by
    arrival order, and each window is sorted by time before analysis -- so a
    late or out-of-order batch is placed correctly, not appended at the end.
  * Windows are computed over whatever samples are present, so lost packets just
    leave gaps rather than breaking the analysis; gaps are counted from `seq`.

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
from bisect import insort

import yaml
import paho.mqtt.client as mqtt
from influxdb import InfluxDBClient

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import processing as P


def default_config_path() -> str:
    """Locate config.yaml for both interpreted and Nuitka --onefile runs."""
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
    """Per (device, measurement): a time-ordered sample buffer that is analysed
    one window at a time, in timestamp order, tolerant of loss and reordering."""

    def __init__(self, cfg: dict):
        self.sample_hz = float(cfg.get("sample_hz", 50))
        self.window_s = float(cfg.get("window_seconds", 4))
        self.prefix = cfg.get("output", {}).get("measurement_prefix", "ai_")
        self.pub_fmt = cfg["mqtt"].get("pub_topic_fmt", "cps/hw1/{device}/ai")
        self.spec = {m["name"]: (m["fields"], m["analysis"]) for m in cfg["measurements"]}

        # key -> list of (ts, {field: val}) kept sorted by ts
        self.buf: dict[tuple, list] = {}
        self.win_end: dict[tuple, float] = {}   # end ts of the last window analysed
        self.last_seq: dict[tuple, int] = {}
        self.gaps: dict[tuple, int] = {}

        inf = cfg["influx"]
        try:
            self.influx = InfluxDBClient(host=inf["host"], port=inf["port"],
                                         database=inf["database"])
            self.influx.ping()
            self.influx_ok = True
        except Exception as exc:                       # noqa: BLE001
            print(f"[warn] InfluxDB unavailable ({exc}); publishing to MQTT only")
            self.influx_ok = False

    def ingest(self, measurement: str, p: dict):
        if measurement not in self.spec:
            return None
        fields, _ = self.spec[measurement]
        device = p["device_id"]
        key = (device, measurement)
        values = p.get("values", {})
        freq = float(p.get("freq", self.sample_hz)) or self.sample_hz
        start_ts = float(p.get("start_ts", time.time()))
        n = int(p.get("n", 0)) or (len(next(iter(values.values()), [])) if values else 0)

        # count lost batches from the monotonic seq (reliability: loss is counted)
        seq = int(p.get("seq", -1))
        if seq >= 0:
            prev = self.last_seq.get(key)
            if prev is not None and seq > prev + 1:
                self.gaps[key] = self.gaps.get(key, 0) + (seq - prev - 1)
            self.last_seq[key] = seq

        buf = self.buf.setdefault(key, [])
        for i in range(n):
            ts = start_ts + i / freq                   # each sample's OWN time
            sample = {}
            for f in fields:
                arr = values.get(f)
                if arr and i < len(arr):
                    sample[f] = float(arr[i])
            if sample:
                insort(buf, (ts, i, sample))           # keep buffer time-ordered
        return key

    def maybe_run(self, client, key):
        fields, analyses = self.spec[key[1]]
        buf = self.buf[key]
        if not buf:
            return
        latest = buf[-1][0]
        start_win = self.win_end.get(key, buf[0][0])
        # only once a full window of time has accrued past the last processed end
        if latest - start_win < self.window_s:
            return
        w0, w1 = start_win, start_win + self.window_s

        # gather samples whose timestamp falls in [w0, w1), already time-sorted
        window = {f: [] for f in fields}
        count = 0
        for ts, _i, sample in buf:
            if ts < w0:
                continue
            if ts >= w1:
                break
            for f in fields:
                if f in sample:
                    window[f].append(sample[f])
            count += 1
        self.win_end[key] = w1

        # trim old samples, but keep a margin so a late packet can still land
        margin = w1 - 2 * self.window_s
        if margin > buf[0][0]:
            self.buf[key] = [s for s in buf if s[0] >= margin]

        if count == 0:            # whole window lost; skip rather than emit noise
            return
        results = P.run_analyses(analyses, window, sample_hz=self.sample_hz)
        if not results:
            return
        results["n_samples"] = float(count)            # visibility into loss
        results["gap_batches"] = float(self.gaps.get(key, 0))

        device, measurement = key
        topic = self.pub_fmt.format(device=device)
        client.publish(topic, json.dumps(
            {"device_id": device, "stream": "ai", "source": measurement,
             "window_end": w1, "results": results}), qos=1)
        if self.influx_ok:
            flds = ",".join(f"{k}={float(v)}" for k, v in results.items())
            line = f"{self.prefix}{measurement},device_id={esc(device)},src=mqtt {flds} {int(w1*1e9)}"
            try:
                self.influx.write_points([line], protocol="line", time_precision="n")
            except Exception as exc:                   # noqa: BLE001
                print(f"[warn] influx write failed: {exc}")
        code = results.get("activity_code")
        print(f"[ai] {device}/{measurement} win_end={w1:.1f} n={count}"
              + (f" activity={code:.0f}" if code is not None else "")
              + (f" gaps={int(results['gap_batches'])}" if results['gap_batches'] else ""))


def main():
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except Exception:
        pass
    ap = argparse.ArgumentParser(description="Live AI processing from MQTT")
    ap.add_argument("--config", default=default_config_path())
    args = ap.parse_args()
    cfg = load_config(args.config)
    mq = cfg["mqtt"]
    proc = LiveProcessor(cfg)

    # persistent session so QoS-1 messages missed during a restart are redelivered
    client = mqtt.Client(client_id="cps-ai-mqtt", clean_session=False)
    client.reconnect_delay_set(min_delay=1, max_delay=30)   # survive long outages
    if mq.get("tls"):
        client.tls_set(ca_certs=mq.get("cafile"), cert_reqs=ssl.CERT_REQUIRED,
                       tls_version=ssl.PROTOCOL_TLS_CLIENT)

    def on_connect(c, u, flags, rc):
        print(f"connected to mqtt://{mq['host']}:{mq['port']} (rc={rc}), sub {mq['sub_topic']} qos1")
        c.subscribe(mq["sub_topic"], qos=1)

    def on_message(c, u, msg):
        try:
            suffix = msg.topic.rsplit("/", 1)[-1]
            if suffix not in proc.spec:
                return
            key = proc.ingest(suffix, json.loads(msg.payload))
            if key:
                proc.maybe_run(c, key)
        except Exception as exc:                       # noqa: BLE001
            print(f"[warn] bad message on {msg.topic}: {exc}")

    client.on_connect = on_connect
    client.on_message = on_message
    client.connect(mq["host"], mq["port"], keepalive=60)
    print(f"window = {proc.window_s}s @ {proc.sample_hz} Hz; "
          f"timestamp-ordered, loss-tolerant")
    try:
        client.loop_forever()                          # auto-reconnects on drop
    except KeyboardInterrupt:
        print("\nstopping")
        client.disconnect()


if __name__ == "__main__":
    main()
