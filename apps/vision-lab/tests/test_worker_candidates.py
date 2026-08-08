"""MacBook worker 候选同步到本地 BP 复核队列。"""

import hashlib
import io
import sys
import tempfile
import unittest
from pathlib import Path

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from labeler import config, db, worker_candidates


class FakeNas:
    def __init__(self, image: bytes):
        self.image = image
        self.downloads = 0

    def read_training_candidate(self, _relative_path: str) -> bytes:
        self.downloads += 1
        return self.image


class TestWorkerCandidateSync(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.conn = db.connect(self.root / 'lab.db')
        self.old_frame_dir = config.FRAME_DIR
        self.old_thumb_dir = config.THUMB_DIR
        config.FRAME_DIR = self.root / 'frames'
        config.THUMB_DIR = self.root / 'thumbs'
        buffer = io.BytesIO()
        Image.new('RGB', (32, 18), (20, 40, 60)).save(buffer, format='JPEG')
        self.image = buffer.getvalue()
        self.nas = FakeNas(self.image)

    def tearDown(self):
        config.FRAME_DIR = self.old_frame_dir
        config.THUMB_DIR = self.old_thumb_dir
        self.conn.close()
        self.tmp.cleanup()

    def item(self, label='bp_aram'):
        return {
            'schema_version': 1,
            'task': 'bp_review',
            'source_id': 'part-7:12000:test',
            'session_id': 3,
            'part_id': 7,
            'part_index': 2,
            'at_ms': 12_000,
            'segment_start_ms': 10_000,
            'streamer': '测试主播',
            'room_id': '123',
            'session_title': '测试直播',
            'filename': 'sample.flv',
            'model_version': 'multi-v2',
            'suggested_label': label,
            'suggestion_confidence': 0.8,
            'stage_class': 'pre_match',
            'stage_confidence': 0.9,
            'mode_class': 'aram',
            'mode_confidence': 0.8,
            'selection_reason': 'worker 开局候选',
            'image_path': 'session-3/part-7/frame.jpg',
            'image_sha256': hashlib.sha256(self.image).hexdigest(),
            'created_at': 100,
        }

    def test_imports_image_and_model_suggestion(self):
        result = worker_candidates.sync_worker_candidates(
            self.conn, self.nas, [self.item()]
        )

        self.assertEqual(result['inserted'], 1)
        self.assertEqual(result['downloaded'], 1)
        pending = db.list_bp_review_items(self.conn, status='pending')
        self.assertEqual(pending[0]['suggested_label'], 'bp_aram')
        self.assertTrue(Path(pending[0]['frame_path']).is_file())
        self.assertEqual(db.list_videos(self.conn), [])

    def test_resync_does_not_download_again_or_erase_human_confirmation(self):
        worker_candidates.sync_worker_candidates(self.conn, self.nas, [self.item()])
        frame_id = db.list_bp_review_items(self.conn, status='pending')[0]['frame_id']
        db.review_bp_item(self.conn, frame_id=frame_id, label='bp_3v3')

        result = worker_candidates.sync_worker_candidates(
            self.conn, self.nas, [self.item('bp_5v5')]
        )

        self.assertEqual(result['updated'], 1)
        self.assertEqual(self.nas.downloads, 1)
        confirmed = db.list_bp_review_items(self.conn, status='confirmed')[0]
        self.assertEqual(confirmed['confirmed_label'], 'bp_3v3')
        self.assertEqual(confirmed['suggested_label'], 'bp_5v5')

    def test_key_screen_candidate_uses_its_own_review_queue(self):
        item = self.item()
        item.update({
            'task': 'key_screen_review',
            'suggested_label': 'scoreboard',
            'stage_class': 'scoreboard',
            'selection_reason': 'worker 粗扫识别为计分板',
        })

        result = worker_candidates.sync_worker_candidates(
            self.conn, self.nas, [item])

        self.assertEqual(result['inserted'], 1)
        self.assertEqual(db.list_bp_review_items(self.conn), [])
        pending = db.list_key_screen_review_items(self.conn, status='pending')
        self.assertEqual(pending[0]['suggested_label'], 'scoreboard')


if __name__ == '__main__':
    unittest.main()
