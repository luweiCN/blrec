"""NAS 控制面不得直接执行视频处理或旧模型批量推理。"""

import tempfile
from pathlib import Path
from unittest import mock

import pytest
from fastapi import HTTPException
from fastapi.responses import RedirectResponse
from labeler import config, db, hero_review, model_prefill, server, vision_jobs


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


def test_postgres_training_snapshot_does_not_hold_global_request_lock(
    monkeypatch,
) -> None:
    class ExplodingLock:
        def __enter__(self):
            raise AssertionError('PostgreSQL 快照不应占用全局请求锁')

        def __exit__(self, *_args):
            return False

    class Connection:
        def close(self) -> None:
            pass

    monkeypatch.setattr(config, 'DATABASE_URL', 'postgresql://configured')
    monkeypatch.setattr(server, '_db_lock', ExplodingLock())
    monkeypatch.setattr(server, '_conn', lambda: Connection())
    monkeypatch.setattr(
        server.training,
        'task_summary',
        lambda _conn, _task_id: {'id': 'match_flow', 'ready': True},
    )
    monkeypatch.setattr(
        server.training,
        'export_snapshot',
        lambda _conn, _task_id, materialize: {'version': 'match-flow-v2'},
    )
    monkeypatch.setattr(
        server.training, 'new_run_id', lambda _task_id: 'match-flow-run-2'
    )
    monkeypatch.setattr(
        server.db, 'create_training_run', lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(
        server.db, 'get_training_run', lambda *_args: {'id': 'match-flow-run-2'}
    )
    monkeypatch.setattr(
        server.vision_jobs,
        'create_job',
        lambda *_args, **_kwargs: {'id': 'train-job-2'},
    )

    result = server.api_start_training({'task_id': 'match_flow'})

    assert result['id'] == 'match-flow-run-2'
    assert result['vision_job']['id'] == 'train-job-2'


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
    assert 'id="candidate-filter-state"' in html
    assert 'candidate-worker-total' in html
    assert 'candidate-prefill-ready' in html
    assert 'candidate-ready-for-review' in html

    script = (
        Path(__file__).resolve().parent.parent / 'labeler/static/app.js'
    ).read_text(encoding='utf-8')
    assert 'const CANDIDATE_PAGE_SIZE = 50;' in script
    assert 'const CANDIDATE_REFILL_LOW_WATER = CANDIDATE_READY_TARGET;' in script
    assert 'limit: String(CANDIDATE_PAGE_SIZE)' in script
    assert "include_stats: 'false'" in script
    assert '/api/training-review/queue-summary?' in script
    control_plane_branch = script.index('if (CFG.control_plane_only)')
    assert control_plane_branch < script.index('loadStats();')
    review_loader = script[
        script.index('async function loadCandidateReview()') : script.index(
            'async function refillCandidateReviewQueue'
        )
    ]
    assert 'void loadCandidateReviewStats(status, sourceScope);' in review_loader
    material_loader = script[
        script.index(
            'async function openCandidateMaterialSuggestions()'
        ) : script.index('function renderCandidateHeroFilter()')
    ]
    assert "api('/api/training-review/material-suggestions')" in material_loader
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
    assert 'const candidatePrefillRequests = new Map();' not in script
    assert 'function prefetchCandidateImage(item)' in script
    assert 'function prefetchNextCandidate()' in script
    assert 'knownRemaining <= CANDIDATE_REFILL_LOW_WATER' in script
    assert 'completeCandidateHeroLineupPrefetch' in script
    assert 'prefetchNextCandidate();' in script
    assert '/api/training-review/items/${frameId}/prefill' not in script
    assert 'requestCandidateModelPrefill(item);' not in script
    assert (
        "function applyCandidateMaterialSuggestion(suggestion, heroScope = 'direct')"
        in script
    )
    material_apply = script[
        script.index('function applyCandidateMaterialSuggestion(') : script.index(
            'function renderCandidateMaterialSuggestions()'
        )
    ]
    assert "$('#candidate-source-type-filter').value = '';" in material_apply
    assert "query.set('hero_scope', candidateHeroScope)" in script
    assert 'renderCandidateMaterialSuggestions();' in script
    assert "const CANDIDATE_DEFAULT_SOURCE_TYPE = 'new_model_prefill';" in script
    assert 'new AbortController()' in script
    assert 'signal: controller.signal' in script


def test_material_suggestions_never_fall_back_to_full_scan(monkeypatch) -> None:
    server._invalidate_training_review_cache()
    conn = mock.Mock()
    indexed = [{'kind': 'scene_mode', 'scene': 'gameplay_hud'}]
    monkeypatch.setattr(
        db, 'training_review_material_index_complete', mock.Mock(return_value=False)
    )
    incremental = mock.Mock(return_value=indexed)
    monkeypatch.setattr(db, 'training_review_material_suggestions', incremental)
    monkeypatch.setattr(
        db,
        'training_review_stats',
        mock.Mock(side_effect=AssertionError('素材建议不应退回全量统计')),
    )
    monkeypatch.setattr(
        server, '_cached_training_review_groups', mock.Mock(return_value={})
    )

    assert server._cached_training_review_material_suggestions(conn) == indexed
    incremental.assert_called_once()


def test_candidate_review_prefetches_ahead_and_reduces_state_polling() -> None:
    script = (
        Path(__file__).resolve().parent.parent / 'labeler/static/app.js'
    ).read_text(encoding='utf-8')

    assert 'const CANDIDATE_READY_TARGET = 24;' in script
    assert 'async function warmCandidateReviewQueue(' in script
    warmer = script[
        script.index('async function warmCandidateReviewQueue(') : script.index(
            'async function prefetchNextCandidate()'
        )
    ]
    assert 'await prepareCandidateForReview(item);' in warmer
    assert 'loadToken !== candidateReviewLoadToken' in warmer
    assert 'await image.decode();' in script
    assert 'candidateImagePrefetches.get(frameId)' in script
    assert "api('/api/worker-candidates/state')" not in script
    assert 'setInterval(refreshCandidateIndexState, 30000)' not in script


def test_candidate_hud_prefill_shows_progress_and_preserves_manual_edits() -> None:
    root = Path(__file__).resolve().parent.parent / 'labeler/static'
    html = (root / 'index.html').read_text(encoding='utf-8')
    script = (root / 'app.js').read_text(encoding='utf-8')

    progress = html.index('id="candidate-hero-progress"')
    collapsed_help = html.index('<details class="candidate-hero-help">')
    assert progress < collapsed_help
    assert 'role="status" aria-live="polite"' in html[progress : progress + 300]
    assert 'let candidateHeroGeometryRevision = 0;' in script
    assert 'let candidateHeroPrefillRunning = false;' in script
    assert 'const candidateHeroSlotRecognitionStates = new Map();' in script
    refresh = script[
        script.index(
            'async function refreshCandidateHeroLayoutAfterWorker('
        ) : script.index('function addCandidateHeroCircle(')
    ]
    assert 'state.generation === target.generation' in refresh
    assert 'candidateHeroPrefillRunning = true;' not in refresh
    assert 'candidateHeroPrefillRunning = true;' in script
    assert 'candidateHeroPrefillRunning = false;' in script
    completion = script[
        script.index('function completeCandidateHeroLineupPrefetch(') : script.index(
            'async function loadCandidateHeroLineup('
        )
    ]
    assert 'delete refreshed.prefill_job;' in completion


def test_candidate_hero_ai_recognition_has_no_cross_frame_cache() -> None:
    root = Path(__file__).resolve().parent.parent / 'labeler/static'
    html = (root / 'index.html').read_text(encoding='utf-8')
    script = (root / 'app.js').read_text(encoding='utf-8')

    recognize_button = html.index('id="btn-candidate-hero-recognize"')
    draw_button = html.index('id="btn-candidate-hero-draw"')
    assert recognize_button < draw_button

    recognize = script[
        script.index('async function recognizeCandidateHeroes(') : script.index(
            'async function persistCandidateHeroLayout('
        )
    ]
    assert 'recognize: true' in recognize
    assert 'refresh: true' in recognize
    assert 'candidateHeroPrefillRunning = true;' in recognize
    assert 'candidateHeroPrefillRunning = false;' in recognize
    assert "$('#btn-candidate-hero-recognize').onclick" in script
    assert 'CANDIDATE_HERO_LINEUP_CACHE_STORAGE_KEY' not in script
    assert 'candidateCachedHeroLineup' not in script
    assert 'cacheCandidateHeroLineup' not in script
    assert 'btn-candidate-hero-save-template' not in html

    prepare = script[
        script.index('function prepareCandidateForReview(') : script.index(
            'function nextMatchingCandidate('
        )
    ]
    assert 'prepareCandidateHeroLineup(' in prepare
    assert 'recognize: true' not in prepare

    addition = script[
        script.index('function addCandidateHeroCircle(') : script.index(
            'async function deleteCandidateHeroSlot('
        )
    ]
    assert 'scheduleCandidateHeroRecognition(addedSlots);' in addition
    edit = script[
        script.index('function finishCandidateHeroEdit(') : script.index(
            'function cancelCandidateHeroEdit('
        )
    ]
    assert 'scheduleCandidateHeroRecognition(changedSlots);' in edit


def test_manual_hero_circle_is_rendered_before_remote_save() -> None:
    script = (
        Path(__file__).resolve().parent.parent / 'labeler/static/app.js'
    ).read_text(encoding='utf-8')
    addition = script[
        script.index('function addCandidateHeroCircle(') : script.index(
            'async function deleteCandidateHeroSlot('
        )
    ]

    local_update = addition.index('candidateHeroLineup.slots = slots;')
    scheduled = addition.index('scheduleCandidateHeroRecognition(addedSlots);')
    assert local_update < scheduled
    scheduler = script[
        script.index('function scheduleCandidateHeroRecognition(') : script.index(
            'function candidateHeroCropCenter('
        )
    ]
    assert 'CANDIDATE_HERO_RECOGNITION_DEBOUNCE_MS' in scheduler
    assert 'void persistCandidateHeroLayout(' in scheduler
    assert 'candidateHeroPersistQueue.then(save, save)' in script
    assert 'image_width:' in script
    assert 'image_height:' in script


def test_manual_hero_recognition_never_locks_other_boxes() -> None:
    script = (
        Path(__file__).resolve().parent.parent / 'labeler/static/app.js'
    ).read_text(encoding='utf-8')
    editor = script[
        script.index('function startCandidateHeroEdit(') : script.index(
            'function moveCandidateHeroEdit('
        )
    ]
    pointer = script[
        script.index('layer.onpointerdown = async (event) =>') : script.index(
            'layer.onpointerup = async (event) =>'
        )
    ]

    assert 'candidateHeroLoading' not in editor
    assert 'candidateHeroPrefillRunning' not in pointer
    assert 'candidateHeroLoading' not in pointer
    assert "['queued', 'running'].includes(state.status)" in script
    assert 'candidateHeroSameCrop(current.crop, target.crop)' in script


def test_worker_claim_autonomously_runs_core_then_hero_before_review(
    monkeypatch,
) -> None:
    monkeypatch.setattr(config, 'CONTROL_PLANE_ONLY', True)
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        database = root / 'lab.db'
        image = root / 'frame.jpg'
        image.write_bytes(b'candidate')
        conn = db.connect(database)
        try:
            video_id = db.upsert_video(
                conn,
                remote_path='/nas/candidate.mp4',
                streamer='测试主播',
                room_id='100',
                filename='candidate.mp4',
                duration_seconds=60,
                size_bytes=1,
            )
            frame_id = db.add_frames(
                conn,
                video_id,
                [
                    {
                        'timestamp_ms': 1_000,
                        'width': 1280,
                        'height': 720,
                        'sha256': '5' * 64,
                        'phash': '',
                        'frame_path': str(image),
                        'thumb_path': '',
                        'strategy': 'test',
                        'model_source': '',
                        'model_confidence': None,
                    }
                ],
            )[0]
            db.add_training_review_source(
                conn,
                frame_id=frame_id,
                source_type='worker',
                source_id='autonomous-worker-candidate',
                stage_for_prefill=True,
            )
            for worker_id in ('mac-studio', 'second-worker'):
                vision_jobs.register_worker(
                    conn,
                    worker_id=worker_id,
                    display_name=worker_id,
                    capabilities=['model_prefill'],
                )
        finally:
            conn.close()

        monkeypatch.setattr(server, '_require_vision_worker', lambda _request: None)
        monkeypatch.setattr(server, '_conn', lambda: db.connect(database))

        def model_specs(_conn, task_ids):
            return {
                task_id: {
                    'run_id': f'{task_id}-v1',
                    'metadata': {'task_id': task_id},
                    'artifact_size': 1,
                }
                for task_id in task_ids
            }

        monkeypatch.setattr(model_prefill, 'latest_model_specs', model_specs)

        claimed = server.api_claim_vision_job(
            mock.Mock(), {'worker_id': 'mac-studio', 'capabilities': ['model_prefill']}
        )['job']

        assert claimed is not None
        assert claimed['payload']['frame_id'] == frame_id
        assert claimed['payload']['operation'] == 'core'
        conn = db.connect(database)
        try:
            assert db.get_training_review_item(conn, frame_id) is None
        finally:
            conn.close()
        assert (
            server.api_claim_vision_job(
                mock.Mock(),
                {'worker_id': 'second-worker', 'capabilities': ['model_prefill']},
            )['job']
            is None
        )

        server.api_complete_vision_job(
            claimed['id'],
            mock.Mock(),
            {
                'worker_id': 'mac-studio',
                'lease_token': claimed['lease_token'],
                'result': {
                    'operation': 'core',
                    'frame_id': frame_id,
                    'suggestions': {
                        'match_flow': {
                            'label': 'match_flow',
                            'confidence': 0.99,
                            'origin': 'new_model_prefill',
                            'model_run_id': 'match-flow-v1',
                        },
                        'hero_select': {
                            'label': 'not_select',
                            'confidence': 0.99,
                            'origin': 'new_model_prefill',
                            'model_run_id': 'hero-select-v1',
                        },
                        'match_mode': {
                            'label': '3v3',
                            'confidence': 0.99,
                            'origin': 'new_model_prefill',
                            'model_run_id': 'match-mode-v1',
                        },
                        'result_panel': {
                            'label': 'no_result_panel',
                            'confidence': 0.99,
                            'origin': 'new_model_prefill',
                            'model_run_id': 'result-v1',
                        },
                    },
                    'model_outputs': [],
                    'suggested_boxes': [],
                    'hero_context_suggestion': {
                        'screen_type': 'scoreboard',
                        'team_size': 3,
                        'confidence': 0.95,
                        'complete_detection': True,
                    },
                    'errors': {},
                    'model_runs': {'match_flow': 'match-flow-v1'},
                },
            },
        )

        conn = db.connect(database)
        try:
            assert db.get_training_review_item(conn, frame_id) is not None
        finally:
            conn.close()

        hero_job = server.api_claim_vision_job(
            mock.Mock(), {'worker_id': 'mac-studio', 'capabilities': ['model_prefill']}
        )['job']
        assert hero_job is not None
        assert hero_job['payload']['operation'] == 'hero_lineup'
        assert hero_job['payload']['screen_type'] == 'scoreboard'
        assert hero_job['payload']['team_size'] == 3

        server.api_complete_vision_job(
            hero_job['id'],
            mock.Mock(),
            {
                'worker_id': 'mac-studio',
                'lease_token': hero_job['lease_token'],
                'result': {
                    'operation': 'hero_lineup',
                    'frame_id': frame_id,
                    'screen_type': 'scoreboard',
                    'team_size': 3,
                    'complete': False,
                    'reason': '头像位置模型只找到 4 个头像',
                    'slots': [],
                    'model_runs': {
                        'hero_avatar_detector': 'hero-avatar-v1',
                        'hero_identity': 'hero-identity-v1',
                        'player_position': 'player-position-v1',
                    },
                },
            },
        )

        lineup = server.api_training_review_hero_lineup(
            frame_id, screen_type='scoreboard', team_size=3
        )
        assert lineup['suggestion_method'] == 'new-model-incomplete-worker-v1'
        assert 'prefill_job' not in lineup

        conn = db.connect(database)
        try:
            indexed = conn.execute(
                'SELECT prefill_status,prefill_stage,prefill_attempts '
                'FROM training_review_material_index WHERE frame_id=?',
                (frame_id,),
            ).fetchone()
            assert indexed is not None
            assert indexed['prefill_status'] == 'ready'
            assert indexed['prefill_stage'] == 'complete'
            assert indexed['prefill_attempts'] == 1
            page, total = db.training_review_page(
                conn,
                status='needs_review',
                source_scope='new',
                prefill_ready_only=True,
                limit=10,
            )
            assert total == 1
            assert [item['frame_id'] for item in page] == [frame_id]
            hero_source = conn.execute(
                'SELECT metadata_json FROM training_review_sources '
                "WHERE source_type='new_model_hero_prefill' AND frame_id=?",
                (frame_id,),
            ).fetchone()
            assert hero_source is not None
            assert '头像位置模型只找到 4 个头像' in hero_source['metadata_json']
        finally:
            conn.close()


def test_failed_core_prefill_never_promotes_candidate(monkeypatch) -> None:
    apply_core = mock.Mock()
    monkeypatch.setattr(server.model_prefill, 'apply_core_prefill', apply_core)

    with pytest.raises(RuntimeError, match='核心模型预打标失败'):
        server._apply_remote_model_prefill(
            mock.Mock(),
            {'payload': {'frame_id': 7, 'operation': 'core'}},
            {'errors': {'match_flow': '模型文件损坏'}},
        )

    apply_core.assert_not_called()


def test_partial_hero_slot_result_merges_without_deleting_other_slots(
    monkeypatch,
) -> None:
    slots = [
        {
            'side': side,
            'slot': slot,
            'crop': {
                'x': 0.30 + (0.20 if side == 'right' else 0),
                'y': 0.10 + slot * 0.10,
                'w': 0.06,
                'h': 0.08,
            },
            'suggested_label': 'Adagio',
            'suggestion_confidence': 0.8,
        }
        for side in ('left', 'right')
        for slot in range(1, 4)
    ]
    changed = {**slots[1], 'suggested_label': '', 'suggestion_confidence': 0.0}
    predicted = {**changed, 'suggested_label': 'Caine', 'suggestion_confidence': 0.93}
    existing = {
        'frame_id': 12,
        'screen_type': 'gameplay_hud',
        'team_size': 3,
        'review_status': 'pending',
        'suggestion_method': 'manual-circle-v1',
        'slots': slots,
    }
    monkeypatch.setattr(
        server, '_single_training_review_item', mock.Mock(return_value={'sources': []})
    )
    monkeypatch.setattr(
        server.db, 'get_training_review_hero_lineup', mock.Mock(return_value=existing)
    )
    replace = mock.Mock(
        side_effect=lambda _conn, **kwargs: {**existing, 'slots': kwargs['slots']}
    )
    monkeypatch.setattr(server.db, 'replace_training_review_hero_layout', replace)
    monkeypatch.setattr(server, '_save_new_model_hero_prefill_source', mock.Mock())

    applied = server._apply_remote_model_prefill(
        mock.Mock(),
        {
            'payload': {
                'frame_id': 12,
                'operation': 'hero_slots',
                'screen_type': 'gameplay_hud',
                'team_size': 3,
                'slots': [changed],
            }
        },
        {'complete': True, 'slots': [predicted], 'model_runs': {}},
    )

    merged = replace.call_args.kwargs['slots']
    assert applied['applied'] is True
    assert len(merged) == 6
    assert merged[1]['suggested_label'] == 'Caine'
    assert merged[1]['suggestion_confidence'] == pytest.approx(0.93)
    assert merged[0]['suggested_label'] == 'Adagio'


def test_paused_worker_does_not_create_autonomous_prefill(monkeypatch) -> None:
    connection = mock.Mock()
    monkeypatch.setattr(server, '_require_vision_worker', lambda _request: None)
    monkeypatch.setattr(server, '_conn', mock.Mock(return_value=connection))
    monkeypatch.setattr(server.vision_jobs, 'claim_job', mock.Mock(return_value=None))
    monkeypatch.setattr(
        server.vision_jobs, 'get_worker', mock.Mock(return_value={'enabled': False})
    )
    queue_next = mock.Mock()
    monkeypatch.setattr(server, '_queue_next_autonomous_model_prefill', queue_next)

    result = server.api_claim_vision_job(
        mock.Mock(), {'worker_id': 'paused', 'capabilities': ['model_prefill']}
    )

    assert result == {'job': None}
    queue_next.assert_not_called()


def test_hero_selection_prefill_does_not_start_lineup_detection(monkeypatch) -> None:
    update_state = mock.Mock()
    monkeypatch.setattr(server.db, 'update_training_review_prefill_state', update_state)

    server._update_autonomous_prefill_after_result(
        mock.Mock(),
        {'payload': {'frame_id': 9, 'operation': 'core'}},
        {
            'suggestions': {
                'hero_select': {'label': 'select_aram', 'confidence': 0.99}
            },
            'hero_context_suggestion': {'screen_type': 'scoreboard', 'team_size': 3},
            'errors': {},
        },
    )

    update_state.assert_called_once_with(
        mock.ANY, frame_id=9, status='ready', stage='complete'
    )


def test_schema_v3_model_outputs_are_used_for_review_defaults() -> None:
    script = (
        Path(__file__).resolve().parent.parent / 'labeler/static/app.js'
    ).read_text(encoding='utf-8')

    assert 'function candidateSourceScreenTypes(source)' in script
    assert 'for (const output of metadata.model_outputs || [])' in script


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

    assert "'SELECT frame_id,source_type FROM training_review_sources'" in stats
    assert (
        "'JOIN training_review_items item ON item.frame_id=source.frame_id '" in stats
    )
    assert "\"WHERE item.review_status IN ('pending','partial') AND ((\"" in stats
    assert (
        "'SELECT frame_id, source_type, suggestions_json, metadata_json '" not in stats
    )


def test_training_review_stats_coalesces_concurrent_cold_requests() -> None:
    source = (Path(__file__).resolve().parent.parent / 'labeler/server.py').read_text(
        encoding='utf-8'
    )

    assert '_training_review_stats_compute_lock = threading.Lock()' in source
    cached = source[
        source.index('def _cached_training_review_stats(') : source.index(
            'def _cached_default_training_review_queue('
        )
    ]
    assert 'with _training_review_stats_compute_lock:' in cached


def test_training_tasks_reuse_persisted_summary(monkeypatch) -> None:
    connection = mock.Mock()
    summaries = [{'id': 'match_flow', 'counts': {'total': 10}}]
    load = mock.Mock(return_value={'summaries': summaries})
    monkeypatch.setattr(config, 'DATABASE_URL', 'postgresql://vision')
    monkeypatch.setattr(server.db, 'load_service_runtime_state', load)
    monkeypatch.setitem(server._training_tasks_cache, 'value', None)
    monkeypatch.setitem(server._training_tasks_cache, 'dirty', False)
    monkeypatch.setitem(server._training_tasks_cache, 'refreshing', False)
    monkeypatch.setitem(server._training_tasks_cache, 'error', '')

    first = server._cached_training_tasks(connection)
    second = server._cached_training_tasks(connection)

    assert first == second
    assert first[0]['counts']['total'] == 10
    assert first[0]['stats_refreshing'] is False
    load.assert_called_once_with(connection, server._TRAINING_TASKS_STATE_KEY)


def test_training_ui_hides_only_stale_model_baseline_while_stats_refresh() -> None:
    script = (
        Path(__file__).resolve().parent.parent / 'labeler/static/app.js'
    ).read_text(encoding='utf-8')
    renderer = script[
        script.index('function trainingDatasetDelta(task)') : script.index(
            'function trainingSnapshotNote(taskId)'
        )
    ]

    assert 'task.latest_successful_run_id' in renderer
    assert 'delta.run_id' in renderer
    assert 'task.stats_refreshing && (!delta || baselineStale)' in renderer
    assert "details.push('新标注统计中')" in renderer
    assert '正在按最新成功模型重新计算基线' in renderer


def test_cold_training_tasks_start_one_background_refresh(monkeypatch) -> None:
    connection = mock.Mock()
    starts = []

    class FakeThread:
        def __init__(self, *, target, args, daemon, name):
            assert target is server._refresh_training_tasks_cache
            assert daemon is True
            assert name == 'vision-training-stats'
            self.args = args

        def start(self):
            starts.append(self.args)

    monkeypatch.setattr(config, 'DATABASE_URL', 'postgresql://vision')
    monkeypatch.setattr(
        server.db, 'load_service_runtime_state', mock.Mock(return_value={})
    )
    monkeypatch.setattr(server.threading, 'Thread', FakeThread)
    monkeypatch.setitem(server._training_tasks_cache, 'value', None)
    monkeypatch.setitem(server._training_tasks_cache, 'dirty', True)
    monkeypatch.setitem(server._training_tasks_cache, 'refreshing', False)
    monkeypatch.setitem(server._training_tasks_cache, 'generation', 8)
    monkeypatch.setitem(server._training_tasks_cache, 'error', '')
    monkeypatch.setitem(server._training_tasks_cache, 'retry_at', 0.0)

    first = server._cached_training_tasks(connection)
    second = server._cached_training_tasks(connection)

    expected = sum(
        definition.get('active', True)
        for definition in server.training.TRAINING_TASKS.values()
    )
    assert len(first) == len(second) == expected
    assert all(item['stats_refreshing'] for item in first)
    assert starts == [(8,)]


def test_postgres_training_tasks_does_not_hold_process_database_lock(
    monkeypatch,
) -> None:
    class FailingLock:
        def __enter__(self):
            raise AssertionError('PostgreSQL 训练统计不应持有进程级数据库锁')

        def __exit__(self, *_args):
            return False

    connection = mock.Mock()
    summaries = [{'id': 'match_flow'}]
    monkeypatch.setattr(config, 'DATABASE_URL', 'postgresql://vision')
    monkeypatch.setattr(server, '_db_lock', FailingLock())
    monkeypatch.setattr(server, '_conn', mock.Mock(return_value=connection))
    monkeypatch.setattr(
        server, '_cached_training_tasks', mock.Mock(return_value=summaries)
    )

    assert server.api_training_tasks() == summaries
    connection.close.assert_called_once()


def test_material_suggestions_are_loaded_only_when_dialog_opens() -> None:
    root = Path(__file__).resolve().parent.parent
    server = (root / 'labeler/server.py').read_text(encoding='utf-8')
    script = (root / 'labeler/static/app.js').read_text(encoding='utf-8')

    assert 'include_material_suggestions=False' in server
    assert "@app.get('/api/training-review/material-suggestions')" in server
    assert "api('/api/training-review/material-suggestions')" in script


def test_review_page_does_not_wait_for_late_core_model_prefill() -> None:
    script = (
        Path(__file__).resolve().parent.parent / 'labeler/static/app.js'
    ).read_text(encoding='utf-8')

    assert 'function refreshCandidateHeroFromUpdatedModelItem(item)' not in script
    assert 'requestCandidateModelPrefill(item);' not in script
    assert 'function prepareCandidateForReview(item)' in script
    assert 'loadCandidateHeroLineup(item);' in script


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


def test_review_queue_summary_never_runs_full_training_stats(monkeypatch) -> None:
    connection = mock.Mock()
    monkeypatch.setattr(server, '_conn', mock.Mock(return_value=connection))
    queue_summary = mock.Mock(
        return_value={
            'total': 40_000,
            'prefill_ready': 100,
            'ready_for_review': 37,
            'prefill_waiting': 39_900,
            'prefill_failed': 0,
        }
    )
    monkeypatch.setattr(server.db, 'training_review_queue_summary', queue_summary)
    monkeypatch.setattr(
        server,
        '_cached_training_review_stats',
        mock.Mock(side_effect=AssertionError('轻量汇总不得运行全量训练统计')),
    )

    response = server.api_training_review_queue_summary()

    assert response['summary']['ready_for_review'] == 37
    queue_summary.assert_called_once_with(connection, source_scope='new')
    connection.close.assert_called_once()


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
    monkeypatch.setitem(server._training_tasks_cache, 'value', [{'id': 'match_flow'}])
    monkeypatch.setitem(server._training_tasks_cache, 'dirty', False)
    monkeypatch.setitem(server._training_tasks_cache, 'generation', 3)

    server._mark_training_review_saved(2)

    assert server._training_review_cache['default_queue'] == (3, 1)
    assert server._training_review_cache['default_queue_expires_at'] == 400.0
    assert server._training_review_cache['groups_expires_at'] == 400.0
    assert server._training_review_cache['stats'] is None
    assert server._training_review_cache['stats_expires_at'] == 0.0
    assert server._training_tasks_cache['value'] == [{'id': 'match_flow'}]
    assert server._training_tasks_cache['dirty'] is True
    assert server._training_tasks_cache['generation'] == 4


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
            'match_kind_label': None,
            'view_context_label': None,
            'hero_select_label': 'not_select',
            'result_panel_label': 'no_result_panel',
            'hero_layout_label': 'none',
            'review_status': 'confirmed',
        },
    )

    assert result == {'frame_id': 7, 'review_status': 'confirmed'}
    assert save.call_args.kwargs['hydrate'] is False
    assert save.call_args.kwargs['match_kind_label'] is None
    assert save.call_args.kwargs['view_context_label'] is None
    mark_saved.assert_called_once_with(7)


