#!/usr/bin/env bash
set -euo pipefail

fail() {
  printf 'ERROR: %s\n' "$*" >&2
  exit 1
}

artifact_id=${1:-}
output_dir=${2:-}
parallelism=${3:-16}

[[ "$artifact_id" =~ ^[0-9]+$ ]] || fail 'invalid artifact id'
[[ "$parallelism" =~ ^[0-9]+$ ]] || fail 'invalid parallelism'
test "$parallelism" -ge 1 && test "$parallelism" -le 32 || \
  fail 'parallelism must be between 1 and 32'
test -n "${GH_TOKEN:-}" || fail 'GH_TOKEN is required'
[[ "${GITHUB_REPOSITORY:-}" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]] || \
  fail 'invalid GitHub repository'
test -n "${RUNNER_TEMP:-}" || fail 'RUNNER_TEMP is required'
[[ "$output_dir" == "$RUNNER_TEMP"/* ]] || fail 'output must be under RUNNER_TEMP'

for command in curl python3 sha256sum; do
  command -v "$command" >/dev/null || fail "$command is required"
done

work_dir=$(mktemp -d "${RUNNER_TEMP}/vision-artifact-download.XXXXXX")
pids=()
cleanup() {
  for pid in "${pids[@]}"; do
    kill "$pid" 2>/dev/null || true
  done
  if [[ "$work_dir" == "$RUNNER_TEMP"/vision-artifact-download.* ]]; then
    rm -rf -- "$work_dir"
  fi
}
trap cleanup EXIT

api_headers="$work_dir/api.headers"
api_config="$work_dir/api.curl"
{
  printf 'silent\nshow-error\nfail\n'
  printf 'dump-header = "%s"\n' "$api_headers"
  printf 'output = "/dev/null"\n'
  printf 'header = "Authorization: Bearer %s"\n' "$GH_TOKEN"
  printf 'header = "Accept: application/vnd.github+json"\n'
  printf 'url = "https://api.github.com/repos/%s/actions/artifacts/%s/zip"\n' \
    "$GITHUB_REPOSITORY" "$artifact_id"
} >"$api_config"
chmod 600 "$api_config"
curl --config "$api_config"

signed_url=$(tr -d '\r' <"$api_headers" | sed -n 's/^location: //Ip' | tail -n 1)
test -n "$signed_url" || fail 'GitHub did not return an artifact URL'
case "$signed_url" in
  https://*'"'* | https://*'\'* | *$'\n'* | *$'\r'*)
    fail 'artifact URL contains unsupported characters'
    ;;
  https://*) ;;
  *) fail 'artifact URL is not HTTPS' ;;
esac

artifact_host=$(printf '%s' "$signed_url" | python3 -c '
import sys
import urllib.parse

print(urllib.parse.urlsplit(sys.stdin.read()).hostname or "")
')
[[ "$artifact_host" =~ ^[A-Za-z0-9.-]+$ ]] || fail 'invalid artifact host'

dns_json="$work_dir/dns.json"
curl --fail --silent --show-error \
  --resolve cloudflare-dns.com:443:1.1.1.1 \
  --header 'accept: application/dns-json' \
  --output "$dns_json" \
  "https://cloudflare-dns.com/dns-query?name=${artifact_host}&type=A"
artifact_ip=$(python3 - "$dns_json" <<'PY'
import ipaddress
import json
import sys

with open(sys.argv[1], encoding='utf8') as stream:
    response = json.load(stream)
for answer in response.get('Answer', []):
    if answer.get('type') != 1:
        continue
    try:
        print(ipaddress.IPv4Address(answer.get('data', '')))
        break
    except ipaddress.AddressValueError:
        continue
PY
)
test -n "$artifact_ip" || fail 'could not resolve artifact host directly'

probe_headers="$work_dir/probe.headers"
probe_config="$work_dir/probe.curl"
{
  printf 'silent\nshow-error\nfail\nlocation\n'
  printf 'connect-timeout = 15\nmax-time = 120\n'
  printf 'retry = 5\nretry-all-errors\n'
  printf 'resolve = "%s:443:%s"\n' "$artifact_host" "$artifact_ip"
  printf 'range = "0-0"\n'
  printf 'dump-header = "%s"\n' "$probe_headers"
  printf 'output = "/dev/null"\n'
  printf 'url = "%s"\n' "$signed_url"
} >"$probe_config"
chmod 600 "$probe_config"
curl --config "$probe_config"

archive_size=$(tr -d '\r' <"$probe_headers" | sed -n \
  's|^[Cc]ontent-[Rr]ange: bytes [0-9-]*/\([0-9][0-9]*\)$|\1|p' | tail -n 1)
[[ "$archive_size" =~ ^[0-9]+$ ]] || fail 'artifact server did not return its size'
test "$archive_size" -gt 0 || fail 'artifact archive is empty'
test "$archive_size" -le 2147483648 || fail 'artifact archive is too large'

part_size=$(((archive_size + parallelism - 1) / parallelism))
part_count=$(((archive_size + part_size - 1) / part_size))
for ((index = 0; index < part_count; index++)); do
  start=$((index * part_size))
  end=$((start + part_size - 1))
  if test "$end" -ge "$archive_size"; then
    end=$((archive_size - 1))
  fi
  expected_size=$((end - start + 1))
  part=$(printf '%s/part-%03d' "$work_dir" "$index")
  part_config="${part}.curl"
  {
    printf 'silent\nshow-error\nfail\nlocation\n'
    printf 'connect-timeout = 15\nmax-time = 1800\n'
    printf 'retry = 8\nretry-all-errors\n'
    printf 'resolve = "%s:443:%s"\n' "$artifact_host" "$artifact_ip"
    printf 'range = "%s-%s"\n' "$start" "$end"
    printf 'output = "%s"\n' "$part"
    printf 'url = "%s"\n' "$signed_url"
  } >"$part_config"
  chmod 600 "$part_config"
  (
    code=$(curl --config "$part_config" --write-out '%{http_code}')
    test "$code" = 206
    test "$(stat --format=%s "$part")" -eq "$expected_size"
  ) &
  pids+=("$!")
done

download_failed=0
for pid in "${pids[@]}"; do
  if ! wait "$pid"; then
    download_failed=1
  fi
done
pids=()
test "$download_failed" -eq 0 || fail 'one or more artifact ranges failed'

archive="$work_dir/artifact.zip"
: >"$archive"
for ((index = 0; index < part_count; index++)); do
  part=$(printf '%s/part-%03d' "$work_dir" "$index")
  cat "$part" >>"$archive"
done
test "$(stat --format=%s "$archive")" -eq "$archive_size"

if test -d "$output_dir"; then
  find "$output_dir" -mindepth 1 -delete
else
  install -d -m 700 "$output_dir"
fi
python3 - "$archive" "$output_dir" <<'PY'
import pathlib
import stat
import sys
import zipfile

archive = pathlib.Path(sys.argv[1])
target = pathlib.Path(sys.argv[2]).resolve()
with zipfile.ZipFile(archive) as bundle:
    for member in bundle.infolist():
        destination = (target / member.filename).resolve()
        if destination != target and target not in destination.parents:
            raise SystemExit('artifact contains an unsafe path')
        mode = (member.external_attr >> 16) & 0o170000
        if mode == stat.S_IFLNK:
            raise SystemExit('artifact contains a symbolic link')
    bundle.extractall(target)
PY
printf 'ACTIONS_ARTIFACT_DOWNLOADED bytes=%s parts=%s host=%s\n' \
  "$archive_size" "$part_count" "$artifact_host"
