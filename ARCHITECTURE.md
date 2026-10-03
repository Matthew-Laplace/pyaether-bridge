# Architecture and internal interfaces

All host-side code uses the **Python 3.9 standard library only** and pulls in no
third-party dependencies; the repository root is written as `<ROOT>`. For the
big picture see the [README](README.md#architecture).

## Layout

```
<ROOT>/pyaether_bridge/__init__.py         # version string
<ROOT>/pyaether_bridge/config.py           # configuration resolution (env var / user config file / default)
<ROOT>/pyaether_bridge/transports.py       # transports: docker / ssh / local
<ROOT>/pyaether_bridge/catalog.py          # offline API catalog (sqlite + FTS5)
<ROOT>/pyaether_bridge/runtime.py          # host-side client (unix socket -> daemon)
<ROOT>/pyaether_bridge/daemon.py           # host daemon (serializes target requests)
<ROOT>/pyaether_bridge/session_bridge.py   # target-side resident executor (NDJSON over stdio)
<ROOT>/pyaether_bridge/cli.py              # command line entry point
<ROOT>/pyaether_bridge/mcp_server.py       # MCP stdio server
<ROOT>/bin/pyaether                        # CLI launcher shim
<ROOT>/bin/pyaether-mcp                    # MCP launcher shim
<ROOT>/tests/smoke_cli.sh                  # CLI smoke test (needs no Docker)
<ROOT>/tests/mcp_probe.py                  # MCP protocol and tool probe
<ROOT>/tests/transport_probe.py            # transport probe (local + fake ssh stub + optional docker)
<ROOT>/data/catalog.sqlite                 # local build artifact, not in git
```

## Request path

```
CLI / MCP ──unix socket(NDJSON)──► daemon ──transport(NDJSON)──► session_bridge
                                     │                                │
                              serialized by one mutex        resident pyAether session
```

- The daemon owns the session handle; the session runs `import pyAether` only on
  first use.
- On the target, fd 1/2 are redirected into temp files for the duration of each
  request so that OpenAccess/pyAether C-level output is captured instead of
  corrupting the protocol stream.
- One session equals one license seat, and there is only ever one session, which
  matches OpenAccess's single-writer constraint.

## transports.py

"Where does PyAether live" is abstracted behind one interface, so neither the
daemon nor the catalog calls docker/ssh directly any more:

```python
class Transport:
    def describe(self) -> dict            # state for `status`
    def label(self) -> str                # e.g. "docker:empyrean-gui" / "ssh:user@host"
    def probe(self) -> dict               # reachability + target interpreter path
    def write_file(path, data)            # deploy session_bridge.py
    def open_session(inner_command)       # start a persistent stdio session (Popen)
    def fetch_file(path, local_path)      # fetch a file back (api sync-live)
```

| Implementation | Deploy | Start a session | Fetch a file |
| --- | --- | --- | --- |
| `DockerTransport` | `docker exec -i <ctr> sh -c 'mkdir -p .. && cat > ..'` | `docker exec -i -w /tmp <ctr> bash -lc <cmd>` | `docker cp` |
| `SSHTransport` | `ssh -T -o BatchMode=yes <host> bash -lc '<cmd>'` | same, stdio straight through | `ssh <host> bash -lc 'cat <path>'` |
| `LocalTransport` | write a local file | local `bash -lc <cmd>` | `shutil.copyfile` |

Notes:

- All three implementations start through a **login shell**, because Aether's
  environment variables come from the profile.
- SSH must quote the whole remote command as one string (ssh joins argv with
  spaces and hands it to the remote login shell to parse); the fake ssh stub in
  `tests/transport_probe.py` reproduces that faithfully.
- The target's `LM_LICENSE_FILE` is overridden only when `PYAETHER_LICENSE_SERVER`
  is set explicitly.
- No credentials are read or stored; SSH authentication is left to ssh itself and
  `BatchMode` is forced so a background call never blocks on a password.

## catalog.py

```python
def build_from_html(docs_html_dir, out_db, *, jobs=1, progress=None) -> dict
    # -> {"entries": int, "pages": int, "db": str, "by_kind": {...},
    #     "by_domain": {...}, "elapsed_s": float, "skipped": int}
def sync_from_runtime(*, db_path=None, timeout=300.0, container=None) -> dict
    # merge dir(pyAether) symbols from the live session into the catalog (kind="runtime")
def search(query, *, limit=20, kind=None, module=None, db_path=None) -> list[dict]
    # each hit: {"name","kind","module","signature","summary","domain","page","anchor"}
    # ranking: exact name > prefix > substring > FTS5 term hit; doc/label rows
    # never outrank real API symbols
def show(name, *, max_chars=4000, db_path=None) -> dict | None
    # {"name","kind","module","signature","summary","description",
    #  "params","returns","page","anchor","domain"}
def stats(*, db_path=None) -> dict
```

Database schema (`schema_version=1`):

```sql
CREATE TABLE entries(
  name TEXT PRIMARY KEY, kind TEXT, module TEXT, signature TEXT,
  summary TEXT, description TEXT, params TEXT, returns TEXT,
  page TEXT, anchor TEXT, domain TEXT);
CREATE VIRTUAL TABLE entries_fts USING fts5(
  name, module, signature, summary, description, kind, domain);
CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT);
-- meta: entries, pages, built_at, source_dir, schema_version,
--       runtime_symbols, runtime_synced_at
```

`params`/`returns` are stored as JSON strings. A missing database raises
`CatalogMissingError` and points at `pyaether api build`.

## runtime.py

```python
def request(method, params=None, *, timeout=30.0, autostart=True) -> dict
def exec_code(code, *, timeout=120.0, autostart=True) -> dict
    # -> {"ok","stdout","stderr","result_repr","result_json","elapsed_s",
    #     "timed_out","error","error_type"}
def status(*, autostart=False) -> dict
def ensure_daemon(*, wait=90.0) -> dict
def stop_daemon() -> dict
```

When the daemon is not running `request()` starts it automatically; set
`PYAETHER_BRIDGE_NO_AUTOSTART=1` to forbid that. A connection failure is retried
once and only when **no request has been sent yet**, so `exec_code` is never
silently replayed.

## Daemon and session protocol

Both hops speak NDJSON (one JSON object per line).

Daemon methods: `ping`, `status`, `exec`, `namespace`, `restart`, `stop`.

Session methods (`session_bridge.py`): `ping`, `status`, `exec`, `namespace`,
`shutdown`, `restart_session`.

A handshake message is sent first:

```json
{"type": "ready", "ok": true, "pid": 123, "python": "3.9.0",
 "cwd": "/tmp", "import_seconds": 8.03, "symbols": 16781, "import_log": "..."}
```

`ok=false` means `import pyAether` failed on the target (for example PyAether is
not installed there at all). The session still works for plain Python, which is
what lets you tell "the transport is broken" apart from "the environment is not
set up".

`exec` takes `{"code": str, "timeout": number}` and returns:

```json
{"ok": true, "stdout": "...", "stderr": "...", "result_repr": "...",
 "result_json": null, "elapsed_s": 0.02, "timed_out": false,
 "error": null, "error_type": null, "generation": 5, "namespace_keys": ["pyAether"]}
```

The session namespace survives across `exec` calls (handy for incremental work
such as `db = pyAether.pyaedb`).

## Configuration resolution

`config.setting(env, key, default)` resolves in three steps: **environment
variable -> user config file -> default**. The user config file is
`$PYAETHER_BRIDGE_HOME/config.json` (default `~/.cache/pyaether-bridge/config.json`)
with the same snake_case keys used in the docs. That keeps local paths, container
names and license endpoints out of the repository.

## CLI

```
pyaether status [--json]
pyaether exec [-c CODE | -f FILE] [--timeout S] [--json]     # reads stdin without -c/-f
pyaether api build [--docs DIR] [--out DB] [--jobs N] [--json]
pyaether api stats [--json]
pyaether api search QUERY [--limit N] [--kind K] [--db DB] [--json]
pyaether api show SYMBOL [--max-chars N] [--db DB] [--json]
pyaether api sync-live [--db DB] [--timeout S] [--json]
pyaether daemon [start|stop|status|restart] [--json]
pyaether version
```

Exit codes: `0` success; `1` runtime failure (exec raised, catalog missing, ...);
`2` usage error. Human-readable output is English; `--json` prints raw JSON.

## MCP server

Single stdio process, NDJSON JSON-RPC 2.0 (protocol version `2024-11-05`).
**stdout carries protocol messages only**; all diagnostics go to stderr.

Methods: `initialize`, `notifications/*`, `ping`, `tools/list`, `tools/call`.
Unknown method `-32601`, unknown tool `-32602`, a bad JSON line `-32700` (the line
is dropped and the server keeps serving). A tool that fails internally answers
with `{"content": [...], "isError": true}` instead of a protocol error.

| Tool | inputSchema |
| --- | --- |
| `pyaether_status` | `{}` |
| `pyaether_api_search` | `{query: str(required), limit?: int=20, kind?: str}` |
| `pyaether_api_help` | `{symbol: str(required), max_chars?: int=4000}` |
| `pyaether_exec` | `{code: str(required), timeout?: number=120}` |
