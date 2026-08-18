"""NAS 控制面不得直接执行视频处理或旧模型批量推理。"""

from pathlib import Path
from unittest import mock

from fastapi import HTTPException
from labeler import config, server


def test_control_plane_rejects_local_heavy_operations(monkeypatch) -> None:
    monkeypatch.setattr(config, 'CONTROL_PLANE_ONLY', True)

    for operation in (
        lambda: server.api_sync(),
        lambda: server.api_extract({'video_ids': [1]}),
        lambda: server.api_live_frame({'video_id': 1}),
        lambda: server.api_download_video(1),
        lambda: server.api_live_frame_local({'video_id': 1}),
        lambda: server.api_collect_bp_review({}),
        lambda: server.api_model_test('legacy-model', {'frame_id': 1}),
    ):
        try:
            operation()
        except HTTPException as error:
            assert error.status_code == 409
            assert 'Vision Worker' in str(error.detail)
        else:
            raise AssertionError('NAS 控制面不应直接执行重任务')


def test_control_plane_uses_automatic_candidate_index_ui() -> None:
    html = (
        Path(__file__).resolve().parent.parent / 'labeler/static/index.html'
    ).read_text(encoding='utf-8')

    assert 'btn-candidate-sync' not in html
    assert 'Worker 待复核' in html
    assert 'candidate-streamer-filter' in html
    assert 'candidate-hero-filter-options' in html
    assert 'btn-candidate-material-suggestions' in html
    assert 'candidate-material-dialog' in html

    script = (
        Path(__file__).resolve().parent.parent / 'labeler/static/app.js'
    ).read_text(encoding='utf-8')
    assert "limit: '20'" in script
    assert "include_stats: 'false'" in script
    assert '/api/training-review/stats?' in script
    assert 'const candidatePrefillRequests = new Map();' in script
    assert 'function prefetchCandidateImage(item)' in script
    assert 'function prefetchNextCandidate()' in script
    assert 'completeCandidateHeroLineupPrefetch' in script
    assert 'prefetchNextCandidate();' in script
    assert 'function applyCandidateMaterialSuggestion(suggestion)' in script
    assert 'renderCandidateMaterialSuggestions();' in script


def test_single_item_prefill_does_not_rebuild_all_result_groups(monkeypatch) -> None:
    connection = mock.Mock()
    item = {'frame_id': 7, 'sources': []}
    monkeypatch.setattr(config, 'CONTROL_PLANE_ONLY', True)
    monkeypatch.setattr(server, '_conn', mock.Mock(return_value=connection))
    monkeypatch.setitem(server._training_review_cache, 'groups', None)
    get_item = mock.Mock(return_value=item)
    monkeypatch.setattr(server.db, 'get_training_review_item', get_item)
    monkeypatch.setattr(
        server.model_prefill, 'latest_model_specs', mock.Mock(return_value={})
    )

    result = server.api_training_review_prefill(7, {})

    assert result['item'] == item
    get_item.assert_called_once_with(connection, 7, result_groups={})


def test_single_item_hero_lineup_does_not_rebuild_all_result_groups(
    monkeypatch,
) -> None:
    connection = mock.Mock()
    item = {'frame_id': 8, 'sources': []}
    monkeypatch.setattr(config, 'CONTROL_PLANE_ONLY', True)
    monkeypatch.setattr(server, '_conn', mock.Mock(return_value=connection))
    monkeypatch.setitem(server._training_review_cache, 'groups', None)
    get_item = mock.Mock(return_value=item)
    monkeypatch.setattr(server.db, 'get_training_review_item', get_item)
    monkeypatch.setattr(
        server.db, 'get_training_review_hero_lineup', mock.Mock(return_value=None)
    )
    monkeypatch.setattr(
        server.hero_review, 'infer_lineup_context', mock.Mock(return_value=None)
    )

    result = server.api_training_review_hero_lineup(8)

    assert result['applicable'] is False
    get_item.assert_called_once_with(connection, 8, result_groups={})


def test_postgres_review_list_does_not_hold_the_process_database_lock(
    monkeypatch,
) -> None:
    class FailingLock:
        def __enter__(self):
            raise AssertionError('PostgreSQL 只读列表不应持有进程级数据库锁')

        def __exit__(self, *_args):
            return False

    connection = mock.Mock()
    monkeypatch.setattr(config, 'DATABASE_URL', 'postgresql://vision')
    monkeypatch.setattr(server, '_db_lock', FailingLock())
    monkeypatch.setattr(server, '_conn', mock.Mock(return_value=connection))
    monkeypatch.setattr(
        server, '_cached_training_review_groups', mock.Mock(return_value={})
    )
    monkeypatch.setattr(
        server.db, 'training_review_page', mock.Mock(return_value=([], 0))
    )

    result = server.api_training_review_items(include_stats=False)

    assert result == {'items': [], 'stats': {}, 'filtered_total': 0}
