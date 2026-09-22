# Architecture

Data flow: `M5StickC PLUS → Mosquitto → forwarder → InfluxDB → Grafana`, with chrony keeping every
clock on one timebase and MySQL holding the mutable metadata.

---

## 1. Design goals

| Goal | How it is met | Section |
|---|---|---|
| Real sensor, real server | M5StickC PLUS (ESP32 + MPU6886 IMU + PDM mic) → Ubuntu 22.04 server | §2 |
| Full, self-hosted stack | chrony, Mosquitto, InfluxDB 1.8.10, MySQL 8.0, Grafana 7.5.7 | §4 |
| Decoupled ingest | Sensor publishes over MQTT; a dedicated forwarder writes to InfluxDB | §5 |
| One shared clock | Server syncs to public NTP; sensor syncs to the server over SNTP | §6 |
| Observable pipeline | Forwarder measures its own latency, skew and dropped batches | §5.5 |
| Minimum CPU and RAM | Versions and settings compared with a repeatable benchmark | §7 |
| Hardware-independent development | A simulator speaks the exact same data contract as the firmware | §5, §9 |

Two of these get first-class treatment: the pipeline **measures its own clock skew and end-to-end
latency** and writes them back into InfluxDB as a `pipeline` measurement, so time-sync quality is a
Grafana panel rather than a claim.

---

## 2. Physical topology

```
┌──────────────────────── Local network (2.4 GHz WiFi) ──────────────────────┐
│                                                                            │
│   ┌───────────────┐         WiFi 2.4 GHz          ┌──────────────────────┐ │
│   │ M5StickC PLUS │ ─────── MQTT/TCP 1883 ──────▶ │  Ubuntu 22.04 VM     │ │
│   │ ESP32-PICO-D4 │ ◀────── SNTP/UDP 123 ──────── │  (bridged, LAN IP)   │ │
│   │ MPU6886 IMU   │                                │  on a MacBook (M4)   │ │
│   └───────────────┘                                └──────────┬───────────┘ │
│                                                               │             │
│                                          Grafana :3000 ◀──────┘             │
└────────────────────────────────────────────────────────────────────────────┘
                                                    │
                                                    ▼  NTP/UDP 123
                                          2.ubuntu.pool.ntp.org
```

### 2.1 Sensor device — M5StickC PLUS (SKU K016-P)

| Property | Value |
|---|---|
| MCU | ESP32-PICO-D4, dual-core 240 MHz, 4 MB flash, 520 KB SRAM |
| Primary sensor | MPU6886 6-axis IMU (accel ±2/4/8/16 g, gyro ±250…2000 dps), I²C `0x68` |
| Secondary | SPM1423 PDM microphone; **AXP192 PMU** (battery V/I); BM8563 RTC; buzzer `GPIO2` |
| Display | 1.14" ST7789v2, 135×240 — live status (WiFi, seq, sent count) |
| Buttons | A `GPIO37`, B `GPIO39` |
| Expansion | Grove I²C (SDA `GPIO32`, SCL `GPIO33`) — optional ENV III (SHT30 + QMP6988) |
| Radio | 802.11 b/g/n, **2.4 GHz only** |
| Battery | 120 mAh LiPo — ≈40–60 min at 50 Hz + WiFi. Run on USB power for long sessions. |

> **Not the PLUS2.** The PLUS2 (a visually near-identical board) removes the AXP192, moves to 8 MB
> flash, and requires driving `GPIO4` high as a power latch or it shuts off on battery. None of that
> applies to the PLUS. `M5Unified` probes the board at runtime, so the firmware source is the same
> either way, but the battery API and the latch requirement are not.

Firmware footprint: **RAM 17.2%, flash 70.8%** of the app partition.

The BM8563 RTC carries the SNTP-disciplined epoch across deep sleep, so the device does not have to
re-acquire time on every wake.

### 2.2 Server — Ubuntu 22.04 LTS VM

The host is an Apple M4 MacBook, so the guest is **arm64**.

**Provisioning — Multipass, bridged:**

```bash
brew install --cask multipass
multipass launch 22.04 --name cps --cpus 4 --memory 4G --disk 32G --network name=en0,mode=auto
```

`--network name=en0,mode=auto` is the load-bearing flag. Multipass' default NAT network puts the VM
on a host-private subnet the ESP32 cannot route to. Bridged mode gives the VM its own DHCP lease on
the LAN, which is what makes `M5StickC → broker` possible at all.

4 vCPU / 4 GB is the development allocation. The measured steady state (§7) fits comfortably in
**2 vCPU / 2 GB**.

