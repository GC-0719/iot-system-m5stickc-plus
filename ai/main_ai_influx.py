#!/usr/bin/env python3
"""
main_ai_influx.py -- retrospective AI processing of ARCHIVED data (HW2, req 2).

Reads a bounded time window of sensor data back out of InfluxDB, runs the
analyses named in config.yaml, and writes the derived series back as new
measurements (ai_<measurement>). This is the "process the archived data from
InfluxDB" half of the framework; the live half is main_ai_mqtt.py, and both
share ai/processing.py so the analysis is identical.

Nothing about which measurement, field, or analysis to run is hardcoded -- it
all comes from the YAML config (requirement: "avoid hard coding and use a yaml
file to specify the sensor measurements to be processed").

    python3 main_ai_influx.py --start_time 2026-10-19T00:00:00Z \
                              --end_time   2026-10-19T01:00:00Z
    python3 main_ai_influx.py --start_time "now()-30m" --end_time "now()"
"""

from __future__ import annotations
import argparse
import os
import sys
import time

import yaml
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



# --------------------------------------------------------------- config --
def load_config(path: str) -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


def influx_time(expr: str) -> str:
    """Turn a --start_time / --end_time argument into an InfluxQL time literal.

    Accepts a relative expression passed straight through (now(), now()-1h) or
    an absolute RFC3339 timestamp, which gets single-quoted for InfluxQL."""
    e = expr.strip()
    if e.startswith("now(") or e[0].isdigit() and "T" not in e and "-" not in e[1:]:
        return e
    if e.startswith("'") and e.endswith("'"):
        return e
    return f"'{e}'"


# ----------------------------------------------------------- windowing --
def query_retry(client, q, tries=4, delay=1.5):
    """Run a query, retrying transient failures (unreliable link / long delay,
    per the HW2 notes). Raises after the last attempt."""
    import time as _t
    last = None
    for a in range(tries):
        try:
            return client.query(q, epoch="ns")
        except Exception as exc:                        # noqa: BLE001
            last = exc
            print(f"[warn] influx query failed (attempt {a+1}/{tries}): {exc}")
            _t.sleep(delay * (a + 1))
    raise last


def fetch_windows(client: InfluxDBClient, measurement: str, fields: list[str],
                  start: str, end: str, window_s: float):
    """Yield (device_id, window_end_ns, {field: [values]}) for each
    non-overlapping window_s slice in [start, end], per device."""
    cols = ", ".join(f'"{f}"' for f in fields)
    q = (f'SELECT {cols}, "device_id"::tag FROM "{measurement}" '
         f'WHERE time >= {influx_time(start)} AND time <= {influx_time(end)} '
         f'ORDER BY time ASC')
    rs = query_retry(client, q)   # ORDER BY time ASC => already time-ordered

    # group points by device, then slice into windows
    by_dev: dict[str, list[tuple]] = {}
    for pt in rs.get_points():
        dev = pt.get("device_id") or "unknown"
        by_dev.setdefault(dev, []).append(pt)

    win_ns = int(window_s * 1e9)
    for dev, pts in by_dev.items():
        if not pts:
            continue
        w_start = pts[0]["time"]
        window: dict[str, list[float]] = {f: [] for f in fields}
        w_end = w_start + win_ns
        for pt in pts:
            if pt["time"] >= w_end:
                if any(window[f] for f in fields):
                    yield dev, w_end, window
                # advance to the window containing this point
                while pt["time"] >= w_end:
                    w_start, w_end = w_end, w_end + win_ns
                window = {f: [] for f in fields}
            for f in fields:
                v = pt.get(f)
                if v is not None:
                    window[f].append(float(v))
        if any(window[f] for f in fields):
            yield dev, w_end, window


# ------------------------------------------------------------- writing --
def esc(v) -> str:
    return str(v).replace(",", r"\,").replace(" ", r"\ ").replace("=", r"\=")


def to_line(measurement: str, device: str, results: dict[str, float],
            ts_ns: int) -> str:
    fields = ",".join(f"{k}={float(v)}" for k, v in results.items())
    return f"{measurement},device_id={esc(device)},src=influx {fields} {ts_ns}"


def main():
    try:
        sys.stdout.reconfigure(line_buffering=True)   # usable logs under pipes/systemd
    except Exception:
        pass
    ap = argparse.ArgumentParser(description="Retrospective AI processing from InfluxDB")
    ap.add_argument("--config", default=default_config_path())
    ap.add_argument("--start_time", required=True,
                    help="RFC3339 (2026-10-19T00:00:00Z) or relative (now()-1h)")
    ap.add_argument("--end_time", required=True,
                    help="RFC3339 or relative (now())")
    ap.add_argument("--dry-run", action="store_true",
                    help="compute and print results but do not write back")
    args = ap.parse_args()

    cfg = load_config(args.config)
    inf = cfg["influx"]
    sample_hz = float(cfg.get("sample_hz", 50))
    window_s = float(cfg.get("window_seconds", 4))
    prefix = cfg.get("output", {}).get("measurement_prefix", "ai_")

    client = InfluxDBClient(host=inf["host"], port=inf["port"], database=inf["database"])

    total_windows = 0
    total_points = 0
    for m in cfg["measurements"]:
        name = m["name"]
        fields = m["fields"]
        analyses = m["analysis"]
        out_measurement = f"{prefix}{name}"
        lines = []
        n_win = 0
        for dev, ts_ns, window in fetch_windows(
                client, name, fields, args.start_time, args.end_time, window_s):
            results = P.run_analyses(analyses, window, sample_hz=sample_hz)
            if results:
                lines.append(to_line(out_measurement, dev, results, ts_ns))
                n_win += 1
        total_windows += n_win

        if args.dry_run:
            print(f"[{name}] {n_win} window(s) -> {out_measurement} (dry-run, not written)")
            for ln in lines[:3]:
                print("   ", ln)
        elif lines:
            client.write_points(lines, protocol="line", time_precision="n")
            total_points += len(lines)
            print(f"[{name}] wrote {len(lines)} point(s) to {out_measurement}")
        else:
            print(f"[{name}] no data in window")

    print(f"\ndone: {total_windows} window(s) analysed"
          + ("" if args.dry_run else f", {total_points} derived point(s) written"))


if __name__ == "__main__":
    main()
