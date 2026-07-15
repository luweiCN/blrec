from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = ROOT / '.github/workflows'


def test_test_workflow_is_reusable_and_covers_runtime_python() -> None:
    workflow = (WORKFLOWS / 'test.yml').read_text(encoding='utf8')
    assert 'workflow_call:' in workflow
    assert "'3.11'" in workflow
    assert 'scripts/docker-smoke.sh blrec:release-test' in workflow


def test_release_workflow_has_test_gate_and_exact_image_contract() -> None:
    workflow = (WORKFLOWS / 'release.yml').read_text(encoding='utf8')
    assert "tags: ['v*.*.*']" in workflow
    assert 'uses: ./.github/workflows/test.yml' in workflow
    assert 'needs: quality' in workflow
    assert 'packages: write' in workflow
    assert 'linux/amd64,linux/arm64' in workflow
    assert 'ghcr.io/luweicn/blrec' in workflow
    assert ':beta' in workflow
    assert ':latest' not in workflow
    assert 'gh release create' in workflow


def test_legacy_automatic_publishers_cannot_run_for_tag() -> None:
    assert not (WORKFLOWS / 'docker-hub.yml').exists()
    assert not (WORKFLOWS / 'ghcr.yml').exists()
    for name in ('pypi.yml', 'portable.yml'):
        workflow = (WORKFLOWS / name).read_text(encoding='utf8')
        assert 'workflow_dispatch:' in workflow
        assert 'tags:' not in workflow
