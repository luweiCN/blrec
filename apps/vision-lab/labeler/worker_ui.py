"""Vision Worker 本地标注控制面入口。"""

from __future__ import annotations

from fastapi import FastAPI

from . import config


def create_worker_control_plane_app() -> FastAPI:
    """返回完整本地控制面，禁止退回到 NAS 全量 API 代理。"""

    if not config.DATABASE_URL:
        raise RuntimeError('Worker 本地控制面必须配置 PostgreSQL')
    if not config.CONTROL_PLANE_ONLY:
        raise RuntimeError('Worker 本地控制面必须启用 CONTROL_PLANE_ONLY')
    if not config.MEDIA_SERVER_URL:
        raise RuntimeError('Worker 本地控制面必须配置 MEDIA_SERVER_URL')
    from .server import app

    return app
