# syntax=docker/dockerfile:1

# ---- build stage: resolve the locked dependencies into a virtualenv -----------------
FROM python:3.12-slim-trixie AS build
COPY --from=astral/uv:0.13.9 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
WORKDIR /app
COPY pyproject.toml uv.lock .python-version ./
RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-dev --no-install-project

# ---- runtime stage --------------------------------------------------------------------
FROM python:3.12-slim-trixie AS runtime

# ffmpeg/ffprobe: conversion, transcoding, probing. A static build instead of Debian's
# package, which drags in ~500 MB of X11/codec libraries we don't need.
# deno: JavaScript runtime that yt-dlp needs to solve YouTube's player challenges.
RUN apt-get update \
    && apt-get install -y --no-install-recommends tini ca-certificates \
    && rm -rf /var/lib/apt/lists/*
COPY --from=mwader/static-ffmpeg:9.0.2-1 /ffmpeg /ffprobe /usr/local/bin/
COPY --from=denoland/deno:bin-2.9.8 /deno /usr/local/bin/deno

RUN useradd --create-home --uid 1000 app \
    && mkdir -p /data/music \
    && chown -R app:app /data

WORKDIR /app
COPY --from=build /app/.venv /app/.venv
COPY alembic.ini ./
COPY alembic ./alembic
COPY app ./app
COPY --chmod=755 docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    MUSIC_DIR=/data/music

USER app
VOLUME ["/data/music"]
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=4).status == 200 else 1)"

ENTRYPOINT ["/usr/bin/tini", "--", "docker-entrypoint.sh"]
CMD ["api"]
