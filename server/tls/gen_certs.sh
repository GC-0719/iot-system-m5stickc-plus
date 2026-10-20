#!/usr/bin/env bash
# Generate a private CA and a broker server certificate for MQTT over TLS
# (HW2, requirement 1). Run on the VM: bash server/tls/gen_certs.sh
#
# Outputs into this directory (gitignored -- private keys never committed):
#   ca.crt        public CA cert     -> embedded in firmware + given to clients
#   ca.key        CA private key     -> keep secret
#   server.crt    broker certificate -> mosquitto certfile
#   server.key    broker private key -> mosquitto keyfile
#
# The server cert's SubjectAltName lists the broker hostname AND its current IP,
# so both the board (connects by IP) and localhost clients validate it. The VM's
# IP changes with the network, so re-run this and restart mosquitto after a move.
set -euo pipefail
cd "$(dirname "$0")"

# detect the bridged LAN IP (exclude the Multipass NAT 192.168.25x range)
LAN_IP="$(ip -4 -o addr show | awk '$2!="lo" && $4 !~ /^192\.168\.25[0-9]\./ {split($4,a,"/"); print a[1]; exit}')"
HOST="${CPS_HOST:-cps.local}"
echo "server cert SAN: DNS:$HOST, DNS:localhost, IP:$LAN_IP, IP:127.0.0.1"

# 1) CA -- reuse if present so the firmware-embedded CA stays valid across
#    server-cert reissues (pass FRESH_CA=1 to force a new CA, then reflash)
if [ "${FRESH_CA:-0}" = "1" ] || [ ! -f ca.key ] || [ ! -f ca.crt ]; then
  openssl genrsa -out ca.key 2048 2>/dev/null
  openssl req -new -x509 -days 3650 -key ca.key -out ca.crt \
      -subj "/C=US/O=CPS-HW2/CN=CPS Local CA" 2>/dev/null
  echo "CA: generated (firmware must embed this ca.crt)"
else
  echo "CA: reused existing ca.crt (no reflash needed)"
fi

# 2) server key + CSR
openssl genrsa -out server.key 2048 2>/dev/null
openssl req -new -key server.key -out server.csr \
    -subj "/C=US/O=CPS-HW2/CN=$HOST" 2>/dev/null

# 3) sign with SAN
cat > server.ext <<EXT
subjectAltName = DNS:$HOST, DNS:localhost, DNS:$LAN_IP, IP:$LAN_IP, IP:127.0.0.1
EXT
openssl x509 -req -in server.csr -CA ca.crt -CAkey ca.key -CAcreateserial \
    -out server.crt -days 825 -extfile server.ext 2>/dev/null
rm -f server.csr server.ext ca.srl

chmod 600 ca.key server.key
echo "generated:"; ls -l ca.crt ca.key server.crt server.key | awk '{print "  "$NF}'
echo "verify:"; openssl verify -CAfile ca.crt server.crt
