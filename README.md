# IoT Sensing System with M5StickC PLUS

An end-to-end, self-hosted IoT pipeline: an **M5StickC PLUS** streams 6-axis motion and microphone
loudness at 50 Hz over MQTT to an Ubuntu server, where a custom forwarder stores it in **InfluxDB**
and **Grafana** shows it live. Every clock in the system is synced to one source, and the pipeline
measures its own latency, clock skew and dropped data.

```
M5StickC PLUS ──MQTT──▶ Mosquitto ──▶ forwarder.py ──▶ InfluxDB 1.8 ──▶ Grafana 7.5
 (or sensor_sim.py)                         │
                                            └──▶ pipeline metrics: latency, skew, dropped batches
        ▲
        └──SNTP── chrony (on the server) ──NTP──▶ pool.ntp.org

MySQL 8.0: device registry + Grafana's config store
```

## Highlights

- **Real hardware, real firmware.** ESP32 firmware (PlatformIO) samples the MPU6886 IMU at 50 Hz on a
  hardware timer and reduces 16 kHz microphone audio to loudness *on the device*.
- **Batched, reliable transport.** One-second batches over MQTT QoS 1, with sequence numbers for gap
  detection and a Last-Will `online`/`offline` status topic.
- **One shared clock.** The server syncs to public NTP with chrony and serves time to the sensor over
  SNTP, so timestamps from the edge and the server agree.
- **Self-instrumenting pipeline.** The forwarder records end-to-end latency, sensor clock skew,
  dropped batches and write time into InfluxDB, and they show up as Grafana panels.
- **Hardware-optional.** A simulator speaks the exact same data contract, so the whole server side
  runs without a board. Swapping in the real device needed no forwarder change.
- **Infrastructure as code.** Numbered provisioning scripts, a provisioned Grafana dashboard, and a
  13-point `verify.sh` health check.
- **Benchmarked.** A load generator and metrics agent compare versions and settings for CPU/RAM cost.

## Tech stack

| Layer | Technology |
|---|---|
| Sensor | M5StickC PLUS (ESP32-PICO-D4, MPU6886 IMU, SPM1423 mic), C++ / Arduino / PlatformIO |
| Transport | Eclipse Mosquitto 2.0 (MQTT, QoS 1, LWT) |
| Ingest | Python 3.10 forwarder (paho-mqtt 1.6.1) |
| Storage | InfluxDB 1.8.10 (time series), MySQL 8.0 (registry + Grafana backend) |
| Visualization | Grafana 7.5.7 |
| Time sync | chrony (NTP server) ↔ ESP32 SNTP client |
| Server | Ubuntu 22.04 LTS on a bridged Multipass VM |

## Repository layout

```
firmware/m5stickc/   ESP32 firmware (PlatformIO)
sim/                 simulated sensor, same data contract as the firmware
forwarder/           MQTT → InfluxDB forwarder with pipeline self-measurement
server/setup/        provisioning scripts 01–06 and verify.sh
server/grafana/      dashboard as code
server/mysql/        device registry schema
bench/               load generator, metrics agent, results summarizer
sandbox/             Docker sandbox for learning InfluxDB/Grafana (not the real stack)
docs/                architecture, hardware setup, InfluxDB/Grafana primer
```

## Documentation

- **[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)** — design, pinned versions, data contract, time
  sync, performance results.
- **[docs/HARDWARE-SETUP.md](docs/HARDWARE-SETUP.md)** — flashing and connecting the M5StickC PLUS.
- **[docs/INFLUX-GRAFANA.md](docs/INFLUX-GRAFANA.md)** — InfluxDB and Grafana from zero, using this
  project's data.
- **[bench/README.md](bench/README.md)** — benchmark methodology and baseline results.

---

## Setup

### 1. Create the server VM

```bash
brew install --cask multipass
multipass launch 22.04 --name cps --cpus 4 --memory 4G --disk 32G --network name=en0,mode=auto
multipass mount /path/to/this/repo cps:/home/ubuntu/cps
```

The `--network` flag is required: Multipass' default NAT network is unreachable from the ESP32.

### 2. Provision the services

On the VM, in order. Each script verifies itself and stops on failure:

