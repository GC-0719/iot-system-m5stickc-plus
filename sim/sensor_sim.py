#!/usr/bin/env python3
"""
Simulated M5StickC IMU sensor.

Publishes the exact payload contract from docs/ARCHITECTURE.md §5, so the
forwarder, the InfluxDB schema and the Grafana dashboards can all be built and
proven before the real board exists. When the M5StickC arrives it replaces this
file and nothing downstream changes -- if it can't, the contract was wrong.

    python3 sim/sensor_sim.py --host 192.168.1.42

The motion model cycles still -> walking -> shaking so the Grafana panels have
something worth looking at. Waveform phase is continuous across batch
boundaries; if it weren't, every graph would show a seam once per second.
"""

import argparse
import json
import math
import random
import time

import paho.mqtt.client as mqtt

# (name, seconds) -- one full cycle is 40 s
STATES = [("still", 12), ("walking", 20), ("shaking", 8)]


class MotionModel:
    """Generates plausible 6-axis IMU samples with a continuous phase."""

    def __init__(self):
        self.phase = 0.0
        self.elapsed = 0.0
        self.idx = 0

    def state_at(self, dt):
        self.elapsed += dt
        name, dur = STATES[self.idx]
        if self.elapsed >= dur:
            self.elapsed = 0.0
            self.idx = (self.idx + 1) % len(STATES)
            name = STATES[self.idx][0]
        return name

    def sample(self, dt):
        """One sample: 3 accelerometer axes in g, 3 gyroscope axes in dps."""
        state = self.state_at(dt)

        if state == "still":
            freq, acc_amp, gyro_amp, noise = 0.0, 0.0, 0.4, 0.008
        elif state == "walking":
            freq, acc_amp, gyro_amp, noise = 2.0, 0.35, 18.0, 0.02
        else:  # shaking
            freq, acc_amp, gyro_amp, noise = 6.0, 0.9, 120.0, 0.05

        self.phase += 2 * math.pi * freq * dt
        if self.phase > 2 * math.pi:
            self.phase -= 2 * math.pi
        s = math.sin(self.phase)
        c = math.cos(self.phase)

        return {
            # Gravity sits on Z at rest -- that 1.0 g offset is the easiest
            # sanity check that the pipeline is carrying real values.
            "ax": round(acc_amp * 0.45 * c + random.gauss(0, noise), 5),
            "ay": round(acc_amp * 0.30 * s + random.gauss(0, noise), 5),
            "az": round(1.0 + acc_amp * s + random.gauss(0, noise), 5),
            "gx": round(gyro_amp * s + random.gauss(0, 0.3), 4),
            "gy": round(gyro_amp * 0.6 * c + random.gauss(0, 0.3), 4),
            "gz": round(gyro_amp * 0.2 * s + random.gauss(0, 0.3), 4),
        }, state


def build_batch(model, n, freq):
    dt = 1.0 / freq
    values = {k: [] for k in ("ax", "ay", "az", "gx", "gy", "gz")}
    state = "still"
    for _ in range(n):
        sample, state = model.sample(dt)
        for k, v in sample.items():
            values[k].append(v)
    return values, state


def main():
    p = argparse.ArgumentParser(description="Simulated M5StickC IMU sensor")
    p.add_argument("--host", default="localhost", help="MQTT broker address")
    p.add_argument("--port", type=int, default=1883)
    p.add_argument("--device-id", default="sim-000001")
    p.add_argument("--site", default="apt")
    p.add_argument("--freq", type=int, default=50, help="sample rate, Hz")
    p.add_argument("--batch", type=int, default=50, help="samples per message")
    p.add_argument("--fw", default="sim-0.1.0")
    args = p.parse_args()

    base = f"cps/hw1/{args.device_id}"
    topic_imu = f"{base}/imu"
    topic_health = f"{base}/health"
    topic_status = f"{base}/status"

    client = mqtt.Client(client_id=f"sim-{args.device_id}")
    # Last Will and Testament: if this process dies without saying goodbye,
    # the broker publishes "offline" for us. Retained, so a dashboard that
    # connects later still learns the true state.
    client.will_set(topic_status, "offline", qos=1, retain=True)
    client.connect(args.host, args.port, keepalive=60)
    client.loop_start()
    client.publish(topic_status, "online", qos=1, retain=True)

    print(f"publishing {args.freq} Hz in batches of {args.batch} -> {topic_imu}")

    model = MotionModel()
    batch_period = args.batch / args.freq
    t0 = time.time()
    seq = 0

    try:
        while True:
            start_ts = t0 + seq * batch_period
            # Wait until this batch's last sample would actually have been
            # taken -- a real sensor cannot publish the future.
            wait = (start_ts + batch_period) - time.time()

            # If we are far behind, the wall clock jumped: the VM was suspended,
            # or chrony stepped after a laptop resume. Without this guard the
            # loop stops sleeping and publishes the whole backlog of backdated
            # batches as fast as it can, which floods the broker, overruns
            # max_queued_messages and shows up downstream as tens of thousands
            # of dropped batches. Re-anchor to now and carry on.
            if wait < -5.0:
                print(f"[warn] {-wait:.0f}s behind (clock step or suspend); "
                      f"re-anchoring instead of replaying the backlog")
                t0 = time.time() - batch_period - seq * batch_period
                start_ts = t0 + seq * batch_period
                wait = 0

            if wait > 0:
                time.sleep(wait)

            values, state = build_batch(model, args.batch, args.freq)
            payload = {
                "device_id": args.device_id,
                "stream": "imu",
                "start_ts": round(start_ts, 4),
                "freq": args.freq,
                "n": args.batch,
                "seq": seq,
                "fw": args.fw,
                "site": args.site,
                "units": {"ax": "g", "gx": "dps"},
                "values": values,
                "meta": {
                    "rssi": random.randint(-70, -40),
                    "vbat": round(random.uniform(3.7, 4.1), 2),
                    "state": state,
                    "clock_src": "sntp",
                    # A real device reports its measured SNTP offset here. The
                    # simulator fakes a small one so the pipeline panel has a
                    # realistic shape to draw.
                    "skew_ms": round(random.gauss(0, 2.5), 2),
                    "sntp_age_s": int(time.time() - t0) % 900,
                },
            }
            client.publish(topic_imu, json.dumps(payload), qos=1)

            if seq % 10 == 0:
                client.publish(topic_health, json.dumps({
                    "device_id": args.device_id,
                    "stream": "health",
                    "start_ts": round(time.time(), 4),
                    "site": args.site,
                    "fw": args.fw,
                    "values": {
                        "vbat": payload["meta"]["vbat"],
                        "rssi": payload["meta"]["rssi"],
                        "heap_free": random.randint(180000, 220000),
                        "uptime_s": int(time.time() - t0),
                    },
                }), qos=1)
                print(f"seq={seq:<6} state={state:<8} az[0]={values['az'][0]}")

            seq += 1
    except KeyboardInterrupt:
        print("\nstopping")
    finally:
        client.publish(topic_status, "offline", qos=1, retain=True)
        client.loop_stop()
        client.disconnect()


if __name__ == "__main__":
    main()
