#!/usr/bin/env python3
"""pyaether command line interface (standard library only, Python 3.9+).

The subcommands match ARCHITECTURE.md; human-readable output is English and
`--json` prints raw JSON. Exit codes: 0 success; 1 runtime failure (exec raised,
catalog missing, daemon won't start, ...); 2 usage error (handled by argparse).
"""

from __future__ import annotations

import argparse
import importlib
import json
import os
import sys

PROG = "pyaether"


def _project_root():
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _bootstrap_path():
    """Put the repository root on sys.path when run directly or via the bin shim."""
    root = _project_root()
    if root not in sys.path:
        sys.path.insert(0, root)
    return root


class CliError(RuntimeError):
    """An expected runtime failure: print an explanation and exit with code 1."""

    def __init__(self, message, hint=None):
        super().__init__(message)
        self.hint = hint


def _load(module_name):
    """Import pyaether_bridge.<module> on demand, with an actionable hint when
    it is missing."""
    _bootstrap_path()
    try:
        return importlib.import_module("pyaether_bridge." + module_name)
    except ImportError as exc:
        raise CliError(
            "cannot import module pyaether_bridge.%s: %s" % (module_name, exc),
            hint="repository root: %s (module missing or dependencies incomplete)"
                 % _project_root(),
        )


def _catalog_error(action, exc):
    """Turn a catalog exception into a CliError carrying a next step."""
    hint = "run `%s api build` to create the offline catalog, or check PYAETHER_CATALOG_DB." % PROG
    if type(exc).__name__ == "CatalogMissingError":
        return CliError(str(exc), hint=hint)
    return CliError("%s failed: %s: %s" % (action, type(exc).__name__, exc), hint=hint)


def _runtime():
    return _load("runtime")


def _catalog():
    return _load("catalog")


def _config():
    return _load("config")


def _simulators():
    return _load("simulators")


# --------------------------------------------------------------------------- #
# output helpers
# --------------------------------------------------------------------------- #
def _print_json(payload):
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))


def _emit(payload, as_json, human):
    if as_json:
        _print_json(payload)
    else:
        human()
    return 0


def _fmt_duration(seconds):
    try:
        seconds = float(seconds)
    except (TypeError, ValueError):
        return str(seconds)
    if seconds < 60:
        return "%.1fs" % seconds
    return "%.0fs" % seconds


def _human_status(info):
    target = info.get("transport") or {}
    if target:
        reachable = target.get("reachable")
        mark = "reachable" if reachable else "unreachable (configuration only)"
        extra = (", interpreter %s" % target["python"]) if target.get("python") else ""
        print("transport: %s -- %s%s" % (target.get("label") or target.get("kind"),
                                    mark, extra))
        if not reachable and target.get("detail"):
            print("      %s" % target["detail"])
    for name, meta in (target.get("targets") or {}).items():
        if isinstance(meta, dict):
            state = meta.get("status") or ("running" if meta.get("running") else "not running")
            image = meta.get("image")
            print("container %s: %s%s" % (name, state, (" (%s)" % image) if image else ""))
        else:
            print("container %s: %s" % (name, meta))

    daemon = info.get("daemon") or {}
    details = []
    if daemon.get("pid"):
        details.append("pid=%s" % daemon["pid"])
    if daemon.get("uptime_s") is not None:
        details.append("up %s" % _fmt_duration(daemon["uptime_s"]))
    if daemon.get("socket"):
        details.append(str(daemon["socket"]))
    print(
        "daemon: %s%s"
        % ("running" if daemon.get("running") else "not running",
           (" (%s)" % ", ".join(details)) if details else "")
    )

    session = info.get("session") or {}
    if session:
        bits = ["ready=%s" % ("yes" if session.get("ready") else "no")]
        if session.get("generation") is not None:
            bits.append("executions=%s" % session["generation"])
        if session.get("import_seconds") is not None:
            bits.append("import took %s" % _fmt_duration(session["import_seconds"]))
        if session.get("symbols") is not None:
            bits.append("symbols=%s" % session["symbols"])
        print("session: %s" % ", ".join(bits))
        init_log = session.get("init_log")
        if init_log:
            print("  init: %s" % str(init_log).strip().splitlines()[0])
    else:
        print("session: not started")

    keys = info.get("namespace_keys") or []
    print("session namespace: %s" % (", ".join(keys) if keys else "(empty)"))

    last_error = info.get("last_error")
    if last_error:
        print("last error: %s" % last_error, file=sys.stderr)
    if info.get("error"):
        print("hint: %s" % info["error"], file=sys.stderr)


