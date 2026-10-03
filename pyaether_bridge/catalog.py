# -*- coding: utf-8 -*-
"""PyAether offline API catalog: compiles Sphinx docs into a searchable sqlite
database (standard library only).

The data comes from ``tools/pyaether/docs/html`` shipped inside the Aether
installer:
  * ``objects.inv``             the full symbol inventory (Sphinx inventory v2, zlib-compressed)
  * ``api_reference/**/*.html`` signature, parameters, return values and description of each symbol

Public API: see ARCHITECTURE.md - build_from_html / sync_from_runtime / search / show / stats.
"""

from __future__ import annotations

import html as html_module
import json
import multiprocessing
import os
import re
import sqlite3
import tempfile
import time
import zlib

from . import config, transports

SCHEMA_VERSION = "1"

_DT_RE = re.compile(r'<dt id="([^"]+)">')
_DL_KIND_RE = re.compile(r'<dl class="py ([a-z_]+)">')
_TAG_RE = re.compile(r"<[^>]+>")
_STRONG_SIG_RE = re.compile(r"<p>\s*<strong>(.*?)</strong>\s*</p>", re.S)
_FIELD_RE = re.compile(
    r"<dt><strong>(.*?)</strong>(?:\s*<span class=\"classifier\">(.*?)</span>)?\s*</dt>"
    r"\s*<dd>(.*?)</dd>",
    re.S,
)
_SECTION_RE = re.compile(
    r'<dt class="field-(?:odd|even)">'
    r"(Parameters|Returns|Raises|Yields|Other Parameters)</dt>"
    r'\s*<dd class="field-(?:odd|even)">(.*?)</dd>\s*(?=<dt class="field-|\Z)',
    re.S,
)

_SELECT_COLUMNS = (
    "name, kind, module, signature, summary, description, params,"
    " returns, page, anchor, domain"
)

# Executed in the pyAether session on the target: dump all of dir(pyAether) to disk (stdout is too small, use a file).
DUMP_REMOTE_PATH = os.path.join(config.REMOTE_DIR, "pyaether-symbols.json")

_RUNTIME_DUMP_CODE_TEMPLATE = r'''
import json, inspect
import pyAether
dump = {}
for name in dir(pyAether):
    if name.startswith("_"):
        continue
    obj = getattr(pyAether, name)
    sig = ""
    if callable(obj):
        try:
            sig = str(inspect.signature(obj))
        except Exception:
            sig = "()"
        if sig.startswith("(") and name:
            sig = name + sig
    try:
        lines = (getattr(obj, "__doc__", "") or "").strip().splitlines()
    except Exception:
        lines = []
    dump[name] = [type(obj).__name__, sig, (lines[0][:300] if lines else "")]
with open("__DUMP_PATH__", "w") as handle:
    json.dump(dump, handle, ensure_ascii=False)
print("PYAETHER_SYMBOLS", len(dump))
'''

_RUNTIME_DUMP_CODE = _RUNTIME_DUMP_CODE_TEMPLATE.replace("__DUMP_PATH__", DUMP_REMOTE_PATH)


class CatalogMissingError(RuntimeError):
    """The catalog database does not exist: run `pyaether api build` first."""


def _db_path(db_path):
    return str(db_path) if db_path else str(config.CATALOG_DB)


def _connect(db_path):
    path = _db_path(db_path)
    if not os.path.exists(path):
        raise CatalogMissingError(
            "API catalog database does not exist: %s (run `pyaether api build` first)" % path
        )
    return sqlite3.connect(path), path


def _text(raw):
    """Strip tags + unescape + collapse whitespace."""
    if not raw:
        return ""
    raw = re.sub(r"<script.*?</script>", " ", raw, flags=re.S)
    raw = _TAG_RE.sub(" ", raw)
    return re.sub(r"\s+", " ", html_module.unescape(raw)).strip()


def _clip(text, limit):
    text = text or ""
    if limit and len(text) > limit:
        return text[: limit - 1].rstrip() + "…"
    return text


def _strip_backticks(text):
    return (text or "").strip().strip("`").strip()