```bash
multipass shell cps
bash ~/cps/server/setup/01_chrony.sh     # first: nothing else is meaningful with a wrong clock
bash ~/cps/server/setup/02_mosquitto.sh
bash ~/cps/server/setup/03_influxdb.sh
bash ~/cps/server/setup/04_grafana.sh
bash ~/cps/server/setup/05_pipeline.sh   # Python venv + forwarder/simulator as systemd services
bash ~/cps/server/setup/06_mysql.sh
```

### 3. Verify

```bash
multipass exec cps -- bash /home/ubuntu/cps/server/setup/verify.sh
```

It prints the dashboard URL, the MQTT broker address and the NTP address. Open the dashboard and log
in with `admin` / `admin`. The simulator cycles still → walking → shaking on a 40-second loop, so the
panels have data right away.

### 4. Connect the M5StickC PLUS

See **[docs/HARDWARE-SETUP.md](docs/HARDWARE-SETUP.md)**. In short:

```bash
cd firmware/m5stickc
cp include/config.h.example include/config.h   # set WiFi SSID/password and the VM's IP
pio run -t upload && pio device monitor
```

---

## Daily use

Every service is `systemd`-enabled, so the whole stack starts with the VM:

```bash
multipass start cps          # start everything
multipass stop cps           # stop everything (all ports close, data is kept)
```

```bash
# Health check
multipass exec cps -- bash /home/ubuntu/cps/server/setup/verify.sh

# Follow the forwarder
multipass exec cps -- journalctl -fu cps-forwarder

# Watch raw MQTT traffic
multipass exec cps -- mosquitto_sub -h localhost -t 'cps/hw1/#' -v

# Query InfluxDB
multipass exec cps -- influx -database cps_hw1

# Simulator off (before using the real board) / on
multipass exec cps -- sudo systemctl stop  cps-sensor-sim
multipass exec cps -- sudo systemctl start cps-sensor-sim
```

The VM has two addresses:

| Address | Use |
|---|---|
| `192.168.252.2` | Host-only. Always reachable from the Mac, with or without WiFi — use it to view Grafana. |
| Bridged LAN IP (printed by `verify.sh`) | What the board connects to. **Changes with the network.** |

### After changing networks or waking the laptop

The bridged IP is a DHCP lease and moves with the network, and the VM clock can jump after sleep.
If `verify.sh` fails:

```bash
multipass exec cps -- sudo networkctl reconfigure enp0s2          # get a lease on the new network
multipass exec cps -- bash /home/ubuntu/cps/server/setup/01_chrony.sh   # serve NTP on the new subnet
multipass exec cps -- sudo systemctl restart cps-forwarder         # recover from a clock jump
```

If the bridged IP changed, update `CPS_SERVER_IP` in `firmware/m5stickc/include/config.h` and
re-flash — it is compiled into the firmware.

---

## Performance

Baseline at 1489 points/s (≈30× one board), 4 vCPU / 4 GB VM:

| Service | CPU % (median) | RAM (MB) |
|---|---:|---:|
| InfluxDB | 11.75 | 286.8 |
| MySQL (tuned) | 1.0 | 185.7 |
| Grafana | 0.0 | 63.0 |
| Forwarder | 2.9 | 27.6 |
| Mosquitto | 0.0 | 6.7 |
| chrony | 0.0 | 4.6 |

InfluxDB write latency p50 11.3 ms / p95 20.3 ms, zero dropped batches. End-to-end transport and
write overhead is ~7 ms on top of the one-second batching window. See
[docs/ARCHITECTURE.md §7](docs/ARCHITECTURE.md#7-performance-evaluation).

## Security

This stack is built for a trusted LAN: anonymous MQTT, no TLS, and default Grafana credentials.
Don't expose it to the internet as is. The hardening path (MQTT credentials and ACLs, TLS, InfluxDB
auth, a current Grafana release) is outlined in
[docs/ARCHITECTURE.md §10](docs/ARCHITECTURE.md#10-known-gotchas-and-security).

## Roadmap

- Real-time stream processing / anomaly detection subscribing to the live MQTT stream
- Security hardening: TLS, per-device credentials, signed OTA updates
- Higher sample rates via a dedicated FreeRTOS sampling task and the IMU hardware FIFO