def _progress_factory():
    started = {"t": None}

    def progress(*args, **kwargs):
        text = " ".join(str(a) for a in args if a is not None)
        for key, value in kwargs.items():
            text += " %s=%s" % (key, value)
        text = text.strip() or "building..."
        sys.stderr.write("[build] %s\n" % text)
        sys.stderr.flush()
        started["t"] = text

    return progress


# --------------------------------------------------------------------------- #
# subcommand implementations
# --------------------------------------------------------------------------- #
def cmd_version(args):
    try:
        version = _load("__init__").__version__
    except CliError:
        version = "unknown"
    text = "pyaether-bridge %s (Python %s)" % (version, sys.version.split()[0])
    if getattr(args, "json", False):
        _print_json({"name": "pyaether-bridge", "version": version,
                     "python": sys.version.split()[0], "root": _project_root()})
    else:
        print(text)
    return 0


def cmd_status(args):
    runtime = _runtime()
    try:
        info = runtime.status(autostart=False)
    except TypeError:  # tolerate an implementation without keyword arguments
        info = runtime.status()
    except Exception as exc:
        raise CliError(
            "reading status failed: %s: %s" % (type(exc).__name__, exc),
            hint="run `%s daemon start` to start the host daemon, then retry." % PROG,
        )
    return _emit(info, getattr(args, "json", False), lambda: _human_status(info))


def cmd_exec(args):
    runtime = _runtime()
    if args.code is not None:
        code = args.code
    elif args.file is not None:
        try:
            with open(args.file, "r", encoding="utf-8") as handle:
                code = handle.read()
        except OSError as exc:
            raise CliError("cannot read the code file: %s" % exc,
                           hint="check that the path exists and is readable: %s" % args.file)
    else:
        if sys.stdin.isatty():
            raise CliError(
                "no code given: pass -c CODE, -f FILE, or pipe it in on stdin.",
                hint="for example: echo 'pyAether.emyInitDb()' | %s exec" % PROG,
            )
        code = sys.stdin.read()
    if not code.strip():
        raise CliError("the code to execute is empty.",
                       hint="pass non-empty code with -c CODE or -f FILE.")

    try:
        result = runtime.exec_code(code, timeout=args.timeout)
    except Exception as exc:
        raise CliError(
            "execution failed: %s: %s" % (type(exc).__name__, exc),
            hint="run `%s daemon start`; if needed check the daemon log and `%s status`."
                 % (PROG, PROG),
        )

    if getattr(args, "json", False):
        _print_json(result)
        return 0 if result.get("ok") else 1

    stdout = result.get("stdout") or ""
    stderr = result.get("stderr") or ""
    if stdout:
        sys.stdout.write(stdout if stdout.endswith("\n") else stdout + "\n")
    if stderr:
        sys.stderr.write(stderr if stderr.endswith("\n") else stderr + "\n")
    repr_text = result.get("result_repr")
    if repr_text and repr_text != "None":
        print(repr_text)
    if result.get("timed_out"):
        print("error: execution timed out (timeout=%ss)" % args.timeout, file=sys.stderr)
    if not result.get("ok"):
        print("error: %s" % (result.get("error") or "unknown error"), file=sys.stderr)
        if result.get("error_type"):
            print("error type: %s" % result["error_type"], file=sys.stderr)
        return 1
    return 0


