# soundable

[![CI](https://github.com/Aitaneuh/soundable/actions/workflows/ci.yml/badge.svg)](https://github.com/Aitaneuh/soundable/actions/workflows/ci.yml)

A personal, single-user, self-hosted music system:

- a **server** (FastAPI + PostgreSQL, Docker / Portainer) that holds your library,
- a **terminal client** in the spirit of Claude Code: paste a link, drop files, search,
  play, all from one prompt,
- a **Subsonic-compatible API**, so native iPhone apps (Amperfy, play:Sub, substreamer)
  stream your library with real background playback and lock-screen controls.

## Features

- **Add music** by pasting a YouTube / SoundCloud link (tracks, YouTube playlists,
  SoundCloud sets) or dropping audio files / folders into the terminal.
- **iPhone-friendly audio**: AAC/MP3/FLAC kept as-is, anything else converted once to AAC;
  on-the-fly MP3 transcoding for mobile data (`maxBitRate`).
- **Playlists** shared by the terminal and the phone; playlist imports can create a
  playlist automatically.
- **Terminal client**: prompt, listening and browse modes; queue, shuffle, repeat
  (all / one), seeking, volume, Windows media keys; command completion; history and most
  played; themes, accent colours, key remapping, loudness normalisation, audio device,
  resume where you left off.
- **Robust**: duplicate detection, job queue with retries, auto-reconnect, mpv crash
  recovery, readable errors.
- **Backups** every 24 h (database dumps + incremental music mirror) with a tested restore.

## Architecture

```
  terminal client ──┐                         ┌── api      FastAPI: REST /api, Subsonic /rest
  (Textual + mpv)   │   HTTPS (Cloudflare      │
                    ├── Tunnel) ─────────────► ├── worker   yt-dlp imports (job queue in Postgres)
  iPhone app ───────┘                         ├── backup   pg_dump + music mirror, every 24 h
  (Amperfy, Subsonic)                         └── db       PostgreSQL 16
```

One Docker image (`server/`) runs as `api`, `worker` and `backup`. Audio files live in a
volume as `<track-id>.<ext>`; everything else (titles, playlists, plays, jobs) is in
PostgreSQL. Imports are jobs in a Postgres table, claimed with `SELECT … FOR UPDATE SKIP
LOCKED`; no Redis/Celery.

## Deploy on Portainer (Cloudflare Tunnel)

1. Portainer → **Stacks → Add stack → Repository**: URL
   `https://github.com/Aitaneuh/soundable`, reference `refs/heads/main` (or a release tag
   such as `refs/tags/v1.0.0`), compose path `docker-compose.yml`.
2. **Environment variables** (see [Configuration](#configuration)): at least
   `POSTGRES_PASSWORD`, `ADMIN_PASSWORD`, `SECRET_KEY`, `SUBSONIC_PASSWORD`, and
   `PROXY_NETWORK` (the Docker network cloudflared is attached to).
3. **Deploy the stack**. Portainer builds the image from the repo (first build: a few minutes).
4. Cloudflare Zero Trust → **Networks → Tunnels** → your tunnel → **Public hostname**:
   `soundable.<your-domain>` → service `HTTP` → `soundable-api:8000`.
5. Check `https://soundable.<your-domain>/api/health` → `{"status":"ok","db":"ok"}`.
6. Install the terminal client (below) and the iPhone app ([docs/subsonic.md](docs/subsonic.md)).

**Updating**: *Stacks → soundable → Pull and redeploy*. The app image is always rebuilt from
the repo (`pull_policy: build`), so that's all it takes. Database migrations run
automatically when the `api` container starts.

If Cloudflare's *Bot Fight Mode* / WAF is on, it may challenge the iPhone app's requests
(non-browser traffic): add a WAF skip rule for this hostname if Amperfy can't log in.

## Terminal client

Needs [mpv](https://mpv.io) (`winget install shinchiro.mpv` on Windows, `brew install mpv`
on macOS, `apt install mpv` on Linux) and [uv](https://docs.astral.sh/uv/).

```sh
uv tool install --managed-python "git+https://github.com/Aitaneuh/soundable@v1.0.0#subdirectory=client"
soundable doctor     # checks mpv and the configuration
soundable            # first run asks for the server URL and password
```

(From a clone, `uv tool install --managed-python --editable ./client` keeps it in sync with
your checkout. On Windows prefer a uv-managed Python over the Microsoft Store one, which
silently redirects AppData writes into a sandbox folder.)

`soundable logout` revokes the token on the server and deletes it locally (it's stored in
the system keyring, or a 0600 file as a fallback).

### Using it

| You type | What happens |
| --- | --- |
| a YouTube / SoundCloud link | imports it; for playlists, asks whether to create a playlist |
| file or folder path(s), drag & drop works | uploads them |
| anything else | searches the library (accent-insensitive) |
| `/` | command menu with completion (Tab / Enter) |

Numbers refer to the last list shown: search, then `/play 3`, `/queue add 1 4-6`,
`/playlist add road 2`, `/edit 3`, `/delete 3`.

**Modes**

- **prompt** (default): type commands and searches.
- **listening** (`Esc`): ←/→ previous/next, ↑/↓ volume, space pause, `,`/`.` seek,
  `s` shuffle, `r` repeat. Typing anything returns to the prompt.
- **browse** (`Ctrl+B`): Library / Playlists / Queue tabs (`Tab`), Enter plays, `a` adds
  to the queue, `p` adds to a playlist, `e` edits, `d` deletes, `/` filters; in a playlist
  Shift+↑/↓ reorders.

**Commands** (all listed by `/help` and the `/` menu): `/search`, `/recent`, `/history`,
`/top`, `/play`, `/queue [add|rm|clear]`, `/next`, `/prev`, `/pause`, `/stop`, `/seek`,
`/vol`, `/shuffle`, `/repeat`, `/playlists`, `/playlist new|save|play|shuffle|show|add|rm|
move|rename|delete`, `/add`, `/jobs [retry|cancel]`, `/edit`, `/delete`, `/settings`,
`/login`, `/logout`, `/clear`, `/quit`.

**Shuffle & repeat**: `Ctrl+S` toggles shuffle without interrupting the current song (off
restores the original order); `Ctrl+R` cycles repeat off → all → one. Both are remembered.

**Settings** (`F2` or `/settings`): theme, accent colour, resume last queue on start,
loudness normalisation, audio output device, volume / seek steps, start mode, library order,
and remapping of every shortcut (with conflict checks).

**History**: a play counts after half a track (or 4 minutes), from the terminal and from
Subsonic apps. `/history` and `/top` show it; Amperfy's "recent"/"frequent" lists use it.

Logs: `client.log` in the client's state folder (`soundable doctor` prints the path).

## iPhone (Subsonic)

See **[docs/subsonic.md](docs/subsonic.md)**: Amperfy setup step by step, how the library
maps to artists/albums, transcoding for mobile data, the implemented endpoints and a curl
cheat sheet.

## Link imports

- **Tracks**: YouTube videos, SoundCloud tracks (incl. private `s-…` links and
  `on.soundcloud.com` short links).
- **Playlists**: YouTube playlists and SoundCloud sets (up to 500 tracks). The client shows
  the title and size and asks whether to also create a soundable playlist. A video link
  that carries a playlist (`watch?v=…&list=…`) asks "just this video, or the whole
  playlist?". Tracks already in the library aren't downloaded again but are added to the
  playlist, which is filled in the original order once every track is done. YouTube
  Mix/radio lists (auto-generated, endless) are refused. `/jobs cancel <n>` stops the rest.
- **Audio**: YouTube's AAC stream is preferred (iPhones play it natively); anything iOS
  can't play (Opus/WebM…) is converted once to AAC 256 kbps.
- **Duplicates** are refused (same video / SoundCloud track, or identical file content).
- **Failures**: network problems, HTTP 5xx and rate limiting are retried 3 times with
  backoff; permanent ones (removed/private/DRM-protected, bot check…) fail with a readable
  message. Single links are handled before the remaining tracks of a big playlist import.
- Only YouTube and SoundCloud URLs are accepted, and yt-dlp is restricted to those
  extractors, so the server can't be used as a generic downloader.

### Update yt-dlp

YouTube changes often and yt-dlp follows. It's pinned in `server/uv.lock`; to update:

```sh
uv lock --directory server --upgrade-package yt-dlp --upgrade-package yt-dlp-ejs
git commit -am "Update yt-dlp" && git push
```

then *Pull and redeploy* in Portainer. yt-dlp needs Deno for YouTube; its version is pinned
in `server/Dockerfile` (`denoland/deno:bin-…`): bump it there if yt-dlp asks for a newer one.

### "Sign in to confirm you're not a bot"

If YouTube starts asking for this, export cookies from a browser logged in to YouTube (a
throwaway account is safer) as a Netscape `cookies.txt`, put it on the server, uncomment the
cookie mount in `docker-compose.yml` and set `YTDLP_COOKIES_FILE=/run/ytdlp/cookies.txt`.
The file is mounted read-only; the worker uses a private copy.

## Backups

The `backup` service runs every `BACKUP_INTERVAL_HOURS` (default 24 h, first run a minute
after start) and writes to `BACKUP_DIR`:

- `db/soundable-<date>.sql.gz`: full database dump; the newest `BACKUP_KEEP` (14) are kept
- `music/`: mirror of the audio files, incremental (only new files are copied)
- `last-backup.json`: result of the last run (also in the `backup` container logs)

**Put `BACKUP_DIR` on another disk** (e.g. `/mnt/backup/soundable`, writable by uid 1000):
a backup on the same disk as Docker doesn't survive that disk dying. For an off-site copy,
sync that folder anywhere (rclone, Syncthing, a NAS job…).

Manual commands (Portainer → Containers → `soundable-backup-1` → Console):

```sh
python -m app.backup run       # back up now
python -m app.backup list      # available dumps
```

### Restore

1. Stop the `api` and `worker` containers (restore refuses to run while they're connected).
2. In the `backup` container: `python -m app.backup restore latest` (or a file name from
   `list`). The database is replaced by the dump and music files missing from the music
   volume are copied back from the mirror.
3. Start `api` and `worker` again.

On a brand-new server: deploy the stack with `BACKUP_DIR` pointing at the backup folder,
then do the same. (Both a partial-damage restore and a restore into an empty stack were
tested.)

## Configuration

All settings are environment variables (Portainer stack variables, or `.env` locally; see
[`.env.example`](.env.example)).

| Variable | Default | |
| --- | --- | --- |
| `POSTGRES_PASSWORD` | *(required)* | database password (internal network only) |
| `ADMIN_PASSWORD` | *(required)* | password for `soundable login` |
| `SECRET_KEY` | *(required)* | ≥ 32 random chars, signs stream links |
| `SUBSONIC_USER` | `admin` | username in the iPhone app |
| `SUBSONIC_PASSWORD` | *(required)* | password in the iPhone app (separate on purpose) |
| `PROXY_NETWORK` | `global` | Docker network shared with cloudflared |
| `MAX_UPLOAD_MB` | `95` | upload limit (Cloudflare caps requests at 100 MB) |
| `YTDLP_COOKIES_FILE` | *(empty)* | cookies for YouTube's bot check, if ever needed |
| `BACKUP_DIR` | `backups` volume | where backups go (prefer another disk) |
| `BACKUP_INTERVAL_HOURS` | `24` | |
| `BACKUP_KEEP` | `14` | database dumps kept |
| `LOG_LEVEL` | `INFO` | |

## Security notes

- Everything goes through HTTPS (Cloudflare Tunnel); the containers publish no ports and
  PostgreSQL is only on an internal network.
- `ADMIN_PASSWORD` is only kept as an argon2 hash in memory; the client stores a revocable
  token (hashed in the database), never the password.
- `SUBSONIC_PASSWORD` must be known in plaintext by the server (Subsonic token auth is
  `md5(password + salt)`), which is why it's a separate password: don't reuse one you care
  about.
- Failed logins (both APIs) are rate-limited per IP and logged.
- Uploads are size-limited, checked to be real audio, and stored under generated names.

## Development

```sh
cp .env.example .env && docker compose up -d --build      # local stack on 127.0.0.1:8000
cd server && uv sync --managed-python && uv run pytest     # needs Docker + ffmpeg (+ psql for backup tests)
cd client && uv sync --managed-python && uv run pytest
uv run ruff check . && uv run ruff format --check .       # in either folder
```

`docker-compose.override.yml` is only used locally (publishes the API on 127.0.0.1:8000);
Portainer reads `docker-compose.yml` only. CI runs ruff and the tests of both packages and
builds the Docker image on every push.
