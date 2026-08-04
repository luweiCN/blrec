from pathlib import Path

import blrec

ROOT = Path(__file__).resolve().parents[2]


def test_release_version_matches_preview_beta() -> None:
    assert blrec.__version__ == '3.0.0-beta.36'


def test_ocr_runtime_pins_cover_supported_python_wheels() -> None:
    setup = (ROOT / 'setup.cfg').read_text(encoding='utf8')
    assert 'backports.zoneinfo >= 0.2.1, < 0.3.0 ; python_version < "3.9"' in setup
    assert 'onnxruntime == 1.23.2 ; python_version == "3.10"' in setup
    assert 'onnxruntime == 1.28.0 ; python_version >= "3.11"' in setup


def test_release_notes_describe_vainglory_short_result_recovery() -> None:
    notes = (ROOT / 'docs/releases/3.0.0-beta.36.md').read_text(encoding='utf8')
    assert '# BLREC 3.0.0-beta.36' in notes
    assert 'NAS 灰度测试版' in notes
    assert '短分 P' in notes
    assert '4 fps' in notes
    assert '批量重新分析' in notes
    assert '阶段耗时' in notes
    assert '算法版本 17' in notes
    assert '数据库迁移' in notes
    assert 'latest' not in notes.lower()
