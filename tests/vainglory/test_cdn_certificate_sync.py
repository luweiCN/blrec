import importlib.util
import sys
from pathlib import Path
from typing import List, Sequence

import pytest

SCRIPT_PATH = (
    Path(__file__).parents[2]
    / 'public-dashboard'
    / 'deploy'
    / 'aliyun'
    / 'sync_cdn_certificate.py'
)
SPEC = importlib.util.spec_from_file_location('cdn_certificate_sync', SCRIPT_PATH)
assert SPEC is not None
assert SPEC.loader is not None
cdn_certificate_sync = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = cdn_certificate_sync
SPEC.loader.exec_module(cdn_certificate_sync)


CERTIFICATE_ONE = '''-----BEGIN CERTIFICATE-----
AQID
-----END CERTIFICATE-----
'''
CERTIFICATE_TWO = '''-----BEGIN CERTIFICATE-----
BAUG
-----END CERTIFICATE-----
'''


def local_certificate(public_pem: str = CERTIFICATE_ONE):
    return cdn_certificate_sync.LocalCertificate(
        public_pem=public_pem,
        private_key_pem='private-key',
        fingerprint=cdn_certificate_sync.certificate_fingerprint(public_pem),
    )


class FakeClient:
    def __init__(self, states: Sequence[object]) -> None:
        self.states = list(states)
        self.uploads = []

    def describe(self, domain: str):
        if len(self.states) > 1:
            return self.states.pop(0)
        return self.states[0]

    def upload(
        self, domain: str, certificate_name: str, public_pem: str, private_key_pem: str
    ) -> None:
        self.uploads.append((domain, certificate_name, public_pem, private_key_pem))


def remote_certificate(public_pem=None, enabled: bool = False):
    return cdn_certificate_sync.RemoteCertificate(
        public_pem=public_pem, enabled=enabled
    )


def test_load_local_certificate_checks_domain_expiry_and_key_match(tmp_path) -> None:
    certificate_path = tmp_path / 'fullchain.cer'
    private_key_path = tmp_path / 'private.key'
    certificate_path.write_text(CERTIFICATE_ONE, encoding='utf-8')
    private_key_path.write_text('private-key', encoding='utf-8')
    commands: List[Sequence[str]] = []

    def run(command: Sequence[str]) -> bytes:
        commands.append(command)
        if '-pubkey' in command or '-pubout' in command:
            return b'public-key'
        return b''

    loaded = cdn_certificate_sync.load_local_certificate(
        certificate_path, private_key_path, 'vg.luwei.host', 14, command_runner=run
    )

    assert loaded.public_pem == CERTIFICATE_ONE
    assert loaded.private_key_pem == 'private-key'
    assert any('-checkhost' in command for command in commands)
    assert any('-checkend' in command for command in commands)
    assert any('1209600' in command for command in commands)


def test_load_local_certificate_rejects_mismatched_private_key(tmp_path) -> None:
    certificate_path = tmp_path / 'fullchain.cer'
    private_key_path = tmp_path / 'private.key'
    certificate_path.write_text(CERTIFICATE_ONE, encoding='utf-8')
    private_key_path.write_text('private-key', encoding='utf-8')

    def run(command: Sequence[str]) -> bytes:
        if '-pubkey' in command:
            return b'certificate-key'
        if '-pubout' in command:
            return b'private-key'
        return b''

    with pytest.raises(
        cdn_certificate_sync.CertificateSyncError, match='证书和私钥不匹配'
    ):
        cdn_certificate_sync.load_local_certificate(
            certificate_path, private_key_path, 'vg.luwei.host', 14, command_runner=run
        )


def test_synchronize_skips_an_active_matching_certificate() -> None:
    local = local_certificate()
    client = FakeClient((remote_certificate(CERTIFICATE_ONE, True),))

    changed = cdn_certificate_sync.synchronize_certificate(
        client, 'vg.luwei.host', local, 2, 0
    )

    assert changed is False
    assert client.uploads == []


def test_synchronize_uploads_changed_certificate_and_waits_for_activation() -> None:
    local = local_certificate()
    client = FakeClient(
        (
            remote_certificate(CERTIFICATE_TWO, True),
            remote_certificate(None, False),
            remote_certificate(CERTIFICATE_ONE, True),
        )
    )
    sleeps = []

    changed = cdn_certificate_sync.synchronize_certificate(
        client, 'vg.luwei.host', local, 2, 3, sleeper=sleeps.append
    )

    assert changed is True
    assert client.uploads == [
        (
            'vg.luwei.host',
            'vg-luwei-host-{}'.format(local.fingerprint[:12]),
            CERTIFICATE_ONE,
            'private-key',
        )
    ]
    assert sleeps == [3]


def test_synchronize_reenables_a_matching_but_disabled_certificate() -> None:
    local = local_certificate()
    client = FakeClient(
        (
            remote_certificate(CERTIFICATE_ONE, False),
            remote_certificate(CERTIFICATE_ONE, True),
        )
    )

    changed = cdn_certificate_sync.synchronize_certificate(
        client, 'vg.luwei.host', local, 1, 0
    )

    assert changed is True
    assert len(client.uploads) == 1


def test_synchronize_fails_when_cdn_never_reports_the_new_certificate() -> None:
    local = local_certificate()
    client = FakeClient(
        (
            remote_certificate(CERTIFICATE_TWO, True),
            remote_certificate(CERTIFICATE_TWO, True),
        )
    )

    with pytest.raises(
        cdn_certificate_sync.CertificateSyncError, match='未在等待时间内'
    ):
        cdn_certificate_sync.synchronize_certificate(
            client, 'vg.luwei.host', local, 2, 0
        )
