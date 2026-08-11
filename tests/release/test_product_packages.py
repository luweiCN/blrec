import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
APPS = ROOT / 'apps'


def test_five_products_have_independent_package_roots() -> None:
    assert (APPS / 'blrec-server/Dockerfile').is_file()
    assert (APPS / 'blrec-server/webapp/package-lock.json').is_file()
    assert (APPS / 'browser-extension/package-lock.json').is_file()
    assert (APPS / 'vision-lab/pyproject.toml').is_file()
    assert (APPS / 'analysis-worker/pyproject.toml').is_file()
    assert (APPS / 'public-dashboard/package-lock.json').is_file()
    assert (APPS / 'public-dashboard/publisher/pyproject.toml').is_file()


def test_analysis_worker_owns_its_runtime_and_models() -> None:
    worker = APPS / 'analysis-worker'
    setup = (ROOT / 'setup.cfg').read_text(encoding='utf8').lower()
    pyproject = (worker / 'pyproject.toml').read_text(encoding='utf8').lower()

    assert 'blrec-analysis-worker = blrec.analysis_worker' not in setup
    for dependency in ('rapidocr', 'onnxruntime', 'opencv-python'):
        assert (
            dependency
            not in setup.split('install_requires =', 1)[1].split(
                '[options.extras_require]', 1
            )[0]
        )
        assert dependency in pyproject

    models = worker / 'src/blrec_analysis_worker/models'
    assert {item.name for item in models.glob('*.onnx')} == {
        'multi-v2.onnx',
        'result-detector-v1.onnx',
        'result-panel.onnx',
    }
    assert len(tuple((worker / 'src/blrec_analysis_worker/heroes').glob('*.jpg'))) == 57
    assert not tuple((ROOT / 'src/blrec/data/vainglory').glob('*.onnx'))


def test_dashboard_build_and_publisher_are_product_owned() -> None:
    dashboard = APPS / 'public-dashboard'
    angular = (dashboard / 'angular.json').read_text(encoding='utf8')
    dockerfile = (dashboard / 'publisher/Dockerfile').read_text(encoding='utf8')
    publisher = (
        dashboard / 'publisher/src/blrec_dashboard_publisher/publisher.py'
    ).read_text(encoding='utf8')
    compose = (dashboard / 'deploy/nas/compose.yml').read_text(encoding='utf8')

    assert '../src/blrec' not in angular
    assert len(tuple((dashboard / 'src/assets/vainglory/heroes').glob('*.jpg'))) == 57
    assert 'ghcr.io/luweicn/blrec:' not in dockerfile
    assert 'blrec.setting' not in publisher
    assert 'load_network_settings' in publisher
    assert 'ghcr.io/luweicn/blrec-dashboard-publisher:' in compose
    assert not (ROOT / 'public-dashboard').exists()
    assert not (ROOT / 'Dockerfile.dashboard-publisher').exists()


def test_server_image_excludes_analysis_runtime_and_wheel_layer() -> None:
    server = APPS / 'blrec-server'
    requirements = (server / 'requirements.txt').read_text(encoding='utf8').lower()
    dockerfile = (server / 'Dockerfile').read_text(encoding='utf8')

    for dependency in ('opencv', 'onnxruntime', 'rapidocr', 'numpy'):
        assert dependency not in requirements
    assert '--mount=type=bind,from=wheel-builder' in dockerfile
    assert 'COPY --from=wheel-builder /wheels /wheels' not in dockerfile
    assert dockerfile.index("-name '*.onnx' -delete") < dockerfile.index(
        'python -m pip wheel'
    )
    assert "-name '*.onnx' -delete" in dockerfile
    assert 'blrec-analysis-worker' in dockerfile


def test_server_requirements_track_the_shared_runtime_without_worker_deps() -> None:
    setup = (ROOT / 'setup.cfg').read_text(encoding='utf8')
    block = setup.split('install_requires =', 1)[1].split(
        '[options.extras_require]', 1
    )[0]
    shared = {line.strip() for line in block.splitlines() if line.strip()}
    server = {
        line.strip()
        for line in (APPS / 'blrec-server/requirements.txt')
        .read_text(encoding='utf8')
        .splitlines()
        if line.strip() and not line.lstrip().startswith('#')
    }

    def requirement_name(requirement: str) -> str:
        return (
            re.split(r'[ <>=;\[]', requirement, maxsplit=1)[0].replace('_', '-').lower()
        )

    excluded = {'backports.zoneinfo', 'rapidocr', 'onnxruntime', 'opencv-python'}
    expected_names = {
        requirement_name(requirement)
        for requirement in shared
        if requirement_name(requirement) not in excluded
    }
    assert {requirement_name(requirement) for requirement in server} == expected_names


def test_vision_lab_keeps_mutable_data_outside_the_distribution() -> None:
    product = APPS / 'vision-lab'
    config = (product / 'labeler/config.py').read_text(encoding='utf8')
    training = (product / 'labeler/training.py').read_text(encoding='utf8')
    pyproject = (product / 'pyproject.toml').read_text(encoding='utf8')

    assert 'VISION_LAB_DATA_DIR' in config
    assert "config.MODELS_DIR / 'base'" in training
    assert 'optional-dependencies' in pyproject
    assert 'data/' not in pyproject


def test_server_and_extension_release_workflows_are_independent() -> None:
    workflows = ROOT / '.github/workflows'
    server = (workflows / 'release-server.yml').read_text(encoding='utf8')
    extension = (workflows / 'release-browser-extension.yml').read_text(encoding='utf8')

    assert "tags: ['server-v*.*.*']" in server
    assert 'browser-extension' not in server
    assert "tags: ['extension-v*.*.*']" in extension
    assert 'apps/browser-extension' in extension
    assert 'docker/build-push-action' not in extension


def test_worker_and_dashboard_release_workflows_are_independent() -> None:
    workflows = ROOT / '.github/workflows'
    worker = (workflows / 'release-analysis-worker.yml').read_text(encoding='utf8')
    dashboard = (workflows / 'release-public-dashboard.yml').read_text(encoding='utf8')

    assert "tags: ['worker-v*.*.*']" in worker
    assert 'apps/analysis-worker' in worker
    assert 'dist/shared-core' in worker
    assert 'python -m build --wheel' in worker
    assert 'docker/build-push-action' not in worker
    assert "tags: ['dashboard-v*.*.*']" in dashboard
    assert 'apps/public-dashboard' in dashboard
    assert 'ghcr.io/luweicn/blrec-dashboard-publisher' in dashboard