def _parse_fields(segment, section):
    """Extract a Parameters/Returns section -> [{"name","type","desc"}]."""
    match = None
    for candidate in _SECTION_RE.finditer(segment):
        if candidate.group(1) == section:
            match = candidate
            break
    if not match:
        return []
    items = []
    for name, type_name, desc in _FIELD_RE.findall(match.group(2)):
        items.append({
            "name": _text(name),
            "type": _strip_backticks(_text(type_name)),
            "desc": _clip(_text(desc), 800),
        })
    return items


def _parse_segment(segment, name, kind):
    entry = {"name": name, "kind": kind, "signature": "", "summary": "",
             "description": "", "params": [], "returns": []}
    sig_match = _STRONG_SIG_RE.search(segment)
    if sig_match:
        entry["signature"] = _strip_backticks(_text(sig_match.group(1)))
        rest = segment[sig_match.end():]
    else:
        end = segment.find("</dt>")
        if end >= 0:
            entry["signature"] = _text(segment[:end])
            rest = segment[end + 5:]
        else:
            rest = segment
    if not entry["signature"]:
        entry["signature"] = name + "()"
    for para in re.findall(r"<p>(.*?)</p>", rest, re.S):
        cleaned = _text(para)
        if cleaned and not cleaned.startswith(("Parameters", "Returns")):
            entry["summary"] = _clip(cleaned, 400)
            break
    entry["description"] = _clip(_text(rest), 6000)
    entry["params"] = _parse_fields(rest, "Parameters")
    entry["returns"] = _parse_fields(rest, "Returns")
    return entry


def parse_page(html_text, names):
    """Parse one API page -> {name: {signature, summary, description, params, returns}}."""
    out = {}
    positions = [(m.start(), m.group(1)) for m in _DT_RE.finditer(html_text)]
    wanted = set(names or [])
    for index, (pos, name) in enumerate(positions):
        if wanted and name not in wanted:
            continue
        end = positions[index + 1][0] if index + 1 < len(positions) else len(html_text)
        segment = html_text[pos:end]
        head = html_text[max(0, pos - 400):pos]
        kinds = _DL_KIND_RE.findall(head)
        out[name] = _parse_segment(segment, name, kinds[-1] if kinds else "unknown")
    return out


def read_inventory(docs_dir):
    """Parse objects.inv -> [(name, kind, page, anchor)]."""
    raw = open(os.path.join(docs_dir, "objects.inv"), "rb").read()
    offset = 0
    for _ in range(4):
        offset = raw.index(b"\n", offset) + 1
    body = zlib.decompress(raw[offset:]).decode("utf-8", "replace")
    entries = []
    for line in body.splitlines():
        parts = line.split(" ", 4)
        if len(parts) < 5:
            continue
        name, domain_role, _priority, uri, _disp = parts
        kind = domain_role.split(":", 1)[1] if ":" in domain_role else domain_role
        page, _, anchor = uri.partition("#")
        entries.append((name, kind, page, anchor))
    return entries


def _domain_of(page):
    if not page:
        return "other"
    parts = page.split("/")
    if len(parts) >= 3 and parts[0] == "api_reference":
        return parts[1]
    return "misc" if parts[0] == "api_reference" else "other"


def _module_of(name):
    return "pyAether" if "." not in name else name.rsplit(".", 1)[0]


def _page_worker(args):
    docs_dir, page, names = args
    try:
        with open(os.path.join(docs_dir, page), encoding="utf-8", errors="replace") as handle:
            html_text = handle.read()
    except OSError:
        return page, {}
    try:
        return page, parse_page(html_text, names)
    except Exception:
        return page, {}


def _create_schema(conn):
    conn.executescript(
        """
        DROP TABLE IF EXISTS entries;
        DROP TABLE IF EXISTS entries_fts;
        DROP TABLE IF EXISTS meta;
        CREATE TABLE entries(
          name TEXT PRIMARY KEY, kind TEXT, module TEXT, signature TEXT,
          summary TEXT, description TEXT, params TEXT, returns TEXT,
          page TEXT, anchor TEXT, domain TEXT);
        CREATE VIRTUAL TABLE entries_fts USING fts5(
          name, module, signature, summary, description, kind, domain);
        CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT);
        """
    )


