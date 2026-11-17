# soundable

A personal, single-user music server with a Claude-Code-style terminal client and a
Subsonic-compatible API for iPhone apps (Amperfy, play:Sub, substreamer).

- `server/`: FastAPI + PostgreSQL + a Postgres-backed job worker (yt-dlp imports)
- `client/`: Textual terminal client (`soundable` command), playback through mpv
- `docker-compose.yml`: the Portainer stack

> Work in progress. Full deploy / backup / update docs land with the last milestone.

## Run the server locally

```sh
cp .env.example .env        # then edit the secrets
docker compose up -d --build
curl http://127.0.0.1:8000/api/health
```

`docker-compose.override.yml` is picked up automatically by `docker compose` locally: it
publishes the API on `127.0.0.1:8000` and creates a throwaway proxy network. Portainer only
reads `docker-compose.yml`, so none of that reaches production.

## Server development

```sh
cd server
uv sync
uv run pytest        # needs Docker: tests run against a real Postgres (testcontainers)
uv run ruff check . && uv run ruff format --check .
```
