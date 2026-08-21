"""远程数据集只能在 Worker 本地物化，并保持清单标签和框。"""

import hashlib
import json
import tempfile
from pathlib import Path

from labeler.remote_dataset import materialize_dataset
from PIL import Image


def _jpeg(path: Path, color: tuple[int, int, int]) -> str:
    Image.new('RGB', (100, 60), color).save(path, format='JPEG', quality=95)
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _manifest(path: Path, samples: list[dict]) -> None:
    path.write_text(
        ''.join(json.dumps(sample) + '\n' for sample in samples), encoding='utf-8'
    )


def test_materializes_full_frame_classification_from_fetcher() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        source = root / 'source.jpg'
        sha256 = _jpeg(source, (10, 20, 30))
        manifest = root / 'samples.jsonl'
        _manifest(
            manifest,
            [
                {
                    'sample_id': 'f00000001',
                    'frame_id': 1,
                    'video_id': 10,
                    'sha256': sha256,
                    'split': 'train',
                    'label': '3v3',
                }
            ],
        )

        result = materialize_dataset(
            task_id='match_mode',
            manifest_path=manifest,
            output_dir=root / 'dataset',
            fetch_image=lambda _frame_id, destination: destination.write_bytes(
                source.read_bytes()
            ),
        )

        assert result['samples'] == 1
        assert (root / 'dataset/images/train/3v3/f00000001.jpg').is_file()
        assert (root / 'dataset/.materialized.json').is_file()


def test_materializes_hero_crop_and_reuses_one_source_frame() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        source = root / 'source.jpg'
        sha256 = _jpeg(source, (100, 120, 140))
        manifest = root / 'samples.jsonl'
        samples = [
            {
                'sample_id': f'f00000001-left-{slot}',
                'frame_id': 1,
                'video_id': 10,
                'sha256': sha256,
                'split': 'train',
                'label': label,
                'crop': {'x': 0.1 * slot, 'y': 0.1, 'w': 0.2, 'h': 0.3},
            }
            for slot, label in ((1, 'hero-a'), (2, 'hero-b'))
        ]
        _manifest(manifest, samples)
        calls = []

        def fetch(frame_id: int, destination: Path) -> None:
            calls.append(frame_id)
            destination.write_bytes(source.read_bytes())

        materialize_dataset(
            task_id='hero_identity',
            manifest_path=manifest,
            output_dir=root / 'dataset',
            fetch_image=fetch,
        )

        assert calls == [1]
        crop = Image.open(root / 'dataset/images/train/hero-a/f00000001-left-1.jpg')
        assert crop.size == (20, 18)


def test_materializes_detector_labels() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        source = root / 'source.jpg'
        sha256 = _jpeg(source, (200, 180, 160))
        manifest = root / 'samples.jsonl'
        _manifest(
            manifest,
            [
                {
                    'sample_id': 'f00000001',
                    'frame_id': 1,
                    'video_id': 10,
                    'sha256': sha256,
                    'split': 'val',
                    'detector_label': 'result_panel',
                    'boxes': {'result_panel': {'x': 0.1, 'y': 0.2, 'w': 0.4, 'h': 0.6}},
                }
            ],
        )

        materialize_dataset(
            task_id='result_detector',
            manifest_path=manifest,
            output_dir=root / 'dataset',
            fetch_image=lambda _frame_id, destination: destination.write_bytes(
                source.read_bytes()
            ),
        )

        label = (root / 'dataset/labels/val/f00000001.txt').read_text()
        assert label == '0 0.300000 0.500000 0.400000 0.600000\n'
        assert "names: ['result_panel']" in (root / 'dataset/data.yaml').read_text()


def test_reuses_shared_frame_cache_between_dataset_versions() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        source = root / 'source.jpg'
        sha256 = _jpeg(source, (40, 50, 60))
        manifest = root / 'samples.jsonl'
        _manifest(
            manifest,
            [
                {
                    'sample_id': 'f00000001',
                    'frame_id': 1,
                    'video_id': 10,
                    'sha256': sha256,
                    'split': 'train',
                    'label': 'match_flow',
                }
            ],
        )
        calls = []

        def fetch(frame_id: int, destination: Path) -> None:
            calls.append(frame_id)
            destination.write_bytes(source.read_bytes())

        def unexpected_fetch(_frame_id: int, _destination: Path) -> None:
            raise AssertionError('第二版数据集不应重复下载同一原图')

        cache = root / 'frame-cache'
        materialize_dataset(
            task_id='match_flow',
            manifest_path=manifest,
            output_dir=root / 'dataset-v1',
            fetch_image=fetch,
            frame_cache_dir=cache,
            download_workers=2,
        )
        materialize_dataset(
            task_id='match_flow',
            manifest_path=manifest,
            output_dir=root / 'dataset-v2',
            fetch_image=unexpected_fetch,
            frame_cache_dir=cache,
            download_workers=2,
        )

        assert calls == [1]
        assert (root / 'dataset-v2/images/train/match_flow/f00000001.jpg').is_file()
