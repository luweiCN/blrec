#!/usr/bin/env bash
set -euo pipefail

archive="${1:-}"
release_id="${2:-}"

if [[ "$(id -u)" != "0" ]]; then
  echo "Internal admin deployment must run as root" >&2
  exit 1
fi
if [[ ! -f "$archive" ]]; then
  echo "Internal admin release archive does not exist" >&2
  exit 1
fi
if [[ ! "$release_id" =~ ^[0-9a-f]{40}-[0-9]+-[0-9]+$ ]]; then
  echo "Internal admin release identifier is invalid" >&2
  exit 1
fi
if [[ ! -s /etc/blrec-dashboard-admin/admin.env ]]; then
  echo "Internal admin environment file is missing" >&2
  exit 1
fi

application_root=/opt/blrec-dashboard-admin
releases_root="$application_root/releases"
release="$releases_root/$release_id"
current_link="$application_root/current"
next_link="$application_root/.current-$release_id"
nginx_available="/etc/nginx/sites-available/blrec-dashboard-admin-$release_id"
nginx_enabled=/etc/nginx/sites-enabled/blrec-dashboard-admin
nginx_next="/etc/nginx/sites-enabled/.blrec-dashboard-admin-$release_id"

install -d -m 0755 -o root -g root "$application_root" "$releases_root"
if [[ -e "$release" ]]; then
  echo "Internal admin release identifier already exists" >&2
  exit 1
fi
install -d -m 0755 -o root -g root "$release"
tar --extract --gzip --file "$archive" --directory "$release"
test -s "$release/site/index.html"
test -s "$release/deploy/nginx.conf.template"
chown -R root:root "$release"
chmod -R u=rwX,go=rX "$release/site"

set -a
# shellcheck disable=SC1091
source /etc/blrec-dashboard-admin/admin.env
set +a
listen_address="${BLREC_INTERNAL_ADMIN_LISTEN_ADDRESS:-}"
if [[ -z "$listen_address" ]]; then
  listen_address="$(
    hostname -I | tr ' ' '\n' | grep -E '^192\.168\.50\.[0-9]+$' | head -n 1 || true
  )"
fi
backend="${BLREC_INTERNAL_ADMIN_BACKEND:-http://192.168.50.24:2234}"
if [[ -z "${BLREC_INTERNAL_ADMIN_API_KEY:-}" ]]; then
  echo "Internal admin API key is missing" >&2
  exit 1
fi
if [[ -z "${BLREC_DASHBOARD_OWNER_TOKEN:-}" ]]; then
  echo "Internal dashboard owner token is missing" >&2
  exit 1
fi
if [[ ! "$listen_address" =~ ^192\.168\.50\.[0-9]+$ ]]; then
  echo "Internal admin must listen on the management LAN" >&2
  exit 1
fi
if [[ ! "$backend" =~ ^http://192\.168\.50\.[0-9]+:[0-9]+$ ]]; then
  echo "Internal admin backend must be an HTTP management-LAN endpoint" >&2
  exit 1
fi

export BLREC_INTERNAL_ADMIN_LISTEN_ADDRESS="$listen_address"
export BLREC_INTERNAL_ADMIN_BACKEND="$backend"
python3 - "$release/deploy/nginx.conf.template" "$nginx_available" <<'PY'
import os
import sys
from pathlib import Path
from urllib.parse import urlsplit


def nginx_quote(value: str) -> str:
    escaped = value.replace('\\', '\\\\').replace('"', '\\"').replace('$', '\\$')
    return f'"{escaped}"'


source = Path(sys.argv[1]).read_text(encoding='utf8')
destination = Path(sys.argv[2])
backend = os.environ['BLREC_INTERNAL_ADMIN_BACKEND'].rstrip('/')
parsed = urlsplit(backend)
rendered = source
rendered = rendered.replace(
    '__BLREC_INTERNAL_ADMIN_LISTEN_ADDRESS__',
    os.environ['BLREC_INTERNAL_ADMIN_LISTEN_ADDRESS'],
)
rendered = rendered.replace('__BLREC_INTERNAL_ADMIN_BACKEND__', backend)
rendered = rendered.replace(
    '__BLREC_INTERNAL_ADMIN_BACKEND_HOST__', parsed.netloc
)
rendered = rendered.replace(
    '__BLREC_INTERNAL_ADMIN_API_KEY__',
    nginx_quote(os.environ['BLREC_INTERNAL_ADMIN_API_KEY']),
)
rendered = rendered.replace(
    '__BLREC_DASHBOARD_OWNER_AUTHORIZATION__',
    nginx_quote('Bearer ' + os.environ['BLREC_DASHBOARD_OWNER_TOKEN']),
)
if '__BLREC_' in rendered:
    raise RuntimeError('internal admin nginx template has unresolved values')
destination.write_text(rendered, encoding='utf8')
PY
chown root:root "$nginx_available"
chmod 0600 "$nginx_available"

previous_nginx=""
if [[ -L "$nginx_enabled" ]]; then
  previous_nginx="$(readlink -f "$nginx_enabled")"
fi
previous_release=""
if [[ -L "$current_link" ]]; then
  previous_release="$(readlink -f "$current_link")"
fi

ln -s "$nginx_available" "$nginx_next"
mv -Tf "$nginx_next" "$nginx_enabled"
ln -s "$release" "$next_link"
mv -Tf "$next_link" "$current_link"

rollback() {
  if [[ -n "$previous_release" ]]; then
    ln -s "$previous_release" "$next_link"
    mv -Tf "$next_link" "$current_link"
  else
    unlink "$current_link" 2>/dev/null || true
  fi
  if [[ -n "$previous_nginx" ]]; then
    ln -s "$previous_nginx" "$nginx_next"
    mv -Tf "$nginx_next" "$nginx_enabled"
  else
    unlink "$nginx_enabled" 2>/dev/null || true
  fi
}

if ! nginx -t; then
  rollback
  echo "Internal admin Nginx validation failed; previous release restored" >&2
  exit 1
fi
systemctl reload nginx

healthy=false
for _attempt in {1..30}; do
  if curl --fail --silent --show-error --max-time 5 \
    "http://$listen_address:8790/" >/dev/null \
    && curl --fail --silent --show-error --max-time 10 \
      "http://$listen_address:8790/internal-api/heroes" >/dev/null \
    && curl --fail --silent --show-error --max-time 10 \
      "http://$listen_address:8790/dashboard-api/v2/dashboard/summary" \
      >/dev/null; then
    healthy=true
    break
  fi
  sleep 1
done
if [[ "$healthy" != "true" ]]; then
  rollback
  nginx -t
  systemctl reload nginx
  echo "Internal admin health check failed; previous release restored" >&2
  exit 1
fi

echo "Internal admin release deployed: $release_id"
