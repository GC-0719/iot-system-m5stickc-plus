#!/usr/bin/env python3
"""
MQTT -> InfluxDB forwarder.

Subscribes to the sensor topics, expands each batched message into one InfluxDB
point per sample, and writes them as line protocol. Also writes a `pipeline`
measurement per batch -- end-to-end latency, clock skew, dropped batches. That
measurement is the whole reason this is a program we own rather than a Telegraf
config: this process is the only one that sees both the sensor's clock and the
server's.

    python3 forwarder/forwarder.py --mqtt-host localhost --influx-host localhost
"""

import argparse
import base64
import json
import struct
import time

import paho.mqtt.client as mqtt
from influxdb import InfluxDBClient

# Waveform streams: batched arrays expanded to one point per sample. The
# measurement name is taken from the topic suffix, so adding a sensor means
# publishing to a new topic -- no forwarder change.
WAVEFORM_STREAMS = ("imu", "audio")

TOPICS = [(f"cps/hw1/+/{s}", 1) for s in WAVEFORM_STREAMS] + [("cps/hw1/+/health", 1)]


def esc_tag(v):
    """Line protocol escaping for tag keys/values: comma, space, equals."""
    return str(v).replace(",", r"\,").replace(" ", r"\ ").replace("=", r"\=")


def line(measurement, tags, fields, ts_ns):
    """Build one line protocol record.

        imu,device_id=sim-000001,site=apt ax=0.01,az=0.99 1758499200004000000
    """
    tag_str = ",".join(f"{esc_tag(k)}={esc_tag(v)}" for k, v in sorted(tags.items()))
    field_str = ",".join(f"{k}={float(v)}" for k, v in fields.items())
    prefix = f"{measurement},{tag_str}" if tag_str else measurement
    return f"{prefix} {field_str} {ts_ns}"


class Forwarder:
    def __init__(self, influx, batch_points, flush_secs):
        self.influx = influx
        self.batch_points = batch_points
        self.flush_secs = flush_secs
        self.buf = []
        self.last_flush = time.time()
        self.last_seq = {}          # device_id -> last seq seen
        self.points_written = 0
        self.last_write_ms = 0.0    # duration of the most recent influx write

    # -- writing --------------------------------------------------------

    def add(self, lines):
        self.buf.extend(lines)
        due = len(self.buf) >= self.batch_points
        stale = (time.time() - self.last_flush) >= self.flush_secs
        if due or stale:
            self.flush()

    def flush(self):
        if not self.buf:
            self.last_flush = time.time()
            return
        t0 = time.perf_counter()
        try:
            self.influx.write_points(self.buf, protocol="line", time_precision="n")
            self.points_written += len(self.buf)
            self.last_write_ms = (time.perf_counter() - t0) * 1000
        except Exception as exc:                       # noqa: BLE001
            # Never let one bad write kill the process -- MQTT QoS 1 with a
            # persistent session means the broker redelivers.
            print(f"[warn] influx write failed ({len(self.buf)} pts): {exc}")
            self.last_write_ms = 0.0
        finally:
            self.buf.clear()
            self.last_flush = time.time()

    # -- message handling -----------------------------------------------

    def on_waveform(self, msg, recv_wall, measurement):
        p = json.loads(msg.payload)
        device_id = p["device_id"]
        freq = float(p["freq"])
        n = int(p["n"])
        start_ts = float(p["start_ts"])
        values = p["values"]
        seq = int(p.get("seq", -1))

        # Factor G: the wire format may be little-endian float32 packed into
        # base64 instead of a JSON number array. Roughly 4x smaller; the cost
        # is that a human can no longer read the payload off mosquitto_sub.
        if p.get("enc") == "b64f32":
            values = {
                k: list(struct.unpack("<%df" % n, base64.b64decode(v)))
                for k, v in values.items()
            }

        tags = {
            "device_id": device_id,
            "site": p.get("site", "unknown"),
            "fw": p.get("fw", "unknown"),
        }

        # Expand the batch. Sample i is timestamped start_ts + i/freq. Doing
        # this here rather than on the device is what keeps the payload small.
        start_ns = int(start_ts * 1e9)
        step_ns = int(1e9 / freq)
        keys = list(values.keys())
        lines = []
        for i in range(n):
            fields = {k: values[k][i] for k in keys}
            lines.append(line(measurement, tags, fields, start_ns + i * step_ns))

        # Gap detection: seq is monotonic per device, so a jump is a countable
        # number of lost batches rather than an invisible hole in the graph.
        seq_key = (device_id, measurement)
        prev = self.last_seq.get(seq_key)
        gap = 0 if prev is None else max(0, seq - prev - 1)
        self.last_seq[seq_key] = seq

        self.add(lines)

        meta = p.get("meta", {})
        pipeline_fields = {
            "e2e_latency_ms": (recv_wall - start_ts) * 1000.0,
            "skew_ms": meta.get("skew_ms", 0.0),
            "sntp_age_s": meta.get("sntp_age_s", 0),
            "gap_count": gap,
            "batch_write_ms": self.last_write_ms,
            "payload_bytes": len(msg.payload),
            "points": n,
        }
        ptags = dict(tags, stream=measurement)
        self.add([line("pipeline", ptags, pipeline_fields, int(recv_wall * 1e9))])

        if gap:
            print(f"[gap] {device_id}/{measurement} lost {gap} batch(es) before seq={seq}")

    def on_health(self, msg, recv_wall):
        p = json.loads(msg.payload)
        tags = {
            "device_id": p["device_id"],
            "site": p.get("site", "unknown"),
            "fw": p.get("fw", "unknown"),
        }
        ts_ns = int(float(p.get("start_ts", recv_wall)) * 1e9)
        self.add([line("health", tags, p["values"], ts_ns)])


