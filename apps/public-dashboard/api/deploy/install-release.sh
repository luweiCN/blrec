#!/usr/bin/env bash
set -euo pipefail

archive="${1:-}"
release_id="${2:-}"

if [[ "$(id -u)" != "0" ]]; then
  echo "API deployment must run as root" >&2
  exit 1
fi
if [[ ! -f "$archive" ]]; then
  echo "API release archive does not exist" >&2
  exit 1
fi
if [[ ! "$release_id" =~ ^[0-9a-f]{40}-[0-9]+-[0-9]+$ ]]; then
  echo "API release identifier is invalid" >&2
  exit 1
fi
if [[ ! -s /etc/blrec-dashboard-api/api.env ]]; then
  echo "API environment file is missing" >&2
  exit 1
fi

application_root=/opt/blrec-dashboard-api
releases_root="$application_root/releases"
release="$releases_root/$release_id"
current_link="$application_root/current"
next_link="$application_root/.current-$release_id"
database_root=/var/lib/blrec-dashboard-api
database_backup_root="$database_root/backups"
nginx_available="/etc/nginx/sites-available/vg-api.luwei.host-$release_id"
nginx_enabled=/etc/nginx/sites-enabled/vg-api.luwei.host
nginx_next="/etc/nginx/sites-enabled/.vg-api.luwei.host-$release_id"

if ! id blrec-dashboard-api >/dev/null 2>&1; then
  useradd --system --home-dir /nonexistent --shell /usr/sbin/nologin \
    blrec-dashboard-api
fi
install -d -m 0755 -o root -g root "$application_root" "$releases_root"
install -d -m 0750 -o blrec-dashboard-api -g blrec-dashboard-api "$database_root"
install -d -m 0750 -o blrec-dashboard-api -g blrec-dashboard-api \
  "$database_backup_root"

if [[ -e "$release" ]]; then
  echo "API release identifier already exists" >&2
  exit 1
fi
install -d -m 0755 -o root -g root "$release"
tar --extract --gzip --file "$archive" --directory "$release"
chmod 0755 "$release"

test -s "$release/runtime-requirements.txt"
test -d "$release/wheelhouse"
test -d "$release/wheels"
test -s "$release/deploy/blrec-dashboard-api.service"
test -s "$release/deploy/blrec-dashboard-db-tunnel.service"
test -s "$release/deploy/vg-api.luwei.host.nginx.conf"

python3 -m venv "$release/venv"
"$release/venv/bin/python" -m pip install \
  --disable-pip-version-check \
  --no-index \
  --find-links "$release/wheelhouse" \
  --requirement "$release/runtime-requirements.txt"

