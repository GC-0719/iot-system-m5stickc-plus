#!/usr/bin/env python3
"""
Per-service CPU and memory sampler (ARCHITECTURE.md §7).

Runs ON the VM and writes into the same InfluxDB it is measuring, as
measurement `hostmetrics`. The stack therefore benchmarks itself and the
results are a Grafana dashboard rather than a pile of text files.

    python3 bench/metrics_agent.py --label "C: batch=500"

Its own footprint is a few MB and one write per second, which is inside the
noise of what it measures -- but it does tag itself as `agent` so you can prove
that rather than assume it.
"""

import argparse
import json
import os
import time

import psutil
from influxdb import InfluxDBClient

# Matched against process name first, then the full command line. The forwarder
# and this agent are both `python3`, so they can only be told apart by cmdline.
SERVICES = {
    "influxd":   ["influxd"],
    "mosquitto": ["mosquitto"],
    "mysqld":    ["mysqld"],
    "grafana":   ["grafana-server", "grafana"],
    "chronyd":   ["chronyd"],
    "forwarder": ["forwarder.py"],
    "agent":     ["metrics_agent.py"],
}

INFLUX_DATA_DIR = "/var/lib/influxdb"


def esc_tag(v):
    """Escape a line protocol tag value: backslash, comma, space, equals.

    A natural run label like 'C: batch=500' contains a space AND an equals
    sign. Unescaped, the space terminates the tag set and InfluxDB rejects the
    entire batch with 'invalid field format' -- which sounds like a problem
    with the fields, not the tags.
    """
    return (str(v).replace("\\", "\\\\")
                  .replace(",", r"\,")
                  .replace(" ", r"\ ")
                  .replace("=", r"\="))


def find_procs():
    """Map service name -> [psutil.Process]. Rebuilt periodically so restarts
    are picked up rather than silently reported as a dead service."""
    found = {k: [] for k in SERVICES}
    for p in psutil.process_iter(["name", "cmdline"]):
        try:
            name = (p.info["name"] or "").lower()
            cmd = " ".join(p.info["cmdline"] or []).lower()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
        for svc, patterns in SERVICES.items():
            for pat in patterns:
                if pat in name or pat in cmd:
                    found[svc].append(p)
                    break
    return found


def dir_bytes(path):
    """Total bytes under `path`, or None if it cannot be read.

    /var/lib/influxdb is root-owned. Walking it as an unprivileged user yields
    nothing, and returning 0 would be reported as 'the database is empty'
    rather than 'I was not allowed to look'. Distinguish the two.
    """
    total = 0
    readable = False
    for root, _dirs, files in os.walk(path, onerror=lambda e: None):
        readable = True
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total if readable else None


def main():
    ap = argparse.ArgumentParser(description="Per-service CPU/RSS sampler")
    ap.add_argument("--influx-host", default="localhost")
    ap.add_argument("--influx-port", type=int, default=8086)
    ap.add_argument("--database", default="cps_hw1")
    ap.add_argument("--label", default="idle", help="benchmark cell label (a tag)")
    ap.add_argument("--interval", type=float, default=1.0)
    ap.add_argument("--duration", type=float, default=0, help="0 = run forever")
    ap.add_argument("--disk-every", type=int, default=30,
                    help="sample InfluxDB disk usage every N cycles")
    args = ap.parse_args()

    influx = InfluxDBClient(host=args.influx_host, port=args.influx_port,
                            database=args.database)

    procs = find_procs()
    # First cpu_percent() call per process always returns 0.0 -- it establishes
    # the baseline. Prime them now so the first real sample is meaningful.
    for plist in procs.values():
        for p in plist:
            try:
                p.cpu_percent(None)
            except psutil.Error:
                pass
    psutil.cpu_percent(None)
    time.sleep(args.interval)

    ncpu = psutil.cpu_count()
    run_tag = esc_tag(args.label)
    print(f"sampling every {args.interval}s, label={args.label!r}, {ncpu} cpus")

    if dir_bytes(INFLUX_DATA_DIR) is None:
        print(f"[note] {INFLUX_DATA_DIR} is not readable as this user, so "
              f"influx_disk_mb will be omitted.\n"
              f"       Re-run under sudo to record disk growth per run.")

    start = time.time()
    cycle = 0
    try:
        while args.duration == 0 or (time.time() - start) < args.duration:
            if cycle % 15 == 0:
                procs = find_procs()

            lines = []
            total_rss = 0
            for svc, plist in procs.items():
                cpu = 0.0
                rss = 0
                threads = 0
                alive = 0
                for p in plist:
                    try:
                        cpu += p.cpu_percent(None)
                        rss += p.memory_info().rss
                        threads += p.num_threads()
                        alive += 1
                    except psutil.Error:
                        continue
                if not alive:
                    continue
                total_rss += rss
                lines.append(
                    f"hostmetrics,service={svc},run={run_tag} "
                    f"cpu_pct={cpu:.2f},rss_mb={rss/1048576:.2f},"
                    f"threads={threads}i,procs={alive}i"
                )

            vm = psutil.virtual_memory()
            sys_fields = (
                f"cpu_pct={psutil.cpu_percent(None):.2f},"
                f"mem_used_mb={(vm.total-vm.available)/1048576:.1f},"
                f"mem_avail_mb={vm.available/1048576:.1f},"
                f"stack_rss_mb={total_rss/1048576:.2f},"
                f"load1={os.getloadavg()[0]:.2f}"
            )
            if cycle % args.disk_every == 0:
                disk = dir_bytes(INFLUX_DATA_DIR)
                if disk is not None:
                    sys_fields += f",influx_disk_mb={disk/1048576:.1f}"
            lines.append(f"hostmetrics,service=_system,run={run_tag} {sys_fields}")

            try:
                influx.write_points(lines, protocol="line")
            except Exception as exc:                      # noqa: BLE001
                print(f"[warn] write failed: {exc}")

            if cycle % 30 == 0:
                summary = {l.split(",")[1].split("=")[1]:
                           round(float(l.split("cpu_pct=")[1].split(",")[0]), 1)
                           for l in lines if "cpu_pct=" in l}
                print(f"t+{int(time.time()-start):>4}s  stack_rss="
                      f"{total_rss/1048576:.0f}MB  cpu%={json.dumps(summary)}")

            cycle += 1
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print("\nstopping")


if __name__ == "__main__":
    main()
