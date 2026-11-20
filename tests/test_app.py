"""The local page's server: its safety rules and one whole run without a model."""
import os
os.environ["L2A_KEYS_FILE"] = "off"  # never read or write the real key file
import http.client
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock
from pathlib import Path

from lecture2audio import app as webapp
from lecture2audio import stages

SAMPLE = Path(__file__).resolve().parent.parent / "examples" / "sample_transcript.txt"


class AppCase(unittest.TestCase):
    command = None

    def setUp(self):
        self.saved = {k: os.environ.get(k) for k in ("L2A_VAULT_DIR", "L2A_PDF")}
        os.environ.pop("L2A_VAULT_DIR", None)  # never write into a real vault
        os.environ["L2A_PDF"] = "0"
        self.tmp = tempfile.TemporaryDirectory()
        self.server = webapp.make_server(Path(self.tmp.name) / "out", 0, self.command)
        self.app = self.server.app
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tearDown(self):
        self.app.close()
        self.server.shutdown()
        self.server.server_close()
        self.tmp.cleanup()
        for key, value in self.saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def request(self, method, path, body=None, headers=None, token=True, timeout=60):
        connection = http.client.HTTPConnection("127.0.0.1", self.app.port, timeout=timeout)
        headers = dict(headers or {})
        if token and method == "POST":
            headers.setdefault("X-L2A-Token", self.app.token)
        if isinstance(body, dict):
            body = json.dumps(body).encode()
        connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse()
        data = response.read()
        connection.close()
        return response, data

    def upload(self, name="lecture.txt", data=None, job=None):
        path = f"/api/upload?name={name}" + (f"&job={job}" if job else "")
        response, body = self.request("POST", path, data or SAMPLE.read_bytes())
        self.assertEqual(response.status, 200, body)
        return json.loads(body)

    def events(self, job):
        """Every event of the job, read from the stream until it ends."""
        response, body = self.request("GET", f"/api/events?job={job}", timeout=180)
        self.assertEqual(response.status, 200)
        self.assertIn("text/event-stream", response.getheader("Content-Type"))
        return [json.loads(line[6:]) for line in body.decode().splitlines()
                if line.startswith("data: ")]


class SafetyTest(AppCase):
    def test_page_holds_the_token_and_stages_come_from_the_graph(self):
        response, body = self.request("GET", "/")
        self.assertEqual(response.status, 200)
        self.assertIn(self.app.token, body.decode())
        self.assertIn("script-src 'nonce-", response.getheader("Content-Security-Policy"))
        _, body = self.request("GET", "/api/config")
        self.assertEqual([s["name"] for s in json.loads(body)["stages"]],
                         [s.name for s in stages.STAGES])

    def test_post_needs_the_token(self):
        response, _ = self.request("POST", "/api/upload?name=a.txt", b"x", token=False)
        self.assertEqual(response.status, 403)
        response, _ = self.request("POST", "/api/run", {"job": "0" * 12},
                                   headers={"X-L2A-Token": "wrong"})
        self.assertEqual(response.status, 403)
        self.assertFalse((self.app.out / "_uploads").exists())

    def test_other_host_or_origin_is_refused(self):
        response, _ = self.request("GET", "/", headers={"Host": "evil.example:80"})
        self.assertEqual(response.status, 403)
        response, _ = self.request("GET", "/api/config",
                                   headers={"Host": f"evil.example:{self.app.port}"})
        self.assertEqual(response.status, 403)
        response, _ = self.request("POST", "/api/upload?name=a.txt", b"x",
                                   headers={"Origin": "http://evil.example"})
        self.assertEqual(response.status, 403)
        response, _ = self.request("GET", "/", headers={"Host": f"localhost:{self.app.port}"})
        self.assertEqual(response.status, 200)

    def test_traversal_is_refused(self):
        secret = Path(self.tmp.name) / "secret.txt"
        secret.write_text("secret")
        for path in ("/files/../secret.txt", "/files/%2e%2e/secret.txt",
                     "/files/x/..%2f..%2fsecret.txt", "/files//etc/passwd",
                     "/api/runs/..", "/api/runs/%2e%2e"):
            response, body = self.request("GET", path)
            self.assertIn(response.status, (403, 404), path)
            self.assertNotIn(b"secret", body.replace(b"output folder", b""))
        # an uploaded name keeps only its base name, inside the job's folder
        info = self.upload("..%2F..%2F..%2Fescape.txt")
        self.assertEqual([f["name"] for f in info["files"]], ["escape.txt"])
        self.assertTrue((self.app.out / "_uploads" / info["job"] / "escape.txt").is_file())
        self.assertFalse((Path(self.tmp.name) / "escape.txt").exists())
        response, _ = self.request("GET", f"/files/_uploads/{info['job']}/escape.txt")
        self.assertEqual(response.status, 403)

    def test_log_lines_map_to_stages(self):
        self.assertEqual(webapp.parse_line("[rewrite] part 3/12: Title"), ("rewrite", 2 / 12))
        self.assertEqual(webapp.parse_line("[verify] part 2: 0 problem(s)"), ("rewrite", None))
        self.assertEqual(webapp.parse_line("[voice] chunk 5/10, part 1/2")[0], "audio")
        self.assertEqual(webapp.parse_line("[course] 4 card(s), 9 note(s): /x")[0], "vault")
        self.assertEqual(webapp.parse_line("120 words, 9 units"), (None, None))


