import json

from loguru import logger

from blrec.logging.audit import audit, safe_audit_payload


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
        'result': 'observed',
        'account_id': 7,
        'access_token': '[REDACTED]',
        'headers': {'Cookie': '[REDACTED]', 'User-Agent': 'BLREC'},
    }
    assert 'token-value' not in str(payload)
    assert 'cookie-value' not in str(payload)


def test_audit_log_has_a_searchable_prefix_and_explicit_result() -> None:
    messages = []
    sink = logger.add(messages.append, format='{message}')
    try:
        audit('network_selected', interface='eth0', result='selected')
    finally:
        logger.remove(sink)

    message = ''.join(str(item) for item in messages)
    assert message.startswith('[audit] ')
    assert '"event":"network_selected"' in message
    assert '"result":"selected"' in message
