from __future__ import annotations

from pathlib import Path
from unittest import mock

import pytest

from blrec.networking import postgres_tunnel
from blrec.networking.manager import RouteSelection
from blrec.networking.postgres_tunnel import PostgresTunnelSettings


def _environment(tmp_path: Path) -> dict[str, str]:
    key = tmp_path / 'database-key'
    key.write_text('private', encoding='utf8')
    key.chmod(0o600)
    known_hosts = tmp_path / 'known-hosts'
    known_hosts.write_text('host key', encoding='utf8')
    settings = tmp_path / 'settings.toml'
    settings.write_text('[network.database]\nmode="fixed"\n', encoding='utf8')
    return {
        'BLREC_DATABASE_SSH_HOST': '203.0.113.20',
        'BLREC_DATABASE_SSH_USER': 'blrec-db-tunnel',
        'BLREC_DATABASE_SSH_KEY_PATH': str(key),
        'BLREC_DATABASE_SSH_KNOWN_HOSTS_PATH': str(known_hosts),
        'BLREC_DEFAULT_SETTINGS_FILE': str(settings),
    }


def test_tunnel_requires_a_fixed_ip(tmp_path: Path) -> None:
    environment = _environment(tmp_path)
    environment['BLREC_DATABASE_SSH_HOST'] = 'database.example.com'

    with pytest.raises(ValueError, match='固定 IP'):
        PostgresTunnelSettings.from_environment(environment)


def test_tunnel_requires_an_ipv4_address(tmp_path: Path) -> None:
    environment = _environment(tmp_path)
    environment['BLREC_DATABASE_SSH_HOST'] = '2001:db8::1'

    with pytest.raises(ValueError, match='固定 IP'):
        PostgresTunnelSettings.from_environment(environment)


def test_tunnel_rejects_a_readable_private_key(tmp_path: Path) -> None:
    environment = _environment(tmp_path)
    Path(environment['BLREC_DATABASE_SSH_KEY_PATH']).chmod(0o644)

    with pytest.raises(ValueError, match='0600'):
        PostgresTunnelSettings.from_environment(environment)


def test_tunnel_command_binds_the_selected_source_address(tmp_path: Path) -> None:
    settings = PostgresTunnelSettings.from_environment(_environment(tmp_path))
    selection = RouteSelection(
        purpose='database',
        interface_name='eth1',
        source_address='192.168.50.24',
        role='primary',
    )

    command = settings.command(selection)

    assert command[-3:] == ('-b', '192.168.50.24', 'blrec-db-tunnel@203.0.113.20')
    assert '127.0.0.1:15432:127.0.0.1:5432' in command
    assert 'StrictHostKeyChecking=yes' in command


def test_unrelated_settings_write_does_not_restart_tunnel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = PostgresTunnelSettings.from_environment(_environment(tmp_path))
    selection = RouteSelection(
        purpose='database',
        interface_name='eth1',
        source_address='192.168.50.24',
        role='primary',
    )
    original_mtime = settings.network_settings_file.stat().st_mtime_ns

    class FakeEvent:
        waits = 0
        stopped = False

        def is_set(self) -> bool:
            return self.stopped

        def set(self) -> None:
            self.stopped = True

        def wait(self, _seconds: float) -> bool:
            self.waits += 1
            if self.waits == 1:
                settings.network_settings_file.touch()
                settings.network_settings_file.write_text(
                    '[network.database]\nmode="fixed"\n# unrelated write\n',
                    encoding='utf8',
                )
                settings.network_settings_file.touch()
                assert (
                    settings.network_settings_file.stat().st_mtime_ns != original_mtime
                )
                return False
            self.stopped = True
            return True

    process = mock.Mock()
    process.poll.return_value = None
    monkeypatch.setattr(
        postgres_tunnel.PostgresTunnelSettings,
        'from_environment',
        classmethod(lambda _cls, _environment: settings),
    )
    monkeypatch.setattr(postgres_tunnel, '_selection', lambda _settings: selection)
    popen = mock.Mock(return_value=process)
    monkeypatch.setattr(postgres_tunnel.subprocess, 'Popen', popen)
    monkeypatch.setattr(postgres_tunnel.threading, 'Event', FakeEvent)
    monkeypatch.setattr(postgres_tunnel.signal, 'signal', mock.Mock())

    assert postgres_tunnel.run({}) == 0
    assert popen.call_count == 1
