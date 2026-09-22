# Benchmark harness

Compares versions and settings of InfluxDB, Mosquitto, MySQL and Grafana to find
the best performance at the lowest CPU and RAM cost.

Three tools, each doing one job:

| Tool | Runs on | Job |
|---|---|---|
| `loadgen.py` | **the Mac** | Push reproducible traffic at a controlled rate |
| `metrics_agent.py` | **the VM**, under `sudo` | Sample per-service CPU/RSS into InfluxDB |
| `summarize.py` | the VM | Turn one run into a result row |

**Run the load generator from the Mac, not the VM.** The measurement is the
server's CPU and RAM; a generator sharing those four cores contaminates exactly
the numbers being collected.

**Run the metrics agent under `sudo`.** `/var/lib/influxdb` is root-owned, and
without it the disk-growth figure is omitted (the agent says so on startup
rather than silently reporting zero).

---

## Methodology

Each cell: **10 minutes**, first **60 seconds discarded** as warm-up, **3
repetitions**, **median** reported. Medians rather than means — one compaction
spike should not decide a configuration.

`summarize.py` applies the warm-up trim for you. Give it the *same* `--window`
as the run length and it keeps the tail:

```
--window 600s --warmup 60s   ->  the last 540s of the run
```

---

## Running one cell

Terminal 1, on the VM:

```bash
multipass shell cps
sudo ~/.cps-venv/bin/python ~/cps/bench/metrics_agent.py --label "C: batch=500" --duration 600
```

Terminal 2, on the Mac:

```bash
python3 bench/loadgen.py --host <vm-ip> --rate 30 --devices 6 --duration 600 --label "C: batch=500"
```

Then, on the VM:

```bash
~/.cps-venv/bin/python ~/cps/bench/summarize.py --label "C: batch=500" --window 600s
```

> **Use `multipass shell`, not `multipass exec`, for labels with spaces.**
> `multipass exec` re-splits quoted arguments, so `--label "C: batch=500"`
> arrives as three separate arguments and argparse rejects it. Either open a
> shell, or use single-word labels with `exec`.

---

## The factor matrix

Change one thing per cell, re-measure, then one combined run at the tuned
settings to confirm the wins compose.

| # | Factor | How to vary it |
|---|---|---|
| A | InfluxDB version | Install 2.7 beside 1.8 (`sandbox/` compose), repoint the forwarder |
| B | InfluxDB tuning | `/etc/influxdb/influxdb.conf`: `index-version`, `cache-max-memory-size`, `wal-fsync-delay` |
| C | Forwarder batching | `forwarder.py --batch-points 1 / 50 / 500 / 5000` |
| D | Mosquitto | `loadgen.py --qos 0` vs `--qos 1`; `persistence` on/off in `conf.d/cps.conf` |
| E | MySQL | Comment out `99-cps.cnf` to compare against stock defaults |
| F | Grafana | 7.5.7 vs 10.x; open the dashboard at 1s/5s/30s refresh during a run |
| G | Payload encoding | `loadgen.py --encoding json` vs `b64f32`; `--batch 10 / 50 / 250` |

Factor G needs no forwarder change — it already decodes `b64f32` when the
payload carries `"enc": "b64f32"`.

---

## Measured baseline

30 msg/s across 6 devices, 1489 points/s, JSON, QoS 1, 4 vCPU / 4 GB VM:

| Service | CPU % (median) | RSS MB |
|---|---:|---:|
| influxd | 11.75 | 286.8 |
| mysqld | 1.0 | 185.7 |
| grafana | 0.0 | 63.0 |
| forwarder | 2.9 | 27.6 |
| agent | 0.0 | 27.4 |
| mosquitto | 0.0 | 6.7 |
| chronyd | 0.0 | 4.6 |
| **total** | **4.45** (system) | **601.9** |

Influx write p50 11.3 ms / p95 20.3 ms, payload 3018 bytes, 0 dropped batches.

Two things stand out:

- **influxd is the whole story.** It is 48% of resident memory and essentially
  all the CPU; every other service is rounding error. Tuning effort belongs in
  factor B, and the `performance_schema=OFF` win on MySQL is already banked
  (185 MB against a ~400 MB default).
- **Median 11.75% CPU, peaks near 50%.** The peaks are compaction. This is why
  the methodology says median over steady state — a mean would be hostage to
  when a shard happened to roll over.

Note this baseline is ~1489 points/s, roughly **30x** a single real M5StickC at
50 Hz. One board will barely register; the load generator is what makes the
differences between configurations visible at all.
