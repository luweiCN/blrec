"""NAS 图片服务与候选素材增量接收进程。"""

from __future__ import annotations

import threading
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, Response

from . import __version__, config, database_backup, server


@asynccontextmanager
async def lifespan(_app: FastAPI):
    stop = threading.Event()
    index_thread = None
    if (
        config.CANDIDATE_LOCAL_DIR is not None
        and config.CANDIDATE_RECONCILIATION_ENABLED
    ):
        index_thread = threading.Thread(
            target=server._candidate_index_loop,
            args=(stop,),
            daemon=True,
            name='vision-candidate-index',
        )
        index_thread.start()
    try:
        yield
    finally:
        stop.set()
        if index_thread is not None:
            index_thread.join(timeout=5)
        server.db.close_connections()


app = FastAPI(title='BLREC Vision NAS Media', version=__version__, lifespan=lifespan)


@app.get('/api/config')
def api_config() -> dict[str, object]:
    return {'version': __version__, 'role': 'media-indexer'}


@app.get('/api/frames/{frame_id}/image')
def api_frame_image(frame_id: int) -> Response:
    return server.api_frame_image(frame_id)


@app.get('/api/frames/{frame_id}/thumb')
def api_frame_thumb(frame_id: int) -> Response:
    return server.api_frame_thumb(frame_id)


@app.get('/api/vision-workers/frames/{frame_id}/image')
def api_vision_worker_frame_image(frame_id: int, request: Request) -> Response:
    server._require_vision_worker(request)
    return server.api_frame_image(frame_id)


@app.get('/api/vision-workers/datasets/{version_id}/manifest')
def api_vision_worker_dataset_manifest(version_id: str, request: Request) -> Response:
    return server.api_vision_worker_dataset_manifest(version_id, request)


@app.get('/api/vision-workers/model-runs/{run_id}/artifact')
def api_vision_worker_model_artifact(run_id: str, request: Request) -> Response:
    return server.api_vision_worker_model_artifact(run_id, request)


@app.get('/api/vision-workers/model-runs/{run_id}/metadata')
def api_vision_worker_model_metadata(run_id: str, request: Request) -> Response:
    return server.api_vision_worker_model_metadata(run_id, request)


@app.get('/api/vision-workers/model-packages/{package_id}/archive')
def api_vision_worker_model_package_archive(
    package_id: str, request: Request
) -> Response:
    server._require_vision_worker(request)
    return server.api_model_package_archive(package_id)


@app.get('/api/vision-workers/database-backups')
def api_vision_database_backups(request: Request) -> dict[str, object]:
    server._require_vision_worker(request)
    return {'backups': database_backup.list_backups(config.DATABASE_BACKUP_DIR)}


@app.get('/api/vision-workers/database-backups/latest')
def api_latest_vision_database_backup(request: Request) -> FileResponse:
    server._require_vision_worker(request)
    backups = database_backup.list_backups(config.DATABASE_BACKUP_DIR)
    if not backups:
        raise HTTPException(404, '尚无 Vision Lab 数据库备份')
    path = config.DATABASE_BACKUP_DIR / str(backups[0]['name'])
    checksum_path = path.with_suffix(path.suffix + '.sha256')
    checksum = (
        checksum_path.read_text(encoding='ascii').split()[0]
        if checksum_path.is_file()
        else ''
    )
    return FileResponse(
        path,
        media_type='application/octet-stream',
        filename=path.name,
        headers={
            'Cache-Control': 'no-store',
            'X-Checksum-Sha256': checksum,
            'X-Backup-Filename': path.name,
        },
    )


@app.put('/api/vision-workers/database-backups/{filename}')
async def api_upload_vision_database_backup(
    filename: str, request: Request
) -> dict[str, object]:
    server._require_vision_worker(request)
    try:
        expected_length = int(request.headers.get('content-length') or 0)
        return await database_backup.store_backup_stream(
            request.stream(),
            directory=config.DATABASE_BACKUP_DIR,
            filename=filename,
            expected_length=expected_length,
            maximum_bytes=config.DATABASE_BACKUP_MAX_BYTES,
            keep=config.DATABASE_BACKUP_KEEP,
        )
    except FileExistsError as exc:
        raise HTTPException(409, '同名 Vision Lab 备份已存在') from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.post('/api/training-candidates/ingest')
def api_training_candidates_ingest(
    body: dict[str, object], request: Request
) -> dict[str, object]:
    """接收 BLREC Server 刚落盘的一小批候选并立即写入共享数据库。"""
    server._require_vision_worker(request)
    if int(body.get('schema_version') or 0) != 1:
        raise HTTPException(400, '候选增量协议版本无效')
    raw_candidates = body.get('candidates')
    if not isinstance(raw_candidates, list) or not raw_candidates:
        raise HTTPException(400, '候选增量不能为空')
    if len(raw_candidates) > 100 or not all(
        isinstance(item, dict) for item in raw_candidates
    ):
        raise HTTPException(400, '单次最多接收 100 张候选')
    conn = server._conn()
    try:
        return server.worker_candidates.sync_worker_candidates(
            conn, server._nas(), raw_candidates, maximum=len(raw_candidates)
        )
    finally:
        conn.close()


def main() -> None:
    import uvicorn

    uvicorn.run(app, host=config.SERVER_HOST, port=config.SERVER_PORT, log_level='info')


if __name__ == '__main__':
    main()
