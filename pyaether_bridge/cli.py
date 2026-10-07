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


def _layout():
    return _load("layout")


def _dsh():
    return _load("dsh")


def _schematic():
    return _load("schematic")


def cmd_sch_snapshot(args):
    sch = _schematic()
    try:
        result = sch.snapshot(args.library, args.cell, args.view, timeout=args.timeout)
    except sch.SchematicError as exc:
        raise CliError(str(exc))
    if args.out and result.get("ok"):
        with open(args.out, "w", encoding="utf-8") as handle:
            json.dump(result["data"]["snapshot"], handle, ensure_ascii=False, indent=1)
        result["metadata"]["written"] = os.path.abspath(args.out)
    if getattr(args, "json", False):
        _print_json(result)
    else:
        print("status : %s" % result["status"])
        print("design : %s/%s/%s" % (args.library, args.cell, args.view))
        counts = result["data"].get("counts") or {}
        if counts:
            print("counts : instances=%s nets=%s terminals=%s"
                  % (counts.get("instances"), counts.get("nets"),
                     counts.get("terminals")))
        if args.out:
            print("written: %s" % os.path.abspath(args.out))
        for error in result["errors"]:
            print("error  : %s" % error, file=sys.stderr)
    return 0 if result.get("ok") else 1


def cmd_sch_build(args):
    sch = _schematic()
    try:
        result = sch.build(args.spec, library=args.library, cell=args.cell,
                           view=args.view, timeout=args.timeout)
    except sch.SchematicError as exc:
        raise CliError(str(exc),
                       hint="run `%s sch snapshot` or check the spec's source block."
                            % PROG)
    if getattr(args, "json", False):
        _print_json(result)
    else:
        print("status : %s" % result["status"])
        created = result["data"].get("created") or {}
        if created:
            print("created: instances=%s nets=%s inst_terms=%s terminals=%s"
                  % (created.get("instances"), created.get("nets"),
                     created.get("inst_terms"), created.get("terminals")))
        print("design : %s" % (result["data"].get("design") or ""))
        if result["metadata"].get("auto_placed"):
            print("note   : no coordinates in the spec, instances were spread on a grid")
        for problem in (result["data"].get("problems") or [])[:8]:
            print("problem: %s" % problem, file=sys.stderr)
        for error in result["errors"][:8]:
            print("error  : %s" % error, file=sys.stderr)
    return 0 if result.get("ok") else 1


def cmd_sch_netlist(args):
    sch = _schematic()
    try:
        text = sch.netlist(args.spec, subckt=args.subckt, library=args.library,
                           cell=args.cell, view=args.view)
    except sch.SchematicError as exc:
        raise CliError(str(exc))
    if args.out:
        with open(args.out, "w", encoding="utf-8") as handle:
            handle.write(text)
        if not getattr(args, "json", False):
            print("written: %s" % os.path.abspath(args.out))
            return 0
    if getattr(args, "json", False):
        _print_json({"ok": True, "netlist": text})
    else:
        sys.stdout.write(text)
    return 0


def cmd_sch_roundtrip(args):
    sch = _schematic()
    try:
        result = sch.roundtrip(args.spec, timeout=args.timeout)
    except sch.SchematicError as exc:
        raise CliError(str(exc))

    def human():
        data = result["data"]
        print("status  : %s" % result["status"])
        print("created : %s" % (data.get("created") or {}))
        print("readback: %s" % (data.get("read_back") or {}))
        print("expected: %s" % (data.get("expected") or {}))
        print("missing connections: %s" % data.get("missing_connections_total"))
        print("extra connections  : %s" % data.get("unexpected_connections_total"))
        for error in (result.get("errors") or [])[:6]:
            print("error   : %s" % error, file=sys.stderr)
        for problem in (data.get("problems") or [])[:6]:
            print("problem : %s" % problem, file=sys.stderr)
    _emit(result, getattr(args, "json", False), human)
    return 0 if result.get("ok") else 1


# --------------------------------------------------------------------------- #
# output helpers
# --------------------------------------------------------------------------- #
_OUTPUT_DEBUG = False


def _add_output_flags(parser):
    """Attach the two output flags every leaf subcommand shares."""
    parser.add_argument("--json", action="store_true", help="print raw JSON")
    parser.add_argument("--debug", action="store_true",
                        help="keep indentation and execution diagnostics in --json")


def _print_json(payload):
    print(_load("protocol").dumps(payload, debug=_OUTPUT_DEBUG))


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
    try:
        docs = args.docs or config.resolve_docs_dir()
    except config.ConfigError as exc:
        raise CliError(str(exc),
                       hint="pin `docs_dir` in the profile, pass `--docs DIR`, or "
                            "set PYAETHER_DOCS_DIR.")
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
# profiles
# --------------------------------------------------------------------------- #
PROFILE_KEYS = ("transport", "container", "ssh_host", "ssh_port", "python",
                "remote_dir", "license_server", "aether_version", "sim_target",
                "sim_backend", "sim_ssh_host", "sim_workdir", "sim_timeout",
                "layout_workdir", "sch_workdir")


