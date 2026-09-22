#!/usr/bin/env bash
# MySQL 8.0: device registry + Grafana's own config store.
# Run on the Ubuntu VM after 01-05.
set -euo pipefail

REPO="${REPO:-$HOME/cps}"
GRAFANA_DB_PASS="${GRAFANA_DB_PASS:-cps_grafana_hw1}"

sudo apt update
sudo apt install -y mysql-server

# Tuning applied up front, because the goal is minimum RAM and mysqld's default
# footprint is the largest single line in the budget. performance_schema alone
# is worth a few hundred MB and this project never queries it.
sudo tee /etc/mysql/mysql.conf.d/99-cps.cnf >/dev/null <<'EOF'
[mysqld]
# Registry data is tiny; the stock 128M buffer pool is oversized for it.
innodb_buffer_pool_size = 64M

# Biggest easy win in the RAM budget (ARCHITECTURE.md §4).
performance_schema = OFF

max_connections = 40
innodb_log_file_size = 32M

# Local only. Add TLS and per-user grants before opening it up.
bind-address = 127.0.0.1
EOF

sudo systemctl restart mysql
sudo systemctl enable mysql

sudo mysql < "$REPO/server/mysql/schema.sql"

# Grafana's own dashboards/users/settings move off SQLite into MySQL, which is
# what makes "install MySQL" load-bearing rather than decorative.
sudo mysql <<EOF
CREATE DATABASE IF NOT EXISTS grafana CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE USER IF NOT EXISTS 'grafana'@'localhost' IDENTIFIED BY '${GRAFANA_DB_PASS}';
GRANT ALL PRIVILEGES ON grafana.* TO 'grafana'@'localhost';
CREATE USER IF NOT EXISTS 'cps'@'localhost' IDENTIFIED BY '${GRAFANA_DB_PASS}';
GRANT ALL PRIVILEGES ON cps_hw1.* TO 'cps'@'localhost';
FLUSH PRIVILEGES;
EOF

# Point Grafana at MySQL. Provisioned dashboards and datasources come from
# files, so they survive the backend switch untouched.
#
# Every substitution is scoped to the [database] block. grafana.ini contains
# several other `;password =` lines (notably [security] admin_password), and an
# unscoped sed would rewrite those too.
#
# NOTE on the password line specifically: the stock file writes it as
#   ;password =
# with a SEMICOLON, not `# password =`. Matching the wrong comment character
# leaves it commented, Grafana connects with an empty password, and the service
# dies with "Access denied for user 'grafana'@'localhost' (using password: NO)"
# -- which reads like a MySQL grant problem and is not one.
sudo sed -i "/^\[database\]/,/^\[[a-z]/ {
  s|^;*[[:space:]]*type = sqlite3|type = mysql|
  s|^;*[[:space:]]*host = 127.0.0.1:3306|host = 127.0.0.1:3306|
  s|^;*[[:space:]]*name = grafana|name = grafana|
  s|^;*[[:space:]]*user = root|user = grafana|
  s|^;*[[:space:]]*password =.*|password = ${GRAFANA_DB_PASS}|
}" /etc/grafana/grafana.ini

# Fail loudly here rather than letting systemd report a confusing auth error.
if ! sudo sed -n "/^\[database\]/,/^\[[a-z]/p" /etc/grafana/grafana.ini | grep -q "^password = "; then
  echo "ERROR: could not set the Grafana database password in grafana.ini"
  exit 1
fi

sudo systemctl restart grafana-server
sleep 8

echo
echo "=== registry ==="
sudo mysql -D cps_hw1 -e 'SELECT device_id, hardware, sample_hz FROM device;'
echo "=== tables ==="
sudo mysql -D cps_hw1 -e 'SHOW TABLES;'
echo "=== grafana backend ==="
sudo grep -A6 '^\[database\]' /etc/grafana/grafana.ini | grep -E '^(type|host|name|user)' || true
echo
echo "mysqld RSS: $(ps -o rss= -C mysqld | awk '{s+=$1} END {printf "%.0f MB", s/1024}')"