def test_candidate_ui_supports_rare_modes_and_video_scoped_context_cache() -> None:
    root = Path(__file__).resolve().parent.parent / 'labeler/static'
    script = (root / 'app.js').read_text(encoding='utf-8')
    page = (root / 'index.html').read_text(encoding='utf-8')

    assert "blitz: '闪电战'" in script or "'blitz': '闪电战'" in script
    assert 'CANDIDATE_MATCH_KINDS' in script
    assert 'CANDIDATE_VIEW_CONTEXTS' in script
    assert 'candidateMatchContextCache' in script
    assert 'CANDIDATE_CONTEXT_CACHE_MAX_GAP_MS' in script
    assert 'item.video_id' in script
    assert 'candidate-match-kind-filter' in page
    assert 'candidate-view-context-filter' in page


def test_candidate_match_context_and_hero_layout_do_not_overwrite_each_other() -> None:
    script = (
        Path(__file__).resolve().parent.parent / 'labeler/static/app.js'
    ).read_text(encoding='utf-8')
    defaults = script[
        script.index('function applyCandidateMatchContextDefaults(') : script.index(
            'function candidateResultHeroCountMode('
        )
    ]
    select_context = script[
        script.index('function selectCandidateMatchContext(') : script.index(
            'function appendCandidateMatchContext('
        )
    ]

    assert "if (!CANDIDATE_MATCH_KINDS[draft.match_kind_label])" in defaults
    assert "if (!CANDIDATE_VIEW_CONTEXTS[draft.view_context_label])" in defaults
    practice = select_context[
        select_context.index("value === 'practice'") : select_context.index(
            "if (field === 'view_context_label'"
        )
    ]
    assert "hero_layout_label = 'none'" not in practice
    assert 'resetCandidateHeroReview()' not in practice


