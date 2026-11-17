# soundable

[![CI](https://github.com/Aitaneuh/soundable/actions/workflows/ci.yml/badge.svg)](https://github.com/Aitaneuh/soundable/actions/workflows/ci.yml)

A personal, single-user music server with a Claude-Code-style terminal client and a
Subsonic-compatible API for iPhone apps (Amperfy, play:Sub, substreamer).

- `server/`: FastAPI + PostgreSQL + a Postgres-backed job worker (yt-dlp imports)
- `client/`: Textual terminal client (`soundable` command), playback through mpv
- `docker-compose.yml`: the Portainer stack
- [`docs/subsonic.md`](docs/subsonic.md): iPhone setup (Amperfy), curl cheat sheet

> Work in progress. Full deploy / backup / update docs land with the last milestone.

## Deploy on Portainer (Cloudflare Tunnel)

1. Portainer → **Stacks → Add stack → Repository**: URL
   `https://github.com/Aitaneuh/soundable`, reference `refs/heads/main`, compose path
   `docker-compose.yml`.
2. **Environment variables** (see `.env.example`): `POSTGRES_PASSWORD`, `ADMIN_PASSWORD`,
   `SECRET_KEY` (≥ 32 chars), `SUBSONIC_USER`, `SUBSONIC_PASSWORD`, and `PROXY_NETWORK`
   (the Docker network cloudflared is attached to, default `global`). Generate secrets with
   `python -c "import secrets; print(secrets.token_urlsafe(32))"`.
3. **Deploy the stack**. Portainer builds the image from the repo (first build: a few minutes).
4. Cloudflare Zero Trust → **Networks → Tunnels** → your tunnel → **Public hostname**:
   `soundable.<your-domain>` → service `HTTP` → `soundable-api:8000`.
5. Check `https://soundable.<your-domain>/api/health` → `{"status":"ok","db":"ok"}`.
6. Terminal: `soundable login` with that URL. iPhone: see [docs/subsonic.md](docs/subsonic.md).

If Cloudflare's *Bot Fight Mode* / WAF is on, it may challenge the iPhone app's requests
(non-browser traffic): add a WAF skip rule for this hostname if Amperfy can't log in.

To update: push to `main`, then in Portainer *Pull and redeploy*. The app image is always
rebuilt from the repo (`pull_policy: build`), so the "re-pull image" toggle only affects Postgres.

## Backups

The `backup` service backs up automatically every `BACKUP_INTERVAL_HOURS` (default 24 h,
first run a minute after start) into `BACKUP_DIR`:

- `db/soundable-<date>.sql.gz`: full database dump (tracks, playlists, jobs…); the
  newest `BACKUP_KEEP` (default 14) are kept
- `music/`: mirror of the audio files; incremental (only new files are copied)
- `last-backup.json`: result of the last run (check it, or the `backup` container logs)

**Put `BACKUP_DIR` on another disk** (e.g. `BACKUP_DIR=/mnt/backup/soundable`, writable by
uid 1000): a backup on the same disk as Docker doesn't survive that disk dying. For an
off-site copy, sync that folder anywhere (rclone, Syncthing, a NAS job…).

Manual commands (Portainer → Containers → `soundable-backup-1` → Console, or
`docker exec -it soundable-backup-1 …`):

```sh
python -m app.backup run       # back up now
python -m app.backup list      # available dumps
```

### Restore

1. Stop the `api` and `worker` containers (restore refuses to run while they're connected).
2. In the `backup` container: `python -m app.backup restore latest`
   (or a specific file name from `list`). The database is replaced by the dump, and any
   music file missing from the music volume is copied back from the mirror.
3. Start `api` and `worker` again.

Restoring onto a brand-new server works the same way: deploy the stack with `BACKUP_DIR`
pointing at the backup folder, stop `api`/`worker`, restore, start them.

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
| a YouTube / SoundCloud link | imports it on the server (progress shown live) |
| file or folder path(s), drag & drop works | uploads them |
| anything else | searches the library |
| `/help` | all commands |

Playlists: `/playlists`, `/playlist new <name>`, `/playlist add <name> 1 3 5-7` (numbers
from the last list shown), `/playlist play <name> [shuffle]`, `/playlist save <name>` (the
current queue), `show`, `rm`, `move`, `rename … -> …`, `delete`. Or in browse mode
(`Ctrl+B`, Playlists tab): Enter plays, → opens, `n` new, `e` rename, `d` delete,
Shift+↑/↓ reorders, and `p` on any track adds it to a playlist.

History: a play counts after half a track (or 4 minutes), from the terminal and from
Subsonic apps (scrobble). `/history` = recently played, `/top` = most played; Amperfy's
"recent" and "frequent" album lists use the same data.

Shuffle / repeat: `Ctrl+S` toggles shuffle (the current song keeps playing, the rest is
reordered; off restores the original order), `Ctrl+R` cycles repeat off → all → one.
Also `/shuffle [on|off]`, `/repeat [off|all|one]`, or `s` / `r` in listening mode.

Settings: `/settings` or `F2`: theme (all Textual themes, live preview), accent colour,
resume the last queue on start, loudness normalisation, audio output device, volume/seek
steps, start mode, library order, and **key remapping** (every shortcut, with conflict
checks). Saved in the client's config file.

Modes: **prompt** (default) · **listening** (`Esc`: ←/→ prev/next, ↑/↓ volume ±5 %,
space pause, `,`/`.` seek ±10 s) · **browse** (`Ctrl+B`: navigate library and queue,
Enter to play, `/` to filter). Windows media keys control playback too (via mpv).

## Link imports (YouTube / SoundCloud)

Pasting a link creates a job; the `worker` container downloads it with yt-dlp and adds it
to the library. `/jobs` in the client shows recent imports, `/jobs retry <n>` retries one.
Single links you paste are handled before the remaining tracks of a big playlist import.

- Tracks: YouTube videos, SoundCloud tracks (incl. private `s-…` links and
  `on.soundcloud.com` short links).
- Playlists: YouTube playlists and SoundCloud sets (up to 500 tracks). The client shows
  the playlist's title and size and asks whether to also create a soundable playlist from
  it. A video link that also carries a playlist (`watch?v=…&list=…`) asks "just this
  video, or the whole playlist?". Tracks already in the library aren't downloaded again
  but are added to the new playlist; the playlist is filled in the original order once
  every track is done. YouTube "Mix"/radio lists (auto-generated, endless) are refused.
  `/jobs cancel <n>` stops the remaining tracks of a playlist import.
- Audio: YouTube's AAC stream (m4a) is preferred because iPhones play it natively. Anything
  iOS can't play (Opus/WebM…) is converted once to AAC 256 kbps.
- Duplicates are refused (same video / SoundCloud track id, or identical file content).
- Transient failures (network, HTTP 5xx, rate limiting) are retried 3 times with backoff;
  permanent ones (removed/private video, bot check…) fail with a readable message.

### Update yt-dlp

YouTube changes often and yt-dlp follows. yt-dlp is pinned in `server/uv.lock`; to update:

```sh
uv lock --directory server --upgrade-package yt-dlp --upgrade-package yt-dlp-ejs
git commit -am "Update yt-dlp" && git push
```

then in Portainer: *Stacks → soundable → Pull and redeploy* (the image is rebuilt from the
repo on every deploy). yt-dlp also needs Deno for YouTube; its version is pinned in
`server/Dockerfile` (`denoland/deno:bin-…`), bump it there if yt-dlp asks for a newer one.

### "Sign in to confirm you're not a bot"

If YouTube starts asking for this, export cookies from a browser where you're logged in to
YouTube (a throwaway account is safer) as a Netscape `cookies.txt`, put it on the server,
uncomment the cookie mount in `docker-compose.yml` and set
`YTDLP_COOKIES_FILE=/run/ytdlp/cookies.txt`. The file is mounted read-only; the worker uses
a private copy.

## Server development

```sh
cd server
uv sync --managed-python
uv run pytest        # needs Docker (real Postgres via testcontainers) and ffmpeg on PATH
uv run ruff check . && uv run ruff format --check .
```