def build_from_html(docs_html_dir, out_db, *, jobs=1, progress=None):
    """Build the catalog database from docs/html and return a stats dict."""
    started = time.time()
    docs_dir, out_db = str(docs_html_dir), str(out_db)

    def note(message):
        if callable(progress):
            try:
                progress(message)
            except Exception:
                pass

    note("reading objects.inv …")
    inventory = read_inventory(docs_dir)
    note("inventory entries: %d" % len(inventory))

    pages, order = {}, []
    for name, _kind, page, _anchor in inventory:
        if not page.startswith("api_reference/") or not page.endswith(".html"):
            continue
        if page not in pages:
            pages[page] = []
            order.append(page)
        pages[page].append(name)

    note("API pages to parse: %d" % len(order))
    details, skipped = {}, 0

    def absorb(index, page, parsed):
        nonlocal skipped
        details[page] = parsed
        if not parsed:
            skipped += 1
        if index % 250 == 0:
            note("parsed %d/%d pages" % (index, len(order)))

    parallel = int(jobs or 1) > 1 and len(order) > 8
    if parallel:
        payload = [(docs_dir, page, pages[page]) for page in order]
        try:
            with multiprocessing.Pool(int(jobs)) as pool:
                for index, (page, parsed) in enumerate(
                        pool.imap_unordered(_page_worker, payload, chunksize=16), 1):
                    absorb(index, page, parsed)
        except Exception as exc:
            note("parallel parsing failed (%s); falling back to serial" % exc)
            details, skipped, parallel = {}, 0, False
    if not parallel:
        for index, page in enumerate(order, 1):
            _page, parsed = _page_worker((docs_dir, page, pages[page]))
            absorb(index, page, parsed)

    note("writing database %s …" % out_db)
    directory = os.path.dirname(os.path.abspath(out_db))
    if directory:
        os.makedirs(directory, exist_ok=True)
    rows = []
    for name, kind, page, anchor in inventory:
        detail = (details.get(page) or {}).get(name) or {}
        rows.append((
            name,
            detail.get("kind") or kind,
            _module_of(name),
            detail.get("signature") or "(%s)" % kind,
            _clip(detail.get("summary"), 400),
            detail.get("description") or "",
            json.dumps(detail.get("params") or [], ensure_ascii=False),
            json.dumps(detail.get("returns") or [], ensure_ascii=False),
            page, anchor, _domain_of(page),
        ))
    conn = sqlite3.connect(out_db)
    try:
        _create_schema(conn)
        conn.executemany(
            "INSERT OR REPLACE INTO entries(name, kind, module, signature, summary,"
            " description, params, returns, page, anchor, domain)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?)", rows)
        conn.execute(
            "INSERT INTO entries_fts(rowid, name, module, signature, summary,"
            " description, kind, domain)"
            " SELECT rowid, name, module, signature, summary, description, kind, domain"
            " FROM entries")
        conn.executemany("INSERT OR REPLACE INTO meta(key, value) VALUES (?,?)", [
            ("entries", str(len(rows))),
            ("pages", str(len(order))),
            ("built_at", time.strftime("%Y-%m-%d %H:%M:%S")),
            ("source_dir", docs_dir),
            ("schema_version", SCHEMA_VERSION),
        ])
        conn.commit()
        counts = dict(conn.execute(
            "SELECT kind, COUNT(*) FROM entries GROUP BY kind ORDER BY 2 DESC"))
        domains = dict(conn.execute(
            "SELECT domain, COUNT(*) FROM entries GROUP BY domain ORDER BY 2 DESC"))
    finally:
        conn.close()
    summary = {
        "entries": len(rows), "pages": len(order), "db": out_db,
        "by_kind": counts, "by_domain": domains, "skipped": skipped,
        "elapsed_s": round(time.time() - started, 2),
    }
    note("done: %d entries / %d pages in %ss"
         % (summary["entries"], summary["pages"], summary["elapsed_s"]))
    return summary