def cmd_api_build(args):
    catalog = _catalog()
    config = _config()
    docs = args.docs or config.find_docs_dir()
    if not docs:
        raise CliError(
            "cannot find the PyAether documentation directory (docs/html).",
            hint="pass `--docs DIR`, or copy tools/pyaether/docs/html out of your "
                 "Aether installation and point `--docs` at it.",
        )
    if not os.path.isdir(docs):
        raise CliError("documentation directory does not exist: %s" % docs,
                       hint="check the --docs argument.")
    out_db = args.out or str(config.CATALOG_DB)
    try:
        summary = catalog.build_from_html(docs, out_db, jobs=args.jobs,
                                          progress=_progress_factory())
    except Exception as exc:
        raise CliError("building the API catalog failed: %s: %s"
                       % (type(exc).__name__, exc),
                       hint="check that the docs directory is complete (it must "
                            "contain objects.inv) and the output path is writable: %s"
                            % out_db)
    if getattr(args, "json", False):
        _print_json(summary)
    else:
        print("API catalog built: %s" % (summary.get("db") or out_db))
        print("  entries: %s    pages: %s    skipped: %s    elapsed: %ss"
              % (summary.get("entries"), summary.get("pages"),
                 summary.get("skipped"), summary.get("elapsed_s")))
        for kind, count in sorted((summary.get("by_kind") or {}).items(),
                                  key=lambda item: -item[1]):
            print("  %-12s %s" % (kind, count))
    return 0


def cmd_api_stats(args):
    catalog = _catalog()
    try:
        info = catalog.stats(db_path=args.db)
    except Exception as exc:
        raise _catalog_error("reading the API catalog", exc)
    def human():
        print("API catalog: %s" % info.get("db"))
        print("  entries: %s    pages: %s    built at: %s"
              % (info.get("entries"), info.get("pages"), info.get("built_at")))
        by_kind = info.get("by_kind") or {}
        if by_kind:
            print("  by kind: %s"
                  % ", ".join("%s=%s" % (k, v) for k, v in
                              sorted(by_kind.items(), key=lambda item: -item[1])))
        domains = info.get("by_domain") or {}
        if domains:
            top = sorted(domains.items(), key=lambda item: -item[1])[:12]
            print("  by domain: %s" % ", ".join("%s=%s" % (k, v) for k, v in top))

    return _emit(info, getattr(args, "json", False), human)


def cmd_api_sync_live(args):
    """Add the runtime session symbols that the docs do not cover to the catalog."""
    catalog = _catalog()
    try:
        info = catalog.sync_from_runtime(db_path=args.db, timeout=args.timeout)
    except Exception as exc:
        raise _catalog_error("syncing runtime symbols", exc)

    def human():
        print("runtime symbols merged into the catalog: %s added, %s rows in total"
              % (info.get("added"), info.get("entries")))
        print("  session exported: %s symbols    target: %s"
              % (info.get("symbols"), info.get("target")))
        print("  database: %s" % info.get("db"))

    return _emit(info, getattr(args, "json", False), human)


def cmd_api_search(args):
    catalog = _catalog()
    try:
        results = catalog.search(args.query, limit=args.limit, kind=args.kind,
                                 db_path=args.db)
    except Exception as exc:
        raise _catalog_error("searching the API catalog", exc)
    def human():
        if not results:
            print("no entries match \"%s\"." % args.query)
            return
        print("%d match(es):" % len(results))
        for item in results:
            line = item.get("name") or "(unnamed)"
            if item.get("kind"):
                line += "  [%s]" % item["kind"]
            module = item.get("module")
            if module and module != item.get("name"):
                line += "  (%s)" % module
            print(line)
            if item.get("signature"):
                print("    %s" % item["signature"])
            summary = (item.get("summary") or "").strip().replace("\n", " ")
            if summary:
                print("    %s" % summary[:200])

    return _emit(list(results), getattr(args, "json", False), human)


