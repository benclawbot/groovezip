#!/usr/bin/env python3
"""
YouTube Playlist -> MP3 ZIP downloader (tiny local web app).

Paste a playlist (or single video) URL, click Download, and the app:
  1. fetches the track list,
  2. downloads each track and converts it to MP3 (via yt-dlp + ffmpeg),
  3. packages every MP3 into a single ZIP file and offers it for download.

Nothing leaves your machine; run it locally.

Usage:
    python3 app.py            # then open http://127.0.0.1:8787
    YTD_PORT=9000 python3 app.py
"""

import glob
import json
import os
import re
import shutil
import time
import uuid
import zipfile
import threading
from urllib.parse import quote, unquote, urlsplit
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

try:
    import yt_dlp
except ImportError:  # pragma: no cover
    yt_dlp = None

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DOWNLOADS_DIR = os.path.join(BASE_DIR, "downloads")
HOST = os.environ.get("HOST", "127.0.0.1")
PORT = int(os.environ.get("PORT", os.environ.get("YTD_PORT", "8787")))

os.makedirs(DOWNLOADS_DIR, exist_ok=True)

# ---------------------------------------------------------------------------
# Job registry
# ---------------------------------------------------------------------------
JOBS = {}
JOBS_LOCK = threading.Lock()

CHECK_OPTS = {
    "quiet": True,
    "no_warnings": True,
    "extract_flat": True,
    "js_runtimes": {"node": {}},
}


def sanitize(name):
    cleaned = re.sub(r'[\\/:*?"<>|\x00-\x1f]+', "_", str(name)).strip().strip(".").strip()
    return cleaned or "playlist"


def is_supported_youtube_url(url):
    """Allow only HTTPS YouTube video and playlist URLs."""
    try:
        parsed = urlsplit(url)
        if parsed.scheme.lower() != "https" or parsed.username or parsed.password:
            return False
        if parsed.port not in (None, 443):
            return False
        host = (parsed.hostname or "").lower().rstrip(".")
        if host not in {
            "youtube.com", "www.youtube.com", "m.youtube.com",
            "music.youtube.com", "youtu.be",
        }:
            return False
        path = parsed.path
        query = dict(
            part.split("=", 1) for part in parsed.query.split("&") if "=" in part
        )
        if host == "youtu.be":
            return bool(path.strip("/"))
        if path == "/watch":
            return bool(query.get("v"))
        if path == "/playlist":
            return bool(query.get("list"))
        return False
    except (TypeError, ValueError):
        return False


def flat_info(url):
    """Quick metadata pass (no downloads): playlist title + entry list."""
    with yt_dlp.YoutubeDL(CHECK_OPTS) as ydl:
        return ydl.extract_info(url, download=False)


def new_job(job_id):
    job = {
        "id": job_id,
        "state": "queued",          # queued | running | done | error
        "title": None,
        "total": None,
        "completed": 0,
        "failed": 0,
        "current": None,
        "downloaded_bytes": 0,
        "error": None,
        "zipfile": None,
        "zip_size": None,
        "dir": os.path.join(DOWNLOADS_DIR, job_id),
        "created": time.time(),
    }
    return job


def download_worker(job, url, quality):
    job_dir = job["dir"]
    os.makedirs(job_dir, exist_ok=True)
    job["state"] = "running"

    def hook(d):
        if d["status"] == "downloading":
            job["current"] = (d.get("info_dict") or {}).get("title")
            job["downloaded_bytes"] = (
                d.get("downloaded_bytes") or d.get("downloaded_bytes_estimate") or 0
            )
        elif d["status"] == "finished":
            job["completed"] += 1
        elif d["status"] == "error":
            job["failed"] += 1

    try:
        # 1) Fast metadata pass -> accurate total + playlist title.
        try:
            info = flat_info(url)
            if info.get("_type") == "playlist":
                entries = info.get("entries") or []
                job["title"] = info.get("title") or "playlist"
                job["total"] = len(entries) or info.get("count") or None
            else:
                job["title"] = info.get("title") or "video"
                job["total"] = 1
        except Exception:
            job["total"] = None

        # 2) Download every track -> mp3.
        opts = {
            "js_runtimes": {"node": {}},
            "format": "bestaudio/best",
            "postprocessors": [
                {
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": "mp3",
                    "preferredquality": str(quality),
                }
            ],
            "outtmpl": os.path.join(
                job_dir, "%(playlist_index,playlist_autonumber,autonumber)s - %(title)s.%(ext)s"
            ),
            "progress_hooks": [hook],
            "ignoreerrors": True,   # skip broken videos, keep going
            "noplaylist": False,
            "quiet": True,
            "no_warnings": True,
            "writethumbnail": False,
            "embed_metadata": True,   # nice ID3 tags
            "retries": 3,
        }
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([url])

        # 3) Zip all produced mp3s.
        mp3s = sorted(glob.glob(os.path.join(job_dir, "*.mp3")))
        if not mp3s:
            raise RuntimeError(
                "No MP3 files were produced. The playlist may be empty, private, "
                "age-restricted, or blocked by YouTube (may need cookies)."
            )
        zip_path = os.path.join(
            DOWNLOADS_DIR, f"{sanitize(job['title'])}-{job['id']}.zip"
        )
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for m in mp3s:
                zf.write(m, os.path.basename(m))
        job["zipfile"] = zip_path
        job["zip_size"] = os.path.getsize(zip_path)
        job["state"] = "done"
    except Exception as exc:  # noqa: BLE001
        job["state"] = "error"
        job["error"] = str(exc)


