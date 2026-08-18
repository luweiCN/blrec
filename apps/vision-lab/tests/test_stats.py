from labeler import stats as stats_mod


class _Cursor:
    def fetchone(self):
        return (0,)

    def fetchall(self):
        return []

    def __iter__(self):
        return iter(())


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