def _active_profile():
    """(name, source, settings) for the profile this process resolved."""
    config = _config()
    settings = {}
    name = getattr(config, "PROFILE", None)
    if name:
        settings = dict(config.known_profiles().get(name) or {})
    return name, getattr(config, "PROFILE_SOURCE", ""), settings


def _resolved_settings():
    """Effective values, after env > active profile > config > default.

    Read off the config module rather than re-resolving each key, so what is
    printed is what the bridge will actually use -- including the profile-scoped
    scratch paths.
    """
    config = _config()
    values = {}
    for key in PROFILE_KEYS:
        value = getattr(config, key.upper(), "")
        values[key] = "" if value is None else str(value)
    return values


def cmd_profile(args):
    config = _config()
    action = getattr(args, "profile_command", None) or "show"
    name, source, settings = _active_profile()

    if action == "list":
        profiles = config.known_profiles()
        payload = {"active": name, "source": source,
                   "profiles": sorted(profiles)}

        def human():
            if not profiles:
                print("no profiles defined in %s" % (config.DATA_DIR / "config.json"))
                print("add a \"profiles\" object with named settings, then "
                      "`%s profile bind <name>`" % PROG)
                return
            for profile_name in sorted(profiles):
                mark = "*" if profile_name == name else " "
                keys = ", ".join(sorted(profiles[profile_name]))
                print("%s %-16s %s" % (mark, profile_name, keys))
            if name:
                print("\nactive: %s (%s)" % (name, source))
        return _emit(payload, getattr(args, "json", False), human)

    if action == "bind":
        target = getattr(args, "name", None)
        if not target:
            raise CliError("profile bind needs a profile name",
                           hint="run `%s profile list` to see the names." % PROG)
        profiles = config.known_profiles()
        if target not in profiles:
            raise CliError("unknown profile %r" % target,
                           hint="known profiles: %s" % (", ".join(sorted(profiles)) or "(none)"))
        path = os.path.join(os.getcwd(), config.PROFILE_BINDING_FILENAME)
        try:
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(target + "\n")
            written = config.find_binding(os.getcwd())
        except OSError as exc:
            raise CliError("cannot write %s: %s" % (path, exc))
        payload = {"bound": target, "path": str(written or path)}
        return _emit(payload, getattr(args, "json", False),
                     lambda: print("bound %s to %s" % (target, path)))

    if action == "clear":
        path = config.find_binding(os.getcwd())
        if path is None:
            return _emit({"cleared": None}, getattr(args, "json", False),
                         lambda: print("no profile binding in this directory tree"))
        try:
            os.unlink(str(path))
        except OSError as exc:
            raise CliError("cannot remove %s: %s" % (path, exc))
        return _emit({"cleared": str(path)}, getattr(args, "json", False),
                     lambda: print("cleared %s" % path))

    if action == "verify":
        from . import daemon as daemon_module

        try:
            report = daemon_module.identity_report()
        except Exception as exc:  # a probe that cannot answer is a failed check, not a crash
            raise CliError("identity check could not run: %s" % exc)
        state = _runtime().status(autostart=False)
        payload = {
            "active": name, "source": source,
            "confirmed": config.profile_is_confirmed(),
            "require_explicit": config.REQUIRE_EXPLICIT_PROFILE,
            "identity": report,
            "target": state.get("target"),
            "daemon": state.get("daemon"),
        }

        def human_verify():
            print("profile     : %s (%s)%s"
                  % (name or "(none)", source,
                     " [confirmed]" if payload["confirmed"] else ""))
            print("fingerprint : %s" % report["fingerprint"])
            target = payload.get("target") or {}
            if target.get("daemon_fingerprint"):
                print("daemon      : %s (%s)" % (
                    target["daemon_fingerprint"],
                    "serves this target" if target.get("matches")
                    else "STALE -- serves another target; run `%s daemon restart`" % PROG))
            else:
                print("daemon      : not running")
            if not report["identity_configured"]:
                print("\nno expected_* values are configured, so nothing is asserted.")
                print("add expected_hostname / expected_container / expected_image /")
                print("expected_aether_version / expected_license_server to the profile.")
                return
            print("\nidentity check:")
            for check in report["checks"]:
                if check["state"] == "unconfigured":
                    continue
                print("  %-18s %-13s expected=%s observed=%s"
                      % (check["field"], check["state"],
                         check["expected"] or "-", check["observed"] or "-"))
            if report["error"]:
                print("  probe error: %s" % report["error"])
            if report["ok"]:
                print("\nidentity OK")
            else:
                print("\n%s" % daemon_module.identity_error_text(report))

        _emit(payload, getattr(args, "json", False), human_verify)
        return 0 if (report["ok"] or not report["identity_configured"]) else 1

    payload = {"active": name, "source": source, "profile": settings,
               "resolved": _resolved_settings(),
               "binding_file": str(config.find_binding() or ""),
               "runtime_dir": str(config.RUNTIME_DIR),
               "confirmed": config.profile_is_confirmed(),
               "require_explicit": config.REQUIRE_EXPLICIT_PROFILE,
               "catalog_db": str(config.CATALOG_DB),
               "docs_dir": str(config.find_docs_dir() or ""),
               "fingerprint": config.TARGET_FINGERPRINT,
               "expected_identity": {key: value
                                     for key, value in config.EXPECTED_IDENTITY.items()
                                     if value}}

    def human_show():
        if name:
            print("active profile: %s (%s)%s"
                  % (name, source, " [confirmed]" if payload["confirmed"] else ""))
            for key in sorted(settings):
                print("  %-16s %s" % (key, settings[key]))
        else:
            print("active profile: (none) -- %s" % source)
        print("\nresolved settings:")
        for key in PROFILE_KEYS:
            value = payload["resolved"].get(key) or ""
            if value:
                print("  %-16s %s" % (key, value))
        print("\ntarget fingerprint: %s" % payload["fingerprint"])
        print("catalog           : %s" % payload["catalog_db"])
        print("docs              : %s" % (payload["docs_dir"] or "(not found)"))
        print("daemon dir        : %s" % payload["runtime_dir"])
        if payload["expected_identity"]:
            print("expected identity :")
            for key in sorted(payload["expected_identity"]):
                print("  %-18s %s" % (key, payload["expected_identity"][key]))
        else:
            print("expected identity : (none -- set one, then `%s profile verify`)" % PROG)
        if payload["binding_file"]:
            print("binding           : %s" % payload["binding_file"])
        if payload["require_explicit"] and not payload["confirmed"]:
            print("\nwarning: PYAETHER_REQUIRE_EXPLICIT_PROFILE is set, but this profile "
                  "came from %s,\n         which selects a target without confirming it. "
                  "Export PYAETHER_PROFILE=<name>." % source)
    return _emit(payload, getattr(args, "json", False), human_show)


