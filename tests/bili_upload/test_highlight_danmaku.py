from pathlib import Path

from lxml import etree

from blrec.bili_upload.highlight_danmaku import (
    DanmakuClipSource,
    HighlightDanmakuClipper,
)


def write_xml(path: Path, messages) -> None:
    root = etree.Element('i')
    for progress, text, attributes in messages:
        values = dict(attributes)
        values['p'] = '{:.3f},1,25,16777215,0,0,user,1'.format(progress)
        element = etree.SubElement(root, 'd', values)
        element.text = text
    etree.ElementTree(root).write(str(path), encoding='UTF-8', xml_declaration=True)


def output_messages(path: Path):
    root = etree.parse(str(path)).getroot()
    return root.findall('d')


def test_cut_filters_rebases_and_sorts_messages_across_parts(tmp_path: Path) -> None:
    first = tmp_path / 'p1.xml'
    second = tmp_path / 'p2.xml'
    output = tmp_path / 'highlight.xml'
    write_xml(
        first,
        (
            (9, 'before', {'uid': '1'}),
            (10, 'start <one>', {'uid': '2', 'user': '甲'}),
            (15, 'middle', {'uid': '3'}),
        ),
    )
    write_xml(second, ((1, 'second part', {'uid': '4'}), (6, 'after', {'uid': '5'})))

    result = HighlightDanmakuClipper().cut(
        (
            DanmakuClipSource(str(first), 10_000, 20_000, 0),
            DanmakuClipSource(str(second), 0, 5_000, 10_000),
        ),
        str(output),
    )

    messages = output_messages(output)
    assert result.output_path == str(output)
    assert result.source_count == 2
    assert result.message_count == 3
    assert [float(item.get('p').split(',')[0]) for item in messages] == [0, 5, 11]
    assert [item.text for item in messages] == ['start <one>', 'middle', 'second part']
    assert messages[0].get('uid') == '2'
    assert messages[0].get('user') == '甲'
    assert 'before' not in output.read_text(encoding='utf8')
    assert 'after' not in output.read_text(encoding='utf8')


def test_cut_never_expands_external_entities(tmp_path: Path) -> None:
    secret = tmp_path / 'secret.txt'
    source = tmp_path / 'unsafe.xml'
    output = tmp_path / 'highlight.xml'
    secret.write_text('DO-NOT-EXPAND', encoding='utf8')
    source.write_text(
        '<!DOCTYPE i [<!ENTITY xxe SYSTEM "{}">]>'
        '<i><d p="1,1,25,1,0,0,u,1">&xxe;</d>'
        '<d p="2,1,25,1,0,0,u,2">safe</d></i>'.format(secret.as_uri()),
        encoding='utf8',
    )

    result = HighlightDanmakuClipper().cut(
        (DanmakuClipSource(str(source), 0, 3_000, 0),), str(output)
    )

    document = output.read_text(encoding='utf8')
    assert result.message_count == 2
    assert 'DO-NOT-EXPAND' not in document
    assert '&xxe;' not in document
    assert 'safe' in document


def test_cut_skips_missing_sources_and_avoids_empty_missing_output(
    tmp_path: Path,
) -> None:
    existing = tmp_path / 'existing.xml'
    missing = tmp_path / 'missing.xml'
    output = tmp_path / 'highlight.xml'
    write_xml(existing, ((1, 'kept', {'uid': '1'}),))

    partial = HighlightDanmakuClipper().cut(
        (
            DanmakuClipSource(str(missing), 0, 2_000, 0),
            DanmakuClipSource(str(existing), 0, 2_000, 0),
        ),
        str(output),
    )
    assert partial.source_count == 1
    assert partial.message_count == 1
    assert output.exists()

    output.unlink()
    empty = HighlightDanmakuClipper().cut(
        (DanmakuClipSource(str(missing), 0, 2_000, 0),), str(output)
    )
    assert empty.output_path is None
    assert empty.source_count == 0
    assert empty.message_count == 0
    assert not output.exists()
