#!/usr/bin/env python3
"""
GrooveZip local YouTube playlist and video downloader.
Paste a playlist (or single video) URL, click Download, and the app:
  1. fetches the track list,
  2. downloads and validates MP3 audio or merged MP4 video,
  3. packages playlist media into a ZIP or offers a single file directly.

Run locally or on a Docker host. Media is fetched by the server and delivered
to your browser. Temporary track files are removed after archiving; the ZIP is
removed after its completed HTTP download transfer.

Usage:
    python3 app.py            # then open http://127.0.0.1:8787
    YTD_PORT=9000 python3 app.py
"""

import glob
import json
import math
import inspect
import os
import random
import re
import shutil
import signal
import subprocess
import tempfile
import time
import uuid
import zipfile
import threading
import csv
import email.utils
from datetime import datetime, timezone
from urllib.parse import quote, unquote, urljoin, urlsplit, parse_qs
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.request import urlopen

try:
    import yt_dlp
except ImportError:  # pragma: no cover
    yt_dlp = None

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DOWNLOADS_DIR = os.path.join(BASE_DIR, "downloads")
HOST = os.environ.get("HOST", "127.0.0.1")
PORT = int(os.environ.get("PORT", os.environ.get("YTD_PORT", "8787")))
POT_PROVIDER_URL = "http://127.0.0.1:4416"
ANONYMOUS_API_BASE = "https://save.brianrianrehan.com"
ANONYMOUS_API_HOST = "save.brianrianrehan.com"
ANONYMOUS_MAX_BYTES = 2048 * 1000 * 1000
POT_PROVIDER_ENTRYPOINT = os.path.join(BASE_DIR, "bgutil-provider", "build", "main.js")
POT_PROVIDER_PROCESS = None
YOUTUBE_EXTRACTOR_ARGS = {
    "youtubepot-bgutilhttp": {"base_url": [POT_PROVIDER_URL]},
}

os.makedirs(DOWNLOADS_DIR, exist_ok=True)

# ---------------------------------------------------------------------------
# Job registry
# ---------------------------------------------------------------------------
JOBS = {}
JOBS_LOCK = threading.Lock()
MEDIA_LOCK = threading.Lock()
PROVIDER_STATE_LOCK = threading.Lock()
PROVIDER_NOT_BEFORE = 0.0

CHECK_OPTS = {
    "quiet": True,
    "no_warnings": True,
    "extract_flat": True,
    "js_runtimes": {"node": {}},
    "extractor_args": YOUTUBE_EXTRACTOR_ARGS,
}


def start_pot_provider():
    """Start the bundled anonymous POT service when this image includes it."""
    global POT_PROVIDER_PROCESS
    if not os.path.isfile(POT_PROVIDER_ENTRYPOINT):
        print(
            "POT provider is not bundled; local mode will continue without anonymous "
            "YouTube tokens. yt-dlp may report YouTube sign-in or bot checks.",
            flush=True,
        )
        return None

    POT_PROVIDER_PROCESS = subprocess.Popen(
        ["node", POT_PROVIDER_ENTRYPOINT, "--host", "127.0.0.1"],
        cwd=os.path.dirname(POT_PROVIDER_ENTRYPOINT),
    )
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if POT_PROVIDER_PROCESS.poll() is not None:
            exit_code = POT_PROVIDER_PROCESS.returncode
            POT_PROVIDER_PROCESS = None
            raise RuntimeError(
                f"POT provider exited during startup (code {exit_code})"
            )
        try:
            with urlopen(f"{POT_PROVIDER_URL}/ping", timeout=1) as response:
                if response.status == 200:
                    print("Anonymous YouTube POT provider is ready.", flush=True)
                    return POT_PROVIDER_PROCESS
        except Exception:
            time.sleep(0.25)

    stop_pot_provider()
    raise RuntimeError("POT provider did not become healthy at /ping within 15 seconds")


def stop_pot_provider():
    """Stop the child service cleanly before the app process exits."""
    global POT_PROVIDER_PROCESS
    if POT_PROVIDER_PROCESS is None:
        return
    if POT_PROVIDER_PROCESS.poll() is None:
        POT_PROVIDER_PROCESS.terminate()
        try:
            POT_PROVIDER_PROCESS.wait(timeout=5)
        except subprocess.TimeoutExpired:
            POT_PROVIDER_PROCESS.kill()
            POT_PROVIDER_PROCESS.wait(timeout=5)
    POT_PROVIDER_PROCESS = None


def sanitize(name):
    cleaned = re.sub(r'[\\/:*?"<>|\x00-\x1f]+', "_", str(name)).strip().strip(".").strip()
    return cleaned or "playlist"


def safe_public_error(message):
    text = re.sub(r"https?://\S+", "YouTube URL", str(message))
    return re.sub(r"\b(token|secret|signature)=\S+", r"\1=[redacted]", text, flags=re.I)[:500]


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
        if path.startswith("/shorts/"):
            return bool(path.strip("/").split("/")[-1])
        return False
    except (TypeError, ValueError):
        return False


def flat_info(url):
    """Quick metadata pass (no downloads): playlist title + entry list."""
    options = dict(CHECK_OPTS)
    parsed = urlsplit(url)
    options["noplaylist"] = parsed.path != "/playlist"
    with yt_dlp.YoutubeDL(options) as ydl:
        return ydl.extract_info(url, download=False)


def _safe_api_url(url, allowed_paths=None):
    """Validate URLs before curl sees them; never follow an unchecked redirect."""
    parsed = urlsplit(url or "")
    if (parsed.scheme != "https" or parsed.hostname != ANONYMOUS_API_HOST
            or parsed.port not in (None, 443) or parsed.username or parsed.password):
        raise RuntimeError("Anonymous download service returned an unsafe URL")
    if allowed_paths and not any(parsed.path.startswith(path) for path in allowed_paths):
        raise RuntimeError("Anonymous download service returned an unexpected URL")
    return url


