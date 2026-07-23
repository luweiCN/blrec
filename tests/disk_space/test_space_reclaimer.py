import os
from pathlib import Path

import pytest

from blrec.disk_space import SpaceMonitor, SpaceReclaimer


@pytest.mark.asyncio
async def test_fallback_reclaimer_never_scans_sibling_favorites(tmp_path: Path) -> None:
    root = tmp_path / 'records'
    regular = root / '100' / 'recording.flv'
    permanent = root.parent / 'favorites' / ('a' * 32) / 'part-0001.mp4'
    regular.parent.mkdir(parents=True)
    permanent.parent.mkdir(parents=True)
    regular.write_bytes(b'recording')
    permanent.write_bytes(b'permanent')
    os.utime(regular, (1, 1))
    os.utime(permanent, (1, 1))
    reclaimer = SpaceReclaimer(
        SpaceMonitor(str(root), check_interval=0), str(root), recycle_records=True
    )

    paths = await reclaimer._get_record_file_paths(2)

    assert paths == [str(regular)]