# --------------------------------------------------------------------------- #
# simulators
# --------------------------------------------------------------------------- #
# --------------------------------------------------------------------------- #
# layout (KLayout)
# --------------------------------------------------------------------------- #
def cmd_dsh(args):
    module = _dsh()
    action = getattr(args, "dsh_command", None) or "status"
    workspace = getattr(args, "workspace", None)
    home = getattr(args, "dsh_home", None)

    if action == "install":
        try:
            report = module.install(workspace, home,
                                    dry_run=getattr(args, "dry_run", False))
        except module.DshError as exc:
            raise CliError(str(exc), hint="check `%s dsh status` first." % PROG)

        def human_install():
            if report["dry_run"]:
                print("dry run -- nothing written")
                print("workspace  : %s" % report["workspace"])
                print("skill      : %s" % report["skill_file"])
            elif report["unchanged"]:
                print("skill already up to date: %s" % report["skill_file"])
            else:
                print("installed skill: %s" % report["skill_file"])
            print("skill root : %s" % report["skills_root"])
            if report["skill_root_enabled_in"]:
                print("scanned by : %s" % ", ".join(report["skill_root_enabled_in"]))
            elif report["skill_root_disabled_in"]:
                print("NOT scanned: the %s row is disabled in %s"
                      % (module.SKILL_PROVIDER_ROW,
                         ", ".join(report["skill_root_disabled_in"])))
            else:
                print("NOT scanned: no profile lists this skills root")
            if report.get("next"):
                print("\n%s" % report["next"])
            print("\nregistering mcp__%s__* is the bundle's job (%s), not this "
                  "command's; see docs/DEEPSEEK-HARNESS.md."
                  % (module.SERVER_NAME, module.BUNDLE_NAME))
        _emit(report, getattr(args, "json", False), human_install)
        return 0

    if action == "uninstall":
        try:
            report = module.uninstall(workspace, home)
        except module.DshError as exc:
            raise CliError(str(exc))
        return _emit(report, getattr(args, "json", False), lambda: (
            print("skill removed: %s" % report["skill_removed"]),
            print("skill        : %s" % report["skill_file"]),
            print("note         : the bundle registered in the profile is untouched")))

    # default: status
    try:
        report = module.status(workspace, home)
    except module.DshError as exc:
        raise CliError(str(exc))

    def human_status():
        print("dsh home   : %s" % report["dsh_home"])
        print("workspace  : %s%s" % (report["workspace"],
                                     "" if report["workspace_marker"]
                                     else " (no %s marker)" % module.WORKSPACE_MARKER))
        print("skill      : %s%s" % (report["skill_file"],
                                     "" if report["skill_installed"]
                                     else " (not installed)"))
        if report["skill_installed"]:
            print("in sync    : %s" % ("yes" if report["skill_in_sync"]
                                       else "NO -- re-run `%s dsh install`" % PROG))
        print("skill root : %s" % report["skills_root"])
        if report["skill_root_enabled_in"]:
            print("scanned by : %s" % ", ".join(report["skill_root_enabled_in"]))
        elif report["skill_root_disabled_in"]:
            print("NOT scanned: the %s row is disabled in %s"
                  % (module.SKILL_PROVIDER_ROW,
                     ", ".join(report["skill_root_disabled_in"])))
        else:
            print("NOT scanned: no profile lists this skills root")
        print("bundle     : %s%s" % (report["bundle_dir"],
                                     "" if report["bundle_present"] else " (missing)"))
        if report["bundle_linked_in"]:
            print("registered : %s -> mcp__%s__*"
                  % (", ".join(report["bundle_linked_in"]), report["server_name"]))
        else:
            print("registered : (no profile declares %s yet)" % report["bundle_name"])
    return _emit(report, getattr(args, "json", False), human_status)


