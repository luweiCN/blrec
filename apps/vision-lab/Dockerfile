# syntax=docker/dockerfile:1.7

FROM python:3.12-slim-bookworm AS wheel-builder
WORKDIR /build
COPY . ./apps/vision-lab
RUN python -m pip install --no-cache-dir build && \
    python -m build --wheel --outdir /wheels apps/vision-lab

FROM python:3.12-slim-bookworm AS runtime
WORKDIR /app
RUN apt-get update && \
    apt-get install -y --no-install-recommends libglib2.0-0 libgl1 && \
    rm -rf /var/lib/apt/lists/*
COPY --from=wheel-builder /wheels /wheels
RUN wheel="$(find /wheels -maxdepth 1 -name '*.whl' -print -quit)" && \
    test -n "$wheel" && \
    python -m pip install --no-cache-dir "$wheel" && \
    rm -rf /wheels
ENV VISION_LAB_HOST=0.0.0.0 \
    VISION_LAB_PORT=8800 \
    VISION_LAB_DATA_DIR=/data \
    VISION_LAB_CANDIDATE_LOCAL_DIR=/candidates \
    VISION_LAB_SYNC_RESULT_ARCHIVE=0 \
    VISION_LAB_CONTROL_PLANE_ONLY=1 \
    TZ=Asia/Shanghai
VOLUME ["/data", "/candidates", "/published-models"]
EXPOSE 8800
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8800/api/config', timeout=3).read()"]
ENTRYPOINT ["blrec-vision-lab"]
