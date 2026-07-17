# syntax=docker/dockerfile:1

FROM --platform=$BUILDPLATFORM node:18-bookworm-slim AS webapp-builder
WORKDIR /build
COPY webapp/package.json webapp/package-lock.json ./webapp/
RUN cd webapp && npm ci
COPY webapp ./webapp
RUN cd webapp && npm run build

FROM python:3.11-slim-bookworm AS wheel-builder
WORKDIR /build
RUN apt-get update && \
    apt-get install -y --no-install-recommends build-essential python3-dev && \
    rm -rf /var/lib/apt/lists/*
COPY pyproject.toml setup.py setup.cfg MANIFEST.in README.md LICENSE ./
COPY src ./src
COPY --from=webapp-builder /build/src/blrec/data/webapp ./src/blrec/data/webapp
RUN python -m pip wheel --no-cache-dir --wheel-dir /wheels .

FROM python:3.11-slim-bookworm AS runtime
ARG VERSION=dev
ARG REVISION=unknown
LABEL org.opencontainers.image.source="https://github.com/luweiCN/blrec" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.revision="${REVISION}" \
      org.opencontainers.image.licenses="GPL-3.0-only" \
      org.opencontainers.image.description="Bilibili live recording and publishing service"
WORKDIR /app
RUN apt-get update && \
    apt-get install -y --no-install-recommends ffmpeg iproute2 && \
    rm -rf /var/lib/apt/lists/*
COPY --from=wheel-builder /wheels /wheels
RUN python -m pip install --no-cache-dir --no-index --find-links=/wheels blrec && \
    rm -rf /wheels
COPY scripts/migrate_legacy_settings.py /app/scripts/migrate_legacy_settings.py
COPY scripts/migrate_biliupforjava_rooms.py /app/scripts/migrate_biliupforjava_rooms.py
ENV BLREC_DEFAULT_SETTINGS_FILE=/cfg/settings.toml \
    BLREC_DEFAULT_LOG_DIR=/log \
    BLREC_DEFAULT_OUT_DIR=/rec \
    TZ=Asia/Shanghai
VOLUME ["/cfg", "/log", "/rec"]
EXPOSE 2233
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:2233/api/v1/auth/status', timeout=3).read()"]
ENTRYPOINT ["blrec", "--host", "0.0.0.0", "--no-progress"]
CMD []
