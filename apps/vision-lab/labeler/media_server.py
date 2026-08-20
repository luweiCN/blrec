"""NAS 图片服务与候选素材增量接收进程。"""

from __future__ import annotations

import threading
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import Response

from . import __version__, config, server


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
