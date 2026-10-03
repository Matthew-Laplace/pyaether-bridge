#!/usr/bin/env python3
"""Protocol probe for the pyaether-bridge MCP server (standard library only).

Usage:

    /usr/bin/python3 tests/mcp_probe.py

The probe builds a mini catalog in a temp directory using the schema from
ARCHITECTURE.md, isolates the PYAETHER_* environment, then starts
pyaether_bridge/mcp_server.py as a subprocess and checks JSON-RPC/MCP
behaviour plus every tool. When catalog.py / runtime.py are missing or a
fully offline run is wanted, a minimal stub is injected via sitecustomize
(the server itself contains no stub code).

All PASS -> exit 0; any FAIL -> exit 1 and the actual response is printed.
"""

from __future__ import annotations

import json
import os
import re
import select
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PKG_DIR = ROOT / "pyaether_bridge"
SERVER = PKG_DIR / "mcp_server.py"
PYTHON = sys.executable or "/usr/bin/python3"
TIMEOUT = 25.0

STUB_SITECUSTOMIZE = '''\
"""probe stub: a minimal pyaether_bridge.runtime / .catalog for missing modules."""
import json
import os
import pathlib
import sqlite3
import sys
import types

_PKG = pathlib.Path(os.environ.get("PYAETHER_PROBE_PKG_DIR", ""))
_TAG = "[mcp-probe]"


def _stub_runtime():
    mod = types.ModuleType("pyaether_bridge.runtime")
    mod.__doc__ = "probe stub runtime"

    def status(*, autostart=False):
        return {
            "daemon": {
                "running": False,
                "pid": None,
                "socket": os.environ.get("PYAETHER_DAEMON_SOCK"),
            },
            "transport": {},
            "note": "probe stub runtime",
        }

    def ensure_daemon(*, wait=90.0):
        raise RuntimeError("probe stub runtime: daemon unavailable")

    def stop_daemon():
        return {"ok": True, "note": "probe stub runtime"}

    def request(method, params=None, *, timeout=30.0, autostart=True):
        raise RuntimeError("probe stub runtime: daemon unavailable")

    def exec_code(code, *, timeout=120.0, autostart=True):
        raise RuntimeError("probe stub runtime: daemon unavailable (runtime.py not provided)")

    mod.status = status
    mod.ensure_daemon = ensure_daemon
    mod.stop_daemon = stop_daemon
    mod.request = request
    mod.exec_code = exec_code
    return mod


def _stub_catalog():
    mod = types.ModuleType("pyaether_bridge.catalog")
    mod.__doc__ = "probe stub catalog"

    def _connect():
        db = os.environ.get("PYAETHER_CATALOG_DB")
        if not db or not os.path.exists(db):
            raise RuntimeError("catalog database missing: %s (run pyaether api build first)" % db)
        return sqlite3.connect(db)

    def search(query, *, limit=20, kind=None, db_path=None):
        con = _connect()
        try:
            sql = (
                "SELECT name, kind, module, signature, summary, domain FROM entries "
                "WHERE (name LIKE ? OR summary LIKE ? OR signature LIKE ?)"
            )
            args = ["%" + query + "%"] * 3
            if kind:
                sql += " AND kind = ?"
                args.append(kind)
            sql += " ORDER BY name LIMIT ?"
            args.append(int(limit))
            rows = con.execute(sql, args).fetchall()
        finally:
            con.close()
        keys = ("name", "kind", "module", "signature", "summary", "domain")
        return [dict(zip(keys, row)) for row in rows]

    def show(name, *, max_chars=4000, db_path=None):
        con = _connect()
        try:
            row = con.execute(
                "SELECT name, kind, module, signature, summary, description, params, "
                "returns, page, anchor, domain FROM entries WHERE name = ?",
                (name,),
            ).fetchone()
        finally:
            con.close()
        if not row:
            return None
        keys = (
            "name", "kind", "module", "signature", "summary", "description",
            "params", "returns", "page", "anchor", "domain",
        )
        entry = dict(zip(keys, row))
        entry["params"] = json.loads(entry.get("params") or "[]")
        entry["returns"] = json.loads(entry.get("returns") or "[]")
        if entry.get("description"):
            entry["description"] = entry["description"][: int(max_chars)]
        return entry

    def stats(*, db_path=None):
        con = _connect()
        try:
            count = con.execute("SELECT COUNT(*) FROM entries").fetchone()[0]
            pages = con.execute("SELECT COUNT(DISTINCT page) FROM entries").fetchone()[0]
        finally:
            con.close()
        return {"entries": count, "pages": pages, "db": os.environ.get("PYAETHER_CATALOG_DB")}

    mod.search = search
    mod.show = show
    mod.stats = stats
    return mod


for _name, _factory in (("runtime", _stub_runtime), ("catalog", _stub_catalog)):
    try:
        if (_PKG / (_name + ".py")).exists():
            sys.stderr.write("%s real module present: pyaether_bridge.%s\\n" % (_TAG, _name))
        else:
            sys.modules["pyaether_bridge." + _name] = _factory()
            sys.stderr.write("%s stub injected: pyaether_bridge.%s\\n" % (_TAG, _name))
    except Exception as exc:  # a failing stub injection must not break the child
        sys.stderr.write("%s stub injector failed for %s: %r\\n" % (_TAG, _name, exc))
'''

