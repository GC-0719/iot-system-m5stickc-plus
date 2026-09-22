#!/usr/bin/env python3
"""
Reproducible synthetic load for the benchmark matrix (ARCHITECTURE.md §7).

Publishes the same payload contract as the real sensor, but at a controlled and
repeatable rate, so a benchmark cell never depends on physically shaking a
board. Run it from the Mac rather than the VM: the point is to measure the
server's CPU and RAM, and a generator sharing those cores contaminates exactly
the numbers being collected.

    # baseline: one device at the real sensor's rate
    python3 bench/loadgen.py --host 10.11.3.114 --rate 1 --duration 60

    # factor C: hammer the forwarder's write batching
    python3 bench/loadgen.py --host 10.11.3.114 --rate 50 --devices 10 --duration 600

    # factor G: payload encoding
    python3 bench/loadgen.py --host 10.11.3.114 --encoding b64f32 --rate 20

Waveforms are pre-generated from a fixed seed and rotated, so the generator's
own CPU cost stays near zero and two runs at the same settings push byte-for-byte
comparable traffic.
"""

import argparse
import base64
import json
import random
import struct
import sys
import time

import paho.mqtt.client as mqtt

AXES = ("ax", "ay", "az", "gx", "gy", "gz")
POOL_SIZE = 16          # distinct pre-built waveforms to rotate through


def build_pool(batch, seed, encoding):
    """Pre-render POOL_SIZE waveform blocks so the hot loop does no maths."""
    rng = random.Random(seed)
    pool = []
    for _ in range(POOL_SIZE):
        vals = {}
        for ax in AXES:
            base = 1.0 if ax == "az" else 0.0
            amp = 0.35 if ax.startswith("a") else 20.0
            series = [
                round(base + amp * rng.uniform(-1, 1), 5 if ax[0] == "a" else 4)
                for _ in range(batch)
            ]
            if encoding == "b64f32":
                vals[ax] = base64.b64encode(
                    struct.pack("<%df" % batch, *series)
                ).decode("ascii")
            else:
                vals[ax] = series
        pool.append(vals)
    return pool


def main():
    p = argparse.ArgumentParser(description="Benchmark load generator")
    p.add_argument("--host", default="localhost", help="MQTT broker")
    p.add_argument("--port", type=int, default=1883)
    p.add_argument("--rate", type=float, default=1.0,
                   help="messages per second, total across all devices")
    p.add_argument("--devices", type=int, default=1)
    p.add_argument("--batch", type=int, default=50, help="samples per message")
    p.add_argument("--freq", type=int, default=50, help="declared sample rate")
    p.add_argument("--qos", type=int, choices=[0, 1, 2], default=1)
    p.add_argument("--encoding", choices=["json", "b64f32"], default="json")
    p.add_argument("--duration", type=float, default=60.0, help="seconds")
    p.add_argument("--label", default="bench", help="run label, becomes a tag")
    p.add_argument("--seed", type=int, default=1337)
    p.add_argument("--prefix", default="ld", help="device_id prefix")
    args = p.parse_args()

    pool = build_pool(args.batch, args.seed, args.encoding)
    device_ids = [f"{args.prefix}-{i:06d}" for i in range(args.devices)]

    client = mqtt.Client(client_id=f"loadgen-{args.seed}")
    client.max_inflight_messages_set(200)   # else QoS 1 throttles at 20
    client.max_queued_messages_set(0)       # unbounded local queue
    client.connect(args.host, args.port, keepalive=60)
    client.loop_start()

    interval = 1.0 / args.rate
    deadline = time.time()
    end = deadline + args.duration
    seq = 0
    sent = failed = wire_bytes = 0
    t0 = time.time()

    print(f"{args.rate} msg/s x {args.batch} samples across {args.devices} "
          f"device(s), qos={args.qos}, encoding={args.encoding}, "
          f"{args.duration:g}s", file=sys.stderr)

    try:
        while time.time() < end:
            dev = device_ids[seq % args.devices]
            payload = {
                "device_id": dev,
                "stream": "imu",
                # Backdate by one batch period: a real sensor cannot publish a
                # batch until its last sample has happened.
                "start_ts": round(time.time() - args.batch / args.freq, 4),
                "freq": args.freq,
                "n": args.batch,
                "seq": seq // args.devices,
                "fw": f"loadgen/{args.label}",
                "site": "bench",
                "values": pool[seq % POOL_SIZE],
                "meta": {"rssi": -55, "vbat": 4.0, "skew_ms": 0.0,
                         "sntp_age_s": 0, "run": args.label},
            }
            if args.encoding == "b64f32":
                payload["enc"] = "b64f32"

            body = json.dumps(payload)
            info = client.publish(f"cps/hw1/{dev}/imu", body, qos=args.qos)
            if info.rc == mqtt.MQTT_ERR_SUCCESS:
                sent += 1
                wire_bytes += len(body)
            else:
                failed += 1

            seq += 1
            deadline += interval
            slack = deadline - time.time()
            if slack > 0:
                time.sleep(slack)
            elif slack < -1.0:
                # More than a second behind: the broker or the network is the
                # bottleneck, which is itself a finding worth surfacing.
                deadline = time.time()
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
    finally:
        client.loop_stop()
        client.disconnect()

    elapsed = time.time() - t0
    print(json.dumps({
        "label": args.label,
        "requested_rate": args.rate,
        "achieved_msg_s": round(sent / elapsed, 2),
        "achieved_points_s": round(sent * args.batch / elapsed, 1),
        "achieved_kb_s": round(wire_bytes / elapsed / 1024, 1),
        "bytes_per_msg": round(wire_bytes / sent) if sent else 0,
        "sent": sent, "failed": failed,
        "elapsed_s": round(elapsed, 2),
        "qos": args.qos, "encoding": args.encoding,
        "devices": args.devices, "batch": args.batch,
    }, indent=2))


if __name__ == "__main__":
    main()
