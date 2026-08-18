from labeler import stats as stats_mod


class _Cursor:
    def __init__(self, rows=()):
        self.rows = list(rows)

    def fetchone(self):
        return self.rows[0] if self.rows else (0,)

    def fetchall(self):
        return self.rows

    def __iter__(self):
        return iter(self.rows)


class _Connection:
    def __init__(self):
        self.calls = []

    def execute(self, sql, parameters=()):
        self.calls.append((sql, parameters))
        return _Cursor()


def test_empty_quality_flags_are_queried_as_a_bound_value() -> None:
    connection = _Connection()

    stats_mod.stats(connection)

    assert (
        'SELECT quality_flags FROM annotations WHERE quality_flags != ?',
        ('[]',),
    ) in connection.calls


def test_malformed_quality_flags_do_not_break_legacy_stats() -> None:
    class QualityConnection(_Connection):
        def execute(self, sql, parameters=()):
            self.calls.append((sql, parameters))
            if sql.startswith('SELECT quality_flags'):
                return _Cursor((('not-json',), ('["blur"]',), ('{}',)))
            return _Cursor()

    result = stats_mod.stats(QualityConnection())

    assert result['quality_flags'] == {'blur': 1}