class RunTest(AppCase):
    def test_upload_run_events_results_and_range(self):
        info = self.upload()
        self.assertEqual(info["kind"], "transcript")
        self.assertIn("Transcript", info["summary"])
        job = info["job"]
        response, body = self.request("POST", "/api/run", {"job": job, "options": {
            "llm": "dry", "language": "en", "audio": False, "api_key": "sk-never-on-disk"}})
        self.assertEqual(response.status, 200, body)
        self.assertEqual(json.loads(body)["name"], "lecture")
        events = self.events(job)
        end = events[-1]
        self.assertEqual(events[0]["type"], "snapshot")  # where the run is when the page opens
        log = "\n".join(events[0]["log"] + [e["line"] for e in events if e["type"] == "log"])
        self.assertEqual((end["type"], end["status"]), ("end", "done"), log)
        self.assertIn("[outline]", log)
        seen = set(events[0]["stages"]) | {e["name"] for e in events if e["type"] == "stage"}
        self.assertLessEqual({"outline", "rewrite"}, seen)
        self.assertEqual(end["stages"]["outline"], "done")
        self.assertEqual(end["stages"]["audio"], "skipped")
        self.assertNotIn("sk-never-on-disk", json.dumps(events))
        for path in self.app.out.rglob("*"):
            if path.is_file() and path.suffix in (".json", ".jsonl", ".md", ".txt", ".csv", ".tex"):
                self.assertNotIn("sk-never-on-disk", path.read_text(encoding="utf-8",
                                                                    errors="replace"), path)

        response, body = self.request("GET", "/api/runs/lecture")
        self.assertEqual(response.status, 200, body)
        files = {f["file"]: f for f in json.loads(body)["files"]}
        self.assertLessEqual({"report.md", "script.md", "ideas.csv", "recitation.tex"}, set(files))
        response, body = self.request("GET", files["script.md"]["url"])
        self.assertEqual(response.status, 200)
        whole = (self.app.out / "lecture" / "script.md").read_bytes()
        self.assertEqual(body, whole)
        _, body = self.request("GET", "/api/runs")
        self.assertEqual([r["name"] for r in json.loads(body)["runs"]], ["lecture"])

        response, body = self.request("GET", files["script.md"]["url"],
                                      headers={"Range": "bytes=5-14"})
        self.assertEqual(response.status, 206)
        self.assertEqual(body, whole[5:15])
        self.assertEqual(response.getheader("Content-Range"), f"bytes 5-14/{len(whole)}")
        response, body = self.request("GET", files["script.md"]["url"],
                                      headers={"Range": "bytes=-4"})
        self.assertEqual((response.status, body), (206, whole[-4:]))
        response, _ = self.request("GET", files["script.md"]["url"],
                                   headers={"Range": f"bytes={len(whole) + 5}-"})
        self.assertEqual(response.status, 416)


