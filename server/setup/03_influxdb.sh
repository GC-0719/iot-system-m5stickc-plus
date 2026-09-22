#!/usr/bin/env bash
# InfluxDB 1.8.10 + the cps_hw1 database. Run on the Ubuntu VM.
#
# 1.8 (not 2.x) because Grafana 7.5.7 speaks InfluxQL natively and 1.8 runs a
# smaller resident set. See docs/ARCHITECTURE.md §4.
set -euo pipefail

VERSION=1.8.10
ARCH=$(dpkg --print-architecture)   # arm64 on an Apple Silicon guest
DEB="influxdb_${VERSION}_${ARCH}.deb"
URL="https://dl.influxdata.com/influxdb/releases/${DEB}"

echo "installing InfluxDB ${VERSION} (${ARCH})"
if ! curl -fsI "$URL" >/dev/null; then
  echo "ERROR: no build at ${URL}"
  echo "Pick the nearest ${ARCH} build on the 1.8 line -- do NOT switch to an"
  echo "emulated x86_64 VM, it would invalidate every §6 measurement."
  exit 1
fi

curl -fsSLO "$URL"
sudo dpkg -i "$DEB"
rm -f "$DEB"

sudo systemctl enable --now influxdb
sleep 3

# Database + retention. rp_7d holds raw 50 Hz waveform for a week; rp_90d holds
# 1 Hz downsampled means for a quarter, so long dashboard ranges never scan raw.
influx -execute 'CREATE DATABASE cps_hw1'
influx -execute 'CREATE RETENTION POLICY rp_7d  ON cps_hw1 DURATION 7d  REPLICATION 1 SHARD DURATION 1h DEFAULT'
influx -execute 'CREATE RETENTION POLICY rp_90d ON cps_hw1 DURATION 90d REPLICATION 1 SHARD DURATION 1d'

# Downsample 50 Hz -> 1 Hz into the long-retention policy, so a dashboard
# showing a month never scans raw waveform.
# NOTE: keep this on ONE line. `influx -execute` does not preserve the newlines
# inside a BEGIN...END block and the parser dies with "found EOF, expected SELECT".
influx -execute 'CREATE CONTINUOUS QUERY cq_imu_1s ON cps_hw1 BEGIN SELECT mean(*) INTO cps_hw1.rp_90d.imu_1s FROM cps_hw1.rp_7d.imu GROUP BY time(1s), * END'

echo
influx -execute 'SHOW RETENTION POLICIES' -database cps_hw1
echo
echo "InfluxDB listening on :8086. Open the shell with:  influx -database cps_hw1"
