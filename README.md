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

## Terminal client

Needs [mpv](https://mpv.io) (`winget install shinchiro.mpv` on Windows, `brew install mpv`
on macOS) and [uv](https://docs.astral.sh/uv/).

```sh
uv tool install --managed-python --editable ./client   # installs the `soundable` command
soundable doctor     # checks mpv / config
soundable            # first run asks for the server URL and password
```

> On Windows, prefer a uv-managed Python (`--managed-python`) over the Microsoft Store
> Python: the Store build silently redirects AppData writes into a sandbox folder.

Inside the app, just type:

| You type | What happens |
| --- | --- |
| a YouTube / SoundCloud link | imports it on the server (milestone 4) |
| file or folder path(s), drag & drop works | uploads them |
| anything else | searches the library |
| `/help` | all commands |

Modes: **prompt** (default) · **listening** (`Esc`: ←/→ prev/next, ↑/↓ volume ±5 %,
space pause, `,`/`.` seek ±10 s) · **browse** (`Ctrl+B`: navigate library and queue,
Enter to play, `/` to filter). Windows media keys control playback too (via mpv).

## Server development

```sh
cd server
uv sync --managed-python
uv run pytest        # needs Docker (real Postgres via testcontainers) and ffmpeg on PATH
uv run ruff check . && uv run ruff format --check .
```
