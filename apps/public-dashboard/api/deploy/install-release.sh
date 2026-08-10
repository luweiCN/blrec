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
incoming="$releases_root/.incoming-$release_id"
current_link="$application_root/current"
next_link="$application_root/.current-$release_id"
database_root=/var/lib/blrec-dashboard-api
nginx_available="/etc/nginx/sites-available/vg-api.luwei.host-$release_id"
nginx_enabled=/etc/nginx/sites-enabled/vg-api.luwei.host
nginx_next="/etc/nginx/sites-enabled/.vg-api.luwei.host-$release_id"

if ! id blrec-dashboard-api >/dev/null 2>&1; then
  useradd --system --home-dir /nonexistent --shell /usr/sbin/nologin \
    blrec-dashboard-api
fi
install -d -m 0755 -o root -g root "$application_root" "$releases_root"
install -d -m 0750 -o blrec-dashboard-api -g blrec-dashboard-api "$database_root"

if [[ -e "$release" || -e "$incoming" ]]; then
  echo "API release identifier already exists" >&2
  exit 1
fi
install -d -m 0755 -o root -g root "$incoming"
tar --extract --gzip --file "$archive" --directory "$incoming"

test -s "$incoming/runtime-requirements.txt"
test -d "$incoming/wheelhouse"
test -d "$incoming/wheels"
test -s "$incoming/deploy/blrec-dashboard-api.service"
test -s "$incoming/deploy/vg-api.luwei.host.nginx.conf"

python3 -m venv "$incoming/venv"
"$incoming/venv/bin/python" -m pip install \
  --disable-pip-version-check \
  --no-index \
  --find-links "$incoming/wheelhouse" \
  --requirement "$incoming/runtime-requirements.txt"

publisher_wheels=("$incoming"/wheels/blrec_dashboard_publisher-*.whl)
api_wheels=("$incoming"/wheels/blrec_dashboard_api-*.whl)
if [[ ${#publisher_wheels[@]} != 1 || ! -f "${publisher_wheels[0]}" ]]; then
  echo "Publisher support wheel is missing" >&2
  exit 1
fi
if [[ ${#api_wheels[@]} != 1 || ! -f "${api_wheels[0]}" ]]; then
  echo "API wheel is missing" >&2
  exit 1
fi
"$incoming/venv/bin/python" -m pip install \
  --disable-pip-version-check --no-index --no-deps \
  "${publisher_wheels[0]}" "${api_wheels[0]}"

mv "$incoming" "$release"
chown -R root:root "$release"

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
