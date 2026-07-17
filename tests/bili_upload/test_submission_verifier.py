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
    assert result.differences == {'is_only_self': {'expected': True, 'actual': False}}
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


def test_repost_description_accepts_bilibili_source_prefix() -> None:
    expected = snapshot()
    expected.update({'copyright': 2, 'source': 'https://live.bilibili.com/100'})
    response = detail()
    response['data']['archive'].update(
        {
            'copyright': 2,
            'source': 'https://live.bilibili.com/100',
            'desc': 'https://live.bilibili.com/100\n主播：测试主播',
        }
    )

    result = verify_submission(expected, response, scheduled_publish_at=10_000)

    assert result.state == 'passed'
    assert result.mismatches == ()


def test_repost_description_accepts_source_as_the_whole_empty_description() -> None:
    expected = snapshot()
    expected.update(
        {'copyright': 2, 'source': 'https://live.bilibili.com/100', 'description': ''}
    )
    response = detail()
    response['data']['archive'].update(
        {
            'copyright': 2,
            'source': 'https://live.bilibili.com/100',
            'desc': 'https://live.bilibili.com/100',
        }
    )

    result = verify_submission(expected, response, scheduled_publish_at=10_000)

    assert result.state == 'passed'
    assert result.mismatches == ()


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


def test_cover_and_collection_are_explicitly_unverifiable_without_extra_reads() -> None:
    expected = snapshot()
    expected.update(
        {
            'cover_mode': 'custom',
            'cover_asset_id': 7,
            'collection_season_id': 88,
            'collection_section_id': 99,
        }
    )

    result = verify_submission(expected, detail(), scheduled_publish_at=10_000)

    assert result.state == 'partial'
    assert result.unverifiable == ('cover', 'collection')


def test_empty_snapshot_cannot_be_reported_as_verified() -> None:
    result = verify_submission({}, detail())

    assert result.state == 'failed'
    assert result.error == 'policy snapshot has no verifiable fields'


def test_part_titles_are_compared_as_the_submitted_80_character_value() -> None:
    expected = snapshot()
    expected['part_titles'] = ['分' * 90, '第二段']
    response = detail()
    response['data']['videos'][0]['title'] = '分' * 80

    result = verify_submission(expected, response, scheduled_publish_at=10_000)

    assert result.state == 'passed'
    assert result.mismatches == ()