def test_review_save_bundles_dirty_hero_lineup_into_one_transaction(
    monkeypatch,
) -> None:
    connection = mock.Mock()
    connection.execute.return_value.fetchone.return_value = (1,)
    monkeypatch.setattr(server, '_conn', mock.Mock(return_value=connection))
    monkeypatch.setattr(
        server.hero_review, 'allowed_hero_labels', mock.Mock(return_value={'Adagio'})
    )
    lineup = {
        'frame_id': 7,
        'screen_type': 'gameplay_hud',
        'review_status': 'confirmed',
        'slots': [],
    }
    save_lineup = mock.Mock(return_value=lineup)
    save_review = mock.Mock(return_value={'frame_id': 7, 'review_status': 'confirmed'})
    monkeypatch.setattr(server.db, 'save_training_review_hero_lineup', save_lineup)
    monkeypatch.setattr(server.db, 'save_training_review', save_review)
    monkeypatch.setattr(server, '_mark_training_review_saved', mock.Mock())

    result = server.api_save_training_review_item(
        7,
        {
            'match_flow_label': 'match_flow',
            'match_mode_label': '3v3',
            'hero_select_label': 'not_select',
            'result_panel_label': 'no_result_panel',
            'hero_layout_label': 'gameplay_hud',
            'review_status': 'confirmed',
            'hero_lineup': {
                'heroes': [{'side': 'left', 'slot': 1, 'hero_label': 'Adagio'}],
                'player_status': 'pending',
            },
        },
    )

    assert result['hero_lineup'] == {'applicable': True, **lineup}
    assert save_lineup.call_args.kwargs['refresh_material_index'] is False
    assert save_lineup.call_args.kwargs['commit'] is False
    assert save_lineup.call_args.kwargs['require_complete'] is True
    assert save_review.call_args.kwargs['commit'] is True