class KeyTest(AppCase):
    def test_remembered_key_goes_to_the_keychain_only(self):
        from lecture2audio import keys
        job = self.app.job(self.upload()["job"])
        options = {"llm": "deepseek", "audio": False, "api_key": "sk-remember", "remember": True}
        with self.assertRaises(webapp.Refused) as refused:  # the key file is off in tests
            self.app.arguments(job, options)
        self.assertEqual(refused.exception.status, 400)
        file = Path(self.tmp.name) / "keys.env"
        try:
            with mock.patch.dict(os.environ, {"L2A_KEYS_FILE": str(file)}):
                os.environ.pop("DEEPSEEK_API_KEY", None)
                _, env = self.app.arguments(job, options)
                self.assertEqual(env["DEEPSEEK_API_KEY"], "sk-remember")
                self.assertEqual(file.stat().st_mode & 0o777, 0o600)
                self.assertIn("DEEPSEEK_API_KEY=sk-remember", file.read_text())
                _, body = self.request("GET", "/api/config")
                self.assertNotIn(b"sk-remember", body)
                config = json.loads(body)
                deepseek = next(p for p in config["providers"] if p["id"] == "deepseek")
                self.assertEqual((deepseek["has_key"], deepseek["key_source"]), (True, "file"))
                self.assertTrue(config["can_save"])
        finally:
            keys._loaded.discard("DEEPSEEK_API_KEY")


class CancelTest(AppCase):
    # a run that never ends by itself
    command = [sys.executable, "-c",
               "import time; print('[outline] 9 units', flush=True); time.sleep(120)"]

    def test_cancel_stops_the_run(self):
        job = self.upload()["job"]
        response, _ = self.request("POST", "/api/run", {"job": job, "options": {"llm": "dry"}})
        self.assertEqual(response.status, 200)
        response, _ = self.request("POST", "/api/run", {"job": job, "options": {"llm": "dry"}})
        self.assertEqual(response.status, 409)  # one run at a time
        deadline = time.time() + 30  # wait for the run's first line
        while not self.app.jobs[job].state and time.time() < deadline:
            time.sleep(0.05)
        response, _ = self.request("POST", "/api/cancel", {"job": job}, token=False)
        self.assertEqual(response.status, 403)
        response, _ = self.request("POST", "/api/cancel", {"job": job})
        self.assertEqual(response.status, 200)
        end = self.events(job)[-1]
        self.assertEqual((end["type"], end["status"]), ("end", "cancelled"))
        self.assertEqual(end["stages"]["outline"], "stopped")
        self.assertIsNotNone(self.app.jobs[job].process.poll())