def _curl_request(url, *, method="GET", body=None, output_path=None, max_time=90):
    """Perform one curl request without automatic redirects, returning status/body/headers."""
    _safe_api_url(url)
    header_fd, header_path = tempfile.mkstemp(prefix="groovezip-api-headers-")
    os.close(header_fd)
    command = ["curl", "--silent", "--show-error", "--connect-timeout", "10",
               "--max-time", str(max_time), "--max-redirs", "0", "-D", header_path,
               "-o", output_path or "/dev/stdout", "-w", "%{http_code}", "-X", method]
    if output_path:
        command += ["--max-filesize", str(ANONYMOUS_MAX_BYTES)]
    if body is not None:
        command += ["-H", "Content-Type: application/json", "--data-binary", json.dumps(body)]
    command.append(url)
    try:
        try:
            result = subprocess.run(command, capture_output=True, timeout=max_time + 5, check=False)
        except subprocess.TimeoutExpired:
            raise AnonymousServiceUnavailable("Anonymous download service request timed out")
        if result.returncode:
            raise AnonymousServiceUnavailable("Anonymous download service request failed")
        status = int(result.stdout[-3:])
        headers = {}
        with open(header_path, encoding="iso-8859-1") as header_file:
            for line in header_file:
                if ":" in line:
                    key, value = line.split(":", 1)
                    headers[key.lower().strip()] = value.strip()
        response_body = b"" if output_path else result.stdout[:-3]
        return status, response_body, headers
    finally:
        try:
            os.remove(header_path)
        except OSError:
            pass


def parse_retry_after(value, *, now=None):
    """Return (delay seconds, absolute UTC epoch), or (60, now+60) if absent/invalid."""
    now = time.time() if now is None else now
    try:
        delay = max(0, int(value.strip()))
    except (AttributeError, ValueError):
        try:
            date = email.utils.parsedate_to_datetime(value)
            if date.tzinfo is None:
                date = date.replace(tzinfo=timezone.utc)
            delay = max(0, math.ceil(date.timestamp() - now))
        except (TypeError, ValueError, OverflowError):
            delay = 60
    return delay, now + delay


def provider_cooldown(retry_at=None):
    """Read or extend the shared provider not-before time."""
    global PROVIDER_NOT_BEFORE
    with PROVIDER_STATE_LOCK:
        if retry_at is not None:
            PROVIDER_NOT_BEFORE = max(PROVIDER_NOT_BEFORE, float(retry_at))
        return PROVIDER_NOT_BEFORE


def wait_until(retry_at):
    while time.time() < retry_at:
        time.sleep(min(1, retry_at - time.time()))


def wait_for_provider(job=None):
    """Honor any provider cooldown shared by all queued jobs."""
    while True:
        retry_at = provider_cooldown()
        remaining = retry_at - time.time()
        if remaining <= 0:
            return
        if job is not None:
            job["retry_at"] = retry_at
            job["phase"] = "cooldown"
        wait_until(retry_at)



def _request_with_retries(url, *, on_cooldown=None, **kwargs):
    """Bound transient retries and wait through provider-directed 429 deadlines."""
    attempt = 0
    while True:
        pending_cooldown = provider_cooldown()
        if pending_cooldown > time.time():
            if on_cooldown:
                on_cooldown(pending_cooldown)
            wait_for_provider()
            if on_cooldown:
                on_cooldown(None)
        try:
            status, body, headers = _curl_request(url, **kwargs)
        except AnonymousServiceUnavailable:
            if attempt >= 2:
                retry_at = time.time() + 300
                provider_cooldown(retry_at)
                if on_cooldown:
                    on_cooldown(retry_at)
                raise AnonymousServiceUnavailable("Anonymous download service remained unavailable", retry_at)
            delay = min(30, 2 ** (attempt + 1))
            time.sleep(delay)
            attempt += 1
            continue
        if status != 429 and not 500 <= status < 600:
            return status, body, headers
        if status == 429:
            delay, retry_at = parse_retry_after(headers.get("retry-after"))
            if not headers.get("retry-after"):
                delay *= 2 ** attempt
                retry_at = time.time() + delay
            provider_cooldown(retry_at)
            if on_cooldown:
                on_cooldown(retry_at)
            wait_until(retry_at)
            if on_cooldown:
                on_cooldown(None)
            attempt = 0
        elif attempt >= 2:
            if status >= 500:
                retry_at = time.time() + 300
                provider_cooldown(retry_at)
                if on_cooldown:
                    on_cooldown(retry_at)
                raise AnonymousServiceUnavailable("Anonymous download service remained unavailable", retry_at)
            return status, body, headers
        else:
            time.sleep(min(30, 2 ** (attempt + 1)))
        attempt += 1


class AnonymousServiceUnavailable(RuntimeError):
    """A global service failure: stop the batch instead of hammering it."""

    def __init__(self, message, retry_at=None):
        super().__init__(message)
        self.retry_at = retry_at


class AnonymousRateLimited(AnonymousServiceUnavailable):
    """Rate limit with a safe retry timestamp, when the service permits retrying."""

    def __init__(self, message, retry_at=None):
        super().__init__(message)
        self.retry_at = retry_at


def _api_payload(status, body):
    if status == 429:
        raise AnonymousServiceUnavailable("Anonymous download service rate limit reached")
    if status in (401, 403) or status >= 500:
        raise AnonymousServiceUnavailable(f"Anonymous download service unavailable (HTTP {status})")
    try:
        payload = json.loads(body)
    except (TypeError, ValueError):
        raise RuntimeError(f"Anonymous download service returned HTTP {status}")
    data = payload.get("data", payload) if isinstance(payload, dict) else {}
    if status not in (200, 202):
        # Do not surface service response bodies, which can contain signed URLs.
        raise RuntimeError(f"Anonymous download service returned HTTP {status}")
    return data


def anonymous_download_track(url, destination, *, pause=None, on_cooldown=None, media_format="mp3", quality=720):
    """Fetch audio or video from the documented anonymous API."""
    status, body, headers = _request_with_retries(
        ANONYMOUS_API_BASE + "/api/v1/download?wait=true", method="POST",
        body={"url": url, "format": "audio" if media_format == "mp3" else "video", "quality": "best" if media_format == "mp3" else f"{quality}p", "outputFormat": media_format},
        on_cooldown=on_cooldown,
    )
    data = _api_payload(status, body)
    deadline = time.monotonic() + 600
    status_url = data.get("statusUrl")
    if not status_url and data.get("downloadId"):
        status_url = "/api/v1/download/" + quote(str(data["downloadId"]), safe="")
    while status == 202 or data.get("status") in ("pending", "processing", "downloading", "converting"):
        status_url = data.get("statusUrl") or status_url
        if not status_url:
            raise RuntimeError("Anonymous download service returned no status URL")
        status_url = _safe_api_url(
            urljoin(ANONYMOUS_API_BASE + "/", status_url), ["/api/v1/download/"]
        )
        if time.monotonic() >= deadline:
            raise RuntimeError("Anonymous download service is still processing")
        time.sleep(min(5, max(0, deadline - time.monotonic())))
        status, body, headers = _request_with_retries(status_url, max_time=20, on_cooldown=on_cooldown)
        data.update(_api_payload(status, body))
    if data.get("status") != "completed":
        raise RuntimeError("Anonymous download service could not process this track")
    download_url = _safe_api_url(data.get("downloadUrl"), ["/api/download/"])
    token = data.get("downloadToken")
    if not isinstance(token, str) or not token or len(token) > 512:
        raise RuntimeError("Anonymous download service returned no valid file token")
    parsed = urlsplit(download_url)
    query = parse_qs(parsed.query)
    if query.get("token") != [token]:
        raise RuntimeError("Anonymous download service returned inconsistent file access")

    temporary = destination + ".part"
    try:
        for _ in range(4):
            status, _body, headers = _request_with_retries(
                download_url, output_path=temporary, max_time=1800, on_cooldown=on_cooldown
            )
            if status in (301, 302, 303, 307, 308):
                location = headers.get("location")
                if not location:
                    raise RuntimeError("Anonymous download service returned an invalid redirect")
                download_url = _safe_api_url(
                    urljoin(download_url, location), ["/api/download/"]
                )
                continue
            if status in (401, 403) or status >= 500:
                raise AnonymousServiceUnavailable(f"Anonymous file service unavailable (HTTP {status})")
            if status != 200:
                raise RuntimeError(f"Anonymous download service file returned HTTP {status}")
            size = os.path.getsize(temporary)
            if not size or size > ANONYMOUS_MAX_BYTES:
                raise RuntimeError("Anonymous MP3 file is empty or exceeds the 2 GiB limit")
            validate_mp3(temporary) if media_format == "mp3" else validate_mp4(temporary)
            os.replace(temporary, destination)
            if pause:
                pause()
            return size
        raise RuntimeError("Anonymous download service redirected too many times")
    finally:
        try:
            os.remove(temporary)
        except OSError:
            pass


