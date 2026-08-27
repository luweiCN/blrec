from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_dockerfile_builds_frontend_wheel_and_runtime_separately() -> None:
    dockerfile = (ROOT / 'apps/blrec-server/Dockerfile').read_text(encoding='utf8')
    assert 'AS webapp-builder' in dockerfile
    assert 'AS wheel-builder' in dockerfile
    assert 'AS runtime' in dockerfile
    assert 'npm ci' in dockerfile
    assert 'npm run build' in dockerfile
    assert 'pip3 install --no-cache-dir -e .' not in dockerfile
    assert 'scripts/migrate_legacy_settings.py' in dockerfile
    assert 'scripts/migrate_biliupforjava_rooms.py' in dockerfile
    assert 'scripts/backup_blrec_database.py' in dockerfile
    assert 'scripts/migrate_blrec_postgres_schema.py' in dockerfile
    assert 'postgresql-client-16' in dockerfile
    assert 'scripts/vainglory_reanalysis_recovery_20260812.py' in dockerfile
    assert 'scripts/vainglory_reanalysis_recovery_20260812.jsonl' in dockerfile
    assert 'scripts/vainglory_tail_reanalysis_20260812.py' in dockerfile
    assert 'HEALTHCHECK' in dockerfile
    assert '/api/v1/auth/status' in dockerfile
    assert '"/favorites"' in dockerfile


def test_dockerfile_builds_native_dependency_wheels_outside_runtime() -> None:
    dockerfile = (ROOT / 'apps/blrec-server/Dockerfile').read_text(encoding='utf8')
    builder, runtime = dockerfile.split(
        'FROM python:3.11-slim-bookworm AS runtime', maxsplit=1
    )

    assert 'AS dependency-builder' in builder
    assert 'gcc libc6-dev' in builder
    assert '--wheel-dir /dependency-wheels' in builder
    assert 'from=dependency-builder,source=/dependency-wheels' in runtime
    assert '--no-index' in runtime
    assert '--find-links=/dependency-wheels' in runtime
    assert 'gcc libc6-dev' not in runtime


def test_synology_compose_persists_sibling_favorites_directory() -> None:
    compose = (ROOT / 'compose.synology.yml').read_text(encoding='utf8')
    environment = (ROOT / 'synology.env.example').read_text(encoding='utf8')
    assert 'BLREC_FAVORITES_DIR' in compose
    assert ':/favorites' in compose
    assert 'BLREC_FAVORITES_DIR=/volume1/docker/blrec-next/favorites' in environment


def test_docker_context_excludes_local_and_generated_state() -> None:
    ignored = (ROOT / '.dockerignore').read_text(encoding='utf8')
    for value in (
        '.git',
        '.venv',
        'apps/blrec-server/webapp/node_modules',
        'apps/browser-extension',
        'apps/vision-lab',
        'apps/analysis-worker',
        'apps/public-dashboard/node_modules',
        'apps/public-dashboard/dist',
        'apps/public-dashboard/public-data',
        'apps/public-dashboard/src',
        'src/blrec/data/webapp',
    ):
        assert value in ignored


def test_smoke_script_uses_ephemeral_credentials_and_cleans_up() -> None:
    script = (ROOT / 'scripts/docker-smoke.sh').read_text(encoding='utf8')
    assert 'mktemp -d' in script
    assert 'trap cleanup EXIT' in script
    assert 'BLREC_CREDENTIAL_KEY_FILE=/cfg/credential.key' in script
    assert '/api/v1/auth/status' in script


def test_vision_lab_healthcheck_allows_slow_synology_python_startup() -> None:
    dockerfile = (ROOT / 'apps/vision-lab/Dockerfile').read_text(encoding='utf8')
    compose = (ROOT / 'apps/vision-lab/deploy/nas/compose.yml').read_text(
        encoding='utf8'
    )

    assert 'HEALTHCHECK --interval=30s --timeout=45s' in dockerfile
    assert 'timeout: 45s' in compose
