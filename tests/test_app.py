import json
import os
import tempfile
import threading
import unittest
import zipfile
from http.server import ThreadingHTTPServer
from unittest.mock import patch
from urllib.request import Request, urlopen
from urllib.error import HTTPError

import app


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
                directory = os.path.dirname(self.options["outtmpl"])
                with open(os.path.join(directory, "1 - Song.mp3"), "wb") as track:
                    track.write(directory.encode())
                self.options["progress_hooks"][0]({"status": "finished"})

        with tempfile.TemporaryDirectory() as downloads:
            with patch.object(app, "DOWNLOADS_DIR", downloads), patch.object(
                app, "flat_info", return_value={"title": "Same title"}
            ), patch.object(app.yt_dlp, "YoutubeDL", FakeDownloader):
                first, second = app.new_job("first"), app.new_job("second")
                for job in (first, second):
                    app.download_worker(job, "https://youtube.com/watch?v=abc", 192)
                    self.assertEqual(job["state"], "done")
                    self.assertEqual(job["completed"], 1)
                    with zipfile.ZipFile(job["zipfile"]) as archive:
                        self.assertEqual(archive.read("1 - Song.mp3"), job["dir"].encode())
                self.assertNotEqual(first["zipfile"], second["zipfile"])


if __name__ == "__main__":
    unittest.main()
