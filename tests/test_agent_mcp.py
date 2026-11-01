# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) Hamza Gouda
"""MCP server conformance (JSON-RPC 2.0 over stdio) and the real process over pipes."""
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tuf_control import cli
from tuf_control.agent.approvals import ApprovalQueue
from tuf_control.agent.audit import AuditLog
from tuf_control.agent.mcp import LATEST, McpServer, serve
from tuf_control.agent.store import STATE_DIR_ENV
from tuf_control.agent.tools import TOOLS, AgentService
from tuf_control.automation.config import CONFIG_DIR_ENV, RUNTIME_DIR_ENV
from tuf_control.backends.fake import build_fake_tree
from tuf_control.controller import FAKE_ROOT_ENV

SRC = Path(__file__).resolve().parents[1] / "src"


def req(mid, method, params=None):
    m = {"jsonrpc": "2.0", "id": mid, "method": method}
    if params is not None:
        m["params"] = params
    return m


INIT = req(1, "initialize", {"protocolVersion": LATEST, "capabilities": {},
                             "clientInfo": {"name": "conformance", "version": "0.1"}})


class Env(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.root = build_fake_tree(self.tmp / "root")
        self.env = {FAKE_ROOT_ENV: str(self.root), CONFIG_DIR_ENV: str(self.tmp / "cfg"),
                    STATE_DIR_ENV: str(self.tmp / "state"),
                    RUNTIME_DIR_ENV: str(self.tmp / "run"), "TUF_CONTROL_AGENT_NOTIFY": "0"}
        for k, v in self.env.items():
            old = os.environ.get(k)
            self.addCleanup(lambda k=k, old=old: os.environ.pop(k, None) if old is None
                            else os.environ.__setitem__(k, old))
            os.environ[k] = v
        self.svc = AgentService(queue=ApprovalQueue(), audit=AuditLog())
        self.srv = McpServer(self.svc)

    def init(self):
        r = self.srv.handle(INIT)
        self.assertIsNone(self.srv.handle({"jsonrpc": "2.0",
                                           "method": "notifications/initialized"}))
        return r


class Protocol(Env):
    def test_initialize_handshake(self):
        r = self.init()
        res = r["result"]
        self.assertEqual((r["jsonrpc"], r["id"]), ("2.0", 1))
        self.assertEqual(res["protocolVersion"], LATEST)
        self.assertEqual(res["serverInfo"]["name"], "tuf-control")
        self.assertEqual(set(res["capabilities"]), {"tools", "resources", "prompts"})
        self.assertIn("dry_run", res["instructions"])
        self.assertEqual(self.svc.client, "conformance/0.1")
        again = self.srv.handle(INIT)
        self.assertEqual(again["error"]["code"], -32600)

    def test_version_negotiation(self):
        for want, got in (("2025-03-26", "2025-03-26"), ("2024-11-05", "2024-11-05"),
                          ("1999-01-01", LATEST)):
            srv = McpServer(AgentService())
            r = srv.handle(req(1, "initialize", {"protocolVersion": want, "capabilities": {},
                                                 "clientInfo": {"name": "x"}}))
            self.assertEqual(r["result"]["protocolVersion"], got)

    def test_requests_before_initialize(self):
        self.assertEqual(self.srv.handle(req(1, "ping")), {"jsonrpc": "2.0", "id": 1,
                                                           "result": {}})
        r = self.srv.handle(req(2, "tools/list"))
        self.assertEqual(r["error"]["code"], -32600)

    def test_malformed_and_invalid(self):
        self.init()
        self.assertEqual(self.srv.handle_line(b"{not json")["error"]["code"], -32700)
        self.assertIsNone(self.srv.handle_line(b"{not json")["id"])
        self.assertEqual(self.srv.handle_line(b"\xff\xfe")["error"]["code"], -32700)
        for bad in ({"id": 3, "method": "ping"}, {"jsonrpc": "1.0", "id": 3, "method": "ping"},
                    "ping", 7, {"jsonrpc": "2.0", "id": 3},
                    {"jsonrpc": "2.0", "id": True, "method": "ping"},
                    {"jsonrpc": "2.0", "id": None, "method": "ping"},
                    {"jsonrpc": "2.0", "id": {"x": 1}, "method": "ping"}):
            r = self.srv.handle(bad)
            self.assertEqual(r["error"]["code"], -32600, bad)
        r = self.srv.handle(req(4, "tools/call", ["get_status"]))
        self.assertEqual(r["error"]["code"], -32602)

    def test_batches_not_supported(self):
        self.init()
        r = self.srv.handle_line(json.dumps([req(5, "ping"), req(6, "ping")]))
        self.assertEqual(r["error"]["code"], -32600)
        self.assertIn("batch", r["error"]["message"])

    def test_unknown_method_and_notifications(self):
        self.init()
        self.assertEqual(self.srv.handle(req(7, "sampling/createMessage"))["error"]["code"],
                         -32601)
        self.assertEqual(self.srv.handle(req(8, "logging/setLevel", {"level": "debug"}))
                         ["error"]["code"], -32601)
        for note in ("notifications/cancelled", "notifications/whatever"):
            self.assertIsNone(self.srv.handle({"jsonrpc": "2.0", "method": note,
                                               "params": {"requestId": 1}}))
        self.assertIsNone(self.srv.handle({"jsonrpc": "2.0", "id": 99, "result": {}}))

    def test_tools_list_and_call(self):
        self.init()
        tools = self.srv.handle(req(2, "tools/list"))["result"]["tools"]
        self.assertEqual({t["name"] for t in tools}, set(TOOLS))
        for t in tools:
            self.assertEqual(t["inputSchema"]["type"], "object")
            self.assertTrue(t["description"])
            self.assertEqual(set(t["annotations"]), {"title", "readOnlyHint", "destructiveHint",
                                                     "idempotentHint", "openWorldHint"})
        r = self.srv.handle(req(3, "tools/call", {"name": "get_status", "arguments": {}}))
        res = r["result"]
        self.assertFalse(res["isError"])
        self.assertEqual(res["structuredContent"]["profile"], "balanced")
        self.assertEqual(json.loads(res["content"][0]["text"]), res["structuredContent"])
        r = self.srv.handle(req(4, "tools/call", {"name": "set_charge_limit",
                                                  "arguments": {"percent": 80}}))
        self.assertTrue(r["result"]["isError"])
        self.assertEqual(r["result"]["structuredContent"]["error"]["code"], "access_denied")
        r = self.srv.handle(req(5, "tools/call", {"name": "get_status", "arguments": {"x": 1}}))
        self.assertTrue(r["result"]["isError"])
        self.assertEqual(r["result"]["structuredContent"]["error"]["code"], "invalid_arguments")
        r = self.srv.handle(req(6, "tools/call", {"name": "exec", "arguments": {}}))
        self.assertEqual(r["error"]["code"], -32602)
        r = self.srv.handle(req(7, "tools/call", {"arguments": {}}))
        self.assertEqual(r["error"]["code"], -32602)

    def test_resources(self):
        self.init()
        res = self.srv.handle(req(2, "resources/list"))["result"]["resources"]
        self.assertEqual({r["uri"] for r in res}, {"tuf://status", "tuf://capabilities",
                                                   "tuf://docs/safety",
                                                   "tuf://telemetry/summary"})
        for uri in ("tuf://status", "tuf://capabilities", "tuf://telemetry/summary"):
            r = self.srv.handle(req(3, "resources/read", {"uri": uri}))
            c = r["result"]["contents"][0]
            self.assertEqual((c["uri"], c["mimeType"]), (uri, "application/json"))
            json.loads(c["text"])
        doc = self.srv.handle(req(4, "resources/read", {"uri": "tuf://docs/safety"}))
        self.assertIn("read-only", doc["result"]["contents"][0]["text"])
        r = self.srv.handle(req(5, "resources/read", {"uri": "file:///etc/passwd"}))
        self.assertEqual(r["error"]["code"], -32002)
        self.assertEqual(self.srv.handle(req(6, "resources/templates/list"))["result"],
                         {"resourceTemplates": []})

    def test_prompts(self):
        self.init()
        ps = self.srv.handle(req(2, "prompts/list"))["result"]["prompts"]
        self.assertEqual({p["name"] for p in ps}, {"diagnose-thermals", "optimise-battery"})
        r = self.srv.handle(req(3, "prompts/get", {"name": "diagnose-thermals",
                                                   "arguments": {"focus": "fans\x1b[2J"}}))
        msg = r["result"]["messages"][0]
        self.assertEqual(msg["role"], "user")
        self.assertIn("get_telemetry", msg["content"]["text"])
        self.assertNotIn("\x1b", msg["content"]["text"])
        self.assertEqual(self.srv.handle(req(4, "prompts/get", {"name": "nope"}))
                         ["error"]["code"], -32602)

    def test_serve_over_buffers(self):
        lines = [json.dumps(INIT), json.dumps({"jsonrpc": "2.0",
                                               "method": "notifications/initialized"}),
                 "", "x" * 10, json.dumps(req(2, "ping"))]
        inp = io.BytesIO(("\n".join(lines) + "\n").encode())
        out = io.BytesIO()
        self.assertEqual(serve(self.svc, stdin=inp, stdout=out), 0)
        frames = [json.loads(x) for x in out.getvalue().decode().splitlines()]
        self.assertEqual([f.get("id") for f in frames], [1, None, 2])
        self.assertEqual(frames[1]["error"]["code"], -32700)

    def test_oversized_line(self):
        big = b'{"jsonrpc":"2.0","id":1,"method":"ping","params":{"x":"' + b"a" * (1 << 20) \
            + b'"}}\n' + json.dumps(req(2, "ping")).encode() + b"\n"
        out = io.BytesIO()
        serve(self.svc, stdin=io.BytesIO(big), stdout=out)
        frames = [json.loads(x) for x in out.getvalue().decode().splitlines()]
        self.assertEqual(frames[0]["error"]["code"], -32700)
        self.assertEqual(frames[1]["id"], 2)


class JsonCli(Env):
    def run_cli(self, *argv):
        import contextlib
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            code = cli.main(list(argv))
        return code, out.getvalue()

    def test_json_after_subcommand_and_stable_keys(self):
        code, out = self.run_cli("status", "--json")
        self.assertEqual(code, 0)
        d = json.loads(out)
        self.assertEqual(set(d), {"profile", "battery_threshold", "battery_health", "gpu_mode",
                                  "kbd_brightness", "power_limits", "fan_curves",
                                  "cross_check"})
        self.assertEqual(set(d["profile"]), {"value", "backend"})
        _, out = self.run_cli("profile", "get", "--json")
        self.assertEqual(set(json.loads(out)), {"profile", "backend"})
        _, out = self.run_cli("limits", "--json")
        lim = json.loads(out)
        self.assertEqual(set(lim), {"backend", "limits"})
        self.assertTrue({"name", "current", "min", "max", "default", "bounded"}
                        <= set(lim["limits"][0]))
        _, out = self.run_cli("telemetry", "--json")
        self.assertTrue({"backend", "timestamp", "cpu_temp_c", "fan_rpm"} <= set(json.loads(out)))
        _, out = self.run_cli("capabilities", "--json")
        self.assertEqual(set(json.loads(out)["profile"]),
                         {"available", "backend", "fallbacks", "unavailable", "notes"})
        _, out = self.run_cli("automation", "list", "--json")
        self.assertIn("rules", json.loads(out))
        _, out = self.run_cli("--json", "daemon", "status")
        self.assertIn("running", json.loads(out))


class Subprocess(Env):
    def test_real_process_over_pipes(self):
        env = {**os.environ, **self.env, "PYTHONPATH": str(SRC)}
        msgs = [INIT, {"jsonrpc": "2.0", "method": "notifications/initialized"},
                req(2, "tools/list"),
                req(3, "tools/call", {"name": "get_telemetry",
                                      "arguments": {"sections": ["cpu", "fans"]}}),
                req(4, "tools/call", {"name": "set_platform_profile",
                                      "arguments": {"profile": "quiet", "dry_run": False}}),
                req(5, "resources/read", {"uri": "tuf://status"}),
                req(6, "prompts/get", {"name": "optimise-battery"}),
                req(7, "nope/nope")]
        data = ("\n".join(json.dumps(m) for m in msgs) + "\nnot json\n").encode()
        p = subprocess.run([sys.executable, "-m", "tuf_control", "mcp"], input=data,
                           capture_output=True, env=env, timeout=60)
        self.assertEqual(p.returncode, 0, p.stderr.decode())
        frames = [json.loads(line) for line in p.stdout.decode().splitlines()]
        self.assertTrue(all(f.get("jsonrpc") == "2.0" for f in frames))
        by_id = {f["id"]: f for f in frames}
        self.assertEqual([f["id"] for f in frames], [1, 2, 3, 4, 5, 6, 7, None])
        self.assertEqual(by_id[1]["result"]["serverInfo"]["name"], "tuf-control")
        self.assertEqual(len(by_id[2]["result"]["tools"]), len(TOOLS))
        tel = by_id[3]["result"]["structuredContent"]
        self.assertIn("usage_pct", tel["cpu"])
        self.assertTrue(by_id[4]["result"]["isError"])  # read-only by default
        self.assertEqual((self.root / "sys/firmware/acpi/platform_profile").read_text().strip(),
                         "balanced")
        self.assertIn("contents", by_id[5]["result"])
        self.assertIn("messages", by_id[6]["result"])
        self.assertEqual(by_id[7]["error"]["code"], -32601)
        self.assertEqual(frames[-1]["error"]["code"], -32700)
        log = [json.loads(x) for x in (self.tmp / "state/agent-audit.log").read_text()
               .splitlines()]
        self.assertTrue(all(e["client"] == "conformance/0.1" for e in log))
        self.assertIn("denied", [e["decision"] for e in log])

    def test_entry_point_print_config(self):
        env = {**os.environ, **self.env, "PYTHONPATH": str(SRC)}
        p = subprocess.run([sys.executable, "-c", "import sys; from tuf_control.agent.mcp "
                            "import main; sys.exit(main(['--print-config']))"],
                           capture_output=True, env=env, timeout=30)
        self.assertEqual(p.returncode, 0)
        cfg = json.loads(p.stdout)["mcpServers"]["tuf-control"]
        self.assertTrue(cfg["command"])
        self.assertIn("mcp", cfg["args"])


if __name__ == "__main__":
    unittest.main()