def _all_candidates(query, limit, kind, module, conn):
    q = (query or "").strip()
    if not q:
        return []
    clauses, params = [], []
    if kind:
        clauses.append("AND kind = ?")
        params.append(kind)
    if module:
        clauses.append("AND module = ?")
        params.append(module)
    extra = " ".join(clauses)
    select = "SELECT " + _SELECT_COLUMNS + " FROM entries"
    found = {}

    def add(tier, rows):
        for row in rows:
            # doc/label are documentation sections and runtime entries come from the
            # live session to fill documentation gaps (their signature is often
            # (*args, **kwargs)); neither should outrank API symbols that carry a
            # fully typed signature.
            rank = tier + 0.5 if row[1] in ("doc", "label", "runtime") else tier
            if row[0] not in found or found[row[0]][0] > rank:
                found[row[0]] = (rank, row)

    add(0, conn.execute(select + " WHERE name = ? COLLATE NOCASE " + extra,
                        [q] + params).fetchmany(limit))
    add(1, conn.execute(select + " WHERE name LIKE ? COLLATE NOCASE " + extra,
                        [q + "%"] + params).fetchmany(limit))
    add(2, conn.execute(
        select + " WHERE (name LIKE ? COLLATE NOCASE OR signature LIKE ? COLLATE NOCASE) "
        + extra, ["%" + q + "%", "%" + q + "%"] + params).fetchmany(limit))
    tokens = re.findall(r"[A-Za-z0-9_]{2,}", q)
    if tokens:
        match = " AND ".join('"%s"' % token for token in tokens)
        try:
            add(3, conn.execute(
                select + " WHERE rowid IN (SELECT rowid FROM entries_fts"
                " WHERE entries_fts MATCH ?) " + extra,
                [match] + params).fetchmany(limit * 2))
        except sqlite3.Error:
            pass
    ordered = sorted(found.values(), key=lambda item: (item[0], len(item[1][0]), item[1][0]))
    return [row for _tier, row in ordered][:limit]


def _row_to_dict(row, *, description=None):
    try:
        params = json.loads(row[6] or "[]")
        returns = json.loads(row[7] or "[]")
    except ValueError:
        params, returns = [], []
    return {
        "name": row[0], "kind": row[1], "module": row[2], "signature": row[3],
        "summary": row[4], "description": row[5] if description is None else description,
        "params": params, "returns": returns, "page": row[8], "anchor": row[9],
        "domain": row[10],
    }


def search(query, *, limit=20, kind=None, module=None, db_path=None):
    """Search API symbols and return [{name,kind,module,signature,summary,domain}]."""
    conn, _path = _connect(db_path)
    try:
        rows = _all_candidates(query, int(limit or 20), kind, module, conn)
    finally:
        conn.close()
    return [_row_to_dict(row) for row in rows]


def show(name, *, max_chars=4000, db_path=None):
    """Fetch the full documentation of one symbol; returns None when it is not found."""
    conn, _path = _connect(db_path)
    select = "SELECT " + _SELECT_COLUMNS + " FROM entries"
    try:
        # When a documented entry and a runtime entry share a name, prefer the documented one (more complete signature and parameters).
        row = conn.execute(
            select + " WHERE name = ? COLLATE NOCASE"
            " ORDER BY (kind = 'runtime') LIMIT 1", [name]).fetchone()
        if row is None and "." not in name:
            row = conn.execute(
                select + " WHERE name LIKE ? COLLATE NOCASE"
                " ORDER BY (kind = 'runtime'), length(name) LIMIT 1",
                ["%." + name]).fetchone()
        if row is None:
            row = conn.execute(
                select + " WHERE name LIKE ? COLLATE NOCASE"
                " ORDER BY (kind = 'runtime'), length(name) LIMIT 1",
                ["%" + name + "%"]).fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    return _row_to_dict(row, description=_clip(row[5], int(max_chars or 4000)))


def stats(*, db_path=None):
    """Catalog statistics."""
    conn, path = _connect(db_path)
    try:
        meta = dict(conn.execute("SELECT key, value FROM meta"))
        kinds = dict(conn.execute(
            "SELECT kind, COUNT(*) FROM entries GROUP BY kind ORDER BY 2 DESC"))
        domains = dict(conn.execute(
            "SELECT domain, COUNT(*) FROM entries GROUP BY domain ORDER BY 2 DESC"))
    finally:
        conn.close()
    return {
        "entries": int(meta.get("entries") or 0),
        "pages": int(meta.get("pages") or 0),
        "by_kind": kinds,
        "by_domain": domains,
        "db": path,
        "built_at": meta.get("built_at") or "",
        "source_dir": meta.get("source_dir") or "",
        "schema_version": meta.get("schema_version") or "",
    }