> **Why not Docker?** Containers inherit the host clock, so `chrony` inside a container cannot
> discipline anything, and the Docker Desktop VM adds a layer that muddies CPU/RAM measurements. A
> Docker sandbox exists in `sandbox/` for learning the tools, but the real stack is the VM.

---

## 3. Logical architecture

| Stage | Component | Responsibility |
|---|---|---|
| Edge | M5StickC firmware | Sample IMU + mic at a fixed rate, batch, timestamp, publish |
| Transport | Mosquitto | Topic routing, QoS 1 durability, LWT liveness |
| Ingest | `forwarder.py` | Validate → expand batch → InfluxDB line protocol → measure |
| Store | InfluxDB 1.8.10 | Time-series retention, downsampling |
| Store | MySQL 8.0 | Device registry, calibration, run labels, Grafana backend |
| Present | Grafana 7.5.7 | Live dashboard, provisioned as code |

**Why a custom forwarder instead of Telegraf:** owning the forwarder is what makes the pipeline
observable. It is the only component that sees both the sensor's timestamp and the server's, so it
is the natural place to compute clock skew and end-to-end latency. A generic MQTT→Influx plugin
would hide exactly those numbers.

**Why both InfluxDB and MySQL:** they answer different questions. InfluxDB stores the append-only,
time-indexed waveform. MySQL stores the small, mutable, relational facts — which device is deployed
where, its IMU calibration offsets, which benchmark run a time window belongs to — plus Grafana's own
config store (moved off its default SQLite).

---

## 4. Component inventory (pinned)

| Component | Pinned version | Source | Rationale |
|---|---|---|---|
| Ubuntu | 22.04.5 LTS (jammy) | Multipass image | Long-term support |
| chrony | 4.2 (jammy) | `apt` | **Replaces `systemd-timesyncd`** — see §6 |
| Mosquitto | 2.0.11 (jammy) | `apt` | MQTT 3.1.1 + 5.0, ~5 MB RSS |
| InfluxDB | **1.8.10** | InfluxData `.deb` | Final 1.8 patch release; InfluxQL |
| MySQL | 8.0.x (jammy) | `apt` | Grafana backend + registry |
| Grafana | **7.5.7** | Grafana `.deb` | Native InfluxQL query editor |
| Python | 3.10.12 | jammy default | Forwarder, simulator, benchmark |
| paho-mqtt | **1.6.1** | `pip` | 2.x changed the callback signature — see below |
| influxdb (py) | 5.3.1 | `pip` | InfluxDB 1.x client |

### Version decisions that carry risk

**InfluxDB 1.8.10 over 2.7.** 1.8 speaks InfluxQL, which Grafana 7.5.7's datasource editor supports
natively; 2.x pushes Flux, which 7.5.7 handles poorly, and runs a noticeably larger resident set.
1.8.10 is the last patch of the 1.8 line. 2.7 is included in the benchmark matrix (factor A, §7) so
the choice can be measured rather than asserted.

**Grafana 7.5.7 is end-of-life** and carries known published vulnerabilities. Keep port 3000 on the
LAN. Upgrading is on the hardening list in §10.

**paho-mqtt pinned to 1.6.1.** paho-mqtt 2.x requires `CallbackAPIVersion` and changed `on_connect`'s
arguments. An unpinned `pip install paho-mqtt` silently pulls 2.x and fails at runtime, not at
install time.

**arm64.** Both `influxdb_1.8.10_arm64.deb` and `grafana_7.5.7_arm64.deb` exist. Never fall back to
an emulated x86_64 VM — it would invalidate every CPU measurement in §7.

### Memory footprint

| Service | Stock default | Measured (§7 baseline) | Lever |
|---|---:|---:|---|
| chronyd | ~2 MB | 4.6 MB | — |
| mosquitto | ~5 MB | 6.7 MB | — |
| influxd | ~200 MB | 286.8 MB | `cache-max-memory-size`, `index-version=tsi1` |
| mysqld | ~400 MB | 185.7 MB | `innodb_buffer_pool_size=64M`, `performance_schema=OFF` |
| grafana | ~80 MB | 63.0 MB | `min_refresh_interval` |
| forwarder | ~25 MB | 27.6 MB | batch size |

InfluxDB's figure is under a load ~30× a single board. The MySQL tuning is the single largest easy
win: 185 MB against a ~400 MB stock default.

---

## 5. Data contract

This is the interface every component agrees on. Changing it means changing firmware, forwarder,
schema, and dashboards together — so it is fixed here.

### 5.1 Identity

