import hashlib
import json
from pathlib import Path

import pytest
from blrec_analysis_worker.model_package import (
    ClassificationPrediction,
    PackageHeroRecognizer,
    PackageRecordedPlayerDetector,
    PackageStageClassifier,
    load_model_package,
)

from blrec.vainglory.stage_classifier import (
    MODE_3V3,
    MODE_ARAM,
    STAGE_GAMEPLAY,
    STAGE_OUT_OF_MATCH,
    STAGE_PRE_MATCH,
    STAGE_TRANSITION,
)
from blrec.vainglory.vision import RgbFrame


def _write_package(root: Path) -> Path:
    package = root / 'vision-package'
    models = package / 'models'
    models.mkdir(parents=True)
    roles = (
        'match_flow',
        'hero_select',
        'match_mode',
        'result_panel',
        'hero_avatar',
        'hero_identity',
        'player_position',
    )
    manifest_models = {}
    for role in roles:
        model_path = models / f'{role}.onnx'
        model_path.write_bytes(role.encode('utf-8'))
        manifest_models[role] = {
            'file': f'models/{role}.onnx',
            'sha256': hashlib.sha256(model_path.read_bytes()).hexdigest(),
            'kind': (
                'detection'
                if role in {'result_panel', 'hero_avatar'}
                else 'classification'
            ),
            'input': {
                'width': 640 if role in {'result_panel', 'hero_avatar'} else 512,
                'height': 640 if role in {'result_panel', 'hero_avatar'} else 288,
                'color': 'RGB',
                'resize': (
                    'letterbox'
                    if role in {'result_panel', 'hero_avatar'}
                    else 'aspect_fit_letterbox'
                ),
                'pad_value': 114,
                'preserve_full_image': role not in {'result_panel', 'hero_avatar'},
                'scale': '0_to_1',
                'normalize': (
                    'none' if role in {'result_panel', 'hero_avatar'} else 'imagenet'
                ),
            },
            'classes': (
                [role]
                if role in {'result_panel', 'hero_avatar'}
                else {
                    'match_flow': ['match_flow', 'not_match_flow'],
                    'hero_select': [
                        'not_select',
                        'select_3v3',
                        'select_5v5',
                        'select_aram',
                    ],
                    'match_mode': ['3v3', '5v5', 'aram'],
                    'hero_identity': ['Kestrel', 'Ringo'],
                    'player_position': ['left1', 'right1'],
                }[role]
            ),
            'dataset_version': f'{role}-v1',
            'training_run_id': f'{role}-run-1',
        }
    manifest = {
        'schema_version': 2,
        'package_id': 'vision-package',
        'pipeline_version': 'timeline-v2',
        'status': 'ready',
        'missing_roles': [],
        'models': manifest_models,
        'runtime': {
            'coarse_interval_ms': 5_000,
            'maximum_keyframe_distance_ms': 2_500,
            'result_scan_fps': 4,
            'thresholds': {'match_flow': 0.55, 'result_panel': 0.55},
        },
        'compatibility': {
            'analysis_protocol_version': 2,
            'product': 'blrec-analysis-worker',
        },
    }
    (package / 'manifest.json').write_text(json.dumps(manifest), encoding='utf-8')
    return package


def test_load_model_package_validates_models_and_runtime_contract(
    tmp_path: Path,
) -> None:
    package = load_model_package(_write_package(tmp_path))

    assert package.package_id == 'vision-package'
    assert package.pipeline_version == 'timeline-v2'
    assert package.runtime.coarse_interval_ms == 5_000
    assert package.runtime.result_scan_fps == 4
    assert package.model('match_flow').classes == ('match_flow', 'not_match_flow')
    assert package.model('match_flow').input_width == 512
    assert package.model('result_panel').kind == 'detection'


def test_load_model_package_rejects_changed_model_file(tmp_path: Path) -> None:
    package_path = _write_package(tmp_path)
    (package_path / 'models/match_flow.onnx').write_bytes(b'changed')

    with pytest.raises(ValueError, match='SHA-256'):
        load_model_package(package_path)