def _layout_summary(result):
    """Human-readable rendering of one layout operation result."""
    operation = result.get("operation") or "?"
    print("status   : %s (ok=%s)" % (result.get("status"), result.get("ok")))
    print("operation: %s" % operation)
    metadata = result.get("metadata") or {}
    for key in ("engine", "target", "artifact", "command"):
        if metadata.get(key):
            print("%-9s: %s" % (key, metadata[key]))
    data = result.get("data") or {}
    if operation == "gen":
        print("top      : %s" % data.get("top"))
        print("counts   : %s" % data.get("counts"))
        if data.get("bbox_um"):
            print("bbox_um  : %s" % ", ".join("%.4g" % v for v in data["bbox_um"]))
        print("layers   : %s" % ", ".join(data.get("layers") or []))
    elif operation == "info":
        print("top cells: %s" % ", ".join(data.get("top_cells") or []))
        print("dbu      : %s" % data.get("dbu"))
        for cell in data.get("cells") or []:
            print("  cell %-20s shapes=%-5s instances=%-4s %s"
                  % (cell.get("name"), cell.get("shapes"), cell.get("instances"),
                     cell.get("shapes_by_layer")))
        for layer in data.get("layers") or []:
            print("  layer %d/%d %s" % (layer["layer"], layer["datatype"],
                                        layer.get("name") or ""))
    elif operation == "drc":
        print("clean    : %s" % data.get("clean"))
        print("violations: %s" % data.get("violations"))
        for rule in data.get("rules") or []:
            line = "  %-28s %-10s %s" % (rule.get("name"), rule.get("check"),
                                         rule.get("violations"))
            if rule.get("error"):
                line += "  ERROR: %s" % rule["error"][:120]
            print(line)
            for marker in (rule.get("markers") or [])[:3]:
                print("      at %s" % ", ".join("%.4g" % v for v in marker["bbox_um"]))
    elif operation == "boolean":
        print("op       : %s" % data.get("op"))
        print("polygons : %s" % data.get("polygons"))
        if data.get("bbox_um"):
            print("bbox_um  : %s" % ", ".join("%.4g" % v for v in data["bbox_um"]))
    timings = metadata.get("timings") or {}
    if timings:
        print("timings  : %s" % ", ".join("%s=%s" % item for item in timings.items()))
    for error in result.get("errors") or []:
        print("error    : %s" % error, file=sys.stderr)
    for warning in (result.get("warnings") or [])[:5]:
        print("warning  : %s" % warning, file=sys.stderr)


def _load_json_argument(value, what):
    """Accept inline JSON or a path to a JSON file."""
    if value is None:
        return None
    text = value.strip()
    if text.startswith(("{", "[")):
        try:
            return json.loads(text)
        except ValueError as exc:
            raise CliError("%s is not valid JSON: %s" % (what, exc))
    if not os.path.isfile(text):
        raise CliError("%s file not found: %s" % (what, text))
    try:
        with open(text, encoding="utf-8") as handle:
            return json.load(handle)
    except ValueError as exc:
        raise CliError("%s file %s is not valid JSON: %s" % (what, text, exc))


def cmd_layout_probe(args):
    layout = _layout()
    info = {"engine": layout.operations(), "probe": layout.probe()}

    def human():
        engine = info["engine"]
        print("engine   : %s (%s -> %s)" % (engine["engine"], engine["binary_config"],
                                            engine["default_binary"]))
        print("invocation: %s" % engine["invocation"])
        for name, text in engine["operations"].items():
            print("  %-8s %s" % (name, text))
        probe = info["probe"]
        print("target   : %s (%s)" % (probe["target"], probe["target_reason"]))
        print("available: %s %s" % (probe["available"],
                                    probe["version"] or probe["detail"]))
    return _emit(info, getattr(args, "json", False), human)


def cmd_layout_gen(args):
    layout = _layout()
    spec = _load_json_argument(args.spec, "spec")
    try:
        result = layout.generate(spec, args.output, timeout=args.timeout)
    except layout.LayoutError as exc:
        raise CliError("layout generation could not start: %s" % exc,
                       hint="run `%s layout probe` to check KLayout." % PROG)
    return _emit(result, getattr(args, "json", False),
                 lambda: _layout_summary(result)) or (0 if result.get("ok") else 1)


def cmd_layout_info(args):
    layout = _layout()
    layers = _load_json_argument(args.layers, "layers")
    try:
        result = layout.info(args.file, layers=layers, timeout=args.timeout)
    except layout.LayoutError as exc:
        raise CliError(str(exc))
    return _emit(result, getattr(args, "json", False),
                 lambda: _layout_summary(result)) or (0 if result.get("ok") else 1)