# mini catalog: column order matches the entries table in ARCHITECTURE.md.
MINI_ENTRIES = [
    {
        "name": "pyAether.emyInitDb",
        "kind": "function",
        "module": "pyAether",
        "signature": "emyInitDb(path=None) -> bool",
        "summary": "Initialise the current EDA database context.",
        "description": "Initialises the current EDA database context; call once "
                       "at the start of a session. Returns False on failure.",
        "params": [{"name": "path", "type": "str", "desc": "optional database path"}],
        "returns": [{"name": "ok", "type": "bool", "desc": "whether init succeeded"}],
        "page": "pyAether.html",
        "anchor": "pyAether.emyInitDb",
        "domain": "pyAether",
    },
    {
        "name": "pyAether.emyCloseDb",
        "kind": "function",
        "module": "pyAether",
        "signature": "emyCloseDb() -> None",
        "summary": "Close the current EDA database context.",
        "description": "Closes the current database context and releases handles.",
        "params": [],
        "returns": [],
        "page": "pyAether.html",
        "anchor": "pyAether.emyCloseDb",
        "domain": "pyAether",
    },
    {
        "name": "pyAether.emyVersion",
        "kind": "attribute",
        "module": "pyAether",
        "signature": "emyVersion -> str",
        "summary": "Version string of the current EDA backend.",
        "description": "Read-only attribute returning the backend version string.",
        "params": [],
        "returns": [],
        "page": "pyAether.html",
        "anchor": "pyAether.emyVersion",
        "domain": "pyAether",
    },
]

EXPECTED_TOOLS = {
    "pyaether_status": [],
    "pyaether_api_search": ["query"],
    "pyaether_api_help": ["symbol"],
    "pyaether_exec": ["code"],
    "pyaether_sim_run": ["netlist"],
    "pyaether_layout_gen": ["spec"],
    "pyaether_layout_info": ["path"],
    "pyaether_layout_drc": ["path", "rules"],
    "pyaether_layout_boolean": ["op", "a", "out_layer"],
    "pyaether_layout_convert": ["source", "output"],
    "pyaether_layout_compare": ["a", "b"],
    "pyaether_layout_deck": ["script"],
}


