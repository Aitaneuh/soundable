# Subsonic API (iPhone apps)

soundable implements a subset of the [Subsonic API](https://www.subsonic.org/pages/api.jsp)
(version 1.16.1, with [OpenSubsonic](https://opensubsonic.netlify.app/) markers) under
`/rest/…`, so native apps such as **Amperfy** (main target), play:Sub or substreamer can
browse, stream with background playback / lock-screen controls, and manage playlists.

Credentials: `SUBSONIC_USER` / `SUBSONIC_PASSWORD` from the stack environment. This is a
*separate* password from `ADMIN_PASSWORD`: Subsonic's token auth (`t = md5(password + salt)`)
forces the server to know it in plaintext, so it should not be a password you use elsewhere.

## How the library looks in a Subsonic app

| soundable | Subsonic |
| --- | --- |
| track `artist` | artist |
| track `album` | album of that artist |
| track without album | album **"Singles"** of that artist |
| playlists | playlists (same ones as in the terminal client) |
| starred tracks | favourites (`star` / `getStarred2`) |

No cover art: songs and albums carry no `coverArt` attribute, so apps show their default
artwork and never request images (`getCoverArt` answers error 70 if called anyway).

## Streaming and mobile data

`stream` serves the original file with HTTP Range support (seeking) unless the app asks
for less:

- `maxBitRate=N` and the file is above N kbps → MP3 at the highest standard bitrate ≤ N
- `format=mp3` and the file isn't MP3 (or is above `maxBitRate`) → MP3 (256 kbps default)
- `format=raw` → always the original

Transcoded streams have no length/Range; apps seek in them with `timeOffset` (the
OpenSubsonic `transcodeOffset` extension, which soundable advertises).

## Add the server in Amperfy

1. Make sure the stack is deployed and `https://<your-domain>/rest/ping.view` answers
   (see the curl check below). Amperfy needs a valid HTTPS URL.
2. Install **Amperfy** from the App Store and open it.
3. On the login screen enter:
   - **Server URL**: `https://<your-domain>` (no `/rest`, no trailing slash)
   - **Username**: your `SUBSONIC_USER` (default `admin`)
   - **Password**: your `SUBSONIC_PASSWORD`
   - **Backend**: Subsonic (or let Amperfy auto-detect)
4. Log in. Amperfy syncs the whole library (artists, albums, every song, playlists); with
   a few hundred tracks this takes seconds.
5. Optional, for 5G: in Amperfy's settings, set the **cellular streaming bitrate** to e.g.
   128 kbps (and format MP3 if offered). On Wi-Fi keep it unlimited/raw for the original
   files.
6. Play something, lock the phone: playback continues, lock-screen controls work, and the
   next track starts automatically.

New tracks imported from the terminal show up after Amperfy's next library sync (pull to
refresh in the library, or Settings → Library → sync).

## curl cheat sheet

Token auth like real apps (bash; set your values first):

```sh
SERVER=https://soundable.example.com
U=admin; P='your-subsonic-password'
S=$(openssl rand -hex 8)
T=$(printf '%s%s' "$P" "$S" | md5sum | cut -d' ' -f1)   # macOS: md5 -q -s "$P$S"
A="u=$U&t=$T&s=$S&v=1.16.1&c=curl"

curl "$SERVER/rest/ping.view?$A"                                  # XML, status="ok"
curl "$SERVER/rest/ping?$A&f=json"                                # JSON
curl "$SERVER/rest/ping.view?u=$U&p=wrong&v=1.16.1&c=curl"        # error code 40
curl "$SERVER/rest/getArtists?$A&f=json"
curl "$SERVER/rest/search3?$A&f=json&query=&songCount=500"        # every song (what Amperfy syncs)
curl "$SERVER/rest/getAlbumList2?$A&f=json&type=newest&size=10"
curl "$SERVER/rest/getPlaylists?$A&f=json"
curl "$SERVER/rest/getSong?$A&f=json&id=<song id>"
curl -D - -o /dev/null -H 'Range: bytes=0-1023' "$SERVER/rest/stream?$A&id=<song id>"   # 206
curl -o low.mp3 "$SERVER/rest/stream?$A&id=<song id>&maxBitRate=96"                      # MP3 96k
mpv "$SERVER/rest/stream?$A&id=<song id>"
```

Simple password auth also works (`p=<password>` or `p=enc:<hex of password>`), which is
handy for quick tests but sends the password in the URL.

## Implemented endpoints

System: `ping`, `getLicense`, `getOpenSubsonicExtensions`, `getMusicFolders`, `getUser`,
`getScanStatus`, `startScan`.
Browsing: `getIndexes`, `getMusicDirectory`, `getArtists`, `getArtist`, `getAlbum`,
`getAlbumList`, `getAlbumList2`, `getSong`, `getRandomSongs`, `search2`, `search3`.
Favourites: `star`, `unstar`, `getStarred`, `getStarred2`.
Playlists: `getPlaylists`, `getPlaylist`, `createPlaylist`, `updatePlaylist`, `deletePlaylist`.
Media: `stream`, `download`, `getCoverArt` (error 70).
Accepted and ignored: `scrobble`, `setRating`, `savePlayQueue`, bookmarks.
Valid empty answers: genres, podcasts, internet radio, now playing, similar/top songs,
artist/album info, lyrics, play queue.

Errors use the standard codes: 10 missing parameter, 40 wrong credentials (also after too
many failed attempts), 42 unsupported auth mechanism (`apiKey`), 70 not found, 0 other.
Every response is HTTP 200 with `status="failed"`, as Subsonic clients expect.
