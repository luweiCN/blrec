"""NAS 控制面不得直接执行视频处理或旧模型批量推理。"""

from pathlib import Path
from unittest import mock

from fastapi import HTTPException
from fastapi.responses import RedirectResponse
from labeler import config, hero_review, server


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
    assert 'const CANDIDATE_PAGE_SIZE = 50;' in script
    assert 'const CANDIDATE_REFILL_LOW_WATER = 10;' in script
    assert 'limit: String(CANDIDATE_PAGE_SIZE)' in script
    assert "include_stats: 'false'" in script
    assert '/api/training-review/stats?' in script
    control_plane_branch = script.index('if (CFG.control_plane_only)')
    assert control_plane_branch < script.index('loadStats();')
    review_loader = script[
        script.index('async function loadCandidateReview()') : script.index(
            'async function refillCandidateReviewQueue'
        )
    ]
    assert 'loadCandidateReviewStats(' not in review_loader
    material_loader = script[
        script.index(
            'async function openCandidateMaterialSuggestions()'
        ) : script.index('function renderCandidateHeroFilter()')
    ]
    assert 'await loadCandidateReviewStats(' in material_loader
    source_scope_setter = script[
        script.index('function setCandidateSourceScope(') : script.index(
            'function candidateSourceText('
        )
    ]
    assert 'loadCandidateFilterOptions();' not in source_scope_setter
    assert (
        "$('#candidate-streamer-filter').onfocus = ensureCandidateFilterOptions"
        in script
    )
    assert 'const candidatePrefillRequests = new Map();' in script
    assert 'function prefetchCandidateImage(item)' in script
    assert 'function prefetchNextCandidate()' in script
    assert 'knownRemaining <= CANDIDATE_REFILL_LOW_WATER' in script
    assert 'completeCandidateHeroLineupPrefetch' in script
    assert 'prefetchNextCandidate();' in script
    assert 'function applyCandidateMaterialSuggestion(suggestion)' in script
    assert 'renderCandidateMaterialSuggestions();' in script


def test_candidate_review_prefetches_ahead_and_reduces_state_polling() -> None:
    script = (
        Path(__file__).resolve().parent.parent / 'labeler/static/app.js'
    ).read_text(encoding='utf-8')

    assert 'const CANDIDATE_PREFETCH_AHEAD = 3;' in script
    assert 'await image.decode();' in script
    assert 'candidateImagePrefetches.get(frameId)' in script
    assert 'setInterval(refreshCandidateIndexState, 30000)' in script


def test_hero_comparison_reuses_prefetched_full_frame_in_browser() -> None:
    script = (
        Path(__file__).resolve().parent.parent / 'labeler/static/app.js'
    ).read_text(encoding='utf-8')

    assert 'function candidateHeroCropPreview(' in script
    assert "source.src = candidateCurrentImageUrl();" in script
    assert "source.style.width = `${100 / width}%`;" in script
    assert "source.style.height = `${100 / height}%`;" in script
    assert "crop.src = `${slot.crop_url}" not in script


def test_training_review_stats_does_not_transfer_unused_source_json() -> None:
    source = (Path(__file__).resolve().parent.parent / 'labeler/db.py').read_text(
        encoding='utf-8'
    )
    stats = source[source.index('def training_review_stats(') :]

    assert "'AS manual_game_mode FROM training_review_sources'" in stats
    assert (
        "'SELECT frame_id, source_type, suggestions_json, metadata_json '" not in stats
    )


def test_late_model_prefill_refreshes_heroes_after_unrelated_form_click() -> None:
    script = (
        Path(__file__).resolve().parent.parent / 'labeler/static/app.js'
    ).read_text(encoding='utf-8')

    assert 'let candidateHeroContextTouched = false;' in script
    assert 'function refreshCandidateHeroFromUpdatedModelItem(item)' in script
    assert 'candidateHeroDirty || candidateHeroContextTouched' in script
    assert 'candidateHeroDirty || candidateFormTouched' not in script
    assert 'refreshCandidateHeroFromUpdatedModelItem(currentCandidate());' in script


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