@pytest.mark.parametrize(
    ('match_kind', 'result_label', 'hero_layout', 'result_occlusion'),
    [
        ('practice', 'no_result_panel', 'gameplay_hud', 'none'),
        ('pvp', 'result_panel', 'result_page', 'occluded'),
    ],
)
def test_review_save_allows_partial_visible_lineup_for_special_contexts(
    monkeypatch, match_kind, result_label, hero_layout, result_occlusion
) -> None:
    connection = mock.Mock()
    connection.execute.return_value.fetchone.return_value = (1,)
    monkeypatch.setattr(server, '_conn', mock.Mock(return_value=connection))
    monkeypatch.setattr(
        server.hero_review, 'allowed_hero_labels', mock.Mock(return_value={'Adagio'})
    )
    save_lineup = mock.Mock(
        return_value={
            'frame_id': 8,
            'screen_type': hero_layout,
            'review_status': 'confirmed',
            'slots': [{'side': 'left', 'slot': 1}],
        }
    )
    monkeypatch.setattr(server.db, 'save_training_review_hero_lineup', save_lineup)
    monkeypatch.setattr(
        server.db,
        'save_training_review',
        mock.Mock(return_value={'frame_id': 8, 'review_status': 'confirmed'}),
    )
    monkeypatch.setattr(server.db, 'save_box', mock.Mock())
    monkeypatch.setattr(server, '_mark_training_review_saved', mock.Mock())

    body = {
        'match_flow_label': 'match_flow',
        'match_mode_label': '5v5' if match_kind == 'practice' else '3v3',
        'match_kind_label': match_kind,
        'view_context_label': 'played',
        'hero_select_label': 'not_select',
        'result_panel_label': result_label,
        'hero_layout_label': hero_layout,
        'result_occlusion': result_occlusion,
        'review_status': 'confirmed',
        'hero_lineup': {
            'heroes': [{'side': 'left', 'slot': 1, 'hero_label': 'Adagio'}],
            'player_status': 'unreadable',
        },
    }
    if result_label == 'result_panel':
        body['result_box'] = {'x': 0.1, 'y': 0.2, 'w': 0.8, 'h': 0.6}

    server.api_save_training_review_item(8, body)

    assert save_lineup.call_args.kwargs['require_complete'] is False


