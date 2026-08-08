from blrec_analysis_worker.resources import (
    hero_reference_directory,
    result_panel_model_path,
    stage_classifier_model_path,
)


def test_worker_owns_complete_model_and_hero_resources() -> None:
    models = stage_classifier_model_path().parent

    assert stage_classifier_model_path().is_file()
    assert result_panel_model_path().name == 'result-detector-v1.onnx'
    assert {item.name for item in models.glob('*.onnx')} == {
        'multi-v2.onnx',
        'result-detector-v1.onnx',
        'result-panel.onnx',
    }
    assert len(tuple(hero_reference_directory().glob('*.jpg'))) == 57
