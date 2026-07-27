from pathlib import Path

import blrec

ROOT = Path(__file__).resolve().parents[2]


def test_release_version_matches_preview_beta() -> None:
    assert blrec.__version__ == '3.0.0-beta.34'


def test_release_notes_describe_audit_fixes_and_fullscreen_highlights() -> None:
    notes = (ROOT / 'docs/releases/3.0.0-beta.34.md').read_text(encoding='utf8')
    assert '# BLREC 3.0.0-beta.34' in notes
    assert '公开测试版' in notes
    assert '快速复播' in notes
    assert '零字节' in notes
    assert '上传固定使用所选线路' in notes
    assert '浏览器插件' in notes
    assert '网页全屏' in notes
    assert '系统全屏' in notes
    assert '/favorites' in notes
    assert '数据库迁移' in notes
    assert 'latest' not in notes.lower()
