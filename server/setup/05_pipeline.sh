#!/usr/bin/env bash
# Python deps + the simulator and forwarder as services. Run on the Ubuntu VM,
# after 01-04. Assumes the repo is at ~/cps.
set -euo pipefail

REPO="${REPO:-$HOME/cps}"
# The venv lives OUTSIDE the repo on purpose: the repo is a multipass mount of
# the Mac's filesystem, so a venv inside it would be written back across the
# mount (slow, and it pollutes the git tree with guest-arch binaries).
VENV="${VENV:-$HOME/.cps-venv}"
cd "$REPO"

sudo apt install -y python3-venv python3-pip
python3 -m venv "$VENV"
"$VENV/bin/pip" install -q -r requirements.txt
echo "deps installed: $("$VENV/bin/pip" freeze | tr '\n' ' ')"

# systemd rather than nohup: the demo should survive a logout, and `journalctl`
# is a better debugging surface than a stray log file.
sudo tee /etc/systemd/system/cps-forwarder.service >/dev/null <<EOF
[Unit]
Description=CPS IoT MQTT -> InfluxDB forwarder
After=network-online.target influxdb.service mosquitto.service
Wants=network-online.target

[Service]
Type=simple
User=$USER
WorkingDirectory=$REPO
ExecStart=$VENV/bin/python $REPO/forwarder/forwarder.py --mqtt-host localhost --influx-host localhost
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF

sudo tee /etc/systemd/system/cps-sensor-sim.service >/dev/null <<EOF
[Unit]
Description=CPS IoT simulated M5StickC sensor
After=network-online.target mosquitto.service
Wants=network-online.target

[Service]
Type=simple
User=$USER
WorkingDirectory=$REPO
ExecStart=$VENV/bin/python $REPO/sim/sensor_sim.py --host localhost --device-id sim-000001
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now cps-forwarder cps-sensor-sim
sleep 8

echo
systemctl --no-pager status cps-forwarder cps-sensor-sim | grep -E 'Active:|●' || true
echo
echo "=== is data landing? (expect a count climbing toward ~3000/min) ==="
influx -database cps_hw1 -execute 'SELECT count("az") FROM "imu" WHERE time > now() - 1m'
echo
echo "Follow either service with:  journalctl -fu cps-forwarder"
echo "Stop the demo with:          sudo systemctl stop cps-sensor-sim cps-forwarder"