def cmd_api_show(args):
    catalog = _catalog()
    try:
        info = catalog.show(args.symbol, max_chars=args.max_chars, db_path=args.db)
    except Exception as exc:
        raise _catalog_error("reading the symbol", exc)
    if not info:
        raise CliError(
            "symbol not found: %s" % args.symbol,
            hint="use `%s api search <keyword>` to look for a similar name." % PROG,
        )

    def human():
        head = info.get("name") or args.symbol
        if info.get("kind"):
            head += "  [%s]" % info["kind"]
        if info.get("module"):
            head += "  (%s)" % info["module"]
        print(head)
        if info.get("signature"):
            print("signature: %s" % info["signature"])
        if info.get("summary"):
            print("summary: %s" % info["summary"])
        if info.get("description"):
            print()
            print(info["description"])
        for title, key in (("parameters", "params"), ("returns", "returns")):
            rows = info.get(key) or []
            if rows:
                print()
                print("%s:" % title)
                for row in rows:
                    if isinstance(row, dict):
                        print("  - %s: %s  %s"
                              % (row.get("name"), row.get("type") or "",
                                 row.get("desc") or ""))
                    else:
                        print("  - %s" % row)
        page = info.get("page")
        if page:
            anchor = info.get("anchor") or ""
            print()
            print("docs: %s%s" % ("%s%s" % (page, "#" + anchor if anchor else ""), ""))

    return _emit(info, getattr(args, "json", False), human)


def cmd_daemon(args):

    return _daemon_action(args)


# --------------------------------------------------------------------------- #
# simulators
# --------------------------------------------------------------------------- #
def _sim_summary(result):
    """Human-readable rendering of one simulation result."""
    print("status : %s (ok=%s)" % (result.get("status"), result.get("ok")))
    print("backend: %s" % result.get("backend"))
    metadata = result.get("metadata") or {}
    for key in ("target", "mode", "work_dir", "command"):
        if metadata.get(key):
            print("%-7s: %s" % (key, metadata[key]))
    data = result.get("data") or {}
    if data:
        parts = []
        for key, value in data.items():
            parts.append("%s[%d]" % (key, len(value)) if isinstance(value, list)
                         else "%s=%s" % (key, value))
        print("data   : %s" % ", ".join(parts))
    else:
        print("data   : (none parsed)")
    for plot in metadata.get("plots") or []:
        print("plot   : %s (points=%s, variables=%s)"
              % (plot.get("name"), plot.get("points"), ", ".join(plot.get("variables") or [])))
    if metadata.get("artifacts"):
        print("files  : %s" % ", ".join(metadata["artifacts"]))
    timings = metadata.get("timings") or {}
    if timings:
        print("timings: %s" % ", ".join("%s=%s" % item for item in timings.items()))
    for error in result.get("errors") or []:
        print("error  : %s" % error, file=sys.stderr)
    for warning in (result.get("warnings") or [])[:5]:
        print("warning: %s" % warning, file=sys.stderr)


def cmd_sim_backends(args):
    simulators = _simulators()
    info = {"backends": simulators.backends()}
    if args.probe:
        probes = {}
        for name in info["backends"]:
            try:
                probes[name] = simulators.probe(name)
            except Exception as exc:  # a broken target must not hide the table
                probes[name] = {"backend": name, "available": False, "detail": str(exc),
                                "target": "", "version": ""}
        info["probe"] = probes

    def human():
        for name, meta in info["backends"].items():
            print("%s  [%s]" % (name, meta.get("kind")))
            print("  binary : %s (env %s)"
                  % (meta.get("default_binary") or "(template)",
                     meta.get("binary_config")))
            if meta.get("modes"):
                print("  modes  : %s" % ", ".join(meta["modes"]))
            print("  notes  : %s" % meta.get("notes"))
            probe = (info.get("probe") or {}).get(name)
            if probe:
                state = "available" if probe.get("available") else "unavailable"
                detail = probe.get("version") or probe.get("detail") or ""
                print("  probe  : %s on %s %s"
                      % (state, probe.get("target") or "?", ("(%s)" % detail) if detail else ""))

    return _emit(info, getattr(args, "json", False), human)


