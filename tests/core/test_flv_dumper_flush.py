from pathlib import Path
from unittest.mock import Mock

from blrec.flv.operators.dump import Dumper


def test_large_recording_buffer_is_flushed_for_live_readers(tmp_path: Path) -> None:
    dumper = Dumper(lambda: (str(tmp_path / 'recording.flv'), 0), 64 * 1024**2)
    file = Mock()
    dumper._file = file

    dumper._record_write(1024**2 - 1)
    file.flush.assert_not_called()

    dumper._record_write(1)
    file.flush.assert_called_once_with()
