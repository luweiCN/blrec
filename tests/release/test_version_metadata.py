from pathlib import Path

import blrec

ROOT = Path(__file__).resolve().parents[2]


def test_release_version_matches_preview_beta() -> None:
    assert blrec.__version__ == '3.0.0-beta.37'


def test_ocr_runtime_pins_cover_supported_python_wheels() -> None:
    setup = (ROOT / 'setup.cfg').read_text(encoding='utf8')
    assert 'backports.zoneinfo >= 0.2.1, < 0.3.0 ; python_version < "3.9"' in setup
    assert 'onnxruntime == 1.23.2 ; python_version == "3.10"' in setup
    assert 'onnxruntime == 1.28.0 ; python_version >= "3.11"' in setup


def test_release_notes_describe_vainglory_scan_acceleration() -> None:
    notes = (ROOT / 'docs/releases/3.0.0-beta.37.md').read_text(encoding='utf8')
    assert '# BLREC 3.0.0-beta.37' in notes
    assert 'NAS 灰度测试版' in notes
    assert '场景变化' in notes
    assert '4 fps' in notes
    assert '3 秒' in notes
    assert 'ONNX' in notes
    assert '算法版本 17' in notes
    assert '数据库迁移' in notes
    assert 'latest' not in notes.lower()
