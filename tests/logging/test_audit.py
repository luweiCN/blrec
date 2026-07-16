import json

from blrec.logging.audit import safe_audit_payload


def test_audit_payload_redacts_secret_fields_recursively() -> None:
    payload = json.loads(
        safe_audit_payload(
            'account_checked',
            {
                'account_id': 7,
                'access_token': 'token-value',
                'headers': {'Cookie': 'cookie-value', 'User-Agent': 'BLREC'},
            },
        )
    )

    assert payload == {
        'event': 'account_checked',
        'account_id': 7,
        'access_token': '[REDACTED]',
        'headers': {'Cookie': '[REDACTED]', 'User-Agent': 'BLREC'},
    }
    assert 'token-value' not in str(payload)
    assert 'cookie-value' not in str(payload)