def test_candidate_save_posts_hero_lineup_with_review_in_one_request() -> None:
    script = (
        Path(__file__).resolve().parent.parent / 'labeler/static/app.js'
    ).read_text(encoding='utf-8')
    save = script[
        script.index('async function saveCandidateReview(') : script.index(
            'function candidatePoint('
        )
    ]

    assert '/hero-lineup' not in save
    assert 'hero_lineup:' in save


def test_candidate_review_supports_confirmed_rechecks_and_afk_labels() -> None:
    root = Path(__file__).resolve().parent.parent / 'labeler/static'
    script = (root / 'app.js').read_text(encoding='utf-8')
    page = (root / 'index.html').read_text(encoding='utf-8')

    assert 'candidate-review-reason-filter' in page
    assert "['missing_afk', '挂机状态待补']" in script
    assert 'candidate-hero-afk' in script
    assert '{is_afk: slot.is_afk === true}' in script
    assert "loadedStatus === 'missing_afk'" in script


def test_candidate_save_allows_partial_special_lineups() -> None:
    script = (
        Path(__file__).resolve().parent.parent / 'labeler/static/app.js'
    ).read_text(encoding='utf-8')
    helper = script[
        script.index('function candidateHeroAllowsPartialLineup(') : script.index(
            'function candidateHeroLayoutComplete('
        )
    ]
    save = script[
        script.index('async function saveCandidateReview(') : script.index(
            'function candidatePoint('
        )
    ]

    assert "draft.match_kind_label === 'practice'" in helper
    assert "draft.result_occlusion === 'occluded'" in helper
    assert 'candidateHeroAllowsPartialLineup(candidateDraft)' in save
    assert '请至少圈出一个实际可见的英雄头像' in save


