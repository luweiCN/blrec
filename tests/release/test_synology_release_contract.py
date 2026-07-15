from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_synology_compose_pulls_one_pinned_public_image() -> None:
    compose = (ROOT / 'compose.synology.yml').read_text(encoding='utf8')
    assert 'build:' not in compose
    assert 'ghcr.io/luweicn/blrec:${BLREC_IMAGE_TAG:-3.0.0-beta.1}' in compose
    assert 'network_mode: host' in compose
    assert 'ports:' not in compose
    assert 'stop_grace_period: 2m' in compose
    for path in ('/cfg', '/log', '/rec'):
        assert path in compose


def test_environment_example_contains_no_credential() -> None:
    example = (ROOT / 'synology.env.example').read_text(encoding='utf8')
    assert 'BLREC_IMAGE_TAG=3.0.0-beta.1' in example
    assert 'BLREC_ADMIN_USERNAME=admin' in example
    assert 'BLREC_API_KEY=\n' in example
    assert 'BLREC_CREDENTIAL_KEY=' not in example


def test_synology_documentation_has_install_upgrade_and_rollback() -> None:
    document = (ROOT / 'docs/operations/synology-multi-network.md').read_text(
        encoding='utf8'
    )
    for heading in ('## 首次安装', '## 升级', '## 回滚', '## 日志与验收'):
        assert heading in document
    assert 'openssl rand -hex 32' in document
    assert 'openssl rand -base64 32' in document