def test_default_review_queue_reuses_cached_frame_ids(monkeypatch) -> None:
    connection = mock.Mock()
    monkeypatch.setattr(config, 'DATABASE_URL', 'postgresql://vision')
    monkeypatch.setattr(server, '_conn', mock.Mock(return_value=connection))
    monkeypatch.setattr(
        server, '_cached_training_review_groups', mock.Mock(return_value={})
    )
    frame_ids = mock.Mock(return_value=[30, 20, 10])
    monkeypatch.setattr(server.db, 'training_review_frame_ids', frame_ids)
    monkeypatch.setattr(
        server.db,
        'get_training_review_items',
        lambda _conn, values, **_kwargs: [
            {'frame_id': frame_id} for frame_id in values
        ],
    )
    monkeypatch.setitem(server._training_review_cache, 'default_queue', None)
    monkeypatch.setitem(server._training_review_cache, 'default_queue_expires_at', 0.0)

    first = server.api_training_review_items(
        status='needs_review',
        source_scope='new',
        limit=2,
        offset=0,
        include_stats=False,
    )
    second = server.api_training_review_items(
        status='needs_review',
        source_scope='new',
        limit=2,
        offset=1,
        include_stats=False,
    )

    assert [item['frame_id'] for item in first['items']] == [30, 20]
    assert [item['frame_id'] for item in second['items']] == [20, 10]
    assert first['filtered_total'] == second['filtered_total'] == 3
    frame_ids.assert_called_once()


def test_worker_candidate_state_coalesces_repeated_database_reads(monkeypatch) -> None:
    connection = mock.Mock()
    monkeypatch.setattr(config, 'CANDIDATE_LOCAL_DIR', None)
    connect = mock.Mock(return_value=connection)
    monkeypatch.setattr(server, '_conn', connect)
    full_stats = mock.Mock(side_effect=AssertionError('状态轮询不应触发全量统计'))
    monkeypatch.setattr(server, '_cached_training_review_stats', full_stats)
    monkeypatch.setitem(server._training_review_cache, 'stats', {'total': 53_000})
    published = mock.Mock(return_value={'running': False, 'processed': 20_000})
    monkeypatch.setattr(server.db, 'load_service_runtime_state', published)
    monkeypatch.setitem(server._worker_candidate_state_response, 'value', None)
    monkeypatch.setitem(server._worker_candidate_state_response, 'expires_at', 0.0)

    first = server.api_worker_candidate_state()
    second = server.api_worker_candidate_state()

    assert first == second
    assert first['review']['total'] == 53_000
    connect.assert_called_once()
    published.assert_called_once()
    full_stats.assert_not_called()


def test_saving_one_review_updates_queue_without_immediate_full_refresh(
    monkeypatch,
) -> None:
    monkeypatch.setattr(server.time, 'monotonic', lambda: 100.0)
    monkeypatch.setitem(server._training_review_cache, 'default_queue', (3, 2, 1))
    monkeypatch.setitem(
        server._training_review_cache, 'default_queue_expires_at', 400.0
    )
    monkeypatch.setitem(server._training_review_cache, 'groups_expires_at', 400.0)
    monkeypatch.setitem(server._training_review_cache, 'stats_expires_at', 400.0)

    server._mark_training_review_saved(2)

    assert server._training_review_cache['default_queue'] == (3, 1)
    assert server._training_review_cache['default_queue_expires_at'] == 400.0
    assert server._training_review_cache['groups_expires_at'] == 400.0
    assert server._training_review_cache['stats'] is None
    assert server._training_review_cache['stats_expires_at'] == 0.0


def test_review_save_returns_lightweight_ack(monkeypatch) -> None:
    connection = mock.Mock()
    connection.execute.return_value.fetchone.return_value = (1,)
    monkeypatch.setattr(server, '_conn', mock.Mock(return_value=connection))
    save = mock.Mock(return_value={'frame_id': 7, 'review_status': 'confirmed'})
    monkeypatch.setattr(server.db, 'save_training_review', save)
    mark_saved = mock.Mock()
    monkeypatch.setattr(server, '_mark_training_review_saved', mark_saved)

    result = server.api_save_training_review_item(
        7,
        {
            'match_flow_label': 'not_match_flow',
            'match_mode_label': None,
            'hero_select_label': 'not_select',
            'result_panel_label': 'no_result_panel',
            'hero_layout_label': 'none',
            'review_status': 'confirmed',
        },
    )

    assert result == {'frame_id': 7, 'review_status': 'confirmed'}
    assert save.call_args.kwargs['hydrate'] is False
    mark_saved.assert_called_once_with(7)