def validate_mp3(path):
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "a:0", "-show_entries",
         "stream=codec_name:format=duration", "-of", "json", path],
        capture_output=True, timeout=30, check=False,
    )
    try:
        info = json.loads(result.stdout)
        valid = (result.returncode == 0 and info["streams"][0]["codec_name"] == "mp3"
                 and float(info["format"]["duration"]) > 0)
    except (ValueError, KeyError, IndexError, TypeError):
        valid = False
    if not valid:
        raise RuntimeError("Anonymous download service returned an invalid MP3")


def validate_mp4(path):
    result = subprocess.run(["ffprobe", "-v", "error", "-show_entries",
        "stream=codec_type,codec_name:format=format_name,duration", "-of", "json", path],
        capture_output=True, timeout=30, check=False)
    try:
        info = json.loads(result.stdout)
        streams = info["streams"]
        kinds = {stream.get("codec_type") for stream in streams}
        codecs = {stream.get("codec_name") for stream in streams}
        containers = info["format"].get("format_name", "").split(",")
        valid = (result.returncode == 0 and "mp4" in containers
                 and {"video", "audio"} <= kinds
                 and codecs <= {"h264", "hevc", "av1", "mpeg4", "aac", "mp3", "ac3", "alac"}
                 and float(info["format"]["duration"]) > 0)
    except (ValueError, KeyError, TypeError):
        valid = False
    if not valid:
        raise RuntimeError("MP4 output is missing playable audio/video or duration")


def new_job(job_id):
    job = {
        "id": job_id,
        "state": "queued",          # queued | running | done | error
        "title": None,
        "total": None,
        "completed": 0,
        "failed": 0,
        "remaining": 0,
        "result": None,
        "result_reason": None,
        "current": None,
        "phase": "queued",
        "downloaded_bytes": 0,
        "total_bytes": None,
        "speed": None,
        "retry_at": None,
        "error": None,
        "zipfile": None,
        "zip_size": None,
        "dir": os.path.join(DOWNLOADS_DIR, job_id),
        "created": time.time(),
        "started": time.time(), "paused_seconds": 0, "playlist_duration": None,
        "completed_duration": 0, "failed_duration": 0, "completed_items": set(),
        "elapsed": 0, "estimated_remaining": None, "estimated_finish": None,
        "file_size": None, "queue_position": 0, "queue_wait": 0,
        "started_at": None, "_pause_started": None,
        "format": "mp3", "mode": "playlist",
        "_zip_lock": threading.Lock(),
    }
    return job


def estimate_playlist_duration(entries):
    durations = []
    missing = 0
    for entry in entries or []:
        try:
            duration = float((entry or {}).get("duration"))
            if math.isfinite(duration) and duration > 0:
                durations.append(duration)
            else:
                missing += 1
        except (TypeError, ValueError):
            missing += 1
    if not durations:
        return None
    average = sum(durations) / len(durations)
    return sum(durations) + missing * average


def estimate_remaining(job, now=None):
    """Estimate time, with rough MP3/MP4 starting rates (not provider quotas)."""
    now = time.time() if now is None else now
    if job.get("state") in ("done", "error"):
        return 0.0
    total = job.get("total")
    completed = int(job.get("completed") or 0)
    pending = max(0, int(total or 0) - completed - int(job.get("failed") or 0))
    if total is None:
        return None
    if pending == 0:
        return 0.0
    retry_at = max(0.0, float(job.get("retry_at") or 0) - now)
    started = job.get("started_at") or now
    paused = float(job.get("paused_seconds") or 0)
    if job.get("_pause_started"):
        paused += max(0.0, now - job["_pause_started"])
    active_elapsed = max(0.0, now - started - paused)
    duration_total = job.get("playlist_duration")
    duration_done = float(job.get("completed_duration") or 0)
    setup_per_track = 10.0
    if duration_total and duration_done > 0 and completed > 0:
        observed_media_time = max(0.0, active_elapsed - completed * setup_per_track)
        rate = observed_media_time / duration_done
        remaining_media_duration = max(0.0, float(duration_total) - duration_done - float(job.get("failed_duration") or 0))
        work = remaining_media_duration * max(0.005, rate) + pending * setup_per_track
    elif duration_total:
        media_rate = 0.4 if job.get("format", "mp3") == "mp4" else 0.08
        remaining_media_duration = max(
            0.0, float(duration_total) - duration_done - float(job.get("failed_duration") or 0)
        )
        work = remaining_media_duration * media_rate + pending * setup_per_track
    else:
        work = pending * (active_elapsed / completed if completed else 90.0)
    return work + retry_at


def public_job_status(job, now=None):
    now = time.time() if now is None else now
    elapsed = max(0.0, now - float(job.get("created", now)))
    remaining = estimate_remaining(job, now)
    public_fields = (
        "id", "state", "title", "total", "completed", "failed", "current", "phase",
        "downloaded_bytes", "remaining", "result", "result_reason", "total_bytes", "speed",
        "retry_at", "error", "zip_size", "file_size", "format", "mode", "playlist_duration",
        "queue_position", "queue_wait_estimate",
    )
    result = {key: job.get(key) for key in public_fields}
    result.update({
        "elapsed": elapsed,
        "estimated_remaining": remaining,
        "estimate_scope": "total" if remaining is not None else None,
        "estimated_finish": now + remaining if remaining is not None and job.get("state") not in ("done", "error") else None,
    })
    if job.get("state") == "error":
        result["remaining"] = 0
    return result


