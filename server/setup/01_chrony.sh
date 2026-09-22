#!/usr/bin/env bash
# chrony: world -> server, and server -> sensor. Run on the Ubuntu VM.
#
# Ubuntu 22.04 ships systemd-timesyncd, which is an SNTP *client only*. It
# cannot serve time to the sensor, and it holds UDP/123 so chrony can't bind.
# It has to go first, or neither the server nor the sensor can be synced.
set -euo pipefail

sudo systemctl disable --now systemd-timesyncd || true
sudo apt update
sudo apt install -y chrony

# Multipass gives the VM TWO interfaces: a private NAT one (192.168.252.x) that
# the sensor can never reach, and the bridged one on the real LAN. `allow` must
# name the bridged network or the M5StickC gets no time service. Override with
# LAN=... if the guess below looks wrong.
NAT_NET=$(ip -4 route show scope link proto kernel | awk '/^192\.168\.25[0-9]\./ {print $1; exit}')
LAN=${LAN:-$(ip -4 route show scope link proto kernel \
              | awk -v nat="$NAT_NET" '$1 != nat {print $1; exit}')}

if [ -z "$LAN" ]; then
  echo "ERROR: could not identify the bridged LAN. Re-run as: LAN=10.0.0.0/24 bash $0"
  exit 1
fi
echo "serving NTP to ${LAN}  (NAT interface ${NAT_NET:-none} excluded)"

sudo tee /etc/chrony/chrony.conf >/dev/null <<EOF
pool 2.ubuntu.pool.ntp.org iburst maxsources 4

driftfile /var/lib/chrony/chrony.drift
logdir /var/log/chrony
rtcsync

# Step the clock at ANY time, not just during the first three updates. The
# guest clock jumps whenever the MacBook sleeps; the default (makestep 1.0 3)
# would leave it slewing for hours afterwards.
makestep 1.0 -1

# This line is what turns chrony from a client into a server.
allow ${LAN}

# Keep serving the sensor even if the VM loses its uplink.
local stratum 10
EOF

sudo systemctl restart chrony
sleep 8

echo
echo "=== server synchronized to world clock ==="
chronyc tracking
echo
chronyc sources -v | head -20
echo
echo "Want: Stratum 3 or better, 'System time' offset within a few ms."
echo
echo "=== verify the server serves time ==="
echo "From the Mac:   sntp -d $(hostname -I | awk '{print $1}')"
echo "Then, once the sensor is up:   chronyc clients"
echo
echo "NOTE: disable your hypervisor's guest time sync. Two clock masters"
echo "fighting over the same clock makes every latency measurement meaningless."
