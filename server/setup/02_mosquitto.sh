#!/usr/bin/env bash
# Mosquitto MQTT broker. Run on the Ubuntu VM.
#
# Mosquitto 2.0 changed its defaults: it binds 127.0.0.1 only and denies
# anonymous clients. Without the config below the broker starts fine, logs
# nothing alarming, and simply refuses every connection from the sensor.
set -euo pipefail

sudo apt update
sudo apt install -y mosquitto mosquitto-clients

# NOTE: do NOT set `persistence` or `persistence_location` here. Ubuntu's stock
# /etc/mosquitto/mosquitto.conf already declares both, and mosquitto treats a
# second `persistence_location` as a fatal duplicate rather than an override --
# it exits before binding, and systemd reports only "start request repeated too
# quickly". Queueing is already on; we just raise the depth.
sudo tee /etc/mosquitto/conf.d/cps.conf >/dev/null <<'EOF'
listener 1883 0.0.0.0
allow_anonymous true

# Queue QoS 1 messages for the forwarder while it is restarting.
max_queued_messages 10000

log_type notice
EOF

sudo systemctl restart mosquitto
sudo systemctl enable mosquitto
systemctl --no-pager status mosquitto | head -5

# The bridged address, not the Multipass NAT one -- this is what the sensor uses.
LAN_IP=$(ip -4 -o addr show | awk '$2!="lo" && $4 !~ /^192\.168\.25[0-9]\./ {split($4,a,"/"); print a[1]; exit}')

echo
echo "Verify from the Mac (in two terminals):"
echo "  mosquitto_sub -h ${LAN_IP} -t 'cps/hw1/#' -v"
echo "  mosquitto_pub -h ${LAN_IP} -t 'cps/hw1/test' -m hello"
echo
echo "allow_anonymous true is a LAN-only development setting. Replace it with"
echo "per-device credentials and ACLs before exposing the broker."