def test_candidate_save_prepares_next_item_while_current_item_is_saving() -> None:
    script = (
        Path(__file__).resolve().parent.parent / 'labeler/static/app.js'
    ).read_text(encoding='utf-8')
    save = script[
        script.index('async function saveCandidateReview(') : script.index(
            'function candidatePoint('
        )
    ]

    start_save = save.index('const savePromise = api(')
    start_next = save.index('const nextPreparation = prepareCandidateAfterSave(')
    await_save = save.index('const saved = await savePromise;')
    assert start_save < await_save
    assert start_next < await_save
    assert 'await nextPreparation;' in save


def test_material_suggestions_show_historical_and_new_confirmed_breakdown() -> None:
    script = (
        Path(__file__).resolve().parent.parent / 'labeler/static/app.js'
    ).read_text(encoding='utf-8')
    render = script[
        script.index('function renderCandidateMaterialSuggestions()') : script.index(
            'async function openCandidateMaterialSuggestions()'
        )
    ]

    assert 'legacy_confirmed_count' in render
    assert 'new_confirmed_count' in render
    assert "'历史'" in render
    assert "'新素材'" in render


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


def test_loading_frame_never_reads_cross_frame_layout_template(monkeypatch) -> None:
    connection = mock.Mock()
    item = {
        'frame_id': 18,
        'width': 1280,
        'height': 720,
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
