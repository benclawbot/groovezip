# YouTube Playlist → MP3 ZIP

A tiny web app: paste a supported YouTube playlist URL, click a button, and you
get a **ZIP file with every track as an MP3** (numbered, with ID3 tags). Single
video URLs work too. The app can run locally or in a Docker host.

## Run locally

- Python 3.8+
- [yt-dlp](https://github.com/yt-dlp/yt-dlp) (`pip install yt-dlp` or your package manager)
- [ffmpeg](https://ffmpeg.org/) (used to convert to MP3)

Install the Python dependency and ffmpeg, then start the app:

```bash
cd youtube-playlist-downloader
python3 app.py
# Open http://127.0.0.1:8787
```

Or directly:

```bash
./run.sh            # convenience wrapper around the same command
```

The server binds to `127.0.0.1:8787` by default. Set `HOST` to change the bind
address and `PORT` to use a hosting platform's assigned port. `YTD_PORT` remains
supported as a legacy port setting.

## Host with Docker

The image includes Python, ffmpeg, yt-dlp with its challenge-solver dependencies,
and Node.js 22 for YouTube's JavaScript challenges. Build and run it with:

```bash
docker build -t groovezip .
docker run --rm -p 8787:8787 -e HOST=0.0.0.0 -e PORT=8787 groovezip
```

For a hosting platform, configure its assigned port as `PORT` and publish that
container port. The app provides `/health` and `/healthz` for health checks.
Downloaded files are stored in `/app/downloads` inside the container; mount a
volume there if jobs must survive container replacement. Supply a persistent
volume with enough free space for downloads and ZIP output.

The app is prepared for deployment but is not currently deployed. On Render,
create a Docker Web Service from this repository, choose the Free plan for a
deployment without a hosting charge, and set the health check to `/health`.
Free instances sleep after inactivity and lose temporary downloads on restart.
Jobs are kept in memory, so use a single instance; a persistent file volume
does not preserve the job registry across restarts.

## How to use

1. Paste a playlist URL, e.g.
   `https://youtube.com/playlist?list=PLOBEuByax3HQZXEdaXUhB6jjviWXxvgRk`
2. Click **Check** to preview the track list (optional).
3. Pick an MP3 quality (128–320 kbps).
4. Click **Download ZIP** — watch progress live.
5. When finished, click **Save the ZIP**.

The ZIP is downloaded through the browser button. Local runs store job files in
`downloads/` next to the app; Docker runs store them in `/app/downloads`.

## API (in case you want to script it)

| Endpoint | Method | Body / Query | Returns |
|---|---|---|---|
| `/api/check` | POST | `{"url": "…"}` | playlist title, track count, first tracks |
| `/api/download` | POST | `{"url": "…", "quality": 192}` | `{"id": jobId}` |
| `/api/status` | GET | `?id=jobId` | live progress (completed/total, current track) |
| `/api/zip` | GET | `?id=jobId` | downloads the finished ZIP |
| `/health` | GET | — | `{"status":"ok"}` |

The API accepts HTTPS video or playlist URLs on YouTube's `youtube.com`,
`www.youtube.com`, `m.youtube.com`, `music.youtube.com`, or `youtu.be` hosts.

## Notes & troubleshooting

- **`pip install yt-dlp` missing** → run it first.
- **"No MP3 files were produced"** → the playlist is private, age-restricted, or
  blocked by YouTube. For private/age-restricted lists, export your browser's
  cookies: `pip install yt-dlp[default]`, then add `"cookiefile": "cookies.txt"` to
  the options in `app.py`.
- Background downloads are stored in `youtube-playlist-downloader/downloads/`.
  Delete files there whenever you like.
- Change the port: `PORT=9000 python3 app.py` (or legacy `YTD_PORT=9000`).
