#!/usr/bin/env python3
"""
Turn a benchmark window into one result row (ARCHITECTURE.md §7).

Reads back what metrics_agent.py and the forwarder wrote, applies the stated
methodology -- discard the first 60 s of warm-up, take medians over the steady
state -- and prints a row you can paste into the report.

    python3 bench/summarize.py --label "C: batch=500" --window 10m

Medians, not means: a single GC pause or compaction spike should not move the
number that decides a configuration.
"""

import argparse
import json
import statistics

from influxdb import InfluxDBClient


def series_values(client, query, field_idx=1):
    try:
        rs = client.query(query)
    except Exception:                                    # noqa: BLE001
        return []
    out = []
    for point in rs.get_points():
        vals = [v for k, v in point.items() if k != "time" and v is not None]
        if vals:
            out.append(vals[field_idx - 1] if len(vals) >= field_idx else vals[0])
    return out


def med(xs, nd=1):
    return round(statistics.median(xs), nd) if xs else None


def to_seconds(s):
    """'10m' -> 600, '90s' -> 90, '1h' -> 3600."""
    s = s.strip().lower()
    mult = {"s": 1, "m": 60, "h": 3600}
    if s and s[-1] in mult:
        return int(float(s[:-1]) * mult[s[-1]])
    return int(float(s))


def main():
    ap = argparse.ArgumentParser(description="Summarize one benchmark cell")
    ap.add_argument("--host", default="localhost")
    ap.add_argument("--port", type=int, default=8086)
    ap.add_argument("--database", default="cps_hw1")
    ap.add_argument("--label", required=True, help="run label used by metrics_agent")
    ap.add_argument("--window", default="10m", help="total run length, e.g. 10m")
    ap.add_argument("--warmup", default="60s",
                    help="leading period to discard (methodology: 60s)")
    ap.add_argument("--json", action="store_true", help="emit JSON not a table")
    args = ap.parse_args()

    c = InfluxDBClient(host=args.host, port=args.port, database=args.database)

    # Steady state = the window with its warm-up HEAD removed, assuming the run
    # just ended. `time > now() - window AND time < now() - warmup` would trim
    # the wrong end -- discarding the most recent samples and keeping the
    # warm-up spike, which is precisely backwards.
    steady_s = to_seconds(args.window) - to_seconds(args.warmup)
    if steady_s <= 0:
        raise SystemExit(f"--warmup ({args.warmup}) must be shorter than "
                         f"--window ({args.window})")
    where = f"WHERE time > now() - {steady_s}s AND \"run\" = '{args.label}'"

    services = ["influxd", "mosquitto", "mysqld", "grafana", "chronyd",
                "forwarder", "agent"]
    per_service = {}
    for svc in services:
        cpu = series_values(c, f'SELECT "cpu_pct" FROM "hostmetrics" {where} '
                               f'AND "service" = \'{svc}\'')
        rss = series_values(c, f'SELECT "rss_mb" FROM "hostmetrics" {where} '
                               f'AND "service" = \'{svc}\'')
        if cpu or rss:
            per_service[svc] = {"cpu_pct": med(cpu, 2), "rss_mb": med(rss, 1)}

    stack_rss = series_values(c, f'SELECT "stack_rss_mb" FROM "hostmetrics" '
                                 f'{where} AND "service" = \'_system\'')
    sys_cpu = series_values(c, f'SELECT "cpu_pct" FROM "hostmetrics" '
                               f'{where} AND "service" = \'_system\'')
    disk = series_values(c, f'SELECT "influx_disk_mb" FROM "hostmetrics" '
                            f'{where} AND "service" = \'_system\'')

    # Pipeline health over the same window. No run tag here -- the forwarder
    # tags by device -- so this is whatever traffic was flowing.
    pwhere = f"WHERE time > now() - {steady_s}s"
    lat = series_values(c, f'SELECT "e2e_latency_ms" FROM "pipeline" {pwhere}')
    wr = series_values(c, f'SELECT "batch_write_ms" FROM "pipeline" {pwhere}')
    byts = series_values(c, f'SELECT "payload_bytes" FROM "pipeline" {pwhere}')
    gaps = series_values(c, f'SELECT "gap_count" FROM "pipeline" {pwhere}')
    pts = series_values(c, f'SELECT count("az") FROM "imu" {pwhere}')

    def pct(xs, q):
        if not xs:
            return None
        s = sorted(xs)
        return round(s[min(int(len(s) * q), len(s) - 1)], 1)

    result = {
        "label": args.label,
        "window": args.window,
        "warmup_discarded": args.warmup,
        "samples": len(stack_rss),
        "stack_rss_mb": med(stack_rss),
        "system_cpu_pct": med(sys_cpu, 2),
        "influx_disk_mb": med(disk),
        "points_in_window": int(pts[0]) if pts else 0,
        "e2e_latency_ms_p50": med(lat),
        "e2e_latency_ms_p95": pct(lat, 0.95),
        "batch_write_ms_p50": med(wr, 2),
        "batch_write_ms_p95": pct(wr, 0.95),
        "payload_bytes": med(byts, 0),
        "dropped_batches": int(sum(gaps)) if gaps else 0,
        "per_service": per_service,
    }

    if args.json:
        print(json.dumps(result, indent=2))
        return

    if not result["samples"]:
        print(f"No hostmetrics tagged run='{args.label}' in the last {args.window}.")
        print("Was metrics_agent.py running with the same --label?")
        return

    print(f"\n  {args.label}   ({result['samples']} samples, "
          f"{args.warmup} warm-up discarded)")
    print("  " + "-" * 52)
    print(f"  {'service':<12} {'CPU %':>9} {'RSS MB':>9}")
    for svc, v in sorted(per_service.items(),
                         key=lambda kv: -(kv[1]["rss_mb"] or 0)):
        print(f"  {svc:<12} {v['cpu_pct']:>9} {v['rss_mb']:>9}")
    print("  " + "-" * 52)
    print(f"  {'TOTAL RSS':<12} {'':>9} {result['stack_rss_mb']:>9}")
    print(f"\n  system CPU      {result['system_cpu_pct']} %")
    print(f"  influx on disk  {result['influx_disk_mb']} MB")
    print(f"  points ingested {result['points_in_window']}")
    print(f"  e2e latency     p50 {result['e2e_latency_ms_p50']} ms   "
          f"p95 {result['e2e_latency_ms_p95']} ms")
    print(f"  influx write    p50 {result['batch_write_ms_p50']} ms   "
          f"p95 {result['batch_write_ms_p95']} ms")
    print(f"  payload         {result['payload_bytes']} bytes")
    print(f"  dropped batches {result['dropped_batches']}\n")


if __name__ == "__main__":
    main()