# ---------------------------------------------------------------------------
# HTTP handling
# ---------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):  # keep the console quiet
        pass

    # -- helpers ------------------------------------------------------------
    def _send_json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0:
                return {}
            return json.loads(self.rfile.read(length) or b"{}")
        except Exception:
            return {}

    def _send_file(self, path, content_type):
        try:
            with open(path, "rb") as f:
                data = f.read()
        except FileNotFoundError:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _send_zip(self, job):
        path = job.get("zipfile")
        if not path or not os.path.isfile(path):
            self._send_json({"error": "ZIP not ready yet"}, 404)
            return
        size = os.path.getsize(path)
        filename = os.path.basename(path)
        ascii_name = filename.encode("ascii", "ignore").decode("ascii")
        self.send_response(200)
        self.send_header("Content-Type", "application/zip")
        self.send_header(
            "Content-Disposition",
            f'attachment; filename="{ascii_name}"; '
            f"filename*=UTF-8''{quote(filename)}",
        )
        self.send_header("Content-Length", str(size))
        self.end_headers()
        with open(path, "rb") as f:
            shutil.copyfileobj(f, self.wfile)

    # -- routes ---------------------------------------------------------------
    def do_GET(self):
        if self.path in ("/", "/index.html"):
            self._send_file(
                os.path.join(BASE_DIR, "index.html"), "text/html; charset=utf-8"
            )
        elif self.path.startswith("/designs/"):
            import mimetypes
            relative = unquote(urlsplit(self.path).path).lstrip("/")
            designs_root = os.path.realpath(os.path.join(BASE_DIR, "designs"))
            asset_path = os.path.realpath(os.path.join(BASE_DIR, relative))
            if os.path.commonpath((designs_root, asset_path)) != designs_root:
                return self.send_error(404)
            self._send_file(asset_path, mimetypes.guess_type(asset_path)[0] or "application/octet-stream")
        elif self.path == "/health" or self.path == "/healthz":
            self._send_json({"status": "ok"})
        elif self.path.startswith("/api/status"):
            job_id = self._parse_job_id()
            with JOBS_LOCK:
                job = dict(JOBS.get(job_id, {}))
            if not job:
                return self._send_json({"error": "Job not found"}, 404)
            self._send_json(
                {
                    k: job[k]
                    for k in (
                        "id", "state", "title", "total", "completed",
                        "failed", "current", "downloaded_bytes",
                        "error", "zip_size",
                    )
                }
            )
        elif self.path.startswith("/api/zip"):
            with JOBS_LOCK:
                job = dict(JOBS.get(self._parse_job_id(), {}))
            if job:
                return self._send_zip(job)
            self._send_json({"error": "Job not found"}, 404)
        else:
            self.send_error(404)

    def _parse_job_id(self):
        query = self.path.split("?", 1)[1] if "?" in self.path else ""
        params = dict(p.split("=", 1) for p in query.split("&") if "=" in p)
        return params.get("id", "")

    def do_POST(self):
        data = self._read_json()
        if self.path == "/api/check":
            self.handle_check(data)
        elif self.path == "/api/download":
            self.handle_download(data)
        else:
            self.send_error(404)

    def handle_check(self, data):
        url = (data.get("url") or "").strip()
        if not url:
            return self._send_json({"error": "Missing URL"}, 400)
        if not is_supported_youtube_url(url):
            return self._send_json({"error": "Use an HTTPS YouTube video or playlist URL"}, 400)
        if yt_dlp is None:
            return self._send_json(
                {"error": "yt-dlp is not installed. Run: pip install yt-dlp"}, 500
            )
        try:
            info = flat_info(url)
        except Exception as exc:  # noqa: BLE001
            return self._send_json({"error": str(exc)}, 500)

        if info and info.get("_type") == "playlist":
            entries = info.get("entries") or []
            return self._send_json(
                {
                    "is_playlist": True,
                    "title": info.get("title"),
                    "count": len(entries) or info.get("count") or 0,
                    "url": info.get("webpage_url"),
                    "entries": [
                        {
                            "index": e.get("playlist_index"),
                            "title": e.get("title"),
                            "length": e.get("duration"),
                        }
                        for e in entries[:1000]
                        if e
                    ],
                }
            )
        return self._send_json(
            {
                "is_playlist": False,
                "title": info.get("title") if info else None,
                "count": 1,
            }
        )

    def handle_download(self, data):
        url = (data.get("url") or "").strip()
        if not url:
            return self._send_json({"error": "Missing URL"}, 400)
        if not is_supported_youtube_url(url):
            return self._send_json({"error": "Use an HTTPS YouTube video or playlist URL"}, 400)
        if yt_dlp is None:
            return self._send_json(
                {"error": "yt-dlp is not installed. Run: pip install yt-dlp"}, 500
            )
        try:
            quality = int(data.get("quality") or 192)
            if quality not in (128, 192, 256, 320):
                quality = 192
        except (TypeError, ValueError):
            quality = 192

        job = new_job(uuid.uuid4().hex)
        with JOBS_LOCK:
            JOBS[job["id"]] = job
        thread = threading.Thread(
            target=download_worker, args=(job, url, quality), daemon=True
        )
        thread.start()
        self._send_json({"id": job["id"]})


def main():
    print("-" * 60)
    print("  YouTube Playlist -> MP3 ZIP downloader")
    print(f"  Open in your browser:  http://{HOST}:{PORT}")
    print("  Stop with Ctrl+C")
    print("-" * 60)
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nBye!")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
