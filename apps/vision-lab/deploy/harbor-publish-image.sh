#!/usr/bin/env bash
set -euo pipefail

readonly registry='www.luwei.space:4008'
readonly image="${registry}/blrec/blrec-vision-lab"

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

auth_dir="/tmp/blrec-harbor-auth-${suffix}"

case "$action" in
  prepare)
    require_parts 3
    rm -rf -- "$auth_dir"
    install -d -m 700 -- "$auth_dir"
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
    require_parts 5
    release_tag=${parts[3]}
    image_size=${parts[4]}
    [[ "$release_tag" =~ ^vision-lab-v[0-9]+\.[0-9]+\.[0-9]+$ ]] || \
      fail 'invalid release tag'
    [[ "$image_size" =~ ^[0-9]+$ ]] || fail 'invalid image size'
    test "$image_size" -gt 0 || fail 'image artifact is empty'
    test "$image_size" -le 2147483648 || fail 'image artifact is too large'
    test -s "$auth_dir/config.json" || fail 'registry authentication is missing'
    export DOCKER_CONFIG="$auth_dir"

    head -c "$image_size" | /usr/bin/docker load
    test "$(/usr/bin/docker image inspect --format '{{.Architecture}}' "$image:$release_tag")" = amd64
    test "$(/usr/bin/docker image inspect --format '{{.Config.Healthcheck.Timeout}}' "$image:$release_tag")" = 45s
    /usr/bin/docker tag "$image:$release_tag" "$image:latest"
    /usr/bin/docker push "$image:$release_tag"
    /usr/bin/docker push "$image:latest"

    printf 'HARBOR_IMAGE_PUBLISHED image=%s tag=%s architecture=amd64\n' \
      "$image" "$release_tag"
    ;;

  cleanup)
    require_parts 3
    rm -rf -- "/tmp/blrec-vision-build-${suffix}" "$auth_dir"
    ;;

  *)
    fail 'action is not allowed'
    ;;
esac
