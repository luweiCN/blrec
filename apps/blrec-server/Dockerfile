# syntax=docker/dockerfile:1.7

ARG BUILDPLATFORM=linux/amd64
FROM --platform=$BUILDPLATFORM node:18-bookworm-slim AS webapp-builder
WORKDIR /build
COPY apps/blrec-server/webapp/package.json \
     apps/blrec-server/webapp/package-lock.json \
     ./apps/blrec-server/webapp/
RUN cd apps/blrec-server/webapp && npm ci
COPY apps/blrec-server/webapp ./apps/blrec-server/webapp
RUN cd apps/blrec-server/webapp && npm run build

FROM python:3.11-slim-bookworm AS wheel-builder
WORKDIR /build
COPY pyproject.toml setup.py setup.cfg MANIFEST.in README.md LICENSE ./
COPY src ./src
COPY --from=webapp-builder /build/src/blrec/data/webapp ./src/blrec/data/webapp
RUN find src/blrec/data/vainglory -maxdepth 1 -name '*.onnx' -delete && \
    python -m pip wheel --no-cache-dir --no-deps --wheel-dir /wheels .

FROM python:3.11-slim-bookworm AS dependency-builder
WORKDIR /build
RUN apt-get update && \
    apt-get install -y --no-install-recommends gcc libc6-dev && \
    rm -rf /var/lib/apt/lists/*
COPY apps/blrec-server/requirements.txt /tmp/server-requirements.txt
RUN python -m pip wheel --no-cache-dir \
    --wheel-dir /dependency-wheels \
    -r /tmp/server-requirements.txt

FROM python:3.11-slim-bookworm AS runtime
WORKDIR /app
RUN apt-get update && \
    apt-get install -y --no-install-recommends ffmpeg iproute2 && \
    rm -rf /var/lib/apt/lists/*
COPY apps/blrec-server/requirements.txt /tmp/server-requirements.txt
RUN --mount=type=bind,from=dependency-builder,source=/dependency-wheels,target=/dependency-wheels \
    python -m pip install --no-cache-dir --no-index \
        --find-links=/dependency-wheels \
        -r /tmp/server-requirements.txt && \
    rm /tmp/server-requirements.txt
RUN --mount=type=bind,from=wheel-builder,source=/wheels,target=/wheels \
    wheel="$(find /wheels -maxdepth 1 -name 'blrec-*.whl' -print -quit)" && \
    test -n "$wheel" && \
    python -m pip install --no-cache-dir --no-deps "$wheel" && \
    find /usr/local/lib/python3.11/site-packages/blrec/data/vainglory \
        -maxdepth 1 -name '*.onnx' -delete && \
    rm -f /usr/local/bin/blrec-analysis-worker && \
    python -c "import importlib.util; names=('cv2','numpy','onnxruntime','rapidocr'); assert all(importlib.util.find_spec(name) is None for name in names)" && \
    test ! -e /usr/local/bin/blrec-analysis-worker
COPY scripts/migrate_legacy_settings.py /app/scripts/migrate_legacy_settings.py
COPY scripts/migrate_biliupforjava_rooms.py /app/scripts/migrate_biliupforjava_rooms.py
COPY scripts/backup_blrec_database.py /app/scripts/backup_blrec_database.py
COPY scripts/vainglory_reanalysis_recovery_20260812.py \
     /app/scripts/vainglory_reanalysis_recovery_20260812.py
COPY scripts/vainglory_reanalysis_recovery_20260812.jsonl \
     /app/scripts/vainglory_reanalysis_recovery_20260812.jsonl
ARG VERSION=dev
ARG REVISION=unknown
LABEL org.opencontainers.image.source="https://github.com/luweiCN/blrec" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.revision="${REVISION}" \
      org.opencontainers.image.licenses="GPL-3.0-only" \
      org.opencontainers.image.description="BLREC server and administration web application"
ENV BLREC_DEFAULT_SETTINGS_FILE=/cfg/settings.toml \
    BLREC_DEFAULT_LOG_DIR=/log \
    BLREC_DEFAULT_OUT_DIR=/rec \
    BLREC_VAINGLORY_WORKER_MODE=remote \
    TZ=Asia/Shanghai
VOLUME ["/cfg", "/log", "/rec", "/favorites", "/clips", "/result-frames"]
EXPOSE 2233
STOPSIGNAL SIGTERM
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:2233/api/v1/auth/status', timeout=3).read()"]
ENTRYPOINT ["blrec", "--host", "0.0.0.0", "--no-progress"]
CMD []
