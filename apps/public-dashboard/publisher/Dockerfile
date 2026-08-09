# syntax=docker/dockerfile:1.7

FROM python:3.11-slim-bookworm AS wheel-builder
WORKDIR /build
COPY apps/public-dashboard/publisher/pyproject.toml \
     apps/public-dashboard/publisher/README.md ./
COPY apps/public-dashboard/publisher/src ./src
RUN python -m pip wheel --no-cache-dir --no-deps --wheel-dir /wheels .

FROM python:3.11-slim-bookworm AS dependency-builder
WORKDIR /build
RUN apt-get update && \
    apt-get install -y --no-install-recommends gcc libc6-dev && \
    rm -rf /var/lib/apt/lists/*
COPY apps/public-dashboard/publisher/requirements.txt /tmp/requirements.txt
RUN python -m pip wheel --no-cache-dir \
    --wheel-dir /dependency-wheels \
    -r /tmp/requirements.txt

FROM python:3.11-slim-bookworm AS runtime
WORKDIR /app
RUN apt-get update && \
    apt-get install -y --no-install-recommends iproute2 && \
    rm -rf /var/lib/apt/lists/*
COPY apps/public-dashboard/publisher/requirements.txt /tmp/requirements.txt
RUN --mount=type=bind,from=dependency-builder,source=/dependency-wheels,target=/dependency-wheels \
    python -m pip install --no-cache-dir --no-index \
        --find-links=/dependency-wheels \
        -r /tmp/requirements.txt && \
    rm /tmp/requirements.txt

# Publisher 共用 Server 的网络选路契约，但不安装 Server 应用或它的运行依赖。
COPY src/blrec/__init__.py /app/blrec/__init__.py
COPY src/blrec/networking /app/blrec/networking
COPY src/blrec/logging /app/blrec/logging
COPY src/blrec/utils /app/blrec/utils
COPY --from=wheel-builder /wheels /wheels
RUN wheel="$(find /wheels -maxdepth 1 -name 'blrec_dashboard_publisher-*.whl' -print -quit)" && \
    test -n "$wheel" && \
    python -m pip install --no-cache-dir --no-deps "$wheel" && \
    rm -rf /wheels

ARG VERSION=dev
ARG REVISION=unknown
LABEL org.opencontainers.image.source="https://github.com/luweiCN/blrec" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.revision="${REVISION}" \
      org.opencontainers.image.licenses="GPL-3.0-only" \
      org.opencontainers.image.description="BLREC Public Dashboard data publisher"
ENV PYTHONPATH=/app \
    TZ=Asia/Shanghai \
    BLREC_CONFIG=/cfg/settings.toml \
    DASHBOARD_DATABASE=/cfg/blrec.sqlite3 \
    DASHBOARD_STATE=/state \
    OSS_ENDPOINT=https://oss-cn-beijing.aliyuncs.com \
    OSS_BUCKET=luwei-vainglory \
    OSS_PREFIX=data \
    DASHBOARD_SCHEDULE_HOUR=0 \
    DASHBOARD_SCHEDULE_MINUTE=5 \
    DASHBOARD_RETRY_SECONDS=900
VOLUME ["/cfg", "/state"]
STOPSIGNAL SIGTERM
HEALTHCHECK NONE
ENTRYPOINT ["blrec-dashboard-publisher"]
