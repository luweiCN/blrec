from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = ROOT / '.github/workflows'


def test_server_workflow_covers_runtime_python_and_server_image() -> None:
    workflow = (WORKFLOWS / 'test-server.yml').read_text(encoding='utf8')
    assert 'workflow_call:' in workflow
    assert "'3.11'" in workflow
    assert 'working-directory: apps/blrec-server/webapp' in workflow
    assert 'uses: docker/build-push-action@' in workflow
    assert 'file: apps/blrec-server/Dockerfile' in workflow
    assert 'cache-from: type=gha,scope=blrec-server' in workflow
    assert 'load: true' in workflow
    assert 'Uncompressed size:' in workflow
    assert 'scripts/docker-smoke.sh blrec-server:release-test' in workflow
    assert 'name: Highlight media regression' in workflow
    assert 'timeout-minutes: 15' in workflow
    assert 'BLREC_RUN_HIGHLIGHT_MEDIA_TESTS' in workflow
    assert 'test_highlight_cut_ffmpeg.py' in workflow
    assert 'apps/browser-extension' not in workflow


def test_browser_extension_has_independent_test_and_release_workflows() -> None:
    test = (WORKFLOWS / 'test-browser-extension.yml').read_text(encoding='utf8')
    release = (WORKFLOWS / 'release-browser-extension.yml').read_text(encoding='utf8')
    assert 'cache-dependency-path: apps/browser-extension/package-lock.json' in test
    assert 'working-directory: apps/browser-extension' in test
    for command in ('npm ci', 'npm test', 'npm run typecheck', 'npm run build'):
        assert command in test
    assert "tags: ['extension-v*.*.*']" in release
    assert 'uses: ./.github/workflows/test-browser-extension.yml' in release
    assert 'blrec-browser-extension-${{ steps.version.outputs.value }}.zip' in release
    assert 'docker/build-push-action' not in release


def test_server_release_has_independent_exact_image_contract() -> None:
    workflow = (WORKFLOWS / 'release-server.yml').read_text(encoding='utf8')
    assert "tags: ['server-v*.*.*']" in workflow
    assert 'packages: write' in workflow
    assert 'linux/amd64,linux/arm64' in workflow
    assert 'ghcr.io/luweicn/blrec-server' in workflow
    assert 'file: apps/blrec-server/Dockerfile' in workflow
    assert 'uses: ./.github/workflows/test-server.yml' in workflow
    assert ':beta' in workflow
    assert ':latest' not in workflow
    assert 'gh release create' in workflow
    assert 'browser-extension' not in workflow
    for asset in (
        'compose.synology.yml',
        'compose.lan-postgres.yml',
        'compose.postgres.yml',
        'synology.env.example',
        'synology.lan-postgres.env.example',
    ):
        assert asset in workflow


def test_vision_lab_has_independent_test_and_release_workflows() -> None:
    test = (WORKFLOWS / 'test-vision-lab.yml').read_text(encoding='utf8')
    release = (WORKFLOWS / 'release-vision-lab.yml').read_text(encoding='utf8')
    assert 'working-directory: apps/vision-lab' in test
    assert "python-version: '3.12'" in test
    assert 'python -m unittest discover' in test
    assert 'python -m build' in test
    assert "tags: ['vision-lab-v*.*.*']" in release
    assert 'uses: ./.github/workflows/test-vision-lab.yml' in release
    assert '${{ env.HARBOR_IMAGE }}:${{ env.RELEASE_TAG }}' in release
    assert 'docker/build-push-action@v6' in release
    assert 'ghcr.io/luweicn/blrec-vision-lab' not in release


def test_legacy_automatic_publishers_cannot_run_for_tag() -> None:
    assert not (WORKFLOWS / 'release.yml').exists()
    assert not (WORKFLOWS / 'test.yml').exists()
    assert not (WORKFLOWS / 'docker-hub.yml').exists()
    assert not (WORKFLOWS / 'ghcr.yml').exists()
    for name in ('pypi.yml', 'portable.yml'):
        workflow = (WORKFLOWS / name).read_text(encoding='utf8')
        assert 'workflow_dispatch:' in workflow
        assert 'tags:' not in workflow
