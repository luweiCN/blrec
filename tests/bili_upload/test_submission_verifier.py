from blrec.bili_upload.submission_verifier import verify_submission


def snapshot() -> dict:
    return {
        'format_version': 4,
        'title': '测试直播 录播',
        'description': '主播：测试主播',
        'part_titles': ['第一段', '第二段'],
        'tid': 17,
        'tags': '直播,录播',
        'copyright': 1,
        'source': '',
        'is_only_self': True,
        'publish_dynamic': False,
        'no_reprint': True,
        'up_selection_reply': True,
        'up_close_reply': False,
        'up_close_danmu': False,
        'creation_statement_id': -1,
        'original_authorization': True,
    }


def detail() -> dict:
    return {
        'code': 0,
        'data': {
            'archive': {
                'title': '测试直播 录播',
                'desc': '主播：测试主播',
                'tid': 17,
                'tag': '录播,直播',
                'copyright': 1,
                'is_only_self': 1,
                'no_disturbance': 1,
                'no_reprint': 1,
                'up_selection_reply': True,
                'up_close_reply': False,
                'up_close_danmu': False,
                'creation_statement': {'id': -1},
                'dtime': 10_000,
            },
            'videos': [{'page': 1, 'title': '第一段'}, {'page': 2, 'title': '第二段'}],
        },
    }


def test_verification_passes_when_all_observable_submission_fields_match() -> None:
    result = verify_submission(snapshot(), detail(), scheduled_publish_at=10_000)

    assert result.state == 'passed'
    assert result.mismatches == ()
    assert result.missing == ()
    assert 'title' in result.checked
    assert 'part_titles' in result.checked


def test_verification_reports_differences_without_exposing_payload_values() -> None:
    response = detail()
    response['data']['archive']['is_only_self'] = 0

    result = verify_submission(snapshot(), response, scheduled_publish_at=10_000)

    assert result.state == 'different'
    assert result.mismatches == ('is_only_self',)
    assert '测试直播' not in result.to_json()


def test_verification_is_partial_when_remote_omits_an_observable_field() -> None:
    response = detail()
    del response['data']['archive']['up_selection_reply']

    result = verify_submission(snapshot(), response, scheduled_publish_at=10_000)

    assert result.state == 'partial'
    assert result.mismatches == ()
    assert result.missing == ('up_selection_reply',)


def test_verification_accepts_no_disturbance_zero_when_dynamic_is_enabled() -> None:
    expected = snapshot()
    expected['publish_dynamic'] = True
    response = detail()
    response['data']['archive']['no_disturbance'] = 0

    result = verify_submission(expected, response, scheduled_publish_at=10_000)

    assert result.state == 'passed'


def test_legacy_snapshot_checks_only_fields_that_were_persisted() -> None:
    expected = {
        'format_version': 1,
        'title': '旧任务',
        'tid': 17,
        'tags': '录播',
        'copyright': 1,
        'part_titles': ['P1'],
    }
    response = {
        'data': {
            'archive': {
                'title': '旧任务',
                'tid': 17,
                'tag': '录播',
                'copyright': 1,
                'is_only_self': 0,
            },
            'videos': [{'page': 1, 'title': 'P1'}],
        }
    }

    result = verify_submission(expected, response)

    assert result.state == 'passed'
    assert 'is_only_self' not in result.checked
    assert result.missing == ()
