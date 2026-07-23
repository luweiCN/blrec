from pathlib import Path

import blrec

ROOT = Path(__file__).resolve().parents[2]


def test_release_version_matches_preview_beta() -> None:
    assert blrec.__version__ == '3.0.0-beta.32'


def test_release_notes_describe_media_library_scope() -> None:
    notes = (ROOT / 'docs/releases/3.0.0-beta.32.md').read_text(encoding='utf8')
    assert '# BLREC 3.0.0-beta.32' in notes
    assert '公开测试版' in notes
    assert '永久收藏' in notes
    assert '/favorites' in notes
    assert '数据库迁移' in notes
    assert 'latest' not in notes.lower()