def test_worker_control_plane_redirects_only_frame_media_to_nas(monkeypatch) -> None:
    monkeypatch.setattr(config, 'MEDIA_SERVER_URL', 'http://nas:8800/')
    local_database = mock.Mock(side_effect=AssertionError('图片回源不应查询本地数据库'))
    monkeypatch.setattr(server, '_conn', local_database)

    image = server.api_frame_image(17)
    thumb = server.api_frame_thumb(17)

    assert isinstance(image, RedirectResponse)
    assert image.headers['location'] == 'http://nas:8800/api/frames/17/image'
    assert isinstance(thumb, RedirectResponse)
    assert thumb.headers['location'] == 'http://nas:8800/api/frames/17/thumb'
    local_database.assert_not_called()


def test_worker_control_plane_crops_remote_frame_bytes_locally(monkeypatch) -> None:
    connection = mock.Mock()
    connection.close = mock.Mock()
    monkeypatch.setattr(server, '_conn', mock.Mock(return_value=connection))
    monkeypatch.setattr(
        server,
        '_single_training_review_item',
        mock.Mock(return_value={'frame_id': 17, 'frame_path': '/nas/frame.jpg'}),
    )
    monkeypatch.setattr(
        server.db,
        'get_training_review_hero_lineup',
        mock.Mock(
            return_value={
                'slots': [
                    {
                        'side': 'left',
                        'slot': 1,
                        'crop': {'x': 0.1, 'y': 0.2, 'w': 0.1, 'h': 0.1},
                    }
                ]
            }
        ),
    )
    monkeypatch.setattr(config, 'MEDIA_SERVER_URL', 'http://nas:8800')
    fetch = mock.Mock(return_value=b'full-frame')
    crop = mock.Mock(return_value=b'hero-crop')
    monkeypatch.setattr(server, '_fetch_frame_image_bytes', fetch)
    monkeypatch.setattr(hero_review, 'crop_image_content', crop)

    response = server.api_training_review_hero_crop(17, 'left', 1)

    assert response.body == b'hero-crop'
    fetch.assert_called_once_with(17)
    crop.assert_called_once_with(
        b'full-frame', {'x': 0.1, 'y': 0.2, 'w': 0.1, 'h': 0.1}
    )


def test_worker_control_plane_serves_remote_model_test_crop(monkeypatch) -> None:
    connection = mock.Mock()
    connection.close = mock.Mock()
    crop_box = {'x': 0.1, 'y': 0.2, 'w': 0.3, 'h': 0.4}
    monkeypatch.setattr(server, '_conn', mock.Mock(return_value=connection))
    monkeypatch.setattr(config, 'MEDIA_SERVER_URL', 'http://nas:8800')
    monkeypatch.setattr(
        server.model_testing,
        'run_sample_image_reference',
        mock.Mock(return_value={'frame_id': 19, 'crop': crop_box}),
    )
    fetch = mock.Mock(return_value=b'full-frame')
    crop = mock.Mock(return_value=b'model-test-crop')
    monkeypatch.setattr(server, '_fetch_frame_image_bytes', fetch)
    monkeypatch.setattr(hero_review, 'crop_image_content', crop)

    response = server.api_model_test_sample_image(
        'hero-run-1', 'f00000019-left-1', 'test'
    )

    assert response.body == b'model-test-crop'
    fetch.assert_called_once_with(19)
    crop.assert_called_once_with(b'full-frame', crop_box)


def test_zero_sized_historical_frame_skips_layout_template_without_500(
    monkeypatch,
) -> None:
    connection = mock.Mock()
    item = {
        'frame_id': 18,
        'width': 0,
        'height': 0,
        'streamer': 'legacy-streamer',
        'sources': [],
    }
    monkeypatch.setattr(config, 'CONTROL_PLANE_ONLY', True)
    monkeypatch.setattr(server, '_conn', mock.Mock(return_value=connection))
    monkeypatch.setattr(
        server, '_single_training_review_item', mock.Mock(return_value=item)
    )
    monkeypatch.setattr(
        server.db, 'get_training_review_hero_lineup', mock.Mock(return_value=None)
    )
    template = mock.Mock()
    monkeypatch.setattr(server.db, 'get_training_review_hero_template', template)
    monkeypatch.setattr(
        server.hero_review,
        'infer_lineup_context',
        mock.Mock(return_value=('scoreboard', 5)),
    )
    monkeypatch.setattr(
        server.model_prefill, 'latest_model_specs', mock.Mock(return_value={})
    )

    result = server.api_training_review_hero_lineup(
        18, screen_type='scoreboard', team_size=5
    )

    assert result['applicable'] is True
    assert result['team_size'] == 5
    assert result['template_found'] is False
    template.assert_not_called()
