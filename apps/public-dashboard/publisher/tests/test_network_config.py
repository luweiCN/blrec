from pathlib import Path

from blrec.networking.config import load_network_settings


def test_publisher_inherits_legacy_upload_route(tmp_path: Path) -> None:
    settings_path = tmp_path / 'settings.toml'
    settings_path.write_text(
        '[network.upload]\n'
        'mode = "fixed"\n'
        'interface = "lan2"\n'
        'failover_enabled = false\n',
        encoding='utf8',
    )

    settings = load_network_settings(settings_path)

    assert settings.dashboard_publish.interface == 'lan2'
    assert settings.dashboard_publish.mode == 'fixed'
    assert settings.dashboard_publish.failover_enabled is False