`device_id = "m5-" + last 6 hex of WiFi MAC` → e.g. `m5-67492c`. Stable across reflashes, needs no
provisioning server.

### 5.2 Topics

```
cps/hw1/<device_id>/imu       # 6-axis waveform batches, 1 msg/s   QoS 1
cps/hw1/<device_id>/audio     # mic loudness batches,    1 msg/s   QoS 1
cps/hw1/<device_id>/health    # device vitals, 1 msg/10s           QoS 1
cps/hw1/<device_id>/status    # "online" / "offline", retained     QoS 1  (LWT)
```

The `status` topic is an MQTT **Last Will and Testament**: the broker publishes `offline` on an
ungraceful disconnect, so Grafana gets a truthful liveness panel without polling.

### 5.3 Payload — batched waveform

Sampling at 50 Hz, batched 50 samples (1 s) per message, using a `(values[], start_ts, freq)`
layout:

```json
{
  "device_id": "m5-67492c",
  "stream": "imu",
  "start_ts": 1758499200.004,
  "freq": 50,
  "n": 50,
  "seq": 10412,
  "fw": "hw1-0.2.0",
  "units": { "ax": "g", "gx": "dps" },
  "values": {
    "ax": [0.012, 0.014, "…48 more"],
    "ay": ["…"], "az": ["…"],
    "gx": ["…"], "gy": ["…"], "gz": ["…"]
  },
  "meta": { "rssi": -54, "vbat": 3.98, "clock_src": "sntp", "sntp_age_s": 312 }
}
```

**Why batch.** One message per sample at 50 Hz costs 50 publishes/s and ~50× the MQTT+TCP header
overhead; batching one second per message is ~3 KB at 1 msg/s (measured). `seq` is monotonic per
device, which lets the forwarder detect gaps — a dropped batch becomes a countable number, not an
invisible hole.

**Why the microphone is reduced on the device.** Raw audio at 16 kHz is 320× the IMU rate. The
firmware reduces each 20 ms block (320 samples) to RMS loudness and peak, so the audio stream lands
at the same 50 Hz as the IMU and shares its timebase.

### 5.4 Forwarder → InfluxDB

Each batch expands to `n` points; sample *i* is timestamped `start_ts + i/freq` at nanosecond
precision:

```
imu,device_id=m5-67492c,site=apt,fw=hw1-0.2.0 ax=0.012,ay=-0.004,az=0.998,gx=0.21,gy=-0.07,gz=0.02 1758499200004000000
```

Tags (indexed, low cardinality): `device_id`, `site`, `fw`. Fields (unindexed): the six axes.
Keeping `seq` a *field* rather than a tag is deliberate — as a tag it would create unbounded series
cardinality and blow up the index.

### 5.5 Self-instrumentation

The forwarder additionally writes one `pipeline` point per batch:

| Field | Meaning |
|---|---|
| `e2e_latency_ms` | forwarder wall clock − `start_ts` — full sensor-to-store delay (see note) |
| `skew_ms` | device's own SNTP offset, reported in `meta` |
| `sntp_age_s` | seconds since the device last resynced |
| `gap_count` | `seq` discontinuities since last point |
| `batch_write_ms` | InfluxDB write duration |
| `payload_bytes` | wire size |

> **Reading `e2e_latency_ms` correctly.** It baselines at **~1000 ms**, not because the pipeline is
> slow but because `start_ts` marks the *first* sample of a batch and the batch cannot publish until
> all 50 samples have occurred one second later. The batching window is the floor; only the excess
> above it is transport and write cost. Measured: ~1007 ms total, so ~7 ms is real. Reducing the
> batch to 10 samples drops the floor to ~200 ms (factor G in §7).

### 5.6 Retention

```sql
CREATE DATABASE cps_hw1;
CREATE RETENTION POLICY rp_7d  ON cps_hw1 DURATION 7d  REPLICATION 1 SHARD DURATION 1h DEFAULT;
CREATE RETENTION POLICY rp_90d ON cps_hw1 DURATION 90d REPLICATION 1 SHARD DURATION 1d;
```

A continuous query downsamples 50 Hz → 1 Hz means into `rp_90d`, so long-range dashboard panels do
not scan raw waveform. Short shard duration on the hot policy keeps eviction cheap.

---

## 6. Time synchronization

Three clock domains. Each is separately verifiable, and the third is continuously graphed.

### 6.1 World → server

Ubuntu 22.04 ships `systemd-timesyncd`, an **SNTP client only — it cannot serve time to other hosts**.
It also holds UDP/123, so it must be removed before chrony can bind:

