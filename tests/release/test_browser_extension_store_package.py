import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
EXTENSION = ROOT / 'browser-extension'


def test_store_manifest_uses_the_blrec_icon_and_minimum_permissions() -> None:
    manifest = json.loads((EXTENSION / 'src/manifest.json').read_text(encoding='utf8'))

    assert manifest['permissions'] == ['storage']
    assert manifest['icons'] == {'128': 'icons/icon-128.png'}
    assert manifest['description'] == (
        '在 B 站直播页面收录房间、标记高光，并连接自建 BLREC 完成剪辑投稿。'
    )
    assert manifest['homepage_url'] == 'https://github.com/luweiCN/blrec'


def test_build_copies_the_existing_blrec_icon() -> None:
    build_script = (EXTENSION / 'build.mjs').read_text(encoding='utf8')

    assert "webapp/src/assets/icons/icon-128x128.png" in build_script
    assert "resolve(output, 'icons', 'icon-128.png')" in build_script


def test_options_disclose_data_use_before_connecting() -> None:
    options = (EXTENSION / 'src/options.html').read_text(encoding='utf8')

    assert '打开 B 站直播页面时读取房间号' in options
    assert '只发送到你填写的 BLREC 服务器' in options
    assert '点击“连接”表示同意上述用途' in options
    assert '浏览器插件隐私说明' in options
    assert 'placeholder="请输入 BLREC 的 IP 地址或域名"' in options
    assert 'nas.local' not in options
    assert 'placeholder="http://192.168.' not in options
