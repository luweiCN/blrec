from pathlib import Path

import blrec

ROOT = Path(__file__).resolve().parents[2]


def test_release_version_matches_preview_beta() -> None:
    assert blrec.__version__ == '3.0.0-beta.114'


def test_server_and_worker_runtime_pins_cover_supported_python_wheels() -> None:
    setup = (ROOT / 'setup.cfg').read_text(encoding='utf8')
    worker = (ROOT / 'apps/analysis-worker/pyproject.toml').read_text(encoding='utf8')

    assert 'backports.zoneinfo >= 0.2.1, < 0.3.0 ; python_version < "3.9"' in setup
    assert (
        'onnxruntime'
        not in setup.split('install_requires =', 1)[1].split(
            '[options.extras_require]', 1
        )[0]
    )
    assert 'version = "0.1.14"' in worker
    assert '"blrec==3.0.0b99"' in worker
    assert '"onnxruntime==1.23.2; python_version == \'3.10\'"' in worker
    assert '"onnxruntime==1.28.0; python_version >= \'3.11\'"' in worker


def test_release_notes_describe_vainglory_operations() -> None:
    notes = (ROOT / 'docs/releases/3.0.0-beta.47.md').read_text(encoding='utf8')
    assert '# BLREC 3.0.0-beta.47' in notes
    assert '发布任务完整性' in notes
    assert '不可变快照' in notes
    assert '零对局' in notes
    assert '全量核对重发' in notes
    assert 'latest' not in notes.lower()
