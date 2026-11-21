"""The MCP server: its tool functions called directly (no network, no model), one dry run
through `build`, and the protocol itself in process when the `mcp` package is installed."""
import asyncio
import importlib.util
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path

from lecture2audio import mcp_server as srv

SAMPLE = Path(__file__).resolve().parent.parent / "examples" / "sample_transcript.txt"
HAS_MCP = importlib.util.find_spec("mcp") is not None


class McpCase(unittest.TestCase):
    def setUp(self):
        self.saved = {k: os.environ.get(k) for k in ("L2A_VAULT_DIR", "L2A_PDF", "L2A_KEYS_FILE",
                                                     "DEEPSEEK_API_KEY")}
        os.environ.pop("L2A_VAULT_DIR", None)  # never write into a real vault
        os.environ["L2A_PDF"] = "0"
        os.environ["L2A_KEYS_FILE"] = "off"
        self.tmp = tempfile.TemporaryDirectory()
        self.out = Path(self.tmp.name) / "out"
        self.run = self.out / "lecture-one"
        self.run.mkdir(parents=True)
        (self.run / "report.md").write_text("# Report\n\nAll ideas arrived.\n", encoding="utf-8")
        (self.run / "ideas.csv").write_text("idea_id,status\nT1.1,verified\n", encoding="utf-8")
        (self.run / "events.jsonl").write_text(
            '{"stage": "outline", "state": "running"}\n'
            '{"stage": "outline", "state": "done", "seconds": 3.5}\n'
            '{"stage": "ideas", "state": "running"}\n'
            '{"stage": "ideas", "state": "done", "seconds": 7.0}\n', encoding="utf-8")
        (self.run / "calls.jsonl").write_text(
            json.dumps({"kind": "llm", "seconds": 2.0,
                        "answer": {"usage": {"prompt_tokens": 100, "completion_tokens": 40}}})
            + "\n" + json.dumps({"kind": "llm", "seconds": 1.0, "error": "RateLimit"}) + "\n"
            + json.dumps({"kind": "search", "seconds": 0.5}) + "\nnot json\n", encoding="utf-8")
        (Path(self.tmp.name) / "secret.md").write_text("outside", encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()
        for key, value in self.saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


class ToolFunctions(McpCase):
    def test_list_runs(self):
        (self.out / "_uploads").mkdir()
        (self.out / "empty").mkdir()  # no run marks: not a run
        runs = srv.list_runs(self.out)
        self.assertEqual([r["run"] for r in runs], ["lecture-one"])
        self.assertEqual(runs[0]["state"], "finished")
        self.assertEqual(runs[0]["results"], ["report.md", "ideas.csv"])
        self.assertEqual(srv.list_runs(self.out / "missing"), [])

    def test_status_reads_events_and_calls(self):
        info = srv.status(self.out, "lecture-one")
        self.assertEqual(info["state"], "finished")
        self.assertEqual([(s["stage"], s["state"], s["seconds"]) for s in info["stages"]],
                         [("outline", "done", 3.5), ("ideas", "done", 7.0)])
        calls = info["calls"]
        self.assertEqual((calls["requests"], calls["errors"]), (3, 1))
        self.assertEqual(calls["by_kind"], {"llm": 2, "search": 1})
        self.assertEqual((calls["input_tokens"], calls["output_tokens"]), (100, 40))
        self.assertEqual(calls["seconds"], 3.5)

    def test_open_stage_without_process_is_stopped(self):
        with (self.run / "events.jsonl").open("a", encoding="utf-8") as events:
            events.write('{"stage": "rewrite", "state": "running"}\n')
        info = srv.status(self.out, "lecture-one")
        self.assertEqual((info["state"], info["current_stage"]), ("stopped", "rewrite"))

    def test_unknown_run(self):
        with self.assertRaisesRegex(srv.ToolError, "unknown run"):
            srv.status(self.out, "nope")
        with self.assertRaisesRegex(srv.ToolError, "unknown run"):
            srv.read_result(self.out, "nope", "report.md")

    def test_read_result(self):
        self.assertIn("All ideas arrived", srv.read_result(self.out, "lecture-one", "report.md"))
        with self.assertRaisesRegex(srv.ToolError, "no script.md yet"):
            srv.read_result(self.out, "lecture-one", "script.md")

    def test_path_traversal_refused(self):
        for run in ("..", "../out", "lecture-one/..", "/etc", "", ".hidden", "a/b"):
            with self.assertRaises(srv.ToolError, msg=run):
                srv.read_result(self.out, run, "report.md")
        for name in ("../secret.md", "../../secret.md", "/etc/passwd", "calls.jsonl",
                     "report.md/..", "transcript.txt"):
            with self.assertRaisesRegex(srv.ToolError, "cannot be read", msg=name):
                srv.read_result(self.out, "lecture-one", name)

    def test_symlink_out_of_folder_refused(self):
        (self.run / "glossary.md").symlink_to(Path(self.tmp.name) / "secret.md")
        with self.assertRaisesRegex(srv.ToolError, "outside"):
            srv.read_result(self.out, "lecture-one", "glossary.md")
        (self.out / "linked").symlink_to(Path(self.tmp.name))
        with self.assertRaises(srv.ToolError):
            srv.status(self.out, "linked")

    def test_keys_never_returned(self):
        os.environ["DEEPSEEK_API_KEY"] = "sk-test-0123456789abcdef"
        (self.run / "report.md").write_text("leaked sk-test-0123456789abcdef here",
                                            encoding="utf-8")
        text = srv.read_result(self.out, "lecture-one", "report.md")
        self.assertNotIn("sk-test", text)
        self.assertIn("[key]", text)

    def test_build_command_checks_options(self):
        cmd = srv.build_command(self.out, SAMPLE, "x", llm="dry", audio=False, research=False)
        self.assertEqual(cmd[3], "text")
        self.assertIn("--no-open-obsidian", cmd)
        self.assertEqual(cmd[cmd.index("--research") + 1], "off")
        for bad in ({"llm": "evil; rm -rf"}, {"language": "de"}, {"tts": "x"},
                    {"redo": "everything"}):
            with self.assertRaises(srv.ToolError, msg=bad):
                srv.build_command(self.out, SAMPLE, "x", **bad)

    def test_build_refuses_missing_input(self):
        with self.assertRaisesRegex(srv.ToolError, "input not found"):
            srv.start_build(self.out, str(Path(self.tmp.name) / "none.txt"), llm="dry")

    def test_run_name(self):
        self.assertEqual(srv.run_name(SAMPLE, None), "sample-transcript")
        self.assertEqual(srv.run_name(SAMPLE.parent, None), "examples")
        self.assertEqual(srv.run_name(SAMPLE, "../../Evil Name"), "evil-name")


@unittest.skipUnless(shutil.which("ffmpeg"), "needs ffmpeg")
class DryBuild(McpCase):
    def test_build_then_status_then_read(self):
        handle = srv.start_build(self.out, str(SAMPLE), llm="dry", audio=False, name="dry-run")
        self.assertEqual(handle["run"], "dry-run")
        self.assertNotIn("KEY", json.dumps(handle))
        with self.assertRaisesRegex(srv.ToolError, "already going"):
            srv.start_build(self.out, str(SAMPLE), llm="dry", audio=False, name="dry-run")
        result = asyncio.run(srv.wait_for(self.out, "dry-run", poll=0.2))
        self.assertEqual(result["state"], "finished", result.get("log_tail"))
        self.assertEqual(result["exit_code"], 0)
        self.assertIn("report.md", result["paths"])
        self.assertIn("rewrite", [s["stage"] for s in result["stages"]])
        self.assertIn("lecture", srv.read_result(self.out, "dry-run", "script.md").lower())


@unittest.skipUnless(HAS_MCP, "the mcp package is not installed (extra [mcp])")
class Protocol(McpCase):
    def call(self, coro_fn):
        from mcp import Client

        async def go():
            async with Client(srv.make_server(self.out)) as client:
                return await coro_fn(client)
        return asyncio.run(go())

    def test_tools_listed(self):
        tools = self.call(lambda c: c.list_tools())
        names = {t.name for t in tools.tools}
        self.assertTrue({"build", "status", "list_runs", "read_result", "stop_run"} <= names)
        build = next(t for t in tools.tools if t.name == "build")
        self.assertNotIn("key", json.dumps(build.input_schema).lower())

    def test_read_and_refuse_over_protocol(self):
        result = self.call(lambda c: c.call_tool(
            "read_result", {"run": "lecture-one", "file": "report.md"}))
        self.assertFalse(result.is_error)
        self.assertIn("All ideas arrived", result.content[0].text)
        bad = self.call(lambda c: c.call_tool(
            "read_result", {"run": "lecture-one", "file": "../secret.md"}))
        self.assertTrue(bad.is_error)
        text = "".join(getattr(c, "text", "") for c in bad.content)
        self.assertIn("cannot be read", text)  # the refusal reaches the agent, not the file
        self.assertNotIn("outside", text)
        unknown = self.call(lambda c: c.call_tool("status", {"run": "nope"}))
        self.assertTrue(unknown.is_error)
        self.assertIn("unknown run", unknown.content[0].text)

    def test_resources(self):
        templates = self.call(lambda c: c.list_resource_templates())
        self.assertIn("lecture2audio://runs/{run}/{file}",
                      [t.uri_template for t in templates.resource_templates])
        read = self.call(lambda c: c.read_resource("lecture2audio://runs/lecture-one/ideas.csv"))
        self.assertIn("T1.1", read.contents[0].text)
        with self.assertRaises(Exception):
            self.call(lambda c: c.read_resource("lecture2audio://runs/lecture-one/calls.jsonl"))
        runs = self.call(lambda c: c.read_resource("lecture2audio://runs"))
        self.assertEqual(json.loads(runs.contents[0].text)[0]["run"], "lecture-one")


SKILL = Path(__file__).resolve().parent.parent / "skills" / "lecture2audio"


class Skill(unittest.TestCase):
    """The Agent Skill follows the agentskills.io specification and matches the code."""

    def frontmatter(self) -> dict:
        text = (SKILL / "SKILL.md").read_text(encoding="utf-8")
        self.assertTrue(text.startswith("---\n"))
        head = text.split("---\n")[1]
        fields, current = {}, None
        for line in head.splitlines():
            if line and not line.startswith(" "):
                current, _, value = line.partition(":")
                fields[current] = value.strip()
        return fields

    def test_spec_fields_only(self):
        fields = self.frontmatter()
        allowed = {"name", "description", "license", "compatibility", "metadata", "allowed-tools"}
        self.assertLessEqual(set(fields), allowed)
        self.assertEqual(fields["name"], SKILL.name)
        self.assertRegex(fields["name"], r"^[a-z0-9]+(-[a-z0-9]+)*$")
        self.assertLessEqual(len(fields["description"]), 1024)
        self.assertLessEqual(len(fields["compatibility"]), 500)

    def test_references_exist_and_name_every_stage(self):
        body = (SKILL / "SKILL.md").read_text(encoding="utf-8")
        for link in ("references/outputs.md", "references/stages.md", "references/options.md",
                     "references/mcp.md"):
            self.assertIn(link, body)
            self.assertTrue((SKILL / link).is_file(), link)
        table = (SKILL / "references" / "stages.md").read_text(encoding="utf-8")
        for stage in srv.stages.STAGES:
            self.assertIn(f"| {stage.name} |", table)
        outputs = (SKILL / "references" / "outputs.md").read_text(encoding="utf-8")
        for name in srv.READABLE:
            self.assertIn(f"`{name}` | yes", outputs)
        for status in srv_ledger_statuses():
            self.assertIn(f"`{status}`", outputs)

    def test_script_status(self):
        import subprocess
        import sys
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp) / "run"
            work.mkdir()
            (work / "events.jsonl").write_text('{"stage": "outline", "state": "done", '
                                               '"seconds": 1.0}\n', encoding="utf-8")
            (work / "ideas.csv").write_text("idea_id,status\nT1.1,missing_cards\n",
                                            encoding="utf-8")
            done = subprocess.run([sys.executable, str(SKILL / "scripts" / "run.py"), "--status",
                                   str(work)], capture_output=True, text=True, check=True)
        self.assertIn("outline (done 1.0 s)", done.stdout)
        self.assertIn("missing_cards 1", done.stdout)


def srv_ledger_statuses():
    from lecture2audio import ledger
    return ledger.STATUSES


if __name__ == "__main__":
    unittest.main()
