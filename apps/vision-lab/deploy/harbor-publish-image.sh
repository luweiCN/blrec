#!/usr/bin/env bash
set -euo pipefail

readonly registry='www.luwei.space:4008'
readonly image="${registry}/blrec/blrec-vision-lab"
readonly base_image="${registry}/blrec/python-base:3.12-slim-bookworm"

fail() {
  printf 'ERROR: %s\n' "$*" >&2
  exit 1
}

require_parts() {
  local expected=$1
  test "${#parts[@]}" -eq "$expected" || fail 'invalid command arguments'
}

original_command=${SSH_ORIGINAL_COMMAND:-}
read -r -a parts <<<"$original_command"
test "${parts[0]:-}" = 'blrec-vision-publish' || fail 'command is not allowed'
test -n "${parts[1]:-}" || fail 'action is required'

action=${parts[1]}
suffix=${parts[2]:-}
[[ "$suffix" =~ ^[0-9]+-[0-9]+$ ]] || fail 'invalid run suffix'

context="/tmp/blrec-vision-build-${suffix}"
auth_dir="/tmp/blrec-harbor-auth-${suffix}"

case "$action" in
  prepare)
    require_parts 3
    rm -rf -- "$context" "$auth_dir"
    install -d -m 700 -- "$context" "$auth_dir"
    ;;

  receive-context)
    require_parts 3
    test -d "$context" || fail 'build context is not prepared'
    tar --extract --file=- --directory="$context" --no-same-owner --no-same-permissions
    test -f "$context/Dockerfile" || fail 'Dockerfile is missing'
    test -f "$context/pyproject.toml" || fail 'pyproject.toml is missing'
    ;;

  receive-auth)
    require_parts 3
    test -d "$auth_dir" || fail 'authentication directory is not prepared'
    umask 077
    config="$(dd bs=16385 count=1 status=none)"
    test "${#config}" -lt 16385 || fail 'authentication payload is too large'
    printf '%s' "$config" >"$auth_dir/config.json.tmp"
    python3 -c '
import json
import sys

path, registry = sys.argv[1:]
with open(path, encoding="utf8") as stream:
    config = json.load(stream)
auths = config.get("auths")
if not isinstance(auths, dict) or set(auths) != {registry}:
    raise SystemExit("unexpected registry authentication")
auth = auths[registry].get("auth")
if not isinstance(auth, str) or not auth:
    raise SystemExit("registry authentication is missing")
' "$auth_dir/config.json.tmp" "$registry"
    mv -- "$auth_dir/config.json.tmp" "$auth_dir/config.json"
    ;;

  publish)
    require_parts 4
    release_tag=${parts[3]}
    [[ "$release_tag" =~ ^vision-lab-v[0-9]+\.[0-9]+\.[0-9]+$ ]] || \
      fail 'invalid release tag'
    test -f "$context/Dockerfile" || fail 'build context is incomplete'
    test -s "$auth_dir/config.json" || fail 'registry authentication is missing'
    export DOCKER_CONFIG="$auth_dir"

    /usr/bin/docker pull --platform linux/amd64 "$base_image"

    cache_args=()
    if /usr/bin/docker image inspect "$image:$release_tag" >/dev/null 2>&1; then
      cache_args=(--cache-from "$image:$release_tag")
    fi

    DOCKER_BUILDKIT=1 /usr/bin/docker build \
      --platform linux/amd64 \
      --pull=false \
      --build-arg "PYTHON_BASE_IMAGE=$base_image" \
      "${cache_args[@]}" \
      --tag "$image:$release_tag" \
      --file "$context/Dockerfile" \
      "$context"
    /usr/bin/docker tag "$image:$release_tag" "$image:latest"
    /usr/bin/docker push "$image:$release_tag"
    /usr/bin/docker push "$image:latest"

    test "$(/usr/bin/docker image inspect --format '{{.Architecture}}' "$image:$release_tag")" = amd64
    test "$(/usr/bin/docker image inspect --format '{{.Config.Healthcheck.Timeout}}' "$image:$release_tag")" = 45s
    printf 'HARBOR_IMAGE_PUBLISHED image=%s tag=%s architecture=amd64\n' \
      "$image" "$release_tag"
    ;;

  cleanup)
    require_parts 3
    rm -rf -- "$context" "$auth_dir"
    ;;

  *)
    fail 'action is not allowed'
    ;;
esac