def build_mini_catalog(path):
    con = sqlite3.connect(str(path))
    try:
        con.executescript(
            """
            CREATE TABLE entries(
              name TEXT PRIMARY KEY, kind TEXT, module TEXT, signature TEXT,
              summary TEXT, description TEXT, params TEXT, returns TEXT,
              page TEXT, anchor TEXT, domain TEXT);
            CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT);
            """
        )
        # The FTS index is optional: some SQLite builds ship without FTS5, and
        # the catalog falls back to LIKE search there.
        try:
            con.execute(
                "CREATE VIRTUAL TABLE entries_fts USING fts5("
                "name, module, signature, summary, description, kind, domain)")
            has_fts = True
        except sqlite3.Error:
            has_fts = False
        for entry in MINI_ENTRIES:
            con.execute(
                "INSERT INTO entries(name, kind, module, signature, summary, description,"
                " params, returns, page, anchor, domain) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    entry["name"], entry["kind"], entry["module"], entry["signature"],
                    entry["summary"], entry["description"],
                    json.dumps(entry["params"], ensure_ascii=False),
                    json.dumps(entry["returns"], ensure_ascii=False),
                    entry["page"], entry["anchor"], entry["domain"],
                ),
            )
            if has_fts:
                con.execute(
                    "INSERT INTO entries_fts(name, module, signature, summary, description,"
                    " kind, domain) VALUES (?,?,?,?,?,?,?)",
                    (
                        entry["name"], entry["module"], entry["signature"], entry["summary"],
                        entry["description"], entry["kind"], entry["domain"],
                    ),
                )
        con.executemany(
            "INSERT INTO meta(key, value) VALUES (?,?)",
            [
                ("entries", str(len(MINI_ENTRIES))),
                ("pages", "1"),
                ("built_at", "2026-10-03T02:00:00Z"),
                ("source_dir", "probe-mini"),
                ("schema_version", "1"),
            ],
        )
        con.commit()
    finally:
        con.close()


class Client:
    """Minimal NDJSON client: write one request line, read one response line
    within the timeout."""

    def __init__(self, proc):
        self.proc = proc
        self.fd = proc.stdout.fileno()
        self.buffer = b""

    def send_raw(self, payload):
        self.proc.stdin.write(payload)
        self.proc.stdin.flush()

    def send(self, message):
        self.send_raw((json.dumps(message, ensure_ascii=False) + "\n").encode("utf-8"))

    def call(self, request_id, method, params=None, timeout=TIMEOUT):
        self.send(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": method,
                "params": {} if params is None else params,
            }
        )
        return self.recv(timeout)

    def _read_line(self, timeout):
        deadline = time.time() + timeout
        while True:
            newline = self.buffer.find(b"\n")
            if newline >= 0:
                line, self.buffer = self.buffer[:newline], self.buffer[newline + 1:]
                return line
            remaining = deadline - time.time()
            if remaining <= 0:
                raise TimeoutError("timed out after %.1fs waiting for a server response" % timeout)
            ready, _, _ = select.select([self.fd], [], [], remaining)
            if not ready:
                raise TimeoutError("timed out after %.1fs waiting for a server response" % timeout)
            chunk = os.read(self.fd, 65536)
            if not chunk:
                raise EOFError("server exited early (returncode=%s)" % self.proc.poll())
            self.buffer += chunk

    def recv(self, timeout=TIMEOUT):
        line = self._read_line(timeout)
        try:
            return json.loads(line.decode("utf-8"))
        except Exception as exc:
            raise AssertionError("non-protocol content on stdout %r: %s" % (line[:200], exc))


def shutdown(proc):
    try:
        proc.stdin.close()
    except Exception:
        pass
    try:
        proc.wait(timeout=5)
    except Exception:
        proc.kill()
        try:
            proc.wait(timeout=5)
        except Exception:
            pass


def package_version():
    try:
        text = (PKG_DIR / "__init__.py").read_text(encoding="utf-8")
    except Exception:
        return None
    match = re.search(r'__version__\s*=\s*["\']([^"\']+)["\']', text)
    return match.group(1) if match else None


def text_of(response):
    result = response.get("result") or {}
    content = result.get("content") or []
    if not content:
        return ""
    return content[0].get("text", "")


class Probe:
    def __init__(self):
        self.results = []

    def step(self, name, func):
        try:
            ok, detail = func()
        except Exception as exc:
            ok, detail = False, "%s: %s" % (type(exc).__name__, exc)
        self.results.append((name, ok, detail))
        print("%s %s" % ("PASS" if ok else "FAIL", name))
        if not ok and detail:
            for line in str(detail).splitlines():
                print("     | %s" % line)
        return ok

    @property
    def failed(self):
        return [item for item in self.results if not item[1]]


