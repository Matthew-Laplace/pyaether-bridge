"""Wire format for CLI replies.

The envelope itself is untouched -- ``ok`` / ``status`` / ``operation`` /
``data`` / ``errors`` / ``warnings`` / ``metadata`` keeps its meaning and its
keys.  This module only decides how much of it reaches stdout.

Two economies, both measured on a real ``layout info`` reply (1885 B before):

* **Separators, not indentation.**  ``indent=2`` cost 679 B (36%) on that
  reply and 616 B (21%) on ``status``.  JSON here is read by programs and by
  agents; neither benefits from the whitespace.
* **No execution diagnostics on success.**  ``target``, ``target_reason``,
  ``work_dir``, ``command``, ``timings`` and friends describe *how the bridge
  ran*, not *what the answer is*; they cost 418 B (22%) on that reply.  They
  survive whenever the call did not succeed, because that is when they matter.

Result-bearing metadata (``output``, ``written``, ``artifacts``, ``deck``,
``source``, ...) is never dropped.  ``--debug`` restores the verbose form.

``status`` does not use the envelope, so it gets its own rule: the session
import log is kept only while the session is not ready.
"""

import json

#: Metadata that describes the run, not the result.  Kept on failure.
DIAGNOSTIC_METADATA = frozenset({
    "command",
    "engine",
    "returncode",
    "target",
    "target_reason",
    "timings",
    "tool",
    "total_s",
    "work_dir",
})

#: Session fields that describe a failure.  Kept while the session is unready.
DIAGNOSTIC_SESSION = frozenset({
    "import_log",
})


def brief(payload):
    """Return ``payload`` with success-path diagnostics removed.

    Anything that is not a dict is returned unchanged, as is a reply that
    carries no diagnostic field.
    """
    if not isinstance(payload, dict):
        return payload
    trimmed = _brief_envelope(payload) if payload.get("ok") else payload
    return _brief_session(trimmed)


def _brief_envelope(payload):
    metadata = payload.get("metadata")
    if not isinstance(metadata, dict) or not metadata:
        return payload
    kept = {key: value for key, value in metadata.items()
            if key not in DIAGNOSTIC_METADATA}
    if len(kept) == len(metadata):
        return payload
    trimmed = dict(payload)
    trimmed["metadata"] = kept
    return trimmed


def _brief_session(payload):
    session = payload.get("session")
    if not isinstance(session, dict) or not session.get("ready"):
        return payload
    kept = {key: value for key, value in session.items()
            if key not in DIAGNOSTIC_SESSION}
    if len(kept) == len(session):
        return payload
    trimmed = dict(payload)
    trimmed["session"] = kept
    return trimmed


def dumps(payload, debug=False):
    """Serialize a reply for stdout.  ``debug`` restores the verbose form."""
    if debug:
        return json.dumps(payload, ensure_ascii=False, indent=2, default=str)
    return json.dumps(brief(payload), ensure_ascii=False,
                      separators=(",", ":"), default=str)