def sync_from_runtime(*, db_path=None, timeout=300.0, target=None):
    """Add the dir(pyAether) symbols of the live session to the catalog (kind='runtime').

    The documentation covers just over 10k symbols while the runtime exposes
    16k+; this path fills exactly the documentation gaps and never overwrites an
    existing documented entry. Requires the host daemon and the target session
    to be available.
    """
    from . import runtime  # deferred import: offline search must work without a daemon

    try:
        transport = target or transports.build()
    except transports.TransportError as exc:
        raise RuntimeError("cannot determine the execution target: %s" % exc)
    path = _db_path(db_path)
    if not os.path.exists(path):
        raise CatalogMissingError(
            "API catalog database does not exist: %s (run `pyaether api build` first)" % path
        )
    result = runtime.exec_code(_RUNTIME_DUMP_CODE, timeout=timeout)
    if not result.get("ok"):
        raise RuntimeError("execution in the target session failed: %s" % (result.get("error") or "unknown error"))
    handle, local_file = tempfile.mkstemp(prefix="pyaether-symbols-", suffix=".json")
    os.close(handle)
    try:
        try:
            transport.fetch_file(DUMP_REMOTE_PATH, local_file)
        except transports.TransportError as exc:
            raise RuntimeError("failed to fetch the symbol dump from %s: %s" % (transport.label(), exc))
        with open(local_file, encoding="utf-8") as source:
            symbols = json.load(source)
    finally:
        try:
            os.unlink(local_file)
        except OSError:
            pass

    conn = sqlite3.connect(path)
    try:
        # Drop the old runtime entries first so this command is repeatable (and so early naming schemes can be upgraded).
        stale = [row[0] for row in conn.execute("SELECT rowid FROM entries WHERE kind = 'runtime'")]
        if stale:
            conn.executemany("DELETE FROM entries WHERE rowid = ?", [(rowid,) for rowid in stale])
            conn.executemany("DELETE FROM entries_fts WHERE rowid = ?", [(rowid,) for rowid in stale])
        known = {row[0] for row in conn.execute("SELECT name FROM entries")}
        rows = []
        for name, (type_name, signature, first_doc_line) in symbols.items():
            # Use the same naming as documented entries (pyAether.xxx) so that an
            # unprefixed name cannot win by exact match against a documented entry
            # with a fully typed signature.
            full = name if name.startswith("pyAether.") else "pyAether." + name
            if full in known:
                continue
            summary = ("[runtime symbol · %s] %s" % (type_name, first_doc_line)).strip()
            rows.append((full, "runtime", _module_of(full), signature or "(runtime)",
                         _clip(summary, 400), "", "[]", "[]", "", "", "runtime"))
        conn.executemany(
            "INSERT OR REPLACE INTO entries(name, kind, module, signature, summary,"
            " description, params, returns, page, anchor, domain)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?)", rows)
        conn.execute(
            "INSERT INTO entries_fts(rowid, name, module, signature, summary,"
            " description, kind, domain) SELECT rowid, name, module, signature,"
            " summary, description, kind, domain FROM entries"
            " WHERE rowid NOT IN (SELECT rowid FROM entries_fts)")
        total = conn.execute("SELECT COUNT(*) FROM entries").fetchone()[0]
        conn.executemany("INSERT OR REPLACE INTO meta(key, value) VALUES (?,?)", [
            ("entries", str(total)),
            ("runtime_symbols", str(len(symbols))),
            ("runtime_synced_at", time.strftime("%Y-%m-%d %H:%M:%S")),
        ])
        conn.commit()
    finally:
        conn.close()
    return {"added": len(rows), "entries": total, "symbols": len(symbols),
            "db": path, "target": transport.label(),
            "session_symbols_reported": result.get("result_repr")}