```bash
sudo systemctl disable --now systemd-timesyncd
sudo apt install -y chrony
```

`/etc/chrony/chrony.conf`:

```conf
pool 2.ubuntu.pool.ntp.org iburst maxsources 4
makestep 1.0 -1              # step at any time, not just first 3 updates — see §6.4
rtcsync
allow <LAN subnet>           # serve the sensor's network (§6.2); written by 01_chrony.sh
local stratum 10             # keep serving if the WAN drops
```

Verify: `chronyc tracking` → stratum ≤ 3, `System time` offset within a few ms.

### 6.2 Server → sensor

`allow` turns chrony into an NTP **server** for the LAN. The M5StickC points its SNTP client at the
VM's bridged LAN IP:

```c
esp_sntp_setoperatingmode(SNTP_OPMODE_POLL);
esp_sntp_setservername(0, CPS_SERVER_IP);   // the VM's bridged LAN address
esp_sntp_init();
sntp_set_sync_interval(15 * 60 * 1000);     // resync every 15 min
```

The `allow` rule is written for the subnet the VM is on *at install time*. If the network changes,
re-run `01_chrony.sh` — `verify.sh` checks for exactly this.

### 6.3 Sampling clock vs. epoch clock

SNTP disciplines the *epoch*; it must not discipline the *sample interval*. The firmware samples off
the ESP32's microsecond timer at a fixed 20 ms period and calls `gettimeofday()` once per batch to
stamp `start_ts`. Per-sample times are then derived as `start_ts + i/freq`. Stamping every sample
from wall clock would let an SNTP step reorder or duplicate samples inside a batch.

### 6.4 The VM clock caveat

When the MacBook sleeps, the guest clock falls behind and jumps on resume (observed: ~11.5 hours).
**Two clock masters produce meaningless measurements.** Disable the hypervisor's guest time sync so
chrony is the sole authority, and set `makestep 1.0 -1` so chrony can step at any time rather than
only during its first three updates. Expect a visible step in the `skew_ms` panel after every laptop
resume — that is evidence the sync works, not a defect. Restart the forwarder after a large step.

---

## 7. Performance evaluation

**Method.** `bench/loadgen.py` replays a synthetic IMU stream at a controlled, reproducible rate, so
runs do not depend on physically shaking the device. `bench/metrics_agent.py` (psutil) samples
per-service CPU% and RSS at 1 Hz and writes them into InfluxDB as measurement `hostmetrics` — the
system benchmarks itself, and the results are a Grafana dashboard. Each cell: 10 min steady state,
first 60 s discarded, 3 repetitions, median reported. Details in [bench/README.md](../bench/README.md).

**Dependent variables:** per-service RSS and CPU%; sustained ingest (msgs/s, points/s); end-to-end
latency p50/p95 (from `pipeline`); disk bytes per 1M points; Grafana panel render time.

| # | Factor | Levels |
|---|---|---|
| A | InfluxDB major version | 1.8.10 · 2.7.x |
| B | InfluxDB tuning | `index-version` tsi1/inmem · `cache-max-memory-size` 1G/128M · `wal-fsync-delay` 0/100ms |
| C | Forwarder write batching | 1 · 50 · 500 · 5000 points per write |
| D | Mosquitto | QoS 0/1 · `persistence` on/off · `max_queued_messages` |
| E | MySQL | default vs `innodb_buffer_pool_size=64M` + `performance_schema=OFF` |
| F | Grafana | 7.5.7 vs 10.x · SQLite vs MySQL backend · refresh 1s/5s/30s |
| G | Payload encoding | JSON vs base64 float32 · batch 10/50/250 samples |

One factor at a time, then one combined run at the tuned settings to confirm the wins compose.

**Baseline** (30 msg/s across 6 devices, 1489 points/s, JSON, QoS 1, 4 vCPU / 4 GB):

| Service | CPU % (median) | RSS MB |
|---|---:|---:|
| influxd | 11.75 | 286.8 |
| mysqld | 1.0 | 185.7 |
| grafana | 0.0 | 63.0 |
| forwarder | 2.9 | 27.6 |
| mosquitto | 0.0 | 6.7 |
| chronyd | 0.0 | 4.6 |

InfluxDB write p50 11.3 ms / p95 20.3 ms, 0 dropped batches. InfluxDB is 48% of resident memory and
essentially all the CPU, so that is where tuning effort belongs.

### Sample-rate headroom

The firmware runs at 50 Hz. Estimated limits, tightest first:

