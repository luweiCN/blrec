import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

SCRIPT_PATH = (
    Path(__file__).parents[2]
    / 'public-dashboard'
    / 'deploy'
    / 'aliyun'
    / 'deploy_site.py'
)
SPEC = importlib.util.spec_from_file_location('dashboard_site_deploy', SCRIPT_PATH)
assert SPEC is not None and SPEC.loader is not None
dashboard_site_deploy = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = dashboard_site_deploy
SPEC.loader.exec_module(dashboard_site_deploy)


class FakeBucket:
    def __init__(self) -> None:
        self.uploads = []

    def put_object_from_file(self, key, source, headers):
        self.uploads.append((key, Path(source).read_bytes(), headers))
        return SimpleNamespace(status=200)


def test_site_upload_plan_keeps_index_last_and_assigns_cache_headers(
    tmp_path: Path,
) -> None:
    (tmp_path / 'index.html').write_text('index', encoding='utf-8')
    (tmp_path / 'main.1234abcd.js').write_text('script', encoding='utf-8')
    assets = tmp_path / 'assets'
    assets.mkdir()
    (assets / 'logo.svg').write_text('<svg/>', encoding='utf-8')

    uploads = dashboard_site_deploy.build_upload_plan(tmp_path)

    assert [upload.object_key for upload in uploads] == [
        'assets/logo.svg',
        'main.1234abcd.js',
        'index.html',
    ]
    assert uploads[1].headers['Cache-Control'].endswith('immutable')
    assert uploads[-1].headers['Cache-Control'] == (
        'no-cache, no-store, must-revalidate'
    )


def test_site_upload_refuses_to_touch_dashboard_data(tmp_path: Path) -> None:
    (tmp_path / 'index.html').write_text('index', encoding='utf-8')
    data = tmp_path / 'data'
    data.mkdir()
    (data / 'manifest.json').write_text('{}', encoding='utf-8')

    with pytest.raises(dashboard_site_deploy.SiteDeploymentError, match=r'data/\*\*'):
        dashboard_site_deploy.build_upload_plan(tmp_path)


def test_site_upload_ignores_the_empty_data_directory_marker(tmp_path: Path) -> None:
    (tmp_path / 'index.html').write_text('index', encoding='utf-8')
    data = tmp_path / 'data'
    data.mkdir()
    (data / '.gitignore').write_text('*\n', encoding='utf-8')

    uploads = dashboard_site_deploy.build_upload_plan(tmp_path)

    assert [upload.object_key for upload in uploads] == ['index.html']


def test_site_upload_sends_the_planned_files_in_order(tmp_path: Path) -> None:
    (tmp_path / 'index.html').write_text('index', encoding='utf-8')
    (tmp_path / 'runtime.1234abcd.js').write_text('runtime', encoding='utf-8')
    uploads = dashboard_site_deploy.build_upload_plan(tmp_path)
    bucket = FakeBucket()

    uploaded_bytes = dashboard_site_deploy.upload_site(bucket, uploads)

    assert [item[0] for item in bucket.uploads] == ['runtime.1234abcd.js', 'index.html']
    assert uploaded_bytes == len(b'runtime') + len(b'index')
