import io
import json
import os
import tempfile
import threading
import time
import unittest
import zipfile
from http.server import ThreadingHTTPServer
from unittest.mock import MagicMock
from unittest.mock import patch
from urllib.request import Request, urlopen
from urllib.error import HTTPError

import app

# Deliberately synthetic authentication fixture; never a real session.
COOKIE_FIXTURE = "# Netscape HTTP Cookie File\n.youtube.com\tTRUE\t/\tTRUE\t2147483647\tSID\tsynthetic-session-not-real\n"


class AppTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = "http://127.0.0.1:%s" % cls.server.server_port

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    def request(self, path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        req = Request(self.base + path, data=data, headers={"Content-Type": "application/json"})
        try:
            with urlopen(req) as response:
                raw = response.read()
                try:
                    body = json.loads(raw)
                except json.JSONDecodeError:
                    body = raw.decode(errors="replace")
                return response.status, body
        except HTTPError as exc:
            raw = exc.read()
            try:
                body = json.loads(raw)
            except json.JSONDecodeError:
                body = raw.decode(errors="replace")
            return exc.code, body

    def test_supported_youtube_urls(self):
        good = [
            "https://youtube.com/watch?v=abc",
            "https://www.youtube.com/playlist?list=PLabc",
            "https://m.youtube.com/watch?v=abc",
            "https://music.youtube.com/playlist?list=PLabc",
            "https://youtu.be/abc",
        ]
        bad = [
            "http://youtube.com/watch?v=abc",
            "https://youtube.com.evil.test/watch?v=abc",
            "https://127.0.0.1/watch?v=abc",
            "https://youtube.com:8443/watch?v=abc",
            "https://youtube.com/redirect?q=x",
            "file:///etc/passwd",
        ]
        for url in good:
            self.assertTrue(app.is_supported_youtube_url(url), url)
        for url in bad:
            self.assertFalse(app.is_supported_youtube_url(url), url)

    def test_designs_traversal_is_rejected(self):
        with tempfile.NamedTemporaryFile() as secret:
            secret.write(b"outside-designs-secret")
            secret.flush()
            traversal = "/designs/%2e%2e/%2e%2e/%2e%2e/%2e%2e/" + secret.name.lstrip("/")
            status, body = self.request(traversal)
            self.assertEqual(status, 404)
            self.assertNotIn("outside-designs-secret", str(body))

    def test_health_endpoint(self):
        self.assertEqual(self.request("/health"), (200, {"status": "ok"}))

    def test_cookie_upload_rejects_other_sites_without_disclosing_values(self):
        with patch.object(app, "flat_info") as info:
            status, body = self.request("/api/check", {
                "url": "https://youtube.com/watch?v=abc",
                "cookies": COOKIE_FIXTURE.replace(".youtube.com", ".google.com"),
            })
        self.assertEqual(status, 400)
        self.assertNotIn("synthetic-session-not-real", json.dumps(body))
        info.assert_not_called()

    def test_check_cookie_copy_is_private_and_deleted_after_request(self):
        paths = []
        def inspect(_url, cookiefile=None):
            paths.append(cookiefile)
            self.assertEqual(os.stat(cookiefile).st_mode & 0o777, 0o600)
            with open(cookiefile) as file:
                self.assertEqual(file.read(), COOKIE_FIXTURE)
            return {"title": "Private session fixture"}
        with patch.object(app, "flat_info", side_effect=inspect):
            status, _ = self.request("/api/check", {
                "url": "https://youtube.com/watch?v=abc", "cookies": COOKIE_FIXTURE,
            })
        self.assertEqual(status, 200)
        self.assertFalse(os.path.exists(paths[0]))

    def test_download_sessions_are_isolated_deleted_and_excluded_from_zip(self):
        seen = []
        class FakeDownloader:
            def __init__(self, options):
                self.options = options
            def __enter__(self):
                return self
            def __exit__(self, *_args):
                pass
            def download(self, _urls):
                cookiefile = self.options.get("cookiefile")
                seen.append(cookiefile)
                if cookiefile:
                    assert os.stat(cookiefile).st_mode & 0o777 == 0o600
                    with open(cookiefile) as file:
                        assert file.read() == COOKIE_FIXTURE
                directory = os.path.dirname(self.options["outtmpl"])
                with open(os.path.join(directory, "track.mp3"), "wb") as file:
                    file.write(b"synthetic-audio")
        with tempfile.TemporaryDirectory() as downloads, patch.object(
            app, "DOWNLOADS_DIR", downloads
        ), patch.object(app, "flat_info", return_value={"title": "Fixture"}), patch.object(
            app.yt_dlp, "YoutubeDL", FakeDownloader
        ):
            jobs = []
            try:
                for cookies in (COOKIE_FIXTURE, ""):
                    status, response = self.request("/api/download", {
                        "url": "https://youtube.com/watch?v=abc", "cookies": cookies,
                    })
                    self.assertEqual(status, 200)
                    job = app.JOBS[response["id"]]
                    jobs.append(job)
                    deadline = time.monotonic() + 2
                    while job["state"] not in ("done", "error") and time.monotonic() < deadline:
                        time.sleep(0.01)
                    self.assertEqual(job["state"], "done", job.get("error"))
                    self.assertFalse(os.path.exists(job["dir"]))
                    with zipfile.ZipFile(job["zipfile"]) as archive:
                        self.assertEqual(archive.namelist(), ["track.mp3"])
                    _, public_status = self.request("/api/status?id=" + job["id"])
                    self.assertNotIn("synthetic-session-not-real", json.dumps(public_status))
                self.assertIsNotNone(seen[0])
                self.assertIsNone(seen[1])
                self.assertFalse(os.path.exists(seen[0]))
            finally:
                with app.JOBS_LOCK:
                    for job in jobs:
                        app.JOBS.pop(job["id"], None)

    def test_ytdlp_uses_bgutil_and_bounded_youtube_clients(self):
        args = app.CHECK_OPTS["extractor_args"]
        self.assertEqual(args["youtube"]["player_client"], ["mweb", "tv", "web_safari"])
        self.assertEqual(
            args["youtubepot-bgutilhttp"]["base_url"], ["http://127.0.0.1:4416"]
        )

    def test_bgutil_plugin_is_loaded_by_ytdlp(self):
        from yt_dlp.extractor.youtube.pot._registry import _pot_providers
        from yt_dlp.plugins import load_all_plugins

        load_all_plugins()
        provider = _pot_providers.value.get("BgUtilHTTP")
        self.assertIsNotNone(provider)
        self.assertEqual(provider.PROVIDER_VERSION, "2.0.2")

    def test_missing_pot_provider_keeps_local_app_available(self):
        with patch.object(app, "POT_PROVIDER_ENTRYPOINT", "/missing/provider.js"), patch(
            "builtins.print"
        ) as print_message:
            self.assertIsNone(app.start_pot_provider())
        self.assertIn("without anonymous", print_message.call_args.args[0])

    def test_pot_provider_waits_for_ping_and_stops_cleanly(self):
        process = MagicMock()
        process.poll.return_value = None
        response = MagicMock()
        response.status = 200
        response.__enter__.return_value = response
        with tempfile.NamedTemporaryFile() as entrypoint, patch.object(
            app, "POT_PROVIDER_ENTRYPOINT", entrypoint.name
        ), patch.object(app.subprocess, "Popen", return_value=process) as popen, patch.object(
            app, "urlopen", return_value=response
        ):
            self.assertIs(process, app.start_pot_provider())
            popen.assert_called_once_with(
                ["node", entrypoint.name, "--host", "127.0.0.1"],
                cwd=os.path.dirname(entrypoint.name),
            )
            app.stop_pot_provider()
        process.terminate.assert_called_once()
        process.wait.assert_called_once_with(timeout=5)

    def test_pot_provider_startup_failure_is_not_ignored(self):
        process = MagicMock()
        process.poll.return_value = 1
        process.returncode = 1
        with tempfile.NamedTemporaryFile() as entrypoint, patch.object(
            app, "POT_PROVIDER_ENTRYPOINT", entrypoint.name
        ), patch.object(app.subprocess, "Popen", return_value=process):
            with self.assertRaisesRegex(RuntimeError, "exited during startup"):
                app.start_pot_provider()

    def test_check_api_shape_with_mocked_ytdlp(self):
        fake_info = {
            "_type": "playlist", "title": "Mix", "webpage_url": "https://youtube.com/playlist?list=PL",
            "entries": [{"playlist_index": 1, "title": "Song", "duration": 12}],
        }
        with patch.object(app, "flat_info", return_value=fake_info):
            status, body = self.request("/api/check", {"url": "https://youtube.com/playlist?list=PL"})
        self.assertEqual(status, 200)
        self.assertEqual(body, {
            "is_playlist": True, "title": "Mix", "count": 1,
            "url": "https://youtube.com/playlist?list=PL",
            "entries": [{"index": 1, "title": "Song", "length": 12}],
        })

    def test_invalid_url_rejected_before_ytdlp(self):
        with patch.object(app, "flat_info") as flat_info:
            status, body = self.request("/api/check", {"url": "http://127.0.0.1/secret"})
        self.assertEqual(status, 400)
        self.assertIn("YouTube", body["error"])
        flat_info.assert_not_called()

    def test_download_jobs_with_same_title_have_independent_zips(self):
        class FakeDownloader:
            def __init__(self, options):
                self.options = options

            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

            def download(self, urls):
                self.assert_provider_options()
                directory = os.path.dirname(self.options["outtmpl"])
                with open(os.path.join(directory, "1 - Song.mp3"), "wb") as track:
                    track.write(directory.encode())
                self.options["progress_hooks"][0]({"status": "finished"})

            def assert_provider_options(self):
                assert "extract_flat" not in self.options
                args = self.options["extractor_args"]
                assert args["youtube"]["player_client"] == ["mweb", "tv", "web_safari"]
                assert args["youtubepot-bgutilhttp"]["base_url"] == [app.POT_PROVIDER_URL]

        with tempfile.TemporaryDirectory() as downloads:
            with patch.object(app, "DOWNLOADS_DIR", downloads), patch.object(
                app, "flat_info", return_value={"title": "Same title"}
            ), patch.object(app.yt_dlp, "YoutubeDL", FakeDownloader):
                first, second = app.new_job("first"), app.new_job("second")
                for job in (first, second):
                    app.download_worker(job, "https://youtube.com/watch?v=abc", 192)
                    self.assertEqual(job["state"], "done")
                    self.assertEqual(job["completed"], 1)
                    self.assertFalse(os.path.exists(job["dir"]))
                    with zipfile.ZipFile(job["zipfile"]) as archive:
                        self.assertEqual(archive.read("1 - Song.mp3"), job["dir"].encode())
                self.assertNotEqual(first["zipfile"], second["zipfile"])

    def test_completed_zip_transfer_deletes_files_and_job(self):
        with tempfile.TemporaryDirectory() as downloads, patch.object(app, "DOWNLOADS_DIR", downloads):
            job = app.new_job("cleanup-success")
            os.makedirs(job["dir"])
            with open(os.path.join(job["dir"], "fragment.part"), "wb") as fragment:
                fragment.write(b"temporary")
            job["zipfile"] = os.path.join(downloads, "result.zip")
            with zipfile.ZipFile(job["zipfile"], "w") as archive:
                archive.writestr("track.mp3", b"audio-output")
            with app.JOBS_LOCK:
                app.JOBS[job["id"]] = job
            with urlopen(self.base + "/api/zip?id=" + job["id"]) as response:
                self.assertEqual(response.status, 200)
                with zipfile.ZipFile(io.BytesIO(response.read())) as archive:
                    self.assertEqual(archive.read("track.mp3"), b"audio-output")
            deadline = time.monotonic() + 2
            while job["id"] in app.JOBS and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertNotIn(job["id"], app.JOBS)
            self.assertFalse(os.path.exists(job["zipfile"]))
            self.assertFalse(os.path.exists(job["dir"]))
            self.assertEqual(self.request("/api/zip?id=" + job["id"])[0], 404)

    def test_interrupted_zip_transfer_preserves_archive_for_retry(self):
        with tempfile.TemporaryDirectory() as downloads, patch.object(app, "DOWNLOADS_DIR", downloads):
            job = app.new_job("cleanup-retry")
            job["zipfile"] = os.path.join(downloads, "result.zip")
            with open(job["zipfile"], "wb") as archive:
                archive.write(b"zip-output")
            handler = object.__new__(app.Handler)
            handler.send_response = MagicMock()
            handler.send_header = MagicMock()
            handler.end_headers = MagicMock()
            handler.wfile = MagicMock()
            handler.wfile.write.side_effect = BrokenPipeError("connection closed")
            handler._send_zip(job)
            self.assertTrue(os.path.isfile(job["zipfile"]))
            # The failed transfer releases the lock so a later request can retry.
            self.assertTrue(job["_zip_lock"].acquire(blocking=False))
            job["_zip_lock"].release()

    def test_download_preserves_sanitized_ytdlp_failure_reason(self):
        class FailingDownloader:
            def __init__(self, options):
                self.options = options

            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

            def download(self, _urls):
                self.options["logger"].error(
                    "Video unavailable: https://youtube.com/watch?v=abc&token=secret"
                )

        with tempfile.TemporaryDirectory() as downloads:
            with patch.object(app, "DOWNLOADS_DIR", downloads), patch.object(
                app, "flat_info", return_value={"title": "Unavailable"}
            ), patch.object(app.yt_dlp, "YoutubeDL", FailingDownloader):
                job = app.new_job("failed")
                app.download_worker(job, "https://youtube.com/watch?v=abc", 192)
        self.assertEqual(job["state"], "error")
        self.assertIn("Video unavailable", job["error"])
        self.assertIn("No MP3 files were produced", job["error"])
        self.assertNotIn("secret", job["error"])
        self.assertNotIn("?v=abc", job["error"])


if __name__ == "__main__":
    unittest.main()
