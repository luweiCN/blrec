from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = ROOT / '.github/workflows'


def test_test_workflow_is_reusable_and_covers_runtime_python() -> None:
    workflow = (WORKFLOWS / 'test.yml').read_text(encoding='utf8')
    assert 'workflow_call:' in workflow
    assert "'3.11'" in workflow
    assert 'scripts/docker-smoke.sh blrec:release-test' in workflow
    assert 'VERSION=3.0.0-beta.5' in workflow


def test_test_workflow_checks_the_browser_extension_independently() -> None:
    workflow = (WORKFLOWS / 'test.yml').read_text(encoding='utf8')
    assert 'name: Browser extension' in workflow
    assert 'cache-dependency-path: browser-extension/package-lock.json' in workflow
    assert 'working-directory: browser-extension' in workflow
    assert 'npm ci' in workflow
    assert 'npm test' in workflow
    assert 'npm run typecheck' in workflow
    assert 'npm run build' in workflow


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
    assert 'BLREC_EXTENSION_VERSION="$manifest_version" npm run build' in workflow
    assert (
        'blrec-highlight-extension-${{ steps.version.outputs.value }}.zip' in workflow
    )
    assert 'compose.synology.yml synology.env.example' in workflow


def test_legacy_automatic_publishers_cannot_run_for_tag() -> None:
    assert not (WORKFLOWS / 'docker-hub.yml').exists()
    assert not (WORKFLOWS / 'ghcr.yml').exists()
    for name in ('pypi.yml', 'portable.yml'):
        workflow = (WORKFLOWS / name).read_text(encoding='utf8')
        assert 'workflow_dispatch:' in workflow
        assert 'tags:' not in workflow
