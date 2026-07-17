from blrec.setting.models import RecorderSettings


def test_recorder_title_keywords_are_trimmed_and_deduplicated() -> None:
    settings = RecorderSettings(title_keywords=[' 比赛 ', '', 'HIGHLIGHT', 'highlight'])

    assert settings.title_keywords == ['比赛', 'HIGHLIGHT']