def cmd_layout_drc(args):
    layout = _layout()
    rules = _load_json_argument(args.rules, "rules")
    layers = _load_json_argument(args.layers, "layers")
    try:
        result = layout.drc(args.file, rules, layers=layers, timeout=args.timeout)
    except layout.LayoutError as exc:
        raise CliError(str(exc))
    _emit(result, getattr(args, "json", False), lambda: _layout_summary(result))
    if not result.get("ok"):
        return 1
    # A check that reports violations must not exit 0: that is how a dirty
    # layout would slip through CI. 3 is reserved for "ran, found violations"
    # so it stays distinguishable from "could not run" (1).
    if (result.get("data") or {}).get("clean") is False and not args.exit_zero:
        return 3
    return 0


def cmd_layout_boolean(args):
    layout = _layout()
    layers = _load_json_argument(args.layers, "layers")
    try:
        result = layout.boolean(args.op, args.a, b=args.b, out_layer=args.out_layer,
                                output=args.output, source=args.source,
                                value=args.value, timeout=args.timeout)
    except layout.LayoutError as exc:
        raise CliError(str(exc))
    return _emit(result, getattr(args, "json", False),
                 lambda: _layout_summary(result)) or (0 if result.get("ok") else 1)


def cmd_layout_tools(args):
    layout = _layout()
    tools = layout.stream_tools()
    payload = {"engine": "klayout", "stream_tools": tools,
               "invocation": "klayout's standalone stream tools (not on PATH)"}

    def human():
        if not tools:
            print("no KLayout stream tools found; set PYAETHER_KLAYOUT_BUDDY_DIR to the "
                  "directory holding them (on macOS: KLayout.app/Contents/Buddy)")
            return
        for key, path in sorted(tools.items()):
            print("%-10s %s" % (key, path))
    return _emit(payload, getattr(args, "json", False), human)


def cmd_layout_convert(args):
    layout = _layout()
    try:
        result = layout.convert(args.source, args.output, tool=args.tool,
                                timeout=args.timeout)
    except layout.LayoutError as exc:
        raise CliError(str(exc))
    return _emit(result, getattr(args, "json", False),
                 lambda: _layout_summary(result)) or (0 if result.get("ok") else 1)


def cmd_layout_compare(args):
    layout = _layout()
    try:
        result = layout.compare(args.a, args.b, tool=args.tool, timeout=args.timeout)
    except layout.LayoutError as exc:
        raise CliError(str(exc))
    if getattr(args, "json", False):
        _print_json(result)
    else:
        code = (result.get("data") or {}).get("returncode")
        identical = (result.get("data") or {}).get("identical")
        print("tool      : %s" % args.tool)
        print("identical : %s" % identical)
        print("exit code : %s (0 = identical)" % code)
        if args.tool == "xor":
            print("output    : %s" % result["metadata"].get("output"))
        if not result.get("ok") and result.get("errors"):
            for error in result["errors"]:
                print("error     : %s" % error, file=sys.stderr)
            return 1
    # Mirror strmcmp's own contract so the command can be used in a script.
    return 0 if (result.get("data") or {}).get("identical") else 1