def cmd_sim_run(args):
    simulators = _simulators()
    if not os.path.isfile(args.netlist):
        raise CliError("netlist not found: %s" % args.netlist,
                       hint="pass the path of a SPICE netlist file.")
    try:
        result = simulators.run(args.netlist, backend=args.backend, mode=args.mode,
                                timeout=args.timeout, includes=args.include,
                                run_id=args.run_id)
    except simulators.SimulatorError as exc:
        raise CliError("simulation could not start: %s" % exc,
                       hint="check `%s sim backends --probe` for what is installed." % PROG)
    code = 0 if result.get("ok") else 1
    return _emit(result, getattr(args, "json", False), lambda: _sim_summary(result)) or code


def _daemon_action(args):
    runtime = _runtime()
    action = args.action
    try:
        if action == "start":
            info = runtime.ensure_daemon()
        elif action == "stop":
            info = runtime.stop_daemon()
        elif action == "restart":
            runtime.stop_daemon()
            info = runtime.ensure_daemon()
        else:  # status
            info = runtime.status(autostart=False)
    except TypeError:  # tolerate an implementation without keyword arguments
        info = runtime.ensure_daemon() if action == "start" else runtime.status()
    except Exception as exc:
        raise CliError(
            "daemon %s failed: %s: %s" % (action, type(exc).__name__, exc),
            hint="check that the target is reachable, plus the daemon log and "
                 "`%s daemon status`." % PROG,
        )

    if getattr(args, "json", False):
        _print_json(info)
        return 0
    if action == "start":
        print("daemon ready." if not info.get("started") else
              "daemon started%s." % (" (pid=%s)" % info["pid"] if info.get("pid") else ""))
    elif action == "stop":
        print("daemon stopped.")
    elif action == "restart":
        print("daemon restarted.")
    else:
        _human_status(info if "session" in info or "daemon" in info else {"daemon": info})
    return 0


