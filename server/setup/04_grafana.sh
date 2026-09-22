#!/usr/bin/env bash
# Grafana 7.5.7 + a provisioned InfluxDB data source. Run on the Ubuntu VM.
#
# 7.5.7 is the last 7.x release with native InfluxQL editing. It is also
# end-of-life with known published CVEs -- keep :3000 on the LAN.
set -euo pipefail

VERSION=7.5.7
ARCH=$(dpkg --print-architecture)
DEB="grafana_${VERSION}_${ARCH}.deb"
URL="https://dl.grafana.com/oss/release/${DEB}"

echo "installing Grafana ${VERSION} (${ARCH})"
if ! curl -fsI "$URL" >/dev/null; then
  echo "ERROR: no build at ${URL} -- take the nearest ${ARCH} build on 7.5.x"
  exit 1
fi

sudo apt install -y adduser libfontconfig1
curl -fsSLO "$URL"
sudo dpkg -i "$DEB"
rm -f "$DEB"

# Provision the data source from a file rather than clicking it in, so the
# configuration is reproducible and lives in the repo.
sudo mkdir -p /etc/grafana/provisioning/datasources
sudo tee /etc/grafana/provisioning/datasources/influxdb.yaml >/dev/null <<'EOF'
apiVersion: 1
datasources:
  - name: InfluxDB-cps
    type: influxdb
    access: proxy
    url: http://localhost:8086
    database: cps_hw1
    isDefault: true
    jsonData:
      httpMode: GET
EOF

# Provision the dashboard too, so there is something on screen the moment data
# starts flowing. Build your own panels afterwards -- clicking through the
# query editor once is how the InfluxQL model actually lands.
sudo mkdir -p /etc/grafana/provisioning/dashboards /var/lib/grafana/dashboards
sudo tee /etc/grafana/provisioning/dashboards/cps.yaml >/dev/null <<'EOF'
apiVersion: 1
providers:
  - name: cps
    orgId: 1
    folder: ''
    type: file
    disableDeletion: false
    options:
      path: /var/lib/grafana/dashboards
EOF

if [ -f "$(dirname "$0")/../grafana/dashboards/cps-live.json" ]; then
  sudo cp "$(dirname "$0")/../grafana/dashboards/cps-live.json" /var/lib/grafana/dashboards/
  sudo chown -R grafana:grafana /var/lib/grafana/dashboards
fi

sudo systemctl daemon-reload
sudo systemctl enable --now grafana-server
sleep 5
systemctl --no-pager status grafana-server | head -5

LAN_IP=$(ip -4 -o addr show | awk '$2!="lo" && $4 !~ /^192\.168\.25[0-9]\./ {split($4,a,"/"); print a[1]; exit}')
echo
echo "Grafana:  http://${LAN_IP}:3000   (admin / admin)"
echo "The InfluxDB-cps data source is already wired to the cps_hw1 database."
