from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_dockerfile_builds_frontend_wheel_and_runtime_separately() -> None:
    dockerfile = (ROOT / 'Dockerfile').read_text(encoding='utf8')
    assert 'AS webapp-builder' in dockerfile
    assert 'AS wheel-builder' in dockerfile
    assert 'AS runtime' in dockerfile
    assert 'npm ci' in dockerfile
    assert 'npm run build' in dockerfile
    assert 'pip3 install --no-cache-dir -e .' not in dockerfile
    assert 'HEALTHCHECK' in dockerfile
    assert '/api/v1/auth/status' in dockerfile


def test_docker_context_excludes_local_and_generated_state() -> None:
    ignored = (ROOT / '.dockerignore').read_text(encoding='utf8')
    for value in ('.git', '.venv', 'webapp/node_modules', 'src/blrec/data/webapp'):
        assert value in ignored


def test_smoke_script_uses_ephemeral_credentials_and_cleans_up() -> None:
    script = (ROOT / 'scripts/docker-smoke.sh').read_text(encoding='utf8')
    assert 'mktemp -d' in script
    assert 'trap cleanup EXIT' in script
    assert 'BLREC_CREDENTIAL_KEY_FILE=/cfg/credential.key' in script
    assert '/api/v1/auth/status' in script
