# Hardware setup — connecting the M5StickC PLUS

From a powered-off board to live IMU data in Grafana. About 15 minutes once the
server is provisioned.

---

## Before you start

| | Check |
|---|---|
| ☐ | A **2.4 GHz** network and its password. The ESP32 cannot see 5 GHz-only networks. |
| ☐ | That network does **not** use WPA2-Enterprise or client isolation. Campus networks often do — use a phone hotspot instead. |
| ☐ | The Mac, the VM and the board are all on **the same** network. |
| ☐ | A USB-C cable that carries **data**, not just power. |
| ☐ | PlatformIO: `pipx install platformio`, or run it ad hoc with `uvx --from platformio pio`. |

### Using an iPhone hotspot

- Turn on **Settings → Personal Hotspot → Maximize Compatibility**. Newer iPhones
  default to 5 GHz, which the ESP32 cannot see.
- iPhone names use a **curly apostrophe**: `GC’s iPhone`, not `GC's iPhone`. The
  SSID in `config.h` must match byte-for-byte. Check it with:
  ```bash
  networksetup -listpreferredwirelessnetworks en0
  ```
- Toggling Maximize Compatibility restarts the hotspot, and the Mac may quietly
  rejoin another saved network. Confirm the Mac's router is `172.20.10.1`:
  ```bash
  ipconfig getsummary en0 | grep ' Router :'
  ```

---

## 1 · Bring the server up on the right network

```bash
multipass start cps
multipass exec cps -- ip -4 -brief addr show enp0s2
```

If that address is not on the same subnet as the Mac (`ipconfig getifaddr en0`),
the VM is still holding a lease from the previous network. Renew it:

```bash
multipass exec cps -- sudo networkctl reconfigure enp0s2
```

Then point chrony at the new subnet and restart the forwarder (the VM clock may
have jumped while the laptop slept):

```bash
multipass exec cps -- bash /home/ubuntu/cps/server/setup/01_chrony.sh
multipass exec cps -- sudo systemctl restart cps-forwarder
```

Stop the simulator so it doesn't mix with real data:

```bash
multipass exec cps -- sudo systemctl stop cps-sensor-sim
```

---

## 2 · Configure and flash

```bash
cd firmware/m5stickc
cp include/config.h.example include/config.h   # first time only
```

Edit `include/config.h`: SSID, password, and `CPS_SERVER_IP` set to the VM's
bridged address from step 1. `config.h` is gitignored.

Plug the board in and confirm it enumerates:

```bash
ls /dev/cu.usbserial-*
```

Nothing there means a charge-only cable or a missing CH9102 driver. Then:

```bash
pio run -t upload && pio device monitor
```

A healthy boot looks like:

```
=== CPS IoT — M5StickC PLUS ===
[id] m5-67492c
[mic] SPM1423 ready
[wifi] joining "GC’s iPhone" ...
[wifi] connected, ip=172.20.10.5 rssi=-49 dBm
[sntp] server 172.20.10.4
[sntp] synced, step 391 ms
[mqtt] 172.20.10.4:1883
[mqtt] connected
[run] streaming imu + audio at 50 Hz
[pub] seq=0 sent=1 fail=0 mic=-37.8 dBFS rssi=-59 heap=209944
```

---

## 3 · Verify, hop by hop

Each step localizes a failure to one hop.

```bash
# 1. Is the board publishing?
multipass exec cps -- mosquitto_sub -h localhost -t 'cps/hw1/#' -v
```
JSON batches once per second, and a retained `online` on the status topic.

```bash
# 2. Are points landing?
multipass exec cps -- influx -database cps_hw1 \
  -execute 'SELECT count("az") FROM "imu" WHERE time > now() - 1m'
```
Expect **~3000** (50 Hz × 60 s). Double that means the simulator is still running.

```bash
# 3. Which devices are reporting?
multipass exec cps -- influx -database cps_hw1 \
  -execute 'SHOW TAG VALUES FROM "imu" WITH KEY = "device_id"'
```

```bash
# 4. Everything at once
multipass exec cps -- bash /home/ubuntu/cps/server/setup/verify.sh
```

Then open `http://<vm-ip>:3000/d/cps-hw1-live` (`admin` / `admin`).

---

## 4 · What real data looks like

- **`az` reads ~1.0 g when the board lies flat**, and goes to ~0 when you stand
  it on edge — gravity moving between axes.
- **Rotating the board spikes the gyro** on the axis you turned around.
- **Talking or clapping** moves the microphone loudness panel.
- **`skew_ms` shows a real SNTP correction** — a visible step on the first sync,
  then near zero.
- **Walk out of WiFi range** and dropped batches appear — QoS 1 and the sequence
  counter doing their job.

---

## If something fails

| Symptom | Most likely cause |
|---|---|
| Screen stuck on `wifi..` | 5 GHz network, wrong password, SSID apostrophe mismatch, or enterprise auth |
| Stuck on `sntp..` | `CPS_SERVER_IP` wrong, or chrony's `allow` doesn't cover the board's subnet |
| Connects but no data in Influx | Forwarder stuck after a clock jump — `sudo systemctl restart cps-forwarder` |
| `fail` counter climbing on screen | MQTT buffer too small, or broker refusing — check `max_queued_messages` |
| Grafana link just keeps loading | The VM's IP changed with the network — re-check step 1 |
| Grafana empty but Influx has rows | Time range. A wrong device clock puts points outside the picker's window. |