def main():
    ap = argparse.ArgumentParser(description="MQTT -> InfluxDB forwarder")
    ap.add_argument("--mqtt-host", default="localhost")
    ap.add_argument("--mqtt-port", type=int, default=1883)
    ap.add_argument("--influx-host", default="localhost")
    ap.add_argument("--influx-port", type=int, default=8086)
    ap.add_argument("--database", default="cps_hw1")
    # Factor C in the benchmark matrix: points accumulated before a write.
    ap.add_argument("--batch-points", type=int, default=500)
    ap.add_argument("--flush-secs", type=float, default=1.0)
    args = ap.parse_args()

    influx = InfluxDBClient(
        host=args.influx_host, port=args.influx_port, database=args.database
    )
    existing = {d["name"] for d in influx.get_list_database()}
    if args.database not in existing:
        raise SystemExit(
            f"database '{args.database}' does not exist. Create it first:\n"
            f"  influx -execute 'CREATE DATABASE {args.database}'"
        )

    fwd = Forwarder(influx, args.batch_points, args.flush_secs)

    def on_connect(client, userdata, flags, rc):
        print(f"connected to mqtt://{args.mqtt_host}:{args.mqtt_port} (rc={rc})")
        client.subscribe(TOPICS)

    def on_message(client, userdata, msg):
        recv_wall = time.time()
        try:
            suffix = msg.topic.rsplit("/", 1)[-1]
            if suffix in WAVEFORM_STREAMS:
                fwd.on_waveform(msg, recv_wall, suffix)
            elif suffix == "health":
                fwd.on_health(msg, recv_wall)
        except Exception as exc:                       # noqa: BLE001
            print(f"[warn] bad message on {msg.topic}: {exc}")

    # clean_session=False + a fixed client_id means the broker queues QoS 1
    # messages for us while this process is restarting.
    client = mqtt.Client(client_id="cps-forwarder", clean_session=False)
    client.on_connect = on_connect
    client.on_message = on_message
    client.connect(args.mqtt_host, args.mqtt_port, keepalive=60)

    last_report = time.time()
    try:
        while True:
            client.loop(timeout=0.5)
            if time.time() - fwd.last_flush >= args.flush_secs:
                fwd.flush()
            if time.time() - last_report >= 10:
                print(f"points written: {fwd.points_written}")
                last_report = time.time()
    except KeyboardInterrupt:
        print("\nflushing and stopping")
        fwd.flush()
        client.disconnect()


if __name__ == "__main__":
    main()