def download_worker(job, url, quality, media_format="mp3", resolution=720):
    job_dir = job["dir"]
    job["format"] = media_format
    job["mode"] = "playlist" if urlsplit(url).path == "/playlist" else "single"
    queue_started = time.monotonic()
    MEDIA_LOCK.acquire()
    queue_wait = time.monotonic() - queue_started
    job["queue_wait"] = queue_wait
    os.makedirs(job_dir, exist_ok=True)
    media_ext = "mp3" if media_format == "mp3" else "mp4"
    job["state"] = "running"
    job["phase"] = "preparing"
    job["started_at"] = time.time()
    failed_items = set()
    current_item = None
    gate_error = None
    media_403_fallback = False
    gate_index = None
    fallback_failures = {}
    outcomes = {}
    format_retry_indices = set()
    playlist_info = None

    class LocalGateDetected(Exception):
        pass

    def hook(d):
        if d["status"] == "downloading":
            info = d.get("info_dict") or {}
            job["current"] = info.get("title")
            if job.get("mode") == "single" and info.get("title"):
                job["title"] = info["title"]
            if job.get("mode") == "single" and info.get("duration"):
                duration = float(info["duration"])
                if math.isfinite(duration) and duration > 0:
                    job["playlist_duration"] = duration
                    if isinstance(playlist_info, dict):
                        playlist_info["duration"] = duration
            job["phase"] = "downloading"
            job["downloaded_bytes"] = (
                d.get("downloaded_bytes") or d.get("downloaded_bytes_estimate") or 0
            )
            job["total_bytes"] = d.get("total_bytes") or d.get("total_bytes_estimate")
            job["speed"] = d.get("speed")
        elif d["status"] == "finished":
            job["phase"] = "converting"
            job["speed"] = None
        elif d["status"] == "error":
            job["failed"] += 1

    class DownloadLogger:
        last_error = None

        def debug(self, message):
            nonlocal current_item
            match = re.search(r"Downloading item (\d+) of", str(message))
            if match:
                current_item = str(int(match.group(1)) + int(job.get("_playlist_start", 1)) - 1)
                job["current"] = "Track " + current_item
                job["phase"] = "preparing"
                job["downloaded_bytes"] = 0
                job["total_bytes"] = None
                job["speed"] = None
                print(f"Job {job['id']}: preparing track {current_item}", flush=True)

        def warning(self, _message):
            message = str(_message)
            if "bgutil" in message.lower() or "po token" in message.lower():
                message = safe_public_error(message).strip()
                if message:
                    self.last_error = message[-500:]

        def error(self, message):
            nonlocal gate_error, media_403_fallback
            # yt-dlp messages can contain full URLs; keep only a safe, useful reason.
            message = safe_public_error(message).strip()
            if message:
                self.last_error = message[-500:]
                if re.search(r"not a bot|bot check|too many requests|http error 429|http error 402", message, re.I):
                    gate_error = message[-500:]
                elif (current_item or job.get("total") == 1):
                    if media_format == "mp3" and re.search(r"http error 403", message, re.I):
                        # Try ordinary advertised audio formats once for a per-asset CDN denial.
                        format_retry_indices.add(str(current_item or "1"))
                    elif media_format == "mp4" and re.search(
                        r"unable to download video data.*http error 403", message, re.I
                    ):
                        # A media CDN denial is per-track; retry this and later
                        # eligible entries through the existing anonymous API.
                        gate_error = "YouTube denied media download with HTTP 403"
                        media_403_fallback = True
                if current_item is not None or job.get("total") == 1:
                    failed_items.add(current_item or "1")
                    job["failed"] = len(failed_items)
                    idx = current_item or "1"
                    outcomes[idx] = {"status": "failed", "reason": self.last_error}
                print(f"Job {job['id']}: {self.last_error}", flush=True)
                if gate_error:
                    raise LocalGateDetected()

    logger = DownloadLogger()
    zip_path = None

    try:
        # 1) Fast metadata pass -> accurate total + playlist title.
        try:
            playlist_info = job.get("_recovery_info") or flat_info(url)
            if not isinstance(playlist_info, dict):
                raise ValueError("No video metadata returned")
            if playlist_info.get("_type") == "playlist":
                entries = playlist_info.get("entries") or []
                job["title"] = playlist_info.get("title") or "playlist"
                job["total"] = len(entries) or playlist_info.get("count") or None
                job["playlist_duration"] = estimate_playlist_duration(entries)
            else:
                job["title"] = playlist_info.get("title") or "video"
                job["total"] = 1
                job["playlist_duration"] = playlist_info.get("duration")
            if playlist_info.get("_type") == "playlist":
                for position, entry in enumerate(playlist_info.get("entries") or [], start=1):
                    entry = entry or {}
                    index = str(int(entry.get("playlist_index") or position))
                    if entry.get("availability") in ("private", "premium_only", "subscriber_only", "needs_auth", "unavailable"):
                        failed_items.add(index)
                        job["failed_duration"] += float(entry.get("duration") or 0)
                        outcomes[index] = {"status": "failed", "reason": "Private or unavailable"}
                job["failed"] = len(failed_items)
        except Exception:
            if job["mode"] == "single":
                parsed = urlsplit(url); q = parse_qs(parsed.query)
                vid = q.get("v", [parsed.path.rstrip("/").split("/")[-1]])[0]
                playlist_info = {"id":vid,"webpage_url":f"https://www.youtube.com/watch?v={quote(vid,safe='')}","title":"Video","duration":None}
                job["total"] = 1; job["title"] = "Video"
            else:
                job["total"] = None

        # 2) Download every track -> mp3.
        numeric_template = os.path.join(job_dir, "%(playlist_index,playlist_autonumber,autonumber)s.%(ext)s")
        opts = {
            "js_runtimes": {"node": {}},
            "extractor_args": YOUTUBE_EXTRACTOR_ARGS,
            "format": "bestaudio" if media_format == "mp3" else (
                f"bestvideo[ext=mp4][vcodec^=avc1][height<={int(resolution)}]+bestaudio[ext=m4a]/"
                f"best[ext=mp4][vcodec^=avc1][acodec^=mp4a][height<={int(resolution)}]/"
                f"bestvideo[height<={int(resolution)}]+bestaudio/best[height<={int(resolution)}]"
            ),
            "postprocessors": [
                {
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": "mp3",
                    "preferredquality": str(quality),
                }
            ],
            "outtmpl": numeric_template,
            "progress_hooks": [hook],
            "ignoreerrors": True,   # skip broken videos, keep going
            "noplaylist": job["mode"] == "single",
            "playliststart": job.get("_playlist_start", 1),
            "quiet": True,
            "no_warnings": True,
            "writethumbnail": False,
            "embed_metadata": True,   # nice ID3 tags
            "retries": 2,
            "fragment_retries": 2,
            "socket_timeout": 15,
            "http_chunk_size": 10 * 1024 * 1024,
            # Pace media requests; the logger stops only on a global bot/rate gate.
            "sleep_interval": 5,
            "max_sleep_interval": 10,
            "logger": logger,
        }
        if media_format == "mp3":
            opts["postprocessors"] = [{"key":"FFmpegExtractAudio","preferredcodec":"mp3","preferredquality":str(quality)}]
        else:
            opts["merge_output_format"] = "mp4"
            opts["postprocessors"] = [{"key":"FFmpegVideoRemuxer","preferedformat":"mp4"}]
        def postprocessor_hook(data):
            job["phase"] = "converting"
            if data.get("status") == "finished":
                info = data.get("info_dict") or {}
                # yt-dlp postprocessor hooks receive a shallow copy of the info
                # dict made before conversion. Its filepath can still point at
                # the source (for example 1.webm), while the requested output
                # already exists as 1.mp3. Only inspect numeric final outputs;
                # fragments and source/intermediate files are never candidates.
                index = info.get("playlist_index") or current_item
                source = info.get("filepath") or info.get("_filename") or ""
                if not index:
                    match = re.match(r"^(\d+)(?:\.| - )", os.path.basename(source))
                    index = match.group(1) if match else None
                if index:
                    prefix = str(int(index))
                    pattern = os.path.join(job_dir, "*." + media_ext)
                    candidates = [path for path in glob.glob(pattern)
                                  if re.match(r"^0*" + re.escape(prefix) + r"(?: - [^/]+)?\." + re.escape(media_ext) + r"$",
                                              os.path.basename(path))]
                else:
                    candidates = [path for path in glob.glob(os.path.join(job_dir, "*." + media_ext))
                                  if re.match(r"^\d+(?: - [^/]+)?\." + re.escape(media_ext) + r"$",
                                              os.path.basename(path))]
                candidate = candidates[0] if len(candidates) == 1 else None
                if candidate and os.path.isfile(candidate):
                    if not index:
                        match = re.match(r"^(\d+)", os.path.basename(candidate))
                        index = match.group(1) if match else os.path.basename(candidate)
                    key = str(int(index))
                    if key not in job["completed_items"]:
                        if media_format == "mp4": validate_mp4(candidate)
                        else: validate_mp3(candidate)
                        job["completed_items"].add(key)
                        job["completed"] += 1
                        failed_items.discard(key)
                        job["failed"] = len(failed_items)
                        outcomes[key] = {"status": "downloaded", "reason": ""}
                        duration = info.get("duration")
                        if duration:
                            job["completed_duration"] += float(duration)
                job["phase"] = "preparing"
                job["current"] = None
                job["downloaded_bytes"] = 0
                job["total_bytes"] = None
                job["speed"] = None

        opts["postprocessor_hooks"] = [postprocessor_hook]
        if job.get("_recovery_info"):
            gate_error = "Retrying previously failed public tracks through the anonymous fallback"
        else:
            with yt_dlp.YoutubeDL(opts) as ydl:
                try:
                    ydl.download([url])
                except LocalGateDetected:
                    pass

        if format_retry_indices and playlist_info:
            entries = (playlist_info.get("entries") or []) if playlist_info.get("_type") == "playlist" else [playlist_info]
            entries_by_index = {
                str(int(entry.get("playlist_index") or position)): entry
                for position, entry in enumerate(entries, start=1) if entry
            }
            for idx in sorted(format_retry_indices, key=lambda value: int(value)):
                if any(re.match(r"^0*" + re.escape(idx) + r"(?: - |\.)", os.path.basename(path))
                       for path in glob.glob(os.path.join(job_dir, "*.mp3"))):
                    continue
                entry = entries_by_index.get(str(int(idx)), {})
                item_url = entry.get("webpage_url") or entry.get("url")
                if not is_supported_youtube_url(item_url) and entry.get("id"):
                    item_url = "https://www.youtube.com/watch?v=" + quote(str(entry["id"]), safe="")
                if not is_supported_youtube_url(item_url):
                    continue
                job["current"] = entry.get("title") or "Track " + idx
                job["phase"] = "preparing"
                retry_opts = dict(opts)
                retry_opts.update({
                    "format": "bestaudio[ext=m4a]/bestaudio[protocol^=m3u8_native]/bestaudio",
                    "outtmpl": os.path.join(job_dir, f"{int(idx)}.%(ext)s"),
                    "noplaylist": True,
                    "playliststart": 1,
                })
                try:
                    with yt_dlp.YoutubeDL(retry_opts) as retry_ydl:
                        retry_ydl.download([item_url])
                    recovered = os.path.join(job_dir, f"{int(idx)}.mp3")
                    if os.path.isfile(recovered):
                        validate_mp3(recovered)
                        failed_items.discard(str(int(idx)))
                        outcomes[str(int(idx))] = {"status": "downloaded", "reason": ""}
                except Exception as retry_error:
                    print(f"Job {job['id']}: advertised audio format retry failed for track {idx}: {safe_public_error(retry_error)}", flush=True)

        if gate_error and playlist_info:
            is_playlist = playlist_info.get("_type") == "playlist"
            entries = (playlist_info.get("entries") or []) if is_playlist else [playlist_info]
            for position, item in enumerate(entries, start=1):
                item = item or {}
                idx = str(int(item.get("playlist_index") or position))
                outcomes.setdefault(idx, {"status": "not attempted", "reason": "Not attempted"})
                if item.get("availability") in ("private", "premium_only", "subscriber_only", "needs_auth", "unavailable"):
                    outcomes[idx] = {"status": "failed", "reason": "Private or unavailable"}
            try:
                gate_index = int(current_item or 1)
            except ValueError:
                gate_index = 1
            start_at = int(job.get("_playlist_start", 1) or 1)
            for position, entry in enumerate(entries, start=1):
                playlist_index = int((entry or {}).get("playlist_index") or position)
                if playlist_index < max(gate_index, start_at):
                    continue
                if job.get("_cancel_api_fallback"):
                    break
                entry = entry or {}
                video_url = entry.get("webpage_url") or entry.get("url")
                video_id = entry.get("id")
                if not is_supported_youtube_url(video_url) and video_id:
                    video_url = "https://www.youtube.com/watch?v=" + quote(str(video_id), safe="")
                if not is_supported_youtube_url(video_url):
                    failed_items.add(str(playlist_index))
                    job["failed"] = len(failed_items)
                    continue
                title = sanitize(entry.get("title") or f"Track {playlist_index}")
                destination = os.path.join(job_dir, f"{playlist_index}.{media_ext}")
                existing = glob.glob(os.path.join(job_dir, "*." + media_ext))
                if any(re.match(r"^0*" + str(playlist_index) + r"(?: - |\.)", os.path.basename(name))
                       for name in existing):
                    failed_items.discard(str(playlist_index))
                    outcomes[str(playlist_index)] = {"status": "downloaded", "reason": ""}
                    continue
                if entry.get("availability") in ("private", "premium_only", "subscriber_only", "needs_auth", "unavailable"):
                    failed_items.add(str(playlist_index))
                    outcomes[str(playlist_index)] = {"status": "failed", "reason": "Private or unavailable"}
                    job["failed"] = len(failed_items)
                    fallback_failures["private or unavailable"] = fallback_failures.get("private or unavailable", 0) + 1
                    continue
                job["current"] = f"{entry.get('title') or ('Track ' + str(playlist_index))} · fallback"
                job["phase"] = "fallback"
                job["downloaded_bytes"] = 0
                job["total_bytes"] = None
                job["speed"] = None
                job["retry_at"] = None

                def update_cooldown(retry_at):
                    job["retry_at"] = retry_at
                    if retry_at:
                        provider_cooldown(retry_at)
                        if not job.get("_pause_started"): job["_pause_started"] = time.time()
                        job["phase"] = "cooldown"
                    else:
                        if job.get("_pause_started"):
                            job["paused_seconds"] += max(0, time.time() - job.pop("_pause_started"))
                        job["phase"] = "fallback"
                try:
                    helper = anonymous_download_track
                    signature_target = getattr(helper, "side_effect", None) or helper
                    accepts_cooldown = "on_cooldown" in inspect.signature(signature_target).parameters
                    macro_retries = 0
                    while True:
                        try:
                            if accepts_cooldown:
                                size = anonymous_download_track(video_url, destination, on_cooldown=update_cooldown, media_format=media_format, quality=resolution if media_format == "mp4" else quality)
                            else:
                                size = anonymous_download_track(video_url, destination)
                            break
                        except AnonymousRateLimited as wait_error:
                            retry_at = wait_error.retry_at or (time.time() + 60)
                            provider_cooldown(retry_at)
                            update_cooldown(retry_at)
                            delay = max(1, retry_at - time.time())
                            wait_until(retry_at)
                            update_cooldown(None)
                        except AnonymousServiceUnavailable as service_error:
                            if not service_error.retry_at or macro_retries >= 1: raise
                            macro_retries += 1
                            retry_at = service_error.retry_at
                            update_cooldown(retry_at)
                            delay = max(0, retry_at - time.time())
                            wait_until(retry_at)
                            update_cooldown(None)
                    job["downloaded_bytes"] = size
                    job["total_bytes"] = size
                    job["speed"] = None
                    failed_items.discard(str(playlist_index))
                    outcomes[str(playlist_index)] = {"status": "downloaded", "reason": ""}
                    job["completed"] = len(glob.glob(os.path.join(job_dir, "*." + media_ext)))
                    job["failed"] = len(failed_items)
                    if entry.get("duration"): job["completed_duration"] += float(entry["duration"])
                except Exception as fallback_error:  # service errors never expose signed URLs
                    message = str(fallback_error)
                    category = "over the fallback size limit" if "HTTP 413" in message else "fallback download failed"
                    safe_reason = "File exceeds the 2 GB fallback limit" if "HTTP 413" in message else "Fallback service rate limit" if "rate limit" in message.lower() else "Fallback service unavailable" if isinstance(fallback_error, AnonymousServiceUnavailable) else "Track could not be downloaded"
                    fallback_failures[category] = fallback_failures.get(category, 0) + 1
                    failed_items.add(str(playlist_index))
                    outcomes[str(playlist_index)] = {"status": "failed", "reason": safe_reason}
                    if isinstance(fallback_error, AnonymousRateLimited) and fallback_error.retry_at:
                        job["retry_at"] = fallback_error.retry_at
                    job["failed"] = len(failed_items)
                    if isinstance(fallback_error, AnonymousServiceUnavailable) or "rate limit" in message.lower():
                        job["result_reason"] = (
                            "The anonymous fallback stopped: " + safe_reason +
                            (" Retry after " + datetime.fromtimestamp(fallback_error.retry_at, timezone.utc).isoformat() + "." if isinstance(fallback_error, AnonymousRateLimited) and fallback_error.retry_at else ".") +
                            " Remaining tracks were not attempted."
                        )
                        job["_cancel_api_fallback"] = True
                    print(f"Job {job['id']}: anonymous fallback failed for track {playlist_index}: {safe_reason}", flush=True)
                finally:
                    if not job.get("_cancel_api_fallback"):
                        job["retry_at"] = None
                    job["downloaded_bytes"] = 0
                    job["total_bytes"] = None
                    job["speed"] = None
                    job["current"] = None
                    job["phase"] = "preparing"
                if not job.get("_cancel_api_fallback"):
                    time.sleep(random.randint(5, 10))

        # 3) Package completed media when this is a playlist job.
        def track_order(path):
            match = re.match(r"^(\d+) - ", os.path.basename(path))
            return (int(match.group(1)) if match else 0, path)
        media_files = sorted(glob.glob(os.path.join(job_dir, "*." + media_ext)), key=track_order)
        for media_path in media_files:
            if media_format == "mp4": validate_mp4(media_path)
            else: validate_mp3(media_path)
        # Validate scratch outputs before counting and resolve titles by playlist
        # position when flat entries omit playlist_index.
        entries = (playlist_info.get("entries", []) if playlist_info and playlist_info.get("_type") == "playlist" else [playlist_info or {}])
        entries_by_index = {}
        for position, entry in enumerate(entries, start=1):
            if entry:
                entries_by_index[str(int(entry.get("playlist_index") or position))] = entry
        for media_path in media_files:
            match_idx = re.match(r"^(\d+)(?: - .*)?\.", os.path.basename(media_path))
            idx = match_idx.group(1) if match_idx else os.path.basename(media_path).split(".", 1)[0]
            if media_format == "mp4": validate_mp4(media_path)
            else: validate_mp3(media_path)
            entry = entries_by_index.get(str(int(idx)), {})
            scratch_name = os.path.basename(media_path).rsplit(".",1)[0]
            original_title = scratch_name.split(" - ",1)[1] if " - " in scratch_name else None
            resolved_title = (entry.get("title") or original_title) if playlist_info and playlist_info.get("_type") == "playlist" else original_title
            final_path = os.path.join(job_dir, f"{idx} - {sanitize(resolved_title or job.get('title') or 'video')}.{media_ext}")
            if media_path != final_path: os.replace(media_path, final_path)
        media_files = sorted(glob.glob(os.path.join(job_dir, "*." + media_ext)), key=track_order)
        job["completed"] = len(media_files)
        job["completed_items"] = {os.path.basename(path).split(" - ", 1)[0] for path in media_files}
        job["completed_duration"] = sum(
            float(entries_by_index.get(str(int(index)), {}).get("duration") or 0)
            for index in job["completed_items"]
        )
        if job["total"] is not None:
            job["failed"] = len(failed_items)
            job["remaining"] = max(0, job["total"] - len(media_files) - job["failed"])
        if playlist_info and job["mode"] == "playlist":
            entries = playlist_info.get("entries") or ([playlist_info] if playlist_info.get("id") else [])
            for pos, entry in enumerate(entries, start=1):
                entry = entry or {}
                idx = str(int(entry.get("playlist_index") or pos))
                if any(re.match(r"^0*" + re.escape(idx) + r" - ", os.path.basename(name)) for name in media_files):
                    outcomes[idx] = {"status": "downloaded", "reason": ""}
                else:
                    outcomes.setdefault(idx, {"status": "not attempted", "reason": "Not attempted"})
            report_path = os.path.join(job_dir, "missing-tracks.csv")
            with open(report_path, "w", newline="", encoding="utf-8") as report:
                writer = csv.writer(report)
                writer.writerow(("track_index", "title", "public_youtube_url", "status", "reason"))
                for pos, entry in enumerate(entries, start=1):
                    entry = entry or {}
                    idx = str(int(entry.get("playlist_index") or pos))
                    result = outcomes.get(idx, {})
                    if result.get("status") not in ("failed", "not attempted"):
                        continue
                    video_url = ""
                    if entry.get("id"):
                        video_url = "https://www.youtube.com/watch?v=" + quote(str(entry["id"]), safe="")
                    else:
                        candidate = entry.get("webpage_url") or entry.get("url") or ""
                        if is_supported_youtube_url(candidate):
                            parsed = urlsplit(candidate)
                            params = parse_qs(parsed.query)
                            if parsed.path == "/watch" and params.get("v"):
                                video_url = "https://www.youtube.com/watch?v=" + quote(params["v"][0], safe="")
                            elif parsed.path == "/playlist" and params.get("list"):
                                video_url = "https://www.youtube.com/playlist?list=" + quote(params["list"][0], safe="")
                    title = re.sub(r"https?://\S+|\b(token|secret|signature)=\S+", "[redacted]", str(entry.get("title") or "Track " + idx), flags=re.I)
                    reason = re.sub(r"https?://\S+|\b(token|secret|signature)=\S+", "[redacted]", result.get("reason", ""), flags=re.I)
                    if title.lstrip().startswith(("=", "+", "-", "@")):
                        title = "'" + title
                    writer.writerow((idx, title, video_url, result.get("status"), reason))
            if outcomes:
                job["failed"] = sum(x["status"] == "failed" for x in outcomes.values())
                job["remaining"] = sum(x["status"] == "not attempted" for x in outcomes.values())
            failed_indices = {index for index, result in outcomes.items() if result.get("status") == "failed"}
            job["failed_duration"] = sum(
                float(entry.get("duration") or 0)
                for position, entry in enumerate(entries, start=1)
                if entry and str(int(entry.get("playlist_index") or position)) in failed_indices
            )
        if not media_files:
            reason = job.get("result_reason") or logger.last_error
            if media_403_fallback:
                fallback_summary = job.get("result_reason") or (
                    "; ".join(f"{count} {category}" for category, count in fallback_failures.items())
                    if fallback_failures else "the fallback did not return a usable video"
                )
                raise RuntimeError(
                    f"{gate_error}. The anonymous video fallback could not produce an MP4 ({fallback_summary}). "
                    "Check that the affected videos are publicly available, then retry. No MP4 files were produced."
                )
            raise RuntimeError(
                (("YouTube/yt-dlp reported: " + reason + ". ") if reason else "")
                + f"No {media_ext.upper()} files were produced. The playlist may be empty, private, "
                "age-restricted, or blocked by YouTube."
            )
        if job["mode"] == "single":
            job["file"] = media_files[0]
            job["file_size"] = os.path.getsize(media_files[0])
            for child in os.listdir(job_dir):
                child_path=os.path.join(job_dir,child)
                if os.path.abspath(child_path)==os.path.abspath(job["file"]): continue
                if os.path.isdir(child_path): shutil.rmtree(child_path,ignore_errors=True)
                else:
                    try: os.remove(child_path)
                    except OSError: pass
            job["result"] = "complete"
            job["phase"] = "ready"
            job["state"] = "done"
            return
        zip_path = os.path.join(
            DOWNLOADS_DIR, f"{sanitize(job['title'])}-{job['id']}.zip"
        )
        job["phase"] = "archiving"
        archive_files = media_files + ([report_path] if playlist_info and os.path.isfile(report_path) and (job["failed"] or job["remaining"]) else [])
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_STORED) as zf:
            for m in archive_files:
                zf.write(m, os.path.basename(m))
        job["zipfile"] = zip_path
        job["zip_size"] = os.path.getsize(zip_path)
        if job["failed"] or job["remaining"]:
            job["result"] = "partial"
            if job.get("result_reason"):
                pass
            elif gate_error:
                job["result_reason"] = (
                    "YouTube required sign-in or a bot check. The anonymous API fallback was used for eligible tracks."
                )
            else:
                job["result_reason"] = (
                    "Some tracks could not be downloaded. Unattempted tracks are counted separately."
                )
            if fallback_failures:
                job["result_reason"] += " " + "; ".join(f"{count} {reason}" for reason, count in fallback_failures.items()) + "."
        else:
            job["result"] = "complete"
        # The archive now owns the complete output; raw audio and fragments
        # need not occupy disk while the user downloads it.
        shutil.rmtree(job_dir)
        job["phase"] = "ready"
        job["state"] = "done"
    except Exception as exc:  # noqa: BLE001
        job["state"] = "error"
        job["error"] = safe_public_error(exc)
        try:
            if os.path.isdir(job_dir):
                shutil.rmtree(job_dir)
            if zip_path and os.path.isfile(zip_path):
                os.remove(zip_path)
        except OSError as cleanup_error:
            print(f"Could not clean failed job {job['id']}: {cleanup_error}", flush=True)
    finally:
        MEDIA_LOCK.release()


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
            if length > 2 * 1024 * 1024:
                raise ValueError("Request is too large")
            if length <= 0:
                return {}
            return json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            raise
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
        # Each ZIP is transferred once. Keep it on disk after a broken
        # connection, and prevent concurrent transfers from racing cleanup.
        lock = job["_zip_lock"]
        if not lock.acquire(blocking=False):
            return self._send_json({"error": "ZIP download already in progress"}, 409)
        try:
            self._transfer_output(job, zipped=True)
        finally:
            lock.release()

    def _transfer_zip(self, job):
        return self._transfer_output(job, zipped=True)

    def _transfer_output(self, job, zipped=False):
        path = job.get("zipfile") if zipped else job.get("file")
        if not path or not os.path.isfile(path):
            self._send_json({"error": "Download not ready yet"}, 404)
            return
        size = os.path.getsize(path)
        filename = os.path.basename(path)
        ascii_name = filename.encode("ascii", "ignore").decode("ascii")
        self.send_response(200)
        self.send_header("Content-Type", "application/zip" if zipped else ("audio/mpeg" if job.get("format") == "mp3" else "video/mp4"))
        self.send_header(
            "Content-Disposition",
            f'attachment; filename="{ascii_name}"; '
            f"filename*=UTF-8''{quote(filename)}",
        )
        self.send_header("Content-Length", str(size))
        self.end_headers()
        try:
            with open(path, "rb") as f:
                shutil.copyfileobj(f, self.wfile)
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            return  # Preserve the ZIP for a retry.
        try:
            os.remove(path)
            if os.path.isdir(job["dir"]): shutil.rmtree(job["dir"])
        except OSError as cleanup_error:
            print(f"Could not clean downloaded job {job['id']}: {cleanup_error}", flush=True)
            return
        with JOBS_LOCK:
            JOBS.pop(job["id"], None)

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
                jobs = list(JOBS.values())
                source_job = JOBS.get(job_id)
                job = dict(source_job or {})
            if not job:
                return self._send_json({"error": "Job not found"}, 404)
            now = time.time()
            if job.get("state") == "queued":
                queued = [item for item in jobs if item.get("state") == "queued"]
                job["queue_position"] = queued.index(source_job) + 1 if source_job in queued else 1
                ahead = [item for item in jobs if item.get("state") == "running"]
                earlier_queued = queued[:max(0, job["queue_position"] - 1)]
                waiting = sum(estimate_remaining(item, now) or 0 for item in ahead + earlier_queued)
                waiting = max(waiting, max(0, provider_cooldown() - now))
                own = estimate_remaining(job, now)
                status = public_job_status(job, now)
                pending_cooldown = provider_cooldown()
                status["retry_at"] = pending_cooldown if pending_cooldown > now else None
                status["queue_wait_estimate"] = waiting
                status["estimated_remaining"] = waiting if own is None and waiting > 0 else (None if own is None else own + waiting)
                status["estimate_scope"] = "queue" if own is None and waiting > 0 else ("total" if own is not None else None)
                status["estimated_finish"] = now + status["estimated_remaining"] if status["estimated_remaining"] is not None else None
            else:
                status = public_job_status(job, now)
            self._send_json(status)
        elif self.path.startswith("/api/zip"):
            with JOBS_LOCK:
                job = dict(JOBS.get(self._parse_job_id(), {}))
            if job:
                return self._send_zip(job)
            self._send_json({"error": "Job not found"}, 404)
        elif self.path.startswith("/api/file"):
            with JOBS_LOCK: job = dict(JOBS.get(self._parse_job_id(), {}))
            if job: return self._send_file_output(job)
            self._send_json({"error":"Job not found"},404)
        else:
            self.send_error(404)

    def _parse_job_id(self):
        query = self.path.split("?", 1)[1] if "?" in self.path else ""
        params = dict(p.split("=", 1) for p in query.split("&") if "=" in p)
        return params.get("id", "")

    def _send_file_output(self, job):
        lock=job["_zip_lock"]
        if not lock.acquire(blocking=False): return self._send_json({"error":"File transfer already in progress"},409)
        try: return self._transfer_output(job, zipped=False)
        finally: lock.release()

    def do_POST(self):
        try:
            data = self._read_json()
            if not isinstance(data, dict):
                raise ValueError("Send a JSON object")
        except ValueError as exc:
            return self._send_json({"error": str(exc)}, 400)
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
            with MEDIA_LOCK:
                info = flat_info(url)
        except Exception as exc:  # noqa: BLE001
            parsed=urlsplit(url); q=parse_qs(parsed.query)
            if parsed.path == "/watch" or parsed.hostname == "youtu.be" or parsed.path.startswith("/shorts/"):
                vid=q.get("v",[parsed.path.strip("/")])[0]
                return self._send_json({"is_playlist":False,"title":"Video","count":1,"duration":None})
            return self._send_json({"error": safe_public_error(exc)}, 500)

        if info and info.get("_type") == "playlist":
            entries = info.get("entries") or []
            return self._send_json(
                {
                    "is_playlist": True,
                    "title": info.get("title"),
                    "count": len(entries) or info.get("count") or 0,
                    "duration": estimate_playlist_duration(entries),
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
                "duration": info.get("duration") if info else None,
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
        media_format = data.get("format", "mp3")
        if media_format not in ("mp3", "mp4"): media_format = "mp3"
        try: resolution=int(data.get("resolution",720))
        except (TypeError,ValueError): resolution=720
        if resolution not in (480,720,1080): resolution=720

        job = new_job(uuid.uuid4().hex)
        job["format"] = media_format
        job["mode"] = "playlist" if urlsplit(url).path == "/playlist" else "single"
        if job["mode"] == "single":
            job["total"] = 1
            job["remaining"] = 1
        with JOBS_LOCK:
            JOBS[job["id"]] = job
        thread = threading.Thread(
            target=download_worker, args=(job, url, quality, media_format, resolution), daemon=True
        )
        thread.start()
        self._send_json({"id": job["id"]})


def main():
    print("-" * 60)
    print("  GrooveZip — MP3 / MP4 downloads")
    print(f"  Open in your browser:  http://{HOST}:{PORT}")
    print("  Stop with Ctrl+C")
    print("-" * 60)
    def handle_sigterm(_signum, _frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, handle_sigterm)
    server = None
    try:
        start_pot_provider()
        server = ThreadingHTTPServer((HOST, PORT), Handler)
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nBye!")
    finally:
        if server is not None:
            server.server_close()
        stop_pot_provider()


if __name__ == "__main__":
    main()