publisher_wheels=("$release"/wheels/blrec_dashboard_publisher-*.whl)
api_wheels=("$release"/wheels/blrec_dashboard_api-*.whl)
if [[ ${#publisher_wheels[@]} != 1 || ! -f "${publisher_wheels[0]}" ]]; then
  echo "Publisher support wheel is missing" >&2
  exit 1
fi
if [[ ${#api_wheels[@]} != 1 || ! -f "${api_wheels[0]}" ]]; then
  echo "API wheel is missing" >&2
  exit 1
fi
"$release/venv/bin/python" -m pip install \
  --disable-pip-version-check --no-index --no-deps \
  "${publisher_wheels[0]}" "${api_wheels[0]}"

chown -R root:root "$release"

set -a
# shellcheck disable=SC1091
source /etc/blrec-dashboard-api/api.env
set +a
database_path="${DASHBOARD_API_DATABASE_PATH:-$database_root/dashboard.sqlite3}"
database_url="${DASHBOARD_API_DATABASE_URL:-}"
if [[ -n "$database_url" ]]; then
  if [[ "$database_url" != postgresql://* && "$database_url" != postgresql+psycopg://* ]]; then
    echo "API database URL must use PostgreSQL" >&2
    exit 1
  fi
  if ! command -v pg_dump >/dev/null || ! command -v pg_restore >/dev/null; then
    echo "PostgreSQL client tools are required for API database backup" >&2
    exit 1
  fi
  if [[ ! -s /etc/blrec-dashboard-api/db-tunnel-ssh.conf ]]; then
    echo "PostgreSQL SSH tunnel configuration is missing" >&2
    exit 1
  fi
  install -m 0644 -o root -g root \
    "$release/deploy/blrec-dashboard-db-tunnel.service" \
    /etc/systemd/system/blrec-dashboard-db-tunnel.service
  systemctl daemon-reload
  systemctl enable blrec-dashboard-db-tunnel.service >/dev/null
  systemctl restart blrec-dashboard-db-tunnel.service
  postgres_url="${database_url/postgresql+psycopg:\/\//postgresql:\/\/}"
  postgres_ready=false
  for _attempt in {1..30}; do
    if "$release/venv/bin/python" - "$postgres_url" <<'PY'
import sys

import psycopg

try:
    with psycopg.connect(sys.argv[1], connect_timeout=2) as connection:
        connection.execute('SELECT 1').fetchone()
except psycopg.Error:
    raise SystemExit(1)
PY
    then
      postgres_ready=true
      break
    fi
    sleep 1
  done
  if [[ "$postgres_ready" != "true" ]]; then
    echo "PostgreSQL SSH tunnel did not become ready" >&2
    exit 1
  fi
  postgres_backup="$database_backup_root/dashboard-$release_id.dump"
  pg_dump --format=custom --file="$postgres_backup" "$postgres_url"
  if [[ ! -s "$postgres_backup" ]]; then
    echo "PostgreSQL database backup is empty" >&2
    exit 1
  fi
  pg_restore --list "$postgres_backup" >/dev/null
  chown blrec-dashboard-api:blrec-dashboard-api "$postgres_backup"
  chmod 0640 "$postgres_backup"
else
  if [[ "$database_path" != "$database_root/"* ]]; then
    echo "API database path must stay below $database_root" >&2
    exit 1
  fi
fi
if [[ -z "$database_url" && -e "$database_path" ]]; then
  if [[ ! -f "$database_path" || -L "$database_path" ]]; then
    echo "API database path is not a regular file" >&2
    exit 1
  fi
  database_backup="$database_backup_root/dashboard-$release_id.sqlite3"
  "$release/venv/bin/python" - "$database_path" "$database_backup" <<'PY'
import sqlite3
import sys
from pathlib import Path

source_path = Path(sys.argv[1])
backup_path = Path(sys.argv[2])
source = sqlite3.connect(f'file:{source_path}?mode=ro', uri=True, timeout=30)
backup = sqlite3.connect(backup_path, timeout=30)
try:
    source_check = source.execute('PRAGMA quick_check').fetchone()
    if source_check != ('ok',):
        raise RuntimeError(f'source database quick_check failed: {source_check!r}')
    source.backup(backup)
    backup_check = backup.execute('PRAGMA quick_check').fetchone()
    if backup_check != ('ok',):
        raise RuntimeError(f'backup database quick_check failed: {backup_check!r}')
finally:
    backup.close()
    source.close()
if not backup_path.is_file() or backup_path.stat().st_size == 0:
    raise RuntimeError('API database backup is empty')
PY
  chown blrec-dashboard-api:blrec-dashboard-api "$database_backup"
  chmod 0640 "$database_backup"
fi

verify_unit="/tmp/blrec-dashboard-api-$release_id.service"
sed "s#/opt/blrec-dashboard-api/current#$release#g" \
  "$release/deploy/blrec-dashboard-api.service" > "$verify_unit"
systemd-analyze verify "$verify_unit"
unlink "$verify_unit"
install -m 0644 -o root -g root \
  "$release/deploy/blrec-dashboard-api.service" \
  /etc/systemd/system/blrec-dashboard-api.service
install -m 0644 -o root -g root \
  "$release/deploy/vg-api.luwei.host.nginx.conf" \
  "$nginx_available"

previous_nginx=""
if [[ -L "$nginx_enabled" ]]; then
  previous_nginx="$(readlink -f "$nginx_enabled")"
fi
ln -s "$nginx_available" "$nginx_next"
mv -Tf "$nginx_next" "$nginx_enabled"

previous_release=""
if [[ -L "$current_link" ]]; then
  previous_release="$(readlink -f "$current_link")"
fi
ln -s "$release" "$next_link"
mv -Tf "$next_link" "$current_link"

systemctl daemon-reload
systemctl enable blrec-dashboard-api.service >/dev/null
if ! nginx -t; then
  if [[ -n "$previous_nginx" ]]; then
    ln -s "$previous_nginx" "$nginx_next"
    mv -Tf "$nginx_next" "$nginx_enabled"
  else
    unlink "$nginx_enabled"
  fi
  if [[ -n "$previous_release" ]]; then
    ln -s "$previous_release" "$next_link"
    mv -Tf "$next_link" "$current_link"
  fi
  exit 1
fi

systemctl restart blrec-dashboard-api.service
healthy=false
for _attempt in {1..30}; do
  if curl --fail --silent --show-error --max-time 2 \
    http://127.0.0.1:8787/v1/health >/dev/null; then
    healthy=true
    break
  fi
  sleep 1
done

if [[ "$healthy" != "true" ]]; then
  if [[ -n "$previous_release" ]]; then
    ln -s "$previous_release" "$next_link"
    mv -Tf "$next_link" "$current_link"
    systemctl restart blrec-dashboard-api.service
  else
    systemctl stop blrec-dashboard-api.service
    unlink "$current_link"
  fi
  if [[ -n "$previous_nginx" ]]; then
    ln -s "$previous_nginx" "$nginx_next"
    mv -Tf "$nginx_next" "$nginx_enabled"
  else
    unlink "$nginx_enabled"
  fi
  nginx -t
  systemctl reload nginx
  echo "API health check failed; previous release restored" >&2
  exit 1
fi

systemctl reload nginx
echo "API release deployed: $release_id"
