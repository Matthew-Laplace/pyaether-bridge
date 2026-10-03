# Target setup and troubleshooting

This tool **does not install, modify, or circumvent** any vendor software or
license. It assumes you already have a properly licensed Aether / PyAether
installation whose pyAether can check out a license. Preparing that environment
is your own deployment work — follow the vendor documentation. This page covers
only what the *bridge* needs and how to confirm it.

The target can be one of three things (see "Connecting to your PyAether" in the
README): a Docker container, an SSH-reachable server, or the local machine. They
are self-checked differently; both variants are shown below.

## 1. Prerequisites

| Item | Requirement | How to check |
| --- | --- | --- |
| Host Python | 3.9+, the distro default is fine | `python3 -V` |
| Connectivity | docker: `docker ps` works; ssh: `ssh <host>` logs in without a password; local: nothing needed | see "Target self-check" |
| Target | Aether install tree + its bundled Python 3.9 + OpenAccess shared libraries | see "Target self-check" |
| License | A floating license server is reachable, or a node-locked license is configured | see "Target self-check" |
| Architecture | Aether ships `linux/amd64` only; on Apple Silicon it only runs through Docker emulation | `docker inspect -f '{{.Platform}}'` |

Running a `linux/amd64` container on Apple Silicon works (Docker Desktop's x86_64
emulation); the cost is a much slower `import pyAether` (about 8 s in practice),
which is exactly why this project keeps a resident session.

If your PyAether lives on a **server** rather than in a container, use the ssh
variant below. The equivalent bridge settings are `PYAETHER_TRANSPORT=ssh` plus
`PYAETHER_SSH_HOST=user@host`.

## 2. Target self-check

First confirm the target itself works. Docker variant (`CTR` is your container
name):

```bash
CTR=empyrean-gui

# a. install tree and login-shell environment
docker exec "$CTR" bash -lc 'echo "$AETHER_ROOT"; echo "$PYAETHER_HOME"; echo "$LM_LICENSE_FILE"'

# b. the bundled interpreter exists
docker exec "$CTR" bash -lc 'command -v python3.9'

# c. pyAether imports (this checks out a license)
docker exec "$CTR" bash -lc 'cd /tmp && python3.9 -c "
import pyAether
print(\"symbols:\", len([n for n in dir(pyAether) if not n.startswith(\"_\")]))"'
```

Server variant (`HOST=aether@lab-server`):

```bash
HOST=aether@lab-server

ssh "$HOST" 'echo "$AETHER_ROOT"; echo "$LM_LICENSE_FILE"'
ssh "$HOST" 'command -v python3.9'
ssh "$HOST" 'cd /tmp && python3.9 -c "
import pyAether
print(\"symbols:\", len([n for n in dir(pyAether) if not n.startswith(\"_\")]))"'
```

Step c printing a symbol count (16781 on 2026.03) means the environment is ready.
If it fails, fix the target's license and library paths first — this tool cannot
do that for you.

> Using a login shell (`bash -lc`) rather than `docker exec ... python3.9` directly
> is **mandatory**: Aether's `LD_LIBRARY_PATH`, `PYTHONPATH` and `AETHER_*` all come
> from the image or server profile. All three transports in this project go through
> a login shell, matching how the target is meant to be used.

## 3. Point the bridge at your environment

The defaults only assume "the docker container is called `empyrean-gui`". Cover any
difference with environment variables or `~/.cache/pyaether-bridge/config.json`
instead of editing code:

```bash
# pick one
export PYAETHER_CONTAINER=my-aether-container          # docker
export PYAETHER_TRANSPORT=ssh                          # or: a server
export PYAETHER_SSH_HOST=aether@lab-server

# optional
export PYAETHER_PYTHON=python3.9              # interpreter on the target (default python3.9)
export PYAETHER_LICENSE_SERVER=port@host      # unset keeps the target's own LM_LICENSE_FILE
```

Or write it as a user-level config file (recommended: local paths stay out of the
repository):

```json
{
  "transport": "ssh",
  "ssh_host": "aether@lab-server",
  "license_server": "port@host",
  "docs_dir": "/path/to/tools/pyaether/docs/html"
}
```

## 4. Smoke test

```bash
./bin/pyaether status                      # target / daemon / session status
./bin/pyaether exec -c "import pyAether; print(len(dir(pyAether)))"
./bin/pyaether api build --docs ./data/docs --jobs 4
./bin/pyaether api sync-live
./bin/pyaether api search region query --limit 5
python3 tests/mcp_probe.py && python3 tests/transport_probe.py && bash tests/smoke_cli.sh
```

The first `exec` starts the daemon and performs one `import pyAether` (about 8 s);
every later call takes milliseconds.

## 5. Troubleshooting

| Symptom | Cause | Fix |
| --- | --- | --- |
| `libpython3.9.so.1.0: cannot open shared object file` | not a login shell, so `LD_LIBRARY_PATH` is empty | make sure the call goes through `bash -lc`; this project already does |
| `LC_Check out license ... failed` | the target's license is unconfigured or unreachable | a vendor licensing problem: fix the target first (this project does not touch licensing) |
| `transport=ssh requires PYAETHER_SSH_HOST to be set` | ssh chosen without a host | set `PYAETHER_SSH_HOST=user@host` |
| ssh reports `Host key verification failed` or permission denied | the host cannot reach the target over SSH yet | run `ssh <host>` by hand and make it passwordless; this tool does not distribute keys |
| ssh hangs or asks for a password | password auth is in use but the bridge forces `BatchMode` | set up keys / ssh-agent (a background service cannot type) |
| `interpreter python3.9 not found on <host>` | the interpreter has a different name or path | set `PYAETHER_PYTHON`, e.g. `/opt/.../bin/python3.9` |
| `daemon did not become ready within Ns` | the daemon or the target session will not start | read `~/.cache/pyaether-bridge/daemon.log` |
| "another daemon is already running" but `status` cannot connect | a half-dead process from the previous exit still holds the lock | run `daemon stop` again or repeat the request; it self-heals |
| the first request after a target restart (container rebuild / server reboot) is slow | the old session is gone, so it is rebuilt and `import pyAether` runs again | expected; variables from the old namespace are lost |
| `api build` cannot find the docs | `$AETHER_ROOT/tools/pyaether/docs/html` was not found | pass `--docs`, or copy them out of the container with `docker cp` (see README) |
| `api search` reports a missing database | the catalog has not been built | `./bin/pyaether api build` |

## 6. Uninstall

This project writes nothing to system directories and installs no dependencies.
Deleting the repository directory is enough; to also clean up runtime traces:

```bash
./bin/pyaether daemon stop        # stop the daemon first (ends the resident session)
rm -rf ~/.cache/pyaether-bridge   # data directory (socket, logs, user config)
```

The target keeps exactly one deployed artifact: `$PYAETHER_REMOTE_DIR/session_bridge.py`
(default `/tmp/pyaether-bridge/`). Delete it when you are done; it writes nothing
to system directories and registers no services.