# --------------------------------------------------------------------------- #
# argument parsing
# --------------------------------------------------------------------------- #
def build_parser():
    parser = argparse.ArgumentParser(
        prog=PROG,
        description="pyaether-bridge: drive a resident pyAether session and query "
                    "the offline API catalog.",
    )
    sub = parser.add_subparsers(dest="command", metavar="<command>")

    p_status = sub.add_parser("status", help="show target, daemon and session status")
    p_status.add_argument("--json", action="store_true", help="print raw JSON")
    p_status.set_defaults(func=cmd_status)

    p_exec = sub.add_parser("exec", help="run Python code in the target pyAether session")
    source = p_exec.add_mutually_exclusive_group()
    source.add_argument("-c", "--code", help="code passed directly")
    source.add_argument("-f", "--file", help="read the code from a file")
    p_exec.add_argument("--timeout", type=float, default=120.0,
                        help="timeout in seconds (default 120)")
    p_exec.add_argument("--json", action="store_true", help="print raw JSON")
    p_exec.set_defaults(func=cmd_exec)

    p_api = sub.add_parser("api", help="offline PyAether API catalog")
    api_sub = p_api.add_subparsers(dest="api_command", metavar="<action>")

    p_build = api_sub.add_parser("build", help="build the catalog database from Sphinx docs/html")
    p_build.add_argument("--docs", help="docs/html directory (auto-detected by default)")
    p_build.add_argument("--out", help="output sqlite path (default data/catalog.sqlite in the repo)")
    p_build.add_argument("--jobs", type=int, default=1, help="worker processes (default 1)")
    p_build.add_argument("--json", action="store_true", help="print raw JSON")
    p_build.set_defaults(func=cmd_api_build)

    p_stats = api_sub.add_parser("stats", help="show catalog statistics")
    p_stats.add_argument("--db", help="catalog sqlite path")
    p_stats.add_argument("--json", action="store_true", help="print raw JSON")
    p_stats.set_defaults(func=cmd_api_stats)

    p_search = api_sub.add_parser("search", help="search API symbols")
    p_search.add_argument("query", help="search keyword")
    p_search.add_argument("--limit", type=int, default=20, help="maximum hits (default 20)")
    p_search.add_argument("--kind", help="only return this kind (e.g. function/class/method)")
    p_search.add_argument("--db", help="catalog sqlite path")
    p_search.add_argument("--json", action="store_true", help="print raw JSON")
    p_search.set_defaults(func=cmd_api_search)

    p_show = api_sub.add_parser("show", help="show the full documentation of one symbol")
    p_show.add_argument("symbol", help="symbol name, e.g. pyAether.emyInitDb")
    p_show.add_argument("--max-chars", type=int, default=4000, help="description truncation length")
    p_show.add_argument("--db", help="catalog sqlite path")
    p_show.add_argument("--json", action="store_true", help="print raw JSON")
    p_show.set_defaults(func=cmd_api_show)

    p_sync = api_sub.add_parser(
        "sync-live", help="merge dir(pyAether) symbols from the live session (needs daemon)")
    p_sync.add_argument("--db", help="catalog sqlite path")
    p_sync.add_argument("--timeout", type=float, default=300.0,
                        help="session execution timeout (default 300)")
    p_sync.add_argument("--json", action="store_true", help="print raw JSON")
    p_sync.set_defaults(func=cmd_api_sync_live)

    p_daemon = sub.add_parser("daemon", help="manage the host daemon")
    p_daemon.add_argument("action", nargs="?", default="status",
                          choices=["start", "stop", "status", "restart"])
    p_daemon.add_argument("--json", action="store_true", help="print raw JSON")
    p_daemon.set_defaults(func=cmd_daemon)

    p_sim = sub.add_parser("sim", help="run SPICE netlists on a switchable simulator")
    sim_sub = p_sim.add_subparsers(dest="sim_command", metavar="<operation>")

    p_sim_backends = sim_sub.add_parser("backends", help="list simulator backends")
    p_sim_backends.add_argument("--probe", action="store_true",
                                help="also check the target for each backend binary")
    p_sim_backends.add_argument("--json", action="store_true", help="print raw JSON")
    p_sim_backends.set_defaults(func=cmd_sim_backends)

    p_sim_run = sim_sub.add_parser("run", help="run one netlist and parse the results")
    p_sim_run.add_argument("netlist", help="path of the netlist to run")
    p_sim_run.add_argument("--backend", choices=["ngspice", "spectre", "custom"],
                           help="simulator backend (default PYAETHER_SIM_BACKEND)")
    p_sim_run.add_argument("--mode", help="Spectre engine/preset: aps, ax, mx, ...")
    p_sim_run.add_argument("--timeout", type=float, help="timeout in seconds (default 600)")
    p_sim_run.add_argument("--include", action="append", metavar="FILE",
                           help="extra file (model/Verilog-A include) staged next to the netlist; repeatable")
    p_sim_run.add_argument("--run-id", help="suffix for the run directory name")
    p_sim_run.add_argument("--json", action="store_true", help="print raw JSON")
    p_sim_run.set_defaults(func=cmd_sim_run)

    p_version = sub.add_parser("version", help="show the version")
    p_version.add_argument("--json", action="store_true", help="print raw JSON")
    p_version.set_defaults(func=cmd_version)

    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(list(sys.argv[1:] if argv is None else argv))
    if not getattr(args, "command", None):
        parser.print_help()
        return 0
    if args.command in ("api", "sim") and not getattr(
            args, "%s_command" % args.command, None):
        for action in parser._subparsers._group_actions:  # pragma: no cover - help text only
            choices = getattr(action, "choices", {})
            if args.command in choices:
                choices[args.command].print_help()
                break
        return 0
    try:
        return args.func(args)
    except CliError as exc:
        print("error: %s" % exc, file=sys.stderr)
        if exc.hint:
            print("next: %s" % exc.hint, file=sys.stderr)
        return 1
    except KeyboardInterrupt:  # pragma: no cover
        print("interrupted.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
