#!/usr/bin/env bash
set -euo pipefail

image="${1:?usage: scripts/docker-smoke.sh IMAGE}"
root="$(mktemp -d)"
container="blrec-smoke-${RANDOM}-${RANDOM}"

cleanup() {
  docker rm -f "$container" >/dev/null 2>&1 || true
  rm -rf "$root"
}
trap cleanup EXIT

mkdir -p "$root/cfg" "$root/log" "$root/rec" "$root/clips"
openssl rand -base64 32 > "$root/cfg/credential.key"
chmod 600 "$root/cfg/credential.key"

docker run -d --name "$container" \
  -p 127.0.0.1::2233 \
  -e BLREC_ADMIN_USERNAME=admin \
  -e BLREC_API_KEY=smoke-test-initialization-key \
  -e BLREC_CREDENTIAL_KEY_FILE=/cfg/credential.key \
  -v "$root/cfg:/cfg" \
  -v "$root/log:/log" \
  -v "$root/rec:/rec" \
  -v "$root/clips:/clips" \
  "$image" >/dev/null

port="$(docker port "$container" 2233/tcp | head -1 | awk -F: '{print $NF}')"
for _ in $(seq 1 90); do
  response="$(curl -fsS "http://127.0.0.1:${port}/api/v1/auth/status" 2>/dev/null || true)"
  if [[ "$response" == *'"setupRequired":true'* ]]; then
    docker inspect --format '{{.State.Status}}' "$container" | grep -qx running
    exit 0
  fi
  sleep 1
done

docker logs "$container"
exit 1
