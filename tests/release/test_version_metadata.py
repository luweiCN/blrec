from pathlib import Path

import blrec

ROOT = Path(__file__).resolve().parents[2]


def test_release_version_matches_preview_beta() -> None:
    assert blrec.__version__ == '3.0.0-beta.35'


def test_release_notes_describe_vainglory_index_and_scan_optimization() -> None:
    notes = (ROOT / 'docs/releases/3.0.0-beta.35.md').read_text(encoding='utf8')
    assert '# BLREC 3.0.0-beta.35' in notes
    assert 'NAS 灰度测试版' in notes
    assert '玩家资料库' in notes
    assert '历史稿件' in notes
    assert '计时器 OCR' in notes
    assert 'HUD' in notes
    assert '算法版本 17' in notes
    assert '数据库迁移' in notes
    assert 'latest' not in notes.lower()