def test_load_model_package_rejects_incomplete_required_roles(tmp_path: Path) -> None:
    package_path = _write_package(tmp_path)
    manifest_path = package_path / 'manifest.json'
    manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    del manifest['models']['match_mode']
    manifest_path.write_text(json.dumps(manifest), encoding='utf-8')

    with pytest.raises(ValueError, match='match_mode'):
        load_model_package(package_path)


def test_load_model_package_rejects_model_path_outside_package(tmp_path: Path) -> None:
    package_path = _write_package(tmp_path)
    manifest_path = package_path / 'manifest.json'
    manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    manifest['models']['match_flow']['file'] = '../outside.onnx'
    manifest_path.write_text(json.dumps(manifest), encoding='utf-8')

    with pytest.raises(ValueError, match='模型包目录'):
        load_model_package(package_path)


class _Classifier:
    def __init__(self, label: str, confidence: float) -> None:
        self.prediction = ClassificationPrediction(label, confidence, ())

    def predict(self, _frame: RgbFrame) -> ClassificationPrediction:
        return self.prediction


def _stage_classifier(
    *,
    flow: tuple[str, float] = ('match_flow', 0.9),
    select: tuple[str, float] = ('not_select', 0.9),
    mode: tuple[str, float] = ('3v3', 0.8),
) -> PackageStageClassifier:
    return PackageStageClassifier(
        package_id='vision-package',
        match_flow=_Classifier(*flow),
        hero_select=_Classifier(*select),
        match_mode=_Classifier(*mode),
        thresholds={'match_flow': 0.55, 'hero_select': 0.55, 'match_mode': 0.5},
    )


def test_package_stage_classifier_maps_new_models_to_timeline_states() -> None:
    frame = RgbFrame(1, 1, b'\x00\x00\x00')

    gameplay = _stage_classifier(mode=('aram', 0.88)).classify(frame)
    hero_select = _stage_classifier(select=('select_3v3', 0.92)).classify(frame)
    outside = _stage_classifier(flow=('not_match_flow', 0.95)).classify(frame)
    uncertain = _stage_classifier(flow=('not_match_flow', 0.51)).classify(frame)

    assert (gameplay.stage, gameplay.mode) == (STAGE_GAMEPLAY, MODE_ARAM)
    assert (hero_select.stage, hero_select.mode) == (STAGE_PRE_MATCH, MODE_3V3)
    assert outside.stage == STAGE_OUT_OF_MATCH
    assert uncertain.stage == STAGE_TRANSITION
    assert gameplay.model_version == 'vision-package'


def test_package_stage_classifier_finds_selection_outside_match_flow() -> None:
    frame = RgbFrame(1, 1, b'\x00\x00\x00')

    selection = _stage_classifier(
        flow=('not_match_flow', 0.96),
        select=('select_aram', 0.93),
    ).classify(frame)

    assert (selection.stage, selection.mode) == (STAGE_PRE_MATCH, MODE_ARAM)
    assert selection.match_flow_label == 'not_match_flow'
    assert selection.hero_select_label == 'select_aram'


def test_package_hero_and_player_classifiers_keep_low_confidence_as_unknown() -> None:
    frame = RgbFrame(1, 1, b'\x00\x00\x00')
    hero = PackageHeroRecognizer(_Classifier('Kestrel', 0.91), threshold=0.5)
    unclear_hero = PackageHeroRecognizer(_Classifier('Kestrel', 0.41), threshold=0.5)
    player = PackageRecordedPlayerDetector(_Classifier('right3', 0.88), threshold=0.5)
    invalid_player = PackageRecordedPlayerDetector(
        _Classifier('right5', 0.88), threshold=0.5
    )
    layout = type('Layout', (), {'team_size': 3})()

    assert hero.recognize(frame).label == 'Kestrel'
    assert unclear_hero.recognize(frame) is None
    assert player.detect(frame, layout).side == 'right'
    assert player.detect(frame, layout).slot == 3
    assert invalid_player.detect(frame, layout) is None
