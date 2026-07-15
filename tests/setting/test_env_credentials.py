import pytest
from pydantic import ValidationError

from blrec.cli import main as cli_main
from blrec.setting.models import EnvSettings


def test_admin_username_defaults_to_admin(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv('BLREC_ADMIN_USERNAME', raising=False)

    assert EnvSettings().admin_username == 'admin'


@pytest.mark.parametrize('value', (' admin ', 'admin\n', ''))
def test_admin_username_rejects_ambiguous_values(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv('BLREC_ADMIN_USERNAME', value)

    with pytest.raises(ValidationError, match='administrator username'):
        EnvSettings()


def test_forwarded_allow_ips_defaults_to_loopback_and_accepts_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv('BLREC_FORWARDED_ALLOW_IPS', raising=False)

    assert cli_main.forwarded_allow_ips() == '127.0.0.1'

    monkeypatch.setenv('BLREC_FORWARDED_ALLOW_IPS', '172.17.0.1')
    assert cli_main.forwarded_allow_ips() == '172.17.0.1'
