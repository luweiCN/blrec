import struct
from pathlib import Path
from typing import Any

import pytest

from blrec.bili_upload.covers import (
    CoverLibrary,
    CoverResolver,
    InvalidCover,
    StoredCoverUnavailable,
)
from blrec.bili_upload.database import BiliUploadDatabase


def png(width: int = 1600, height: int = 1000) -> bytes:
    return (
        b'\x89PNG\r\n\x1a\n'
        + struct.pack('>I', 13)
        + b'IHDR'
        + struct.pack('>II', width, height)
        + b'\x08\x02\x00\x00\x00'
        + b'\x00\x00\x00\x00'
    )


def jpeg(width: int = 1600, height: int = 1000) -> bytes:
    return (
        b'\xff\xd8'
        + b'\xff\xe0\x00\x04xx'
        + b'\xff\xc0\x00\x0b\x08'
        + struct.pack('>HH', height, width)
        + b'\x01\x01\x11\x00'
        + b'\xff\xd9'
    )


async def seed_accounts(database: BiliUploadDatabase) -> None:
    for account_id in (1, 2):
        await database.execute(
            'INSERT INTO bili_accounts('
            'id,uid,display_name,credential_ciphertext,credential_version,key_id,'
            'state,created_at,updated_at) VALUES(?,?,?,X\'00\',1,\'k\',\'active\',1,1)',
            (account_id, 40 + account_id, '账号{}'.format(account_id)),
        )


@pytest.mark.asyncio
async def test_cover_library_validates_deduplicates_and_opens_images(
    tmp_path: Path,
) -> None:
    database = BiliUploadDatabase(str(tmp_path / 'upload.sqlite3'))
    await database.open()
    try:
        library = CoverLibrary(database, tmp_path / 'covers', clock=lambda: 1000)

        first = await library.add(png(), '../直播封面.png')
        duplicate = await library.add(png(), '重复文件名.png')
        second = await library.add(jpeg(), '另一张.jpg')
        opened = await library.open(first.id)

        assert duplicate == first
        assert first.filename == '直播封面.png'
        assert first.mime_type == 'image/png'
        assert (first.width, first.height) == (1600, 1000)
        assert second.mime_type == 'image/jpeg'
        assert [asset.id for asset in await library.list()] == [second.id, first.id]
        assert opened.path.read_bytes() == png()
        assert opened.view == first
        assert oct(opened.path.stat().st_mode & 0o777) == '0o600'
        assert oct((tmp_path / 'covers').stat().st_mode & 0o777) == '0o700'
    finally:
        await database.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ('content', 'message'),
    (
        (b'not-an-image', 'JPEG or PNG'),
        (png(100, 100), '1146'),
        (png() + b'x' * (2 * 1024 * 1024), '2 MiB'),
    ),
)
async def test_cover_library_rejects_invalid_images(
    tmp_path: Path, content: bytes, message: str
) -> None:
    database = BiliUploadDatabase(str(tmp_path / 'upload.sqlite3'))
    await database.open()
    try:
        library = CoverLibrary(database, tmp_path / 'covers')
        with pytest.raises(InvalidCover, match=message):
            await library.add(content, 'cover.png')
        assert await database.scalar('SELECT COUNT(*) FROM cover_assets') == 0
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_cover_library_refuses_a_database_path_outside_its_root(
    tmp_path: Path,
) -> None:
    database = BiliUploadDatabase(str(tmp_path / 'upload.sqlite3'))
    await database.open()
    try:
        library = CoverLibrary(database, tmp_path / 'covers')
        asset = await library.add(png(), 'cover.png')
        await database.execute(
            'UPDATE cover_assets SET storage_path=? WHERE id=?',
            (str(tmp_path / 'outside.png'), asset.id),
        )

        with pytest.raises(StoredCoverUnavailable, match='outside'):
            await library.open(asset.id)
    finally:
        await database.close()


class FakeProtocol:
    def __init__(self) -> None:
        self.calls = []
        self.fail = False

    async def upload_cover(
        self, bundle: Any, *, filename: str, mime_type: str, content: bytes
    ) -> str:
        self.calls.append((bundle, filename, mime_type, content))
        if self.fail:
            raise RuntimeError('upload failed')
        return 'https://archive.biliimg.com/{}/{}.jpg'.format(bundle, len(self.calls))


@pytest.mark.asyncio
async def test_cover_resolver_caches_remote_url_per_account(tmp_path: Path) -> None:
    database = BiliUploadDatabase(str(tmp_path / 'upload.sqlite3'))
    await database.open()
    try:
        await seed_accounts(database)
        library = CoverLibrary(database, tmp_path / 'covers')
        asset = await library.add(png(), 'cover.png')
        protocol = FakeProtocol()

        async def load_bundle(account_id: int) -> str:
            return 'bundle-{}'.format(account_id)

        resolver = CoverResolver(
            database, library, protocol, bundle_loader=load_bundle, clock=lambda: 1000
        )

        first = await resolver.remote_url(asset.id, 1)
        cached = await resolver.remote_url(asset.id, 1)
        other_account = await resolver.remote_url(asset.id, 2)

        assert cached == first
        assert other_account != first
        assert len(protocol.calls) == 2
        assert await database.scalar('SELECT COUNT(*) FROM cover_asset_uploads') == 2
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_cover_resolver_does_not_cache_failed_upload(tmp_path: Path) -> None:
    database = BiliUploadDatabase(str(tmp_path / 'upload.sqlite3'))
    await database.open()
    try:
        await seed_accounts(database)
        library = CoverLibrary(database, tmp_path / 'covers')
        asset = await library.add(jpeg(), 'cover.jpg')
        protocol = FakeProtocol()
        protocol.fail = True

        async def load_bundle(account_id: int) -> str:
            return 'bundle-{}'.format(account_id)

        resolver = CoverResolver(database, library, protocol, bundle_loader=load_bundle)

        with pytest.raises(RuntimeError, match='upload failed'):
            await resolver.remote_url(asset.id, 1)
        assert await database.scalar('SELECT COUNT(*) FROM cover_asset_uploads') == 0
    finally:
        await database.close()