1. **Payload buffer** (~110 samples/message at ~60 B/sample JSON) — just above 100 Hz.
2. **Publishing blocks sampling** — the QoS 1 publish waits for PUBACK inside the sampling loop.
   Moving sampling to its own FreeRTOS task or the IMU's hardware FIFO removes this.
3. **Mic alignment** — `MIC_BLOCK = 16000 / SAMPLE_HZ` must be adjusted with the rate.
4. **I²C polling** of the IMU — roughly 1–2 kHz.

The server side is not the bottleneck at these rates.

---

## 8. Grafana dashboards

**`CPS IoT — Live`** (`/d/cps-hw1-live`), five panels:

| Panel | Source |
|---|---|
| IMU acceleration (g) | `imu` — `ax`, `ay`, `az` |
| Gyroscope (dps) | `imu` — `gx`, `gy`, `gz` |
| Pipeline: end-to-end latency and clock skew (ms) | `pipeline` |
| Ingest rate (points/min) and dropped batches | `imu`, `pipeline.gap_count` |
| Microphone — SPM1423 loudness (dBFS) | `audio` |

The dashboard and the data source are provisioned from files (`server/grafana/`) rather than clicked
together in the UI, so the configuration is reproducible and reviewable in the repo. Benchmark runs
write `hostmetrics` into the same database, ready for a per-service CPU/RAM panel.

---

## 9. Extensibility

- **Simulator-first.** `sim/sensor_sim.py` speaks the same data contract as the firmware, so the
  whole server pipeline can be developed and tested with no hardware. Swapping in the real board
  required **no forwarder change** — the proof that the contract in §5 holds.
- **New sensors.** The forwarder takes the measurement name from the topic suffix, so a new stream is
  a new topic, not a forwarder change.
- **Stream processing / ML.** The forwarder subscribes to a topic **pattern** (`cps/hw1/+/imu`), so an
  analysis process can subscribe to the same live stream without touching the transport. Measurement
  names `ai_*` and the tag convention `model=<name>,version=<semver>` are reserved for it.

---

## 10. Known gotchas and security

| # | Gotcha | Mitigation |
|---|---|---|
| 1 | Campus WiFi uses WPA2-Enterprise or client isolation; ESP32 can't join or can't reach the VM | Use a phone hotspot or travel router |
| 2 | ESP32 is **2.4 GHz only**; a 5 GHz-only SSID is invisible to it | iPhone: enable **Maximize Compatibility** |
| 3 | iPhone hotspot names use a curly apostrophe (`’`) | Copy the SSID exactly into `config.h` |
| 4 | Multipass default NAT makes the VM unreachable from the sensor | Bridged: `--network name=en0,mode=auto` |
| 5 | VM keeps its old DHCP lease after the Mac changes networks | `sudo networkctl reconfigure enp0s2` in the VM |
| 6 | chrony's `allow` subnet is fixed at install time | Re-run `01_chrony.sh` after a network change |
| 7 | Mosquitto 2.0 defaults to `127.0.0.1` and denies anonymous | Explicit `listener 1883 0.0.0.0` + `allow_anonymous true` |
| 8 | `systemd-timesyncd` holds UDP/123 and cannot serve time | Disable it before installing chrony (§6.1) |
| 9 | `pip install paho-mqtt` pulls 2.x and breaks callbacks at runtime | Pin `paho-mqtt==1.6.1` |
| 10 | Laptop sleep steps the guest clock | Single clock master + `makestep 1.0 -1` (§6.4) |

**Security posture.** The stack is deliberately open for LAN development — anonymous MQTT, no TLS,
default Grafana credentials — and must stay on a trusted network. Hardening path: per-device MQTT
credentials and ACLs, TLS with a private CA, InfluxDB authentication, a supported Grafana release
with changed credentials, `ufw`, and signed OTA firmware updates.

---

## 11. Repository layout

```
.
├── docs/
│   ├── ARCHITECTURE.md          # this document
│   ├── HARDWARE-SETUP.md        # flashing and connecting the M5StickC PLUS
│   └── INFLUX-GRAFANA.md        # primer for InfluxDB and Grafana
├── firmware/m5stickc/           # ESP32 acquisition firmware (PlatformIO)
├── sim/sensor_sim.py            # simulated sensor, same data contract
├── forwarder/forwarder.py       # MQTT → InfluxDB, self-instrumenting
├── server/
│   ├── setup/                   # provisioning scripts 01–06 + verify.sh
│   ├── mysql/schema.sql         # device registry
│   └── grafana/dashboards/      # dashboards as code
├── bench/                       # load generator, metrics agent, summarizer
└── sandbox/                     # Docker sandbox for learning (not the real stack)
```
