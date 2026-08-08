from pathlib import Path


def _package_directory() -> Path:
    return Path(__file__).resolve().parent


def stage_classifier_model_path() -> Path:
    return _package_directory() / 'models' / 'multi-v2.onnx'


def result_panel_model_path() -> Path:
    models = _package_directory() / 'models'
    preferred = models / 'result-detector-v1.onnx'
    return preferred if preferred.is_file() else models / 'result-panel.onnx'


def hero_reference_directory() -> Path:
    return _package_directory() / 'heroes'