def cmd_layout_deck(args):
    layout = _layout()
    try:
        result = layout.deck(args.script, source=args.source, top=args.top,
                             timeout=args.timeout, extra_args=args.define)
    except layout.LayoutError as exc:
        raise CliError(str(exc))
    if getattr(args, "json", False):
        _print_json(result)
    else:
        print("status   : %s (ok=%s)" % (result.get("status"), result.get("ok")))
        print("deck     : %s (%s)" % (result["metadata"].get("deck"),
                                      result["metadata"].get("deck_kind")))
        print("command  : %s" % result["metadata"].get("command"))
        print("exit code: %s" % (result.get("data") or {}).get("returncode"))
        for path in (result.get("data") or {}).get("report_artifacts") or []:
            print("report   : %s" % path)
        out = ((result.get("data") or {}).get("stdout") or "").strip()
        if out:
            print("--- output ---")
            print(out[-4000:])
        for error in result.get("errors") or []:
            print("error    : %s" % error, file=sys.stderr)
    if not result.get("ok"):
        return 1
    return 0


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
    _add_output_flags(p_status)
    p_status.set_defaults(func=cmd_status)

    p_exec = sub.add_parser("exec", help="run Python code in the target pyAether session")
    source = p_exec.add_mutually_exclusive_group()
    source.add_argument("-c", "--code", help="code passed directly")
    source.add_argument("-f", "--file", help="read the code from a file")
    p_exec.add_argument("--timeout", type=float, default=120.0,
                        help="timeout in seconds (default 120)")
    _add_output_flags(p_exec)
    p_exec.set_defaults(func=cmd_exec)

    p_api = sub.add_parser("api", help="offline PyAether API catalog")
    api_sub = p_api.add_subparsers(dest="api_command", metavar="<action>")

    p_build = api_sub.add_parser("build", help="build the catalog database from Sphinx docs/html")
    p_build.add_argument("--docs", help="docs/html directory (auto-detected by default)")
    p_build.add_argument("--out", help="output sqlite path (default data/catalog.sqlite in the repo)")
    p_build.add_argument("--jobs", type=int, default=1, help="worker processes (default 1)")
    _add_output_flags(p_build)
    p_build.set_defaults(func=cmd_api_build)

    p_stats = api_sub.add_parser("stats", help="show catalog statistics")
    p_stats.add_argument("--db", help="catalog sqlite path")
    _add_output_flags(p_stats)
    p_stats.set_defaults(func=cmd_api_stats)

    p_search = api_sub.add_parser("search", help="search API symbols")
    p_search.add_argument("query", help="search keyword")
    p_search.add_argument("--limit", type=int, default=20, help="maximum hits (default 20)")
    p_search.add_argument("--kind", help="only return this kind (e.g. function/class/method)")
    p_search.add_argument("--db", help="catalog sqlite path")
    _add_output_flags(p_search)
    p_search.set_defaults(func=cmd_api_search)

    p_show = api_sub.add_parser("show", help="show the full documentation of one symbol")
    p_show.add_argument("symbol", help="symbol name, e.g. pyAether.emyInitDb")
    p_show.add_argument("--max-chars", type=int, default=4000, help="description truncation length")
    p_show.add_argument("--db", help="catalog sqlite path")
    _add_output_flags(p_show)
    p_show.set_defaults(func=cmd_api_show)

    p_sync = api_sub.add_parser(
        "sync-live", help="merge dir(pyAether) symbols from the live session (needs daemon)")
    p_sync.add_argument("--db", help="catalog sqlite path")
    p_sync.add_argument("--timeout", type=float, default=300.0,
                        help="session execution timeout (default 300)")
    _add_output_flags(p_sync)
    p_sync.set_defaults(func=cmd_api_sync_live)

    p_daemon = sub.add_parser("daemon", help="manage the host daemon")
    p_daemon.add_argument("action", nargs="?", default="status",
                          choices=["start", "stop", "status", "restart"])
    _add_output_flags(p_daemon)
    p_daemon.set_defaults(func=cmd_daemon)

    p_profile = sub.add_parser("profile",
                               help="named target profiles (one machine, several targets)")
    profile_sub = p_profile.add_subparsers(dest="profile_command", metavar="<operation>")

    p_profile_list = profile_sub.add_parser("list", help="list profiles defined in the config file")
    _add_output_flags(p_profile_list)
    p_profile_list.set_defaults(func=cmd_profile)

    p_profile_show = profile_sub.add_parser("show", help="show the active profile and resolved settings")
    _add_output_flags(p_profile_show)
    p_profile_show.set_defaults(func=cmd_profile)

    p_profile_bind = profile_sub.add_parser("bind", help="bind the current directory to a profile")
    p_profile_bind.add_argument("name", help="profile name defined in the config file")
    _add_output_flags(p_profile_bind)
    p_profile_bind.set_defaults(func=cmd_profile)

    p_profile_clear = profile_sub.add_parser("clear", help="remove the binding file found from here")
    _add_output_flags(p_profile_clear)
    p_profile_clear.set_defaults(func=cmd_profile)

    p_profile_verify = profile_sub.add_parser(
        "verify", help="check the target against the profile's expected_* values")
    _add_output_flags(p_profile_verify)
    p_profile_verify.set_defaults(func=cmd_profile)

    p_sim = sub.add_parser("sim", help="run SPICE netlists on a switchable simulator")
    sim_sub = p_sim.add_subparsers(dest="sim_command", metavar="<operation>")

    p_sim_backends = sim_sub.add_parser("backends", help="list simulator backends")
    p_sim_backends.add_argument("--probe", action="store_true",
                                help="also check the target for each backend binary")
    _add_output_flags(p_sim_backends)
    p_sim_backends.set_defaults(func=cmd_sim_backends)

    p_sim_run = sim_sub.add_parser("run", help="run one netlist and parse the results")
    p_sim_run.add_argument("netlist", help="path of the netlist to run")
    p_sim_run.add_argument("--backend", choices=["ngspice", "spectre", "alps", "custom"],
                           help="simulator backend (default PYAETHER_SIM_BACKEND)")
    p_sim_run.add_argument("--mode", help="Spectre engine/preset: aps, ax, mx, ...")
    p_sim_run.add_argument("--timeout", type=float, help="timeout in seconds (default 600)")
    p_sim_run.add_argument("--include", action="append", metavar="FILE",
                           help="extra file (model/Verilog-A include) staged next to the netlist; repeatable")
    p_sim_run.add_argument("--run-id", help="suffix for the run directory name")
    _add_output_flags(p_sim_run)
    p_sim_run.set_defaults(func=cmd_sim_run)

    p_layout = sub.add_parser("layout", help="generate and check layouts with KLayout")
    layout_sub = p_layout.add_subparsers(dest="layout_command", metavar="<operation>")

    p_lay_probe = layout_sub.add_parser("probe", help="check that KLayout is available")
    _add_output_flags(p_lay_probe)
    p_lay_probe.set_defaults(func=cmd_layout_probe)

    p_lay_gen = layout_sub.add_parser("gen", help="build a GDS2/OASIS layout from a spec")
    p_lay_gen.add_argument("spec", help="spec as inline JSON or a path to a .json file")
    p_lay_gen.add_argument("-o", "--output", help="output layout path (.gds / .oas)")
    p_lay_gen.add_argument("--timeout", type=float, help="timeout in seconds (default 600)")
    _add_output_flags(p_lay_gen)
    p_lay_gen.set_defaults(func=cmd_layout_gen)

    p_lay_info = layout_sub.add_parser("info", help="read a layout: cells, layers, shapes, extents")
    p_lay_info.add_argument("file", help="layout file to read")
    p_lay_info.add_argument("--layers", help="layer name map as JSON, e.g. '{\"m1\":[1,0]}'")
    p_lay_info.add_argument("--timeout", type=float, help="timeout in seconds")
    _add_output_flags(p_lay_info)
    p_lay_info.set_defaults(func=cmd_layout_info)

    p_lay_drc = layout_sub.add_parser("drc", help="run width/space/notch/enclosing/area checks")
    p_lay_drc.add_argument("file", help="layout file to check")
    p_lay_drc.add_argument("--rules", required=True,
                           help="rules as inline JSON or a path to a .json file")
    p_lay_drc.add_argument("--layers", help="layer name map as JSON")
    p_lay_drc.add_argument("--timeout", type=float, help="timeout in seconds")
    p_lay_drc.add_argument("--exit-zero", action="store_true",
                           help="exit 0 even when violations are found (for scripts that "
                                "want to inspect them; default is 3)")
    _add_output_flags(p_lay_drc)
    p_lay_drc.set_defaults(func=cmd_layout_drc)

    p_lay_bool = layout_sub.add_parser("boolean", help="layer algebra between two layers")
    p_lay_bool.add_argument("op", choices=["merge", "and", "not", "xor", "size", "grow", "shrink"])
    p_lay_bool.add_argument("--a", required=True, help="first layer (name or [layer,datatype])")
    p_lay_bool.add_argument("--b", help="second layer (and/not/xor)")
    p_lay_bool.add_argument("--value", type=float, help="size in microns (size/grow/shrink)")
    p_lay_bool.add_argument("--out-layer", required=True, help="layer for the result")
    p_lay_bool.add_argument("--output", help="output layout path")
    p_lay_bool.add_argument("--source", help="input layout; omit to build from the spec")
    p_lay_bool.add_argument("--layers", help="layer name map as JSON")
    p_lay_bool.add_argument("--timeout", type=float, help="timeout in seconds")
    _add_output_flags(p_lay_bool)
    p_lay_bool.set_defaults(func=cmd_layout_boolean)

    p_lay_tools = layout_sub.add_parser(
        "tools", help="list KLayout's standalone stream tools on the target")
    _add_output_flags(p_lay_tools)
    p_lay_tools.set_defaults(func=cmd_layout_tools)

    p_lay_conv = layout_sub.add_parser(
        "convert", help="convert or clip a layout with KLayout's stream tools")
    p_lay_conv.add_argument("source", help="input layout file")
    p_lay_conv.add_argument("output", help="output layout file")
    p_lay_conv.add_argument("--tool", help="tool key (oasis/gds/cif/dxf/txt/mag/lstr/clip)")
    p_lay_conv.add_argument("--timeout", type=float, help="timeout in seconds")
    _add_output_flags(p_lay_conv)
    p_lay_conv.set_defaults(func=cmd_layout_convert)

    p_lay_cmp = layout_sub.add_parser(
        "compare", help="compare two layouts (exit code 0 = identical)")
    p_lay_cmp.add_argument("a", help="first layout")
    p_lay_cmp.add_argument("b", help="second layout")
    p_lay_cmp.add_argument("--tool", default="compare", choices=["compare", "xor"],
                           help="compare with strmcmp (default) or XOR with strmxor")
    p_lay_cmp.add_argument("--timeout", type=float, help="timeout in seconds")
    _add_output_flags(p_lay_cmp)
    p_lay_cmp.set_defaults(func=cmd_layout_compare)

    p_lay_deck = layout_sub.add_parser(
        "deck", help="run a KLayout rule deck (.drc / .lvs) with the real engine")
    p_lay_deck.add_argument("script", help="rule deck (.drc or .lvs)")
    p_lay_deck.add_argument("--source", help="layout passed to the deck as $source")
    p_lay_deck.add_argument("--top", help="top cell passed to the deck")
    p_lay_deck.add_argument("--define", action="append", metavar="NAME=VALUE",
                            help="extra -rd variable; repeatable")
    p_lay_deck.add_argument("--timeout", type=float, help="timeout in seconds")
    _add_output_flags(p_lay_deck)
    p_lay_deck.set_defaults(func=cmd_layout_deck)

    p_dsh = sub.add_parser(
        "dsh", help="workspace skill, and bundle wiring, for DeepSeek Harness")
    dsh_sub = p_dsh.add_subparsers(dest="dsh_command", metavar="<operation>")

    def _dsh_common(parser):
        parser.add_argument("--workspace",
                            help="workspace root holding .dsh/skills (default: the "
                                 "nearest .dsh-workspace ancestor of the cwd)")
        parser.add_argument("--dsh-home",
                            help="override $DSH_HOME (default ~/.dsh); read-only, "
                                 "used to report the wiring")
        _add_output_flags(parser)

    p_dsh_status = dsh_sub.add_parser(
        "status", help="is the workspace skill installed, and does a profile scan it")
    _dsh_common(p_dsh_status)
    p_dsh_status.set_defaults(func=cmd_dsh)

    p_dsh_install = dsh_sub.add_parser(
        "install", help="install the skill into <workspace>/.dsh/skills")
    _dsh_common(p_dsh_install)
    p_dsh_install.add_argument("--dry-run", action="store_true",
                               help="show what would be written, change nothing")
    p_dsh_install.set_defaults(func=cmd_dsh)

    p_dsh_uninstall = dsh_sub.add_parser("uninstall", help="remove the workspace skill")
    _dsh_common(p_dsh_uninstall)
    p_dsh_uninstall.set_defaults(func=cmd_dsh)

    p_sch = sub.add_parser("sch", help="schematic round trip (canvas <-> Aether)")
    sch_sub = p_sch.add_subparsers(dest="sch_command", metavar="<operation>")

    p_sch_snap = sch_sub.add_parser("snapshot", help="read a live schematic into a snapshot")
    p_sch_snap.add_argument("library", help="library name, e.g. myLib")
    p_sch_snap.add_argument("cell", help="cell name")
    p_sch_snap.add_argument("--view", default="schematic")
    p_sch_snap.add_argument("-o", "--out", help="write the snapshot JSON here")
    p_sch_snap.add_argument("--timeout", type=float, default=300.0)
    _add_output_flags(p_sch_snap)
    p_sch_snap.set_defaults(func=cmd_sch_snapshot)

    p_sch_build = sch_sub.add_parser("build", help="create a schematic from a snapshot/canvas")
    p_sch_build.add_argument("spec", help="snapshot or canvas document (JSON file or inline)")
    p_sch_build.add_argument("--library", help="target library (overrides the spec)")
    p_sch_build.add_argument("--cell", help="target cell (overrides the spec)")
    p_sch_build.add_argument("--view", default="schematic")
    p_sch_build.add_argument("--timeout", type=float, default=600.0)
    _add_output_flags(p_sch_build)
    p_sch_build.set_defaults(func=cmd_sch_build)

    p_sch_net = sch_sub.add_parser("netlist", help="emit SPICE text from a spec")
    p_sch_net.add_argument("spec", help="snapshot or canvas document")
    p_sch_net.add_argument("-o", "--out", help="write the netlist here (default stdout)")
    p_sch_net.add_argument("--subckt", help="subcircuit name (default: the cell name)")
    p_sch_net.add_argument("--library", help="target library (for an unbound drawing)")
    p_sch_net.add_argument("--cell", help="target cell (for an unbound drawing)")
    p_sch_net.add_argument("--view", default="schematic")
    _add_output_flags(p_sch_net)
    p_sch_net.set_defaults(func=cmd_sch_netlist)

    p_sch_rt = sch_sub.add_parser("roundtrip",
                                  help="build, read back and compare the connectivity")
    p_sch_rt.add_argument("spec", help="snapshot or canvas document")
    p_sch_rt.add_argument("--timeout", type=float, default=900.0)
    _add_output_flags(p_sch_rt)
    p_sch_rt.set_defaults(func=cmd_sch_roundtrip)

    p_version = sub.add_parser("version", help="show the version")
    _add_output_flags(p_version)
    p_version.set_defaults(func=cmd_version)

    return parser


