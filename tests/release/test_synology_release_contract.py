from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_synology_compose_pulls_one_pinned_public_image() -> None:
    compose = (ROOT / 'compose.synology.yml').read_text(encoding='utf8')
    assert 'build:' not in compose
    assert 'ghcr.io/luweicn/blrec:${BLREC_IMAGE_TAG:-3.0.0-beta.18}' in compose
    assert 'container_name: blrec-next' in compose
    assert 'network_mode: host' in compose
    assert 'ports:' not in compose
    assert 'stop_grace_period: 2m' in compose
    for path in ('/cfg', '/log', '/rec', '/clips'):
        assert path in compose


def test_environment_example_contains_no_credential() -> None:
    example = (ROOT / 'synology.env.example').read_text(encoding='utf8')
    assert 'BLREC_IMAGE_TAG=3.0.0-beta.18' in example
    assert 'BLREC_ADMIN_USERNAME=admin' in example
    assert 'BLREC_API_KEY=\n' in example
    assert 'BLREC_CREDENTIAL_KEY=' not in example
    for directory in ('config', 'log', 'rec', 'clips'):
        assert f'/volume1/docker/blrec-next/{directory}' in example


def test_synology_documentation_has_install_upgrade_and_rollback() -> None:
    document = (ROOT / 'docs/operations/synology-multi-network.md').read_text(
        encoding='utf8'
    )
    for heading in ('## 首次安装', '## 升级', '## 回滚', '## 日志与验收'):
        assert heading in document
    assert 'openssl rand -hex 32' in document
    assert 'openssl rand -base64 32' in document


def test_first_install_preserves_credentials_and_secures_environment() -> None:
    document = (ROOT / 'docs/operations/synology-multi-network.md').read_text(
        encoding='utf8'
    )
    install = document.split('## 首次安装', 1)[1].split('## 升级', 1)[0]
    key_guard = 'if [ -e "$credential_key" ]; then'
    generate_key = (
        'openssl rand -base64 32 > ' '/volume1/docker/blrec-next/config/credential.key'
    )
    assert 'set -eu' in install
    assert key_guard in install
    assert install.index(key_guard) < install.index(generate_key)
    assert 'test -s "$credential_key"' in install
    assert 'chmod 600 .env' in install


def test_upgrade_stops_on_error_and_verifies_secure_backups() -> None:
    document = (ROOT / 'docs/operations/synology-multi-network.md').read_text(
        encoding='utf8'
    )
    upgrade = document.split('## 升级', 1)[1].split('## 回滚', 1)[0]
    required = (
        'set -eu',
        'backup_config_dir="${config_dir}.backup-${backup_id}"',
        'backup_env=".env.backup-${backup_id}"',
        'cp -a "$config_dir" "$backup_config_dir"',
        'cp .env "$backup_env"',
        'chmod 600 "$backup_env"',
        'test -d "$backup_config_dir"',
        'test -s "$backup_config_dir/credential.key"',
        'test -s "$backup_env"',
        'cmp -s .env "$backup_env"',
    )
    for command in required:
        assert command in upgrade
    assert upgrade.index('set -eu') < upgrade.index(
        'docker compose --env-file .env -f compose.synology.yml stop'
    )
    assert upgrade.index('cp .env "$backup_env"') < upgrade.index(
        'test -s "$backup_env"'
    )
    assert '必须使用目标版本仓库中的 `compose.synology.yml` 替换旧文件' in upgrade
    assert 'clip_dir=/volume1/docker/blrec-next/clips' in upgrade
    assert 'mkdir -p "$clip_dir"' in upgrade
    assert 'test -d "$clip_dir"' in upgrade
    assert 'BLREC_CLIP_DIR' in upgrade


def test_rollback_validates_and_stages_restore_before_replacing_config() -> None:
    document = (ROOT / 'docs/operations/synology-multi-network.md').read_text(
        encoding='utf8'
    )
    rollback = document.split('## 回滚', 1)[1].split('## 日志与验收', 1)[0]
    required = (
        'set -eu',
        'backup_config_dir="${config_dir}.backup-${backup_id}"',
        'backup_env=".env.backup-${backup_id}"',
        'restore_candidate="${config_dir}.restore-${backup_id}"',
        'test -d "$backup_config_dir"',
        'test -s "$backup_config_dir/credential.key"',
        'test -s "$backup_env"',
        "grep -Eq '^BLREC_IMAGE_TAG=[^[:space:]]+$' \"$backup_env\"",
        'docker compose --env-file "$backup_env" '
        '-f compose.synology.yml config >/dev/null',
        'docker compose --env-file "$backup_env" ' '-f compose.synology.yml pull',
        'cp -a "$backup_config_dir" "$restore_candidate"',
        'test -s "$restore_candidate/credential.key"',
        'mv "$config_dir" "${config_dir}.failed-${failed_id}"',
        'mv "$restore_candidate" "$config_dir"',
        'cp "$backup_env" .env',
        'chmod 600 .env',
    )
    for command in required:
        assert command in rollback
    pull = rollback.index(
        'docker compose --env-file "$backup_env" ' '-f compose.synology.yml pull'
    )
    replace = rollback.index('mv "$config_dir" "${config_dir}.failed-${failed_id}"')
    assert rollback.index('set -eu') < pull < replace
    assert rollback.index('test -s "$restore_candidate/credential.key"') < replace


def test_readme_uses_only_the_verified_compose_installation_flow() -> None:
    readme = (ROOT / 'README.md').read_text(encoding='utf8')
    assert 'bili2233' not in readme
    assert '--api-key' not in readme
    assert 'docker run' not in readme
    assert '[群晖双网络部署](docs/operations/synology-multi-network.md)' in readme
