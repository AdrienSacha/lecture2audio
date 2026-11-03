import http.client
import json
import pathlib
import shutil
import sys
import tempfile
import threading
import time
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from lecture2audio.gui import build_argv, make_server, option_spec, render_markdown  # noqa: E402

SAMPLE = ROOT / "examples" / "sample_transcript.txt"


class MarkdownTest(unittest.TestCase):
    def test_escapes_everything(self):
        out = render_markdown("# T <script>\n\nText <b>x</b> & **bold** `a<b`\n")
        self.assertNotIn("<script>", out)
        self.assertNotIn("<b>", out)
        self.assertIn("&lt;script&gt;", out)
        self.assertIn("<strong>bold</strong>", out)
        self.assertIn("<code>a&lt;b</code>", out)

    def test_blocks(self):
        out = render_markdown("- a\n  - b\n- c\n\n| x | y |\n|---|---|\n| 1 | 2 |\n\n> q _i_\n")
        self.assertEqual(out.count("<ul>"), 2)
        self.assertEqual(out.count("</ul>"), 2)
        self.assertIn("<td>1</td>", out)
        self.assertIn("<blockquote><p>q <em>i</em></p></blockquote>", out)


class ArgvTest(unittest.TestCase):
    def setUp(self):
        self.spec = option_spec()

    def test_options_follow_the_parser(self):
        dests = {f["dest"] for g in self.spec["build"]["groups"] for f in g["fields"]}
        self.assertTrue({"input", "llm", "tts", "format", "redo", "no_audit"} <= dests)
        text = {f["dest"] for g in self.spec["text"]["groups"] for f in g["fields"]}
        self.assertNotIn("tts", text)

    def test_build_argv(self):
        argv = build_argv(self.spec, "build", {"input": str(SAMPLE), "llm": "dry", "no_audit": True,
                                               "workers": "2", "voice": ""},
                          pathlib.Path("/tmp/o"), ROOT)
        self.assertEqual(argv[:2], ["build", str(SAMPLE)])
        self.assertIn("--llm=dry", argv)
        self.assertIn("--no-audit", argv)
        self.assertIn("--workers=2", argv)
        self.assertIn("--out=" + str(pathlib.Path("/tmp/o").resolve()), argv)
        self.assertFalse(any(a.startswith("--voice") for a in argv))
        for bad in ({"llm": "nope"}, {"workers": "x"}, {"unknown": "1"}, {"tts": "fake"}):
            with self.assertRaises(ValueError):
                build_argv(self.spec, "text", {"input": str(SAMPLE), **bad},
                           pathlib.Path("/tmp/o"), ROOT)


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "needs ffmpeg")
class ServerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        cls.server = make_server(0, pathlib.Path(cls.tmp) / "out")
        cls.port = cls.server.port
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.stop_job()
        cls.server.shutdown()
        cls.server.server_close()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def request(self, method, path, body=None, headers=None, host=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        h = {"Host": host or f"127.0.0.1:{self.port}"}
        h.update(headers or {})
        if isinstance(body, dict):
            body = json.dumps(body).encode()
            h["Content-Type"] = "application/json"
        conn.request(method, path, body=body, headers=h)
        r = conn.getresponse()
        data = r.read()
        conn.close()
        return r.status, dict(r.getheaders()), data

    def post(self, path, body, token=True):
        headers = {"X-L2A-Token": self.server.token} if token else {}
        return self.request("POST", path, body, headers)

    def get_json(self, path):
        status, _, data = self.request("GET", path)
        self.assertEqual(status, 200, data)
        return json.loads(data)

    def test_index_page(self):
        status, headers, data = self.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn(b"lecture2audio", data)
        self.assertIn(self.server.token.encode(), data)
        self.assertIn("default-src 'none'", headers["Content-Security-Policy"])
        self.assertNotIn(b"http://", data)  # nothing fetched from elsewhere
        self.assertNotIn(b"https://", data)

    def test_bad_host_refused(self):
        for host in ("evil.example:%d" % self.port, "127.0.0.1:1", "localhost.evil.com"):
            status, _, _ = self.request("GET", "/api/options", host=host)
            self.assertEqual(status, 403, host)

    def test_post_needs_token(self):
        status, _, _ = self.post("/api/cancel", {}, token=False)
        self.assertEqual(status, 403)
        status, _, _ = self.request("POST", "/api/cancel", {}, {"X-L2A-Token": "wrong"})
        self.assertEqual(status, 403)

    def test_path_traversal_refused(self):
        secret = pathlib.Path(self.tmp) / "secret.txt"
        secret.write_text("no")
        for path in ("/files/../secret.txt", "/files/%2e%2e/secret.txt", "/files/..%2fsecret.txt",
                     "/files//etc/passwd", "/files/x?out=" + self.tmp,
                     "/api/doc?run=..&doc=report", "/api/doc?run=x&doc=../../secret",
                     "/api/runs?out=/"):
            status, _, data = self.request("GET", path)
            self.assertIn(status, (403, 404), path)
            self.assertNotEqual(data, b"no", path)

    def test_full_dry_job(self):
        upload = SAMPLE.read_bytes()
        status, _, data = self.request("POST", "/api/upload?name=sample_transcript.txt", upload,
                                       {"X-L2A-Token": self.server.token,
                                        "Content-Type": "application/octet-stream"})
        self.assertEqual(status, 200, data)
        path = json.loads(data)["path"]
        self.assertTrue(path.startswith(str(self.server.out_root)))
        values = {"input": path, "llm": "dry", "tts": "fake", "language": "en",
                  "max_part_words": "60"}
        status, _, data = self.post("/api/run", {"command": "build", "values": values,
                                                 "keys": {"ANTHROPIC_API_KEY": "sk-secret-123"}})
        self.assertEqual(status, 200, data)
        name = json.loads(data)["name"]
        self.assertEqual(name, "sample-transcript")
        status, _, _ = self.post("/api/run", {"command": "build", "values": values})
        self.assertEqual(status, 409)
        deadline = time.time() + 240
        lines = []
        while True:
            job = self.get_json(f"/api/job?since={len(lines)}")
            lines += job["lines"]
            if job["state"] != "running" or time.time() > deadline:
                break
            time.sleep(0.3)
        self.assertEqual(job["state"], "done", "\n".join(lines))
        self.assertFalse(any("sk-secret-123" in line for line in lines))
        for file in self.server.out_root.rglob("*"):
            if file.is_file():
                self.assertNotIn(b"sk-secret-123", file.read_bytes(), file)

        runs = self.get_json("/api/runs")["runs"]
        self.assertEqual([r["name"] for r in runs], [name])
        report = self.get_json(f"/api/doc?run={name}&doc=report")
        self.assertTrue(report["exists"])
        self.assertIn("<h1>", report["html"])
        self.assertIn("<table>", report["html"])
        chapters = self.get_json(f"/api/chapters?run={name}")
        self.assertEqual(chapters["audio"], f"{name}.m4a")
        self.assertGreaterEqual(len(chapters["chapters"]), 2)
        self.assertEqual(chapters["chapters"][0]["start"], 0.0)

        status, headers, data = self.request("GET", f"/files/{name}/{name}.m4a")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "audio/mp4")
        size = len(data)
        status, headers, data = self.request("GET", f"/files/{name}/{name}.m4a",
                                             headers={"Range": "bytes=10-19"})
        self.assertEqual(status, 206)
        self.assertEqual(len(data), 10)
        self.assertEqual(headers["Content-Range"], f"bytes 10-19/{size}")


if __name__ == "__main__":
    unittest.main()