# Commands that act on a target: they start a session, stage files, or write
# artifacts. They are the ones the explicit-profile guard covers; read-only
# commands never need a confirmed profile. (`exec` is guarded inside
# runtime.exec_code instead, which the MCP server shares.)
WRITE_COMMANDS = frozenset({
    ("sim", "run"),
    ("layout", "gen"), ("layout", "drc"), ("layout", "boolean"),
    ("layout", "convert"), ("layout", "deck"),
    ("sch", "build"), ("sch", "roundtrip"),
    ("api", "sync-live"),
})


def _target_operation(args):
    """``(command, operation)`` when this invocation acts on a target, else None."""
    command = getattr(args, "command", None)
    if not command:
        return None
    operation = None
    for key, value in vars(args).items():
        if key.endswith("_command") and isinstance(value, str):
            operation = value
            break
    return (command, operation) if (command, operation) in WRITE_COMMANDS else None


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(list(sys.argv[1:] if argv is None else argv))
    global _OUTPUT_DEBUG
    _OUTPUT_DEBUG = bool(getattr(args, "debug", False))
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
    acts = _target_operation(args)
    if acts is not None:
        refusal = _config().profile_confirmation_error(
            "`%s %s`" % (PROG, " ".join(part for part in acts if part)))
        if refusal:
            print("error: %s" % refusal, file=sys.stderr)
            return 1
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
