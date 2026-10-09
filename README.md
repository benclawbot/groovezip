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
Node.js 26 for YouTube's JavaScript challenges, and the pinned bgutil proof-of-
origin token provider. The provider listens only on container loopback and starts
with the app. It generates anonymous tokens; it does not use account cookies or
OAuth. YouTube can still block requests based on the host's IP or change its
checks, so this cannot guarantee that every playlist downloads. Build and run it
with:

```bash
docker build -t groovezip .
docker run --rm -p 8787:8787 -e HOST=0.0.0.0 -e PORT=8787 groovezip
```

For a hosting platform, configure its assigned port as `PORT` and publish that
container port. The app provides `/health` and `/healthz` for health checks. The
POT provider's `/ping` endpoint is available only inside the container at
`127.0.0.1:4416`.
Downloaded files are stored in `/app/downloads` inside the container; mount a
volume there if jobs must survive container replacement. Supply a persistent
volume with enough free space for downloads and ZIP output.

The app is deployed at https://groovezip.onrender.com/ on Render's Free plan.
To deploy another instance, create a Docker Web Service from this repository,
choose the Free plan and set the health check to `/health`.
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

The ZIP is downloaded through the browser button. Temporary audio and fragment
files are deleted when archiving finishes. The ZIP and job record are deleted
after a completed download transfer; interrupted transfers retain the ZIP so
you can retry. To download a completed archive again, create a new job.
Local runs use `downloads/` next to the app; Docker runs use `/app/downloads`.

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
- **"No MP3 files were produced"** → the playlist may be private,
  age-restricted, or blocked by YouTube. The hosted Docker image starts the free
  anonymous POT provider automatically, but YouTube may still require sign-in or
  block the hosting IP. A local source checkout without the bundled provider
  remains usable and reports that token support is unavailable.
- ZIPs that have not yet been downloaded remain in `downloads/` until transfer
  completion or the host restarts.
- Change the port: `PORT=9000 python3 app.py` (or legacy `YTD_PORT=9000`).
