from labeler import backfill_material_index


def test_backfill_imports_every_historical_candidate_in_bounded_batches(
    monkeypatch,
) -> None:
    candidates = [{'source_id': f'candidate-{index}'} for index in range(1_201)]
    batch_sizes = []

    class Nas:
        def list_training_candidates(self):
            return candidates

    def sync(_conn, _nas, items, *, maximum):
        batch_sizes.append((len(items), maximum))
        return {
            'processed': len(items),
            'inserted': len(items),
            'updated': 0,
            'unchanged': 0,
            'downloaded': 0,
            'failed': 0,
            'last_error': '',
        }

    monkeypatch.setattr(
        backfill_material_index.worker_candidates, 'sync_worker_candidates', sync
    )
    monkeypatch.setattr(
        backfill_material_index.db,
        'rebuild_training_review_material_index',
        lambda _conn, *, batch_size, progress: {'indexed': 1, 'batch_size': batch_size},
    )

    result = backfill_material_index.backfill_material_index(
        object(), Nas(), batch_size=500
    )

    assert batch_sizes == [(500, 500), (500, 500), (201, 201)]
    assert result['candidates']['total'] == 1_201
    assert result['candidates']['inserted'] == 1_201
    assert result['index'] == {'indexed': 1, 'batch_size': 500}
