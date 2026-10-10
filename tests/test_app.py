import io
import json
import os
import subprocess
import tempfile
import threading
import time
import unittest
import zipfile
from http.server import ThreadingHTTPServer
from unittest.mock import MagicMock, call
from unittest.mock import patch
from urllib.request import Request, urlopen
from urllib.error import HTTPError

import app

class AppTests(unittest.TestCase):
    def setUp(self):
        app.PROVIDER_NOT_BEFORE = 0

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
            "https://youtube.com/shorts/abc",
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

    def test_status_endpoint_projects_all_job_states_safely(self):
        jobs = []
        for state in ("queued", "running", "error", "done"):
            job = app.new_job("status-" + state)
            job.update(state=state, phase=state, format="mp4", mode="single",
                       total=10, remaining=8, playlist_duration=3000, created=900)
            if state == "error": job["error"] = "offline"
            with app.JOBS_LOCK: app.JOBS[job["id"]] = job
            jobs.append(job)
        try:
            for job in jobs:
                status, body = self.request("/api/status?id=" + job["id"])
                self.assertEqual(status, 200)
                self.assertEqual(body["format"], "mp4")
                self.assertEqual(body["mode"], "single")
                self.assertIsNone(body["file_size"])
                self.assertEqual(body["playlist_duration"], 3000)
                self.assertIn("estimated_remaining", body)
            self.assertEqual(self.request("/api/status?id=status-error")[1]["estimated_remaining"], 0)
        finally:
            with app.JOBS_LOCK:
                for job in jobs: app.JOBS.pop(job["id"], None)

    def test_queued_job_publishes_requested_format_and_mode_before_worker_starts(self):
        class QueuedDownloader:
            def __init__(self, options): self.options=options
            def __enter__(self): return self
            def __exit__(self, *_): pass
            def download(self, _urls):
                path=os.path.join(os.path.dirname(self.options["outtmpl"]), "1.mp4")
                with open(path,"wb") as output: output.write(b"video")
        app.MEDIA_LOCK.acquire()
        job_id=None
        released=False
        try:
            with patch.object(app,"DOWNLOADS_DIR",tempfile.gettempdir()), patch.object(app,"flat_info",return_value={"id":"clip","title":"Clip","duration":10}), patch.object(app.yt_dlp,"YoutubeDL",QueuedDownloader), patch.object(app,"validate_mp4"):
                status, body=self.request("/api/download",{"url":"https://youtube.com/watch?v=clip&list=playlist","format":"mp4"})
                self.assertEqual(status,200); job_id=body["id"]
                status, queued=self.request("/api/status?id="+job_id)
                self.assertEqual(status,200)
                self.assertEqual((queued["state"],queued["format"],queued["mode"]),("queued","mp4","single"))
                app.MEDIA_LOCK.release(); released=True
                deadline=time.monotonic()+3
                while time.monotonic()<deadline:
                    status, current=self.request("/api/status?id="+job_id)
                    if current.get("state") in ("done","error"): break
                    time.sleep(.01)
        finally:
            if not released: app.MEDIA_LOCK.release()
        self.assertEqual(current["state"],"done",current.get("error"))
        with app.JOBS_LOCK: finished=app.JOBS.pop(job_id)
        if finished.get("file") and os.path.isfile(finished["file"]): os.remove(finished["file"])
        app.shutil.rmtree(finished["dir"],ignore_errors=True)

    def test_eta_has_duration_based_initial_estimate_and_pause_safe_adaptation(self):
        job = app.new_job("eta")
        job.update(state="running", total=10, completed=0, playlist_duration=3000,
                   created=900, started_at=900)
        mp3_initial = app.estimate_remaining(job, now=1000)
        self.assertEqual(mp3_initial, 340)
        mp4_job = dict(job, format="mp4")
        self.assertGreater(app.estimate_remaining(mp4_job, now=1000), mp3_initial)
        long_audio = dict(job, playlist_duration=10_000)
        self.assertLess(app.estimate_remaining(long_audio, now=1000), 10_000)

        job.update(completed=2, completed_duration=100, paused_seconds=50,
                   playlist_duration=300, started_at=900)
        estimate = app.estimate_remaining(job, now=1000)
        self.assertGreater(estimate, 0)
        job["paused_seconds"] = 100
        self.assertAlmostEqual(app.estimate_remaining(job, now=1050), estimate)
        job.update(completed=1, completed_duration=100, total=10, paused_seconds=0,
                   started_at=988, playlist_duration=1000)
        self.assertLess(app.estimate_remaining(job, now=1000), mp3_initial)
        job["retry_at"] = 1015
        self.assertAlmostEqual(app.estimate_remaining(job, now=1000),
                               app.estimate_remaining(dict(job, retry_at=None), now=1000) + 15)
        job["state"] = "done"
        self.assertEqual(app.estimate_remaining(job, now=1050), 0)

    def test_mp3_hook_uses_completed_output_when_yt_dlp_copy_points_to_source(self):
        class StalePathDownloader:
            def __init__(self, options): self.options = options
            def __enter__(self): return self
            def __exit__(self, *_): pass
            def download(self, _urls):
                directory = os.path.dirname(self.options["outtmpl"])
                with open(os.path.join(directory, "1.webm"), "wb") as source:
                    source.write(b"source")
                with open(os.path.join(directory, "1.mp3"), "wb") as output:
                    output.write(b"valid mp3 fixture")
                self.options["logger"].debug("Downloading item 1 of 1")
                self.options["logger"].error("Anonymous download service returned an invalid MP3")
                event = {"status": "finished", "info_dict": {
                    "filepath": os.path.join(directory, "1.webm"),
                    "playlist_index": 1, "duration": 19,
                }}
                self.options["postprocessor_hooks"][0](event)
                self.options["postprocessor_hooks"][0](event)

        info = {"_type": "playlist", "title": "Stale path", "entries": [
            {"title": "Track", "duration": 19, "playlist_index": 1}
        ]}
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(app, "DOWNLOADS_DIR", directory), \
             patch.object(app, "flat_info", return_value=info), \
             patch.object(app.yt_dlp, "YoutubeDL", StalePathDownloader), \
             patch.object(app, "validate_mp3") as validate, \
             patch("builtins.print") as log:
            job = app.new_job("stale-path")
            job["dir"] = os.path.join(directory, job["id"])
            app.download_worker(job, "https://youtube.com/playlist?list=abc", 192)
            self.assertEqual(job["state"], "done", job.get("error"))
            self.assertEqual(job["result"], "complete")
            self.assertEqual((job["completed"], job["failed"], job["completed_duration"]), (1, 0, 19))
            self.assertEqual(validate.call_args_list[:2], [
                call(os.path.join(job["dir"], "1.mp3")),
                call(os.path.join(job["dir"], "1.mp3")),
            ])
            self.assertEqual(validate.call_count, 3)  # one hook validation, then final archive validation
            self.assertEqual(sum("invalid MP3" in str(call) for call in log.call_args_list), 1)
            with zipfile.ZipFile(job["zipfile"]) as archive:
                self.assertEqual(archive.namelist(), ["1 - Track.mp3"])
            if os.path.isfile(job["zipfile"]): os.remove(job["zipfile"])

    def test_single_zero_padded_output_uses_discovered_duration_and_reconciles_failure(self):
        class Downloader:
            def __init__(self, options): self.options = options
            def __enter__(self): return self
            def __exit__(self, *_): pass
            def download(self, _urls):
                directory = os.path.dirname(self.options["outtmpl"])
                source = os.path.join(directory, "00001.webm")
                with open(source, "wb") as f: f.write(b"source")
                with open(os.path.join(directory, "00001.mp3"), "wb") as f: f.write(b"fixture")
                self.options["progress_hooks"][0]({"status":"downloading", "info_dict":{"title":"Song", "duration":19}})
                self.options["logger"].error("Transient download issue")
                event = {"status":"finished", "info_dict":{"filepath":source, "duration":19}}
                self.options["postprocessor_hooks"][0](event)
                self.options["postprocessor_hooks"][0](event)
        with tempfile.TemporaryDirectory() as directory, patch.object(app, "DOWNLOADS_DIR", directory), patch.object(app, "flat_info", side_effect=RuntimeError("metadata unavailable")), patch.object(app.yt_dlp, "YoutubeDL", Downloader), patch.object(app, "validate_mp3"):
            job = app.new_job("zero-padded-single")
            app.download_worker(job, "https://youtube.com/watch?v=abc", 192)
            self.assertEqual(job["state"], "done", job.get("error"))
            self.assertEqual((job["completed"], job["failed"], job["playlist_duration"], job["completed_duration"]), (1, 0, 19, 19))
            self.assertTrue(job["file"].endswith("00001 - Song.mp3"))

    def test_shared_provider_deadline_is_honored_by_next_request_without_real_wait(self):
        clock = [1000.0]
        app.provider_cooldown(1100)
        job = app.new_job("queued-cooldown")
        with patch.object(app.time, "time", side_effect=lambda: clock[0]), patch.object(
            app, "wait_until", side_effect=lambda deadline: clock.__setitem__(0, deadline)
        ) as wait, patch.object(app, "_curl_request", return_value=(200, b"ok", {})) as request:
            self.assertEqual(app._request_with_retries(app.ANONYMOUS_API_BASE + "/api/v1/download")[0], 200)
        wait.assert_called_once_with(1100)
        request.assert_called_once()

    def test_queued_eta_includes_running_job_wait(self):
        now=time.time()
        running=app.new_job("ahead-job")
        running.update(state="running",total=2,playlist_duration=120,created=now-30,started_at=now-30)
        queued=app.new_job("behind-job")
        queued.update(state="queued",total=None,playlist_duration=None,created=now)
        with app.JOBS_LOCK:
            app.JOBS[running["id"]]=running; app.JOBS[queued["id"]]=queued
        try:
            status,body=self.request("/api/status?id="+queued["id"])
            self.assertEqual(status,200)
            self.assertEqual(body["queue_position"],1)
            self.assertEqual(body["estimate_scope"],"queue")
            self.assertEqual(body["estimated_remaining"],body["queue_wait_estimate"])
            self.assertGreater(body["queue_wait_estimate"],0)
            deadline=now+300
            app.provider_cooldown(deadline)
            status,waiting=self.request("/api/status?id="+queued["id"])
            self.assertEqual(status,200)
            self.assertEqual(waiting["retry_at"],deadline)
            self.assertGreaterEqual(waiting["queue_wait_estimate"],deadline-time.time()-1)
        finally:
            with app.JOBS_LOCK:
                app.JOBS.pop(running["id"],None); app.JOBS.pop(queued["id"],None)

    def test_ytdlp_uses_default_youtube_clients_with_bgutil(self):
        args = app.CHECK_OPTS["extractor_args"]
        self.assertNotIn("youtube", args)
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
            "duration": 12,
            "url": "https://youtube.com/playlist?list=PL",
            "entries": [{"index": 1, "title": "Song", "length": 12}],
        })

    def test_invalid_url_rejected_before_ytdlp(self):
        with patch.object(app, "flat_info") as flat_info:
            status, body = self.request("/api/check", {"url": "http://127.0.0.1/secret"})
        self.assertEqual(status, 400)
        self.assertIn("YouTube", body["error"])
        flat_info.assert_not_called()

    def test_shorts_metadata_failure_falls_back_to_single_video_entry(self):
        with patch.object(app,"flat_info",side_effect=RuntimeError("metadata unavailable")):
            status, body=self.request("/api/check",{"url":"https://youtube.com/shorts/short-id"})
        self.assertEqual(status,200)
        self.assertFalse(body["is_playlist"])
        self.assertEqual(body["count"],1)

    def test_download_jobs_with_same_title_have_independent_zips(self):
        job_by_dir = {}

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
                hook = self.options["progress_hooks"][0]
                hook({"status": "downloading", "downloaded_bytes": 100,
                      "total_bytes": 200, "speed": 50,
                      "info_dict": {"title": "Song"}})
                job = job_by_dir.get(directory)
                if job is not None:
                    assert job["phase"] == "downloading"
                    assert job["downloaded_bytes"] == 100
                    assert job["total_bytes"] == 200
                    assert job["speed"] == 50
                hook({"status": "finished"})
                if job is not None:
                    assert job["phase"] == "converting"
                    assert job["completed"] == 0
                self.options["postprocessor_hooks"][0]({"status": "started"})
                assert job["completed"] == 0
                self.options["postprocessor_hooks"][0]({"status": "finished"})
                assert job["completed"] == 1
                self.options["postprocessor_hooks"][0]({"status": "finished"})
                assert job["completed"] == 1

            def assert_provider_options(self):
                assert "extract_flat" not in self.options
                args = self.options["extractor_args"]
                assert "youtube" not in args
                assert self.options["format"] == "bestaudio"
                assert args["youtubepot-bgutilhttp"]["base_url"] == [app.POT_PROVIDER_URL]

        with tempfile.TemporaryDirectory() as downloads:
            with patch.object(app, "DOWNLOADS_DIR", downloads), patch.object(
                app, "flat_info", return_value={"title": "Same title"}
            ), patch.object(app.yt_dlp, "YoutubeDL", FakeDownloader):
                first, second = app.new_job("first"), app.new_job("second")
                for job in (first, second):
                    job["dir"] = os.path.join(downloads, job["id"])
                    job_by_dir[job["dir"]] = job
                    with patch.object(app, "validate_mp3"):
                        app.download_worker(job, "https://youtube.com/playlist?list=abc", 192)
                    self.assertEqual(job["state"], "done", job.get("error"))
                    self.assertEqual(job["completed"], 1)
                    self.assertEqual(job["phase"], "ready")
                    self.assertFalse(os.path.exists(job["dir"]))
                    with zipfile.ZipFile(job["zipfile"]) as archive:
                        self.assertEqual(archive.read("1 - Song.mp3"), job["dir"].encode())
                self.assertNotEqual(first["zipfile"], second["zipfile"])

    def test_public_cdn_403_retries_standard_m4a_selector_once(self):
        formats = []
        class FormatRetryDownloader:
            def __init__(self, options): self.options = options; formats.append(options["format"])
            def __enter__(self): return self
            def __exit__(self, *_): pass
            def download(self, _urls):
                if len(formats) == 1:
                    self.options["logger"].debug("Downloading item 1 of 1")
                    self.options["logger"].error("HTTP Error 403: Forbidden")
                else:
                    with open(os.path.join(os.path.dirname(self.options["outtmpl"]), "1.mp3"), "wb") as output:
                        output.write(b"validated by test double")
        info = {"_type":"playlist", "title":"CDN", "entries":[
            {"id":"asset", "title":"Recovered", "duration":12}
        ]}
        with tempfile.TemporaryDirectory() as directory, patch.object(app, "DOWNLOADS_DIR", directory), patch.object(app, "flat_info", return_value=info), patch.object(app.yt_dlp, "YoutubeDL", FormatRetryDownloader), patch.object(app, "validate_mp3"):
            job=app.new_job("cdn-retry"); job["dir"]=os.path.join(directory, job["id"])
            app.download_worker(job, "https://youtube.com/playlist?list=abc", 192)
            self.assertEqual(job["state"], "done", job.get("error"))
            self.assertEqual(job["completed"], 1)
            self.assertIn("bestaudio[ext=m4a]/bestaudio[protocol^=m3u8_native]", formats[1])
            self.assertEqual(job["failed"], 0)

    def test_numeric_scratch_names_resolve_titles_by_position(self):
        options={}
        class NumericDownloader:
            def __init__(self, settings): options.update(settings)
            def __enter__(self): return self
            def __exit__(self, *_): pass
            def download(self, _urls):
                scratch=os.path.dirname(options["outtmpl"])
                for index in (1,2):
                    with open(os.path.join(scratch, f"{index}.mp3"), "wb") as output: output.write(b"audio")
        entries=[{"title":"First [mix] 50%", "duration":10}, {"title":"Second [mix] 99%", "duration":20}]
        info={"_type":"playlist", "title":"Numeric", "entries":entries}
        with tempfile.TemporaryDirectory() as directory, patch.object(app,"DOWNLOADS_DIR",directory), patch.object(app,"flat_info",return_value=info), patch.object(app.yt_dlp,"YoutubeDL",NumericDownloader), patch.object(app,"validate_mp3"):
            job=app.new_job("numeric-title"); job["dir"]=os.path.join(directory,job["id"])
            app.download_worker(job,"https://youtube.com/playlist?list=numeric",192)
            with zipfile.ZipFile(job["zipfile"]) as archive:
                self.assertEqual(archive.namelist(),["1 - First [mix] 50%.mp3","2 - Second [mix] 99%.mp3"])

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

    def test_single_file_transfer_uses_format_mime_and_cleans_job_on_success(self):
        with tempfile.TemporaryDirectory() as downloads, patch.object(app, "DOWNLOADS_DIR", downloads):
            job=app.new_job("direct-file"); job["format"]="mp4"; job["mode"]="single"
            os.makedirs(job["dir"])
            job["file"]=os.path.join(job["dir"],"1 - Clip.mp4")
            with open(job["file"],"wb") as target: target.write(b"mp4 fixture")
            with app.JOBS_LOCK: app.JOBS[job["id"]]=job
            with urlopen(self.base+"/api/file?id="+job["id"]) as response:
                self.assertEqual(response.headers.get_content_type(),"video/mp4")
                self.assertEqual(response.read(),b"mp4 fixture")
            deadline=time.monotonic()+2
            while job["id"] in app.JOBS and time.monotonic()<deadline: time.sleep(.01)
            self.assertNotIn(job["id"],app.JOBS)
            self.assertFalse(os.path.exists(job["dir"]))

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

    def test_interrupted_direct_file_transfer_preserves_output_for_retry(self):
        with tempfile.TemporaryDirectory() as directory:
            job=app.new_job("direct-retry"); job.update(mode="single",format="mp3")
            job["dir"]=os.path.join(directory,job["id"])
            os.makedirs(job["dir"]); job["file"]=os.path.join(job["dir"],"1 - Song.mp3")
            with open(job["file"],"wb") as target: target.write(b"audio")
            handler=object.__new__(app.Handler)
            handler.send_response=MagicMock(); handler.send_header=MagicMock(); handler.end_headers=MagicMock()
            handler.wfile=MagicMock(); handler.wfile.write.side_effect=BrokenPipeError("connection closed")
            handler._send_file_output(job)
            self.assertTrue(os.path.isfile(job["file"]))
            self.assertTrue(job["_zip_lock"].acquire(blocking=False)); job["_zip_lock"].release()

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

    def test_bot_gate_stops_local_requests_and_keeps_partial_zip_on_api_limit(self):
        class PartiallyBlockedDownloader:
            def __init__(self, options):
                self.options = options

            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

            def download(self, _urls):
                assert "skip_playlist_after_errors" not in self.options
                assert self.options["sleep_interval"] == 5
                assert self.options["max_sleep_interval"] == 10
                directory = os.path.dirname(self.options["outtmpl"])
                with open(os.path.join(directory, "1 - Ready.mp3"), "wb") as track:
                    track.write(b"ready")
                logger = self.options["logger"]
                logger.debug("Downloading item 2 of 8")
                logger.error("Sign in to confirm you're not a bot")

        info = {
            "_type": "playlist", "title": "Blocked Mix",
            "entries": [{"playlist_index": i, "id": str(i)} for i in range(1, 9)],
        }
        with tempfile.TemporaryDirectory() as downloads, patch.object(
            app, "DOWNLOADS_DIR", downloads
        ), patch.object(app, "flat_info", return_value=info), patch.object(
            app.yt_dlp, "YoutubeDL", PartiallyBlockedDownloader
        ), patch.object(
            app, "anonymous_download_track", side_effect=RuntimeError("Anonymous download service rate limit reached")
        ):
            job = app.new_job("partial")
            job["dir"] = os.path.join(downloads, job["id"])
            with patch.object(app, "validate_mp3"):
                app.download_worker(job, "https://youtube.com/playlist?list=abc", 192)
            self.assertEqual(job["state"], "done", job.get("error"))
            self.assertEqual(job["result"], "partial")
            self.assertEqual(job["completed"], 1)
            self.assertEqual(job["failed"], 1)
            self.assertEqual(job["remaining"], 6)
            self.assertIn("rate limit", job["result_reason"])
            with zipfile.ZipFile(job["zipfile"]) as archive:
                self.assertEqual(archive.namelist(), ["1 - Ready.mp3", "missing-tracks.csv"])
                report = archive.read("missing-tracks.csv").decode()
                self.assertIn("2,Track 2,", report)
                self.assertIn("8,Track 8,", report)

    def test_one_unavailable_track_is_counted_as_failed_not_unattempted(self):
        class OneUnavailableDownloader:
            def __init__(self, options):
                self.options = options

            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

            def download(self, _urls):
                directory = os.path.dirname(self.options["outtmpl"])
                with open(os.path.join(directory, "1 - Ready.mp3"), "wb") as track:
                    track.write(b"ready")
                logger = self.options["logger"]
                logger.debug("Downloading item 2 of 2")
                logger.error("Video unavailable")

        info = {
            "_type": "playlist", "title": "Mix",
            "entries": [{"playlist_index": 1}, {"playlist_index": 2}],
        }
        with tempfile.TemporaryDirectory() as downloads, patch.object(
            app, "DOWNLOADS_DIR", downloads
        ), patch.object(app, "flat_info", return_value=info), patch.object(
            app.yt_dlp, "YoutubeDL", OneUnavailableDownloader
        ):
            job = app.new_job("unavailable")
            job["dir"] = os.path.join(downloads, job["id"])
            with patch.object(app, "validate_mp3"):
                app.download_worker(job, "https://youtube.com/playlist?list=abc", 192)
            self.assertEqual(job["state"], "done")
            self.assertEqual(job["result"], "partial")
            self.assertEqual(job["failed"], 1)
            self.assertEqual(job["remaining"], 0)
            self.assertTrue(os.path.isfile(job["zipfile"]))

    def test_anonymous_api_completed_download_writes_mp3_atomically(self):
        def api_request(url, *, method="GET", body=None, output_path=None, max_time=90):
            if method == "POST":
                self.assertEqual(url, app.ANONYMOUS_API_BASE + "/api/v1/download?wait=true")
                self.assertEqual(body, {
                    "url": "https://www.youtube.com/watch?v=public",
                    "format": "audio", "quality": "best", "outputFormat": "mp3",
                })
                return 200, json.dumps({"data": {
                    "status": "completed", "downloadUrl": app.ANONYMOUS_API_BASE +
                    "/api/download/abc/file?token=secret", "downloadToken": "secret",
                }}).encode(), {}
            self.assertEqual(output_path.endswith(".part"), True)
            with open(output_path, "wb") as audio:
                audio.write(b"documented-mp3")
            return 200, b"", {}

        with tempfile.TemporaryDirectory() as directory, patch.object(app, "_curl_request", side_effect=api_request), patch.object(app, "validate_mp3"):
            destination = os.path.join(directory, "track.mp3")
            size = app.anonymous_download_track("https://www.youtube.com/watch?v=public", destination)
            self.assertEqual(size, len(b"documented-mp3"))
            with open(destination, "rb") as audio:
                self.assertEqual(audio.read(), b"documented-mp3")
            self.assertFalse(os.path.exists(destination + ".part"))

    def test_anonymous_api_polls_documented_202_status_url(self):
        calls = []
        def api_request(url, *, method="GET", body=None, output_path=None, max_time=90):
            calls.append((url, method))
            if len(calls) == 1:
                return 202, json.dumps({"status": "processing", "statusUrl":
                    app.ANONYMOUS_API_BASE + "/api/v1/download/abc"}).encode(), {}
            if len(calls) == 2:
                return 200, json.dumps({"data": {"status": "completed", "downloadUrl":
                    app.ANONYMOUS_API_BASE + "/api/download/abc/file?token=t", "downloadToken": "t"}}).encode(), {}
            with open(output_path, "wb") as audio:
                audio.write(b"mp3")
            return 200, b"", {}
        with tempfile.TemporaryDirectory() as directory, patch.object(
            app, "_curl_request", side_effect=api_request
        ), patch.object(app.time, "sleep"), patch.object(app, "validate_mp3"):
            app.anonymous_download_track("https://youtube.com/watch?v=abc", os.path.join(directory, "x.mp3"))
        self.assertEqual(calls[1][0], app.ANONYMOUS_API_BASE + "/api/v1/download/abc")

    def test_anonymous_api_rejects_unsafe_status_and_file_urls(self):
        with self.assertRaisesRegex(RuntimeError, "unsafe URL"):
            app._safe_api_url("https://attacker.example/file")
        with self.assertRaisesRegex(RuntimeError, "unexpected URL"):
            app._safe_api_url(app.ANONYMOUS_API_BASE + "/api/download/abc", ["/api/v1/download/"])
        with patch.object(app, "_curl_request", return_value=(200, json.dumps({"data": {
            "status": "completed", "downloadUrl": "https://attacker.example/file",
            "downloadToken": "secret",
        }}).encode(), {})):
            with self.assertRaisesRegex(RuntimeError, "unsafe URL"):
                app.anonymous_download_track("https://youtube.com/watch?v=abc", "/tmp/no-write.mp3")

    def test_anonymous_api_reports_413_and_429_without_response_body(self):
        for status in (413,):
            with self.subTest(status=status), patch.object(
                app, "_curl_request", return_value=(status, b"signed file token=private", {})
            ):
                with self.assertRaises(RuntimeError) as error:
                    app.anonymous_download_track("https://youtube.com/watch?v=abc", "/tmp/no-write.mp3")
                self.assertNotIn("private", str(error.exception))

    def test_api_429_retries_the_same_call_after_long_retry_after(self):
        responses = iter([(429, b"private body", {"retry-after": "7200"}), (200, b"ok", {})])
        clock = [1000.0]
        def advance(retry_at): clock[0] = retry_at
        with patch.object(app, "_curl_request", side_effect=lambda *_a, **_k: next(responses)) as request, patch.object(app, "wait_until", side_effect=advance) as wait, patch.object(app.time, "time", side_effect=lambda: clock[0]):
            result = app._request_with_retries(app.ANONYMOUS_API_BASE + "/api/v1/download")
        self.assertEqual(result[0], 200)
        self.assertEqual(request.call_count, 2)
        self.assertEqual(wait.call_args.args[0], 8200)

    def test_playlist_gate_pipeline_recovers_remaining_public_tracks(self):
        class GatedDownloader:
            def __init__(self, options):
                self.options = options
            def __enter__(self):
                return self
            def __exit__(self, *args):
                pass
            def download(self, _urls):
                directory = os.path.dirname(self.options["outtmpl"])
                with open(os.path.join(directory, "1 - Ready.mp3"), "wb") as audio:
                    audio.write(b"existing-success")
                logger = self.options["logger"]
                logger.debug("Downloading item 2 of 3")
                logger.error("Sign in to confirm you're not a bot")

        info = {"_type": "playlist", "title": "Recovery", "entries": [
            {"playlist_index": 1, "title": "Ready", "id": "one"},
            {"playlist_index": 2, "title": "Recovered", "id": "two"},
            {"playlist_index": 3, "title": "Private", "id": "three"},
        ]}
        calls = []
        def recover(track_url, destination):
            calls.append(track_url)
            with open(destination, "wb") as audio:
                audio.write(b"api-mp3")
            return 7
        with tempfile.TemporaryDirectory() as downloads, patch.object(
            app, "DOWNLOADS_DIR", downloads
        ), patch.object(app, "flat_info", return_value=info), patch.object(
            app.yt_dlp, "YoutubeDL", GatedDownloader
        ), patch.object(app, "anonymous_download_track", side_effect=recover), patch.object(
            app.time, "sleep"
        ):
            job = app.new_job("recovery")
            job["dir"] = os.path.join(downloads, job["id"])
            with patch.object(app, "validate_mp3"):
                app.download_worker(job, "https://youtube.com/playlist?list=public", 192)
            with zipfile.ZipFile(job["zipfile"]) as archive:
                self.assertEqual(len(archive.namelist()), 3)
        self.assertEqual(calls, [
            "https://www.youtube.com/watch?v=two", "https://www.youtube.com/watch?v=three"
        ])
        self.assertEqual(job["state"], "done", job.get("error"))
        self.assertEqual(job["result"], "complete")
        self.assertEqual(job["completed"], 3)

    def test_anonymous_invalid_audio_is_removed(self):
        def request(url, **kwargs):
            if kwargs.get("method") == "POST":
                return 200, json.dumps({"data": {"status": "completed",
                    "downloadUrl": app.ANONYMOUS_API_BASE + "/api/download/x/file?token=t",
                    "downloadToken": "t"}}).encode(), {}
            with open(kwargs["output_path"], "wb") as f:
                f.write(b"this is not audio")
            return 200, b"", {}
        with tempfile.TemporaryDirectory() as directory, patch.object(app, "_curl_request", side_effect=request):
            destination = os.path.join(directory, "invalid.mp3")
            with self.assertRaisesRegex(RuntimeError, "invalid MP3"):
                app.anonymous_download_track("https://youtube.com/watch?v=abc", destination)
            self.assertFalse(os.path.exists(destination))
            self.assertFalse(os.path.exists(destination + ".part"))

    def test_api_global_errors_stop_batch_but_413_is_asset_failure(self):
        for status in (401, 403, 429, 500, 503):
            with self.subTest(status=status), self.assertRaises(app.AnonymousServiceUnavailable):
                app._api_payload(status, b"untrusted signed URL")
        with self.assertRaises(RuntimeError) as caught:
            app._api_payload(413, b"untrusted signed URL")
        self.assertNotIsInstance(caught.exception, app.AnonymousServiceUnavailable)

    def test_retry_after_integer_then_success_retries_same_request(self):
        responses = iter([(429, b"", {"retry-after": "3"}), (200, b"ok", {})])
        cooldowns = []
        clock = [1000.0]
        with patch.object(app, "_curl_request", side_effect=lambda *_a, **_k: next(responses)) as request, patch.object(app, "wait_until", side_effect=lambda at: clock.__setitem__(0, at)) as wait, patch.object(app.time, "time", side_effect=lambda: clock[0]):
            result = app._request_with_retries("https://save.brianrianrehan.com/api/v1/download", on_cooldown=cooldowns.append)
        self.assertEqual(result[0], 200)
        self.assertEqual(request.call_count, 2)
        wait.assert_called_once()
        self.assertEqual(len(cooldowns), 2)
        self.assertEqual(cooldowns[0], 1003)
        self.assertIsNone(cooldowns[1])

    def test_retry_after_http_date_and_excessive_cooldown(self):
        now = 1_800_000_000
        value = app.email.utils.format_datetime(app.datetime.fromtimestamp(now + 20, app.timezone.utc), usegmt=True)
        delay, retry_at = app.parse_retry_after(value, now=now)
        self.assertEqual(delay, 20)
        self.assertEqual(retry_at, now + 20)
        responses = iter([(429, b"", {"retry-after": "3600"}), (200, b"ok", {})])
        clock = [float(now)]
        with patch.object(app, "_curl_request", side_effect=lambda *_a, **_k: next(responses)) as request, patch.object(app, "wait_until", side_effect=lambda at: clock.__setitem__(0, at)) as wait, patch.object(app.time, "time", side_effect=lambda: clock[0]):
            self.assertEqual(app._request_with_retries("https://save.brianrianrehan.com/api/v1/download")[0], 200)
        self.assertEqual(wait.call_args.args[0], now + 3600)
        self.assertEqual(request.call_count, 2)

    def test_retry_does_not_repeat_413_or_403_and_bounds_transient_failures(self):
        for status in (413, 403):
            with patch.object(app, "_curl_request", return_value=(status, b"", {})) as request:
                self.assertEqual(app._request_with_retries("https://save.brianrianrehan.com/api/v1/download")[0], status)
                request.assert_called_once()
        with patch.object(app, "_curl_request", side_effect=app.AnonymousServiceUnavailable("network")) as request, patch.object(app.time, "sleep"):
            with self.assertRaises(app.AnonymousServiceUnavailable):
                app._request_with_retries("https://save.brianrianrehan.com/api/v1/download")
        self.assertEqual(request.call_count, 3)

    def test_partial_zip_has_accurate_safe_missing_track_report(self):
        info = {"_type": "playlist", "title": "Report", "entries": [
            {"playlist_index": 1, "title": "Ready", "id": "ready"},
            {"playlist_index": 2, "title": "Bad", "id": "bad"},
            {"playlist_index": 3, "title": "Pending", "id": "pending"},
        ]}
        class FailingDownloader:
            def __init__(self, opts): self.opts = opts
            def __enter__(self): return self
            def __exit__(self, *_): pass
            def download(self, _):
                path = os.path.join(os.path.dirname(self.opts["outtmpl"]), "1 - Ready.mp3")
                with open(path, "wb") as f: f.write(b"audio")
                self.opts["logger"].debug("Downloading item 2 of 3")
                self.opts["logger"].error("HTTP Error 403: https://secret.example/?token=SIGNED")
        with tempfile.TemporaryDirectory() as directory, patch.object(app, "DOWNLOADS_DIR", directory), patch.object(app, "flat_info", return_value=info), patch.object(app.yt_dlp, "YoutubeDL", FailingDownloader), patch.object(app.time, "sleep"):
            job = app.new_job("report")
            job["dir"] = os.path.join(directory, job["id"])
            with patch.object(app, "validate_mp3"):
                app.download_worker(job, "https://youtube.com/playlist?list=abc", 192)
            with zipfile.ZipFile(job["zipfile"]) as archive:
                self.assertEqual(archive.namelist(), ["1 - Ready.mp3", "missing-tracks.csv"])
                report = archive.read("missing-tracks.csv").decode()
                self.assertIn("2,Bad,https://www.youtube.com/watch?v=bad,failed", report)
                self.assertIn("3,Pending,https://www.youtube.com/watch?v=pending,not attempted", report)
                self.assertNotIn("secret.example", report)
                self.assertNotIn("SIGNED", report)
            self.assertEqual((job["completed"], job["failed"], job["remaining"]), (1, 1, 1))

    def test_poll_retains_status_url_and_token_across_pending_responses(self):
        responses = iter([
            (202, json.dumps({"data": {"status": "pending", "downloadId": "abc",
                                      "downloadToken": "t"}}).encode(), {}),
            (200, json.dumps({"data": {"status": "processing"}}).encode(), {}),
            (200, json.dumps({"data": {"status": "completed", "downloadUrl":
                app.ANONYMOUS_API_BASE + "/api/download/abc/file?token=t"}}).encode(), {}),
        ])
        def request(url, **kwargs):
            if kwargs.get("output_path"):
                with open(kwargs["output_path"], "wb") as f:
                    f.write(b"audio fixture")
                return 200, b"", {}
            return next(responses)
        with tempfile.TemporaryDirectory() as directory, patch.object(app, "_curl_request", side_effect=request), patch.object(app.time, "sleep"), patch.object(app, "validate_mp3"):
            app.anonymous_download_track("https://youtube.com/watch?v=abc", os.path.join(directory, "x.mp3"))

    def test_recovery_targets_original_indices_and_skips_known_private_video(self):
        info = {"_type": "playlist", "title": "Missing only", "entries": [
            {"playlist_index": 47, "id": "public", "title": "Public"},
            {"playlist_index": 55, "id": "private", "title": "Private", "availability": "private"},
        ]}
        def recover(url, destination):
            self.assertEqual(url, "https://www.youtube.com/watch?v=public")
            self.assertEqual(os.path.basename(destination), "47.mp3")
            with open(destination, "wb") as f: f.write(b"fixture")
            return 7
        with tempfile.TemporaryDirectory() as directory, patch.object(app, "DOWNLOADS_DIR", directory), patch.object(app, "flat_info") as metadata, patch.object(app.yt_dlp, "YoutubeDL") as local, patch.object(app, "anonymous_download_track", side_effect=recover) as fallback, patch.object(app.time, "sleep"):
            job=app.new_job("missing-only")
            job["_recovery_info"]=info
            with patch.object(app, "validate_mp3"):
                app.download_worker(job, "https://youtube.com/playlist?list=abc", 192)
            metadata.assert_not_called()
            local.assert_not_called()
            self.assertEqual(fallback.call_count, 1)
            self.assertEqual((job["completed"], job["failed"], job["remaining"]), (1,1,0))
            self.assertEqual(job["result"], "partial")
            self.assertIn("private or unavailable", job["result_reason"])
            self.assertFalse(os.path.exists(job["dir"]))
            with zipfile.ZipFile(job["zipfile"]) as archive:
                self.assertEqual(archive.namelist(), ["47 - Public.mp3", "missing-tracks.csv"])
                report = archive.read("missing-tracks.csv").decode()
                self.assertIn("55,Private", report)

    def test_http_date_cooldown_never_retries_early(self):
        now=1800000000.75
        date=app.email.utils.format_datetime(app.datetime.fromtimestamp(1800000010, app.timezone.utc),usegmt=True)
        delay, retry_at=app.parse_retry_after(date,now=now)
        self.assertEqual(delay,10)
        self.assertGreaterEqual(retry_at,1800000010)

    def test_missing_retry_header_uses_increasing_bounded_delays(self):
        responses=iter([(429,b"",{}),(429,b"",{}),(200,b"ok",{})])
        clock=[1000.0]
        with patch.object(app,"_curl_request",side_effect=lambda *_a,**_k: next(responses)) as request, patch.object(app,"wait_until",side_effect=lambda at: clock.__setitem__(0,at)) as wait, patch.object(app.time,"time",side_effect=lambda:clock[0]):
            self.assertEqual(app._request_with_retries(app.ANONYMOUS_API_BASE + "/api/v1/download")[0],200)
        self.assertEqual(request.call_count,3)
        self.assertEqual([c.args[0] for c in wait.call_args_list],[1060,1180])

    def test_502_is_retried_but_remains_global_failure_on_exhaustion(self):
        with patch.object(app,"_curl_request",return_value=(502,b"",{})) as request, patch.object(app.time,"sleep"):
            with self.assertRaises(app.AnonymousServiceUnavailable) as error:
                app._request_with_retries(app.ANONYMOUS_API_BASE + "/api/v1/download")
        self.assertEqual(request.call_count,3)
        self.assertGreater(error.exception.retry_at,app.time.time())

    def test_single_mp4_uses_video_selector_and_keeps_only_validated_direct_file(self):
        observed = {}
        class VideoDownloader:
            def __init__(self, options): self.options=options; observed.update(options)
            def __enter__(self): return self
            def __exit__(self, *_): pass
            def download(self, _urls):
                self.options["progress_hooks"][0]({"status":"downloading","info_dict":{"title":"Useful clip title"}})
                self_path = os.path.join(os.path.dirname(observed["outtmpl"]), "1.mp4")
                with open(self_path, "wb") as output: output.write(b"video-fixture")
        with tempfile.TemporaryDirectory() as directory, patch.object(app, "DOWNLOADS_DIR", directory), patch.object(app, "flat_info", side_effect=RuntimeError("metadata blocked")), patch.object(app.yt_dlp, "YoutubeDL", VideoDownloader), patch.object(app, "validate_mp4"):
            job = app.new_job("single-video")
            job["dir"] = os.path.join(directory, job["id"])
            app.download_worker(job, "https://youtube.com/watch?v=abc&list=PL", 192, "mp4", 720)
            self.assertEqual(job["state"], "done")
            self.assertEqual(job["mode"], "single")
            self.assertTrue(os.path.isfile(job["file"]))
            self.assertTrue(job["file"].endswith("1 - Useful clip title.mp4"))
            self.assertFalse(job.get("zipfile"))
            self.assertTrue(observed["noplaylist"])
            self.assertIn("bestvideo[height<=720]+bestaudio", observed["format"])

    def test_mp4_validation_requires_audio_video_and_positive_duration(self):
        good = {"streams":[{"codec_type":"video","codec_name":"h264"},{"codec_type":"audio","codec_name":"aac"}],"format":{"format_name":"mov,mp4,m4a","duration":"12"}}
        with patch.object(app.subprocess, "run", return_value=type("R", (), {"returncode":0,"stdout":json.dumps(good).encode()})()):
            app.validate_mp4("fixture.mp4")
        for fixture in (
            {"streams":[{"codec_type":"video","codec_name":"h264"}],"format":{"format_name":"mov,mp4,m4a","duration":"12"}},
            {"streams":[{"codec_type":"video","codec_name":"h264"},{"codec_type":"audio","codec_name":"aac"}],"format":{"format_name":"mov,mp4,m4a","duration":"0"}},
            {"streams":[{"codec_type":"video","codec_name":"h264"},{"codec_type":"audio","codec_name":"aac"}],"format":{"format_name":"matroska,webm","duration":"12"}},
        ):
            with patch.object(app.subprocess, "run", return_value=type("R", (), {"returncode":0,"stdout":json.dumps(fixture).encode()})()):
                with self.assertRaisesRegex(RuntimeError, "missing playable"):
                    app.validate_mp4("fixture.mp4")

    def test_offline_ffmpeg_fixture_rejects_matroska_renamed_mp4(self):
        import shutil
        if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
            self.skipTest("ffmpeg and ffprobe are required for the container fixture")
        with tempfile.TemporaryDirectory() as directory:
            mkv = os.path.join(directory, "valid.mkv")
            mp4_name = os.path.join(directory, "mkv-renamed.mp4")
            subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i",
                            "color=c=black:s=64x64:d=1", "-f", "lavfi", "-i",
                            "sine=frequency=440:duration=1", "-shortest", "-c:v", "mpeg4",
                            "-c:a", "aac", mkv], check=True, timeout=30)
            valid_mp4 = os.path.join(directory, "valid.mp4")
            subprocess.run(["ffmpeg", "-v", "error", "-i", mkv, "-c", "copy", valid_mp4],
                           check=True, timeout=30)
            app.validate_mp4(valid_mp4)
            shutil.copyfile(mkv, mp4_name)
            with self.assertRaisesRegex(RuntimeError, "missing playable"):
                app.validate_mp4(mp4_name)


if __name__ == "__main__":
    unittest.main()
