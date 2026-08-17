"""NAS 控制面不得直接执行视频处理或旧模型批量推理。"""

from pathlib import Path

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

    script = (
        Path(__file__).resolve().parent.parent / 'labeler/static/app.js'
    ).read_text(encoding='utf-8')
    assert "limit: '20'" in script
    assert "include_stats: 'false'" in script
    assert '/api/training-review/stats?' in script
