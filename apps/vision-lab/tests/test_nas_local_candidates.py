from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from labeler.nas import NasClient


class LocalCandidateStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.client = NasClient(candidate_local_root=self.root)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _candidate(self) -> dict[str, object]:
        content = b'candidate-image'
        digest = hashlib.sha256(content).hexdigest()
        relative = f'objects/{digest[:2]}/{digest}.jpg'
        image = self.root / relative
        image.parent.mkdir(parents=True)
        image.write_bytes(content)
        metadata = {
            'schema_version': 3,
            'task': 'unified_review',
            'source_id': 'part-1:1000:test',
            'image_path': relative,
            'image_sha256': digest,
            'at_ms': 1000,
            'suggestions': {'match_flow': {'label': 'match_flow', 'confidence': 0.9}},
        }
        item = self.root / 'items/session-1/part-1/000000001000-test.json'
        item.parent.mkdir(parents=True)
        item.write_text(json.dumps(metadata), encoding='utf-8')
        return metadata

    def test_reads_metadata_and_references_mounted_image(self) -> None:
        metadata = self._candidate()

        self.assertEqual(self.client.list_training_candidates(), [metadata])
        image_path = self.client.training_candidate_local_path(
            str(metadata['image_path'])
        )
        self.assertIsNotNone(image_path)
        assert image_path is not None
        self.assertEqual(image_path.read_bytes(), b'candidate-image')

    def test_review_is_written_atomically_next_to_object(self) -> None:
        metadata = self._candidate()
        review = {
            'schema_version': 2,
            'image_path': metadata['image_path'],
            'review_status': 'confirmed',
        }

        self.client.write_training_candidate_review(str(metadata['image_path']), review)

        self.assertEqual(self.client.list_training_candidate_reviews(), [review])

    def test_candidate_and_review_lists_share_one_directory_scan(self) -> None:
        metadata = self._candidate()
        review = {
            'schema_version': 2,
            'image_path': metadata['image_path'],
            'review_status': 'confirmed',
        }
        self.client.write_training_candidate_review(str(metadata['image_path']), review)
        scans = 0
        original = self.client._scan_local_candidate_json

        def counted():
            nonlocal scans
            scans += 1
            return original()

        self.client._scan_local_candidate_json = counted

        self.assertEqual(self.client.list_training_candidates(), [metadata])
        self.assertEqual(self.client.list_training_candidate_reviews(), [review])
        self.assertEqual(scans, 1)

    def test_rejects_path_outside_mount(self) -> None:
        with self.assertRaises(ValueError):
            self.client.training_candidate_local_path('../secret.jpg')


if __name__ == '__main__':
    unittest.main()
