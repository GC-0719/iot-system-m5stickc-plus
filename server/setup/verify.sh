#!/usr/bin/env bash
# Health check for the whole stack. Run on the VM:
#     multipass exec cps -- bash /home/ubuntu/cps/server/setup/verify.sh
#
# Every line checks one hop of the pipeline. Use this before a demo, and after any
# change, rather than trusting that a service which starts is a service that works.

LAN_IP=$(ip -4 -o addr show | awk '$2!="lo" && $4 !~ /^192\.168\.25[0-9]\./ {split($4,a,"/"); print a[1]; exit}')
pass=0; fail=0
chk() { # chk "<label>" "<expected>" "<actual>"
  if [ "$2" = "$3" ] || { [ -n "$2" ] && [ "$2" = "nonzero" ] && [ "$3" -gt 0 ] 2>/dev/null; }; then
    printf "  \033[32mPASS\033[0m  %-42s %s\n" "$1" "$3"; pass=$((pass+1))
  else
    printf "  \033[31mFAIL\033[0m  %-42s got '%s' want '%s'\n" "$1" "$3" "$2"; fail=$((fail+1))
  fi
}

echo "=== core services ==="
for s in chrony mosquitto influxdb mysql grafana-server; do
  chk "$s running" "active" "$(systemctl is-active $s 2>/dev/null)"
done

echo
echo "=== server clock synced to world clock ==="
stratum=$(chronyc tracking 2>/dev/null | awk '/Stratum/ {print $3}')
chk "chrony stratum <= 4" "true" "$([ -n "$stratum" ] && [ "$stratum" -le 4 ] && echo true || echo false)"
echo "        offset: $(chronyc tracking 2>/dev/null | awk -F': ' '/System time/ {print $2}')"

echo
echo "=== server serves NTP to the sensor's network ==="
chk "chrony allow rule present" "nonzero" "$(grep -c '^allow' /etc/chrony/chrony.conf 2>/dev/null)"
ALLOW=$(grep '^allow' /etc/chrony/chrony.conf 2>/dev/null | awk '{print $2}' | head -1)
echo "        serving: ${ALLOW:-none}"
echo "        this VM: ${LAN_IP}"

# The allow rule is baked in at install time from whatever subnet the VM had
# then. Move the laptop to a different network and the DHCP lease changes to a
# different subnet -- chrony keeps serving the OLD one and silently refuses the
# sensor. Nothing else fails, so this is invisible until the board won't sync.
covers=$(python3 -c "
import ipaddress, sys
try:
    print(ipaddress.ip_address(sys.argv[1]) in ipaddress.ip_network(sys.argv[2], strict=False))
except Exception:
    print('False')
" "$LAN_IP" "${ALLOW:-0.0.0.0/32}" 2>/dev/null)
chk "allow rule covers this VM's subnet" "True" "$covers"
if [ "$covers" != "True" ]; then
  echo "        -> network changed. Fix with: bash ~/cps/server/setup/01_chrony.sh"
fi
echo "        clients: $(chronyc clients 2>/dev/null | tail -n +3 | wc -l) seen"

echo
echo "=== data reaching InfluxDB ==="
pts=$(influx -database cps_hw1 -execute 'SELECT count("az") FROM "imu" WHERE time > now() - 1m' 2>/dev/null | tail -1 | awk '{print $2}')
chk "points in the last minute" "nonzero" "${pts:-0}"
# The check is for unbounded GROWTH, not a specific number. Benchmark runs
# legitimately add series (hostmetrics is tagged per service per run label), so
# a few dozen is healthy. Thousands would mean something high-cardinality --
# a sequence number, a timestamp, a measured value -- leaked into a tag.
series=$(influx -database cps_hw1 -execute 'SHOW SERIES' 2>/dev/null | tail -n +3 | grep -c .)
chk "series cardinality (want < 500)" "true" "$([ "${series:-99999}" -lt 500 ] && echo true || echo false)"
echo "        series: ${series:-?}"
echo "        devices: $(influx -database cps_hw1 -execute 'SHOW TAG VALUES FROM "imu" WITH KEY = "device_id"' 2>/dev/null | tail -n +4 | awk '{print $2}' | tr '\n' ' ')"
drops=$(influx -database cps_hw1 -execute 'SELECT sum("gap_count") FROM "pipeline"' 2>/dev/null | tail -1 | awk '{print $2}')
echo "        dropped batches (all time): ${drops:-0}"

echo
echo "=== MySQL is actually load-bearing ==="
reg=$(sudo mysql -N -B -D cps_hw1 -e 'SELECT COUNT(*) FROM device' 2>/dev/null)
chk "registry rows" "nonzero" "${reg:-0}"
gt=$(sudo mysql -N -B -e 'SELECT COUNT(*) FROM information_schema.tables WHERE table_schema="grafana"' 2>/dev/null)
chk "grafana tables in MySQL (not SQLite)" "nonzero" "${gt:-0}"
echo "        grafana backend: $(sudo sed -n '/^\[database\]/,/^\[[a-z]/p' /etc/grafana/grafana.ini | awk -F'= ' '/^type/ {print $2}')"

echo
echo "=== Grafana serving ==="
chk "grafana health" "200" "$(curl -s -o /dev/null -w '%{http_code}' http://localhost:3000/api/health)"
echo
echo "  Dashboard: http://${LAN_IP}:3000/d/cps-hw1-live"
echo "  MQTT broker for the sensor: ${LAN_IP}:1883"
echo "  NTP server for the sensor:  ${LAN_IP}"
echo
printf "  %d passed, %d failed\n" "$pass" "$fail"
[ "$fail" -eq 0 ] || exit 1