class RoomTest(AppCase):
    """The control room's endpoints: only run folders, only whole lines, never a key."""

    def make_run(self, name="demo"):
        work = Path(self.app.out) / name
        work.mkdir(parents=True)
        return work

    def stream(self, run, until, timeout=10):
        """The messages of /api/room/stream until `until(messages)` is true."""
        connection = http.client.HTTPConnection("127.0.0.1", self.app.port, timeout=timeout)
        connection.request("GET", f"/api/room/stream?run={run}")
        response = connection.getresponse()
        self.assertEqual(response.status, 200)
        messages = []
        try:
            while not until(messages):
                line = response.fp.readline()
                if not line:
                    break
                if line.startswith(b"data: "):
                    messages.append(json.loads(line[6:]))
        finally:
            connection.close()
        return messages

    def test_run_names_and_hosts_are_checked(self):
        self.make_run()
        (Path(self.app.out) / "demo" / "events.jsonl").write_text("")
        for bad in ("..", "_uploads", "demo/../..", "%2e%2e", "nope", ""):
            for path in (f"/api/room/stream?run={bad}", f"/api/room/detail?run={bad}&at=0"):
                response, _ = self.request("GET", path)
                self.assertEqual(response.status, 404, path)
        response, _ = self.request("GET", "/api/room/stream?run=demo", headers={"Host": "evil.com"})
        self.assertEqual(response.status, 403)
        response, _ = self.request("GET", "/api/room/runs", headers={"Origin": "http://evil.com"})
        self.assertEqual(response.status, 403)
        response, body = self.request("GET", "/api/room/runs")
        self.assertEqual([r["name"] for r in json.loads(body)["runs"]], ["demo"])

    def test_stream_and_detail_never_send_a_key(self):
        key = "sk-room-secret-1234567"
        work = self.make_run()
        line = json.dumps({"time": "2026-11-20T10:00:00.000+00:00", "kind": "model",
                           "phase": "start", "id": "abc", "stage": "outline", "role": "planner",
                           "request": {"model": "m", "messages": [{"role": "user", "content": f"x {key}"}]}})
        (work / "calls.jsonl").write_text(line + "\n" + '{"kind": "model", "phase": "end", "id": "ab')
        (work / "events.jsonl").write_text('{"stage": "outline", "state": "running", "time": 1.0}\n')
        (work / "ideas.csv").write_text("idea_id,node_id,kind,statement,source_units,covered_in_card,status\n"
                                        f"T1.1,1,claim,Bits {key},3,,scripted\n")
        with mock.patch.dict(os.environ, {"DEEPSEEK_API_KEY": key}):
            messages = self.stream("demo", lambda m: {"hello", "stages", "calls", "ideas"}
                                   <= {x["t"] for x in m})
            self.assertNotIn(key, json.dumps(messages))
            calls = next(m for m in messages if m["t"] == "calls")["items"]
            self.assertEqual([c["id"] for c in calls], ["abc"])  # the half-written line waits
            self.assertEqual(calls[0]["at"], 0)
            ideas = next(m for m in messages if m["t"] == "ideas")
            self.assertEqual(ideas["ideas"][0]["status"], "scripted")
            self.assertEqual([s["name"] for s in messages[0]["graph"]],
                             [s.name for s in stages.STAGES])
            response, body = self.request("GET", "/api/room/detail?run=demo&at=0,5,999999")
            self.assertEqual(response.status, 200)
            self.assertNotIn(key, body.decode())
            records = json.loads(body)["records"]
            self.assertEqual(len(records), 1)  # 5 is inside a line, 999999 past the end
            self.assertIn("[key]", records[0]["request"]["messages"][0]["content"])
        response, _ = self.request("GET", "/api/room/detail?run=demo&at=x")
        self.assertEqual(response.status, 400)

    def test_a_run_without_calls_shows_its_stages(self):
        work = self.make_run("old")
        (work / "events.jsonl").write_text('{"stage": "transcribe", "state": "running"}\n'
                                           '{"stage": "transcribe", "state": "done", "seconds": 1}\n')
        messages = self.stream("old", lambda m: any(x["t"] == "stages" for x in m)
                               and any(x["t"] == "ideas" for x in m))
        self.assertFalse(messages[0]["calls"])
        seen = next(m for m in messages if m["t"] == "stages")["items"]
        self.assertEqual([s["state"] for s in seen], ["running", "done"])
        self.assertEqual(next(m for m in messages if m["t"] == "ideas")["ideas"], [])

    def test_tail_reads_whole_lines_and_sees_a_rewrite(self):
        from lecture2audio import room
        path = self.make_run("tail") / "events.jsonl"
        path.write_text('{"a": 1}\n{"b"')
        tail = room.Tail(path)
        self.assertEqual([o for o, _ in tail.lines()], [0])
        self.assertEqual(tail.lines(), [])
        with open(path, "a") as out:
            out.write(': 2}\n')
        self.assertEqual([raw for _, raw in tail.lines()], [b'{"b": 2}'])
        path.write_text('{"c": 3}\n')
        self.assertEqual([raw for _, raw in tail.lines()], [b'{"c": 3}'])
        self.assertTrue(tail.reset)
        big = '{"d": "' + "x" * (room.CHUNK + 10) + '"}\n'
        with open(path, "a") as out:
            out.write(big + '{"e": 1}\n')
        got = tail.lines()
        self.assertEqual(len(got[0][1]), len(big) - 1)
        self.assertEqual(tail.lines()[0][1], b'{"e": 1}')


if __name__ == "__main__":
    unittest.main()