def main():
    version = package_version()
    print("== pyaether-bridge MCP probe ==")
    print("server : %s" % SERVER)
    print("python : %s (%s)" % (PYTHON, sys.version.split()[0]))
    backend = {
        name: ("real" if (PKG_DIR / (name + ".py")).exists() else "stub")
        for name in ("runtime", "catalog")
    }
    print("backend: runtime=%s catalog=%s (stubs injected via sitecustomize when missing)" % (
        backend["runtime"], backend["catalog"]))
    if not SERVER.exists():
        print("FAIL cannot find mcp_server.py: %s" % SERVER)
        return 1

    tmp = Path(tempfile.mkdtemp(prefix="pyaether-mcp-probe-"))
    probe = Probe()
    try:
        db_path = tmp / "catalog.sqlite"
        build_mini_catalog(db_path)
        stub_dir = tmp / "stubs"
        stub_dir.mkdir()
        (stub_dir / "sitecustomize.py").write_text(STUB_SITECUSTOMIZE, encoding="utf-8")

        env = dict(os.environ)
        env["PYTHONPATH"] = str(stub_dir)
        env["PYTHONUNBUFFERED"] = "1"
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        env["PYAETHER_PROBE_PKG_DIR"] = str(PKG_DIR)
        env["PYAETHER_CATALOG_DB"] = str(db_path)
        env["PYAETHER_DAEMON_SOCK"] = str(tmp / "no-such-dir" / "daemon.sock")
        env["PYAETHER_BRIDGE_HOME"] = str(tmp / "home")
        env["PYAETHER_BRIDGE_NO_AUTOSTART"] = "1"

        stderr_path = tmp / "server.err"
        stderr_handle = open(stderr_path, "wb")
        proc = subprocess.Popen(
            [PYTHON, str(SERVER)],
            cwd=str(ROOT),
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=stderr_handle,
            bufsize=0,
        )
        client = Client(proc)

        def check_initialize():
            response = client.call(
                1,
                "initialize",
                {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "mcp_probe", "version": "0.1"},
                },
            )
            result = response.get("result") or {}
            server_info = result.get("serverInfo") or {}
            problems = []
            if response.get("jsonrpc") != "2.0":
                problems.append("jsonrpc != 2.0")
            if response.get("id") != 1:
                problems.append("id != 1")
            if result.get("protocolVersion") != "2024-11-05":
                problems.append("protocolVersion not echoed back")
            if server_info.get("name") != "pyaether-bridge":
                problems.append("serverInfo.name != pyaether-bridge")
            if version and server_info.get("version") != version:
                problems.append("serverInfo.version=%r, expected %r" % (server_info.get("version"), version))
            if "tools" not in (result.get("capabilities") or {}):
                problems.append("capabilities.tools missing")
            return (not problems), ("; ".join(problems) + " | actual=" + json.dumps(response, ensure_ascii=False))

        def check_initialized_notification():
            client.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
            response = client.call(2, "ping")
            ok = response.get("id") == 2 and response.get("result") == {}
            return ok, "first response after the notification = " + json.dumps(response, ensure_ascii=False)

        def check_tools_list():
            response = client.call(3, "tools/list")
            tools = (response.get("result") or {}).get("tools")
            problems = []
            if not isinstance(tools, list):
                return False, "tools/list did not return a list: " + json.dumps(response, ensure_ascii=False)
            names = [tool.get("name") for tool in tools]
            if sorted(names) != sorted(EXPECTED_TOOLS):
                problems.append("tool name set mismatch: %s" % names)
            for tool in tools:
                name = tool.get("name")
                schema = tool.get("inputSchema") or {}
                if schema.get("type") != "object":
                    problems.append("%s: inputSchema.type != object" % name)
                if schema.get("additionalProperties") is not False:
                    problems.append("%s: additionalProperties is not false" % name)
                if list(schema.get("required") or []) != EXPECTED_TOOLS.get(name):
                    problems.append("%s: required=%s, expected %s" % (
                        name, schema.get("required"), EXPECTED_TOOLS.get(name)))
                if not tool.get("description"):
                    problems.append("%s: missing description" % name)
            by_name = {tool.get("name"): tool for tool in tools}
            props = (by_name.get("pyaether_api_search", {}).get("inputSchema") or {}).get("properties") or {}
            if sorted(props) != ["kind", "limit", "query"]:
                problems.append("api_search properties=%s" % sorted(props))
            elif props["limit"].get("default") != 20 or props["limit"].get("maximum") != 100:
                problems.append("api_search.limit schema=%s" % props.get("limit"))
            props = (by_name.get("pyaether_exec", {}).get("inputSchema") or {}).get("properties") or {}
            if sorted(props) != ["code", "timeout"]:
                problems.append("api_exec properties=%s" % sorted(props))
            elif props["timeout"].get("default") != 120 or props["timeout"].get("maximum") != 3600:
                problems.append("api_exec.timeout schema=%s" % props.get("timeout"))
            return (not problems), "; ".join(problems) or json.dumps(response, ensure_ascii=False)

        def check_api_search():
            response = client.call(
                4, "tools/call", {"name": "pyaether_api_search", "arguments": {"query": "emyInitDb"}}
            )
            text = text_of(response)
            result = response.get("result") or {}
            ok = ("pyAether.emyInitDb" in text) and not result.get("isError")
            return ok, "response=" + json.dumps(response, ensure_ascii=False)

        def check_api_search_kind_filter():
            response = client.call(
                5,
                "tools/call",
                {"name": "pyaether_api_search", "arguments": {"query": "emy", "limit": 10, "kind": "attribute"}},
            )
            text = text_of(response)
            if response.get("result", {}).get("isError"):
                return False, "isError=true: " + json.dumps(response, ensure_ascii=False)
            body = text.split("\n", 1)[1] if "\n" in text else text
            hits = json.loads(body)
            ok = bool(hits) and all(hit.get("kind") == "attribute" for hit in hits)
            return ok, "kind-filtered hits=" + json.dumps(hits, ensure_ascii=False)

        def check_api_help():
            response = client.call(
                6, "tools/call", {"name": "pyaether_api_help", "arguments": {"symbol": "pyAether.emyInitDb"}}
            )
            text = text_of(response)
            ok = (
                not (response.get("result") or {}).get("isError")
                and "pyAether.emyInitDb" in text
                and "signature:" in text
            )
            return ok, "text=" + text.replace("\n", " / ")[:400]

        def check_api_help_missing():
            response = client.call(
                7, "tools/call", {"name": "pyaether_api_help", "arguments": {"symbol": "pyAether.NoSuchSymbol"}}
            )
            result = response.get("result") or {}
            text = text_of(response)
            ok = result.get("isError") is True and "no API named" in text
            return ok, "response=" + json.dumps(response, ensure_ascii=False)

        def check_exec_failure_is_graceful():
            response = client.call(
                8, "tools/call", {"name": "pyaether_exec", "arguments": {"code": "print(1)", "timeout": 5}}
            )
            result = response.get("result") or {}
            text = text_of(response)
            ok = result.get("isError") is True and bool(text.strip()) and "error" not in response
            return ok, "response=" + json.dumps(response, ensure_ascii=False)

        def check_status_without_daemon():
            response = client.call(9, "tools/call", {"name": "pyaether_status", "arguments": {}})
            result = response.get("result") or {}
            text = text_of(response)
            ok = result.get("isError") is not True and "daemon" in text
            return ok, "response=" + json.dumps(response, ensure_ascii=False)

        def check_unknown_method():
            response = client.call(10, "pyaether/does-not-exist", {})
            error = response.get("error") or {}
            ok = error.get("code") == -32601
            return ok, "response=" + json.dumps(response, ensure_ascii=False)

        def check_unknown_tool():
            response = client.call(
                11, "tools/call", {"name": "pyaether_nope", "arguments": {}}
            )
            error = response.get("error") or {}
            ok = error.get("code") == -32602
            return ok, "response=" + json.dumps(response, ensure_ascii=False)

        def check_bad_json_then_ping():
            client.send_raw(b"{oops-not-json\n")
            bad = client.recv()
            after = client.call(12, "ping")
            ok = (
                (bad.get("error") or {}).get("code") == -32700
                and after.get("id") == 12
                and after.get("result") == {}
            )
            return ok, "parse-error response=%s then ping=%s" % (
                json.dumps(bad, ensure_ascii=False), json.dumps(after, ensure_ascii=False))

        def check_still_alive_after_failures():
            response = client.call(13, "ping")
            ok = response.get("id") == 13 and response.get("result") == {}
            return ok, "response=" + json.dumps(response, ensure_ascii=False)

        probe.step("initialize echoes protocolVersion / serverInfo / capabilities.tools", check_initialize)
        probe.step("notifications/initialized is not answered, ping still works", check_initialized_notification)
        probe.step("tools/list returns every expected tool with a valid schema",
                   check_tools_list)
        probe.step("tools/call pyaether_api_search hits the mini catalog", check_api_search)
        probe.step("tools/call pyaether_api_search supports kind filtering", check_api_search_kind_filter)
        probe.step("tools/call pyaether_api_help hits and returns a signature", check_api_help)
        probe.step("tools/call pyaether_api_help sets isError when not found", check_api_help_missing)
        probe.step("tools/call pyaether_exec fails gracefully with no daemon and no autostart", check_exec_failure_is_graceful)
        probe.step("tools/call pyaether_status still reports state without a daemon", check_status_without_daemon)
        probe.step("unknown method returns -32601", check_unknown_method)
        probe.step("unknown tool returns -32602", check_unknown_tool)
        probe.step("a bad JSON line returns -32700 and the server keeps serving", check_bad_json_then_ping)
        probe.step("the server stays alive after repeated tool failures", check_still_alive_after_failures)

        shutdown(proc)
        stderr_handle.close()

        def check_module_entrypoint():
            env2 = dict(env)
            env2["PYTHONPATH"] = os.pathsep.join([str(stub_dir), str(ROOT)])
            err2 = open(tmp / "server-m.err", "wb")
            proc2 = subprocess.Popen(
                [PYTHON, "-m", "pyaether_bridge.mcp_server"],
                cwd=str(ROOT),
                env=env2,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=err2,
                bufsize=0,
            )
            try:
                client2 = Client(proc2)
                init = client2.call(1, "initialize", {"protocolVersion": "2024-11-05"})
                ping = client2.call(2, "ping")
                ok = (
                    ((init.get("result") or {}).get("protocolVersion") == "2024-11-05")
                    and ping.get("result") == {}
                )
                if not ok:
                    return False, "init=%s ping=%s" % (
                        json.dumps(init, ensure_ascii=False), json.dumps(ping, ensure_ascii=False))
                return True, ""
            finally:
                shutdown(proc2)
                err2.close()

        probe.step("python3 -m pyaether_bridge.mcp_server entry point works", check_module_entrypoint)

        print("-" * 64)
        passed = len(probe.results) - len(probe.failed)
        print("result: %d/%d PASS" % (passed, len(probe.results)))
        if probe.failed:
            try:
                err_text = stderr_path.read_text(encoding="utf-8", errors="replace")
            except Exception:
                err_text = ""
            if err_text.strip():
                print("--- server stderr (last 2000 chars) ---")
                print(err_text[-2000:])
            print("verdict: FAIL (%d check(s) failed)" % len(probe.failed))
            return 1
        print("verdict: ALL PASS")
        return 0
    finally:
        if os.environ.get("PYAETHER_MCP_PROBE_KEEP") != "1":
            try:
                import shutil

                shutil.rmtree(str(tmp), ignore_errors=True)
            except Exception:
                pass
        else:
            print("(temp directory kept: %s)" % tmp)


if __name__ == "__main__":
    raise SystemExit(main())
