from pathlib import Path

import blrec

ROOT = Path(__file__).resolve().parents[2]


def test_release_version_matches_preview_beta() -> None:
    assert blrec.__version__ == '3.0.0-beta.17'


def test_release_notes_describe_beta_scope_without_claiming_validation() -> None:
    notes = (ROOT / 'docs/releases/3.0.0-beta.17.md').read_text(encoding='utf8')
    assert '# BLREC 3.0.0-beta.17' in notes
    assert '公开测试版' in notes
    assert '尚未完成真实环境验收' in notes
    assert 'latest' not in notes.lower()
