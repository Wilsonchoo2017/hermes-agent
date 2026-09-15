"""Kanban dispatch pause (ESTOP) — a resumable pause for NEW Kanban work only.

``hermes pause`` writes a sentinel file at ``$HERMES_HOME/ESTOP``;
``hermes resume`` removes it. While the sentinel exists, the cron scheduler,
kanban dispatcher, and new gateway turns skip work; in-flight work is never
killed. The check is one or two ``os.stat`` calls (process home + fleet root
when they differ) so callers may run it every tick; no caching beyond the OS
is performed, so engaging/disengaging takes effect on the very next check.

The sentinel body is optional JSON ``{"reason": ..., "engaged_at": ...}``.
A corrupt or empty file still counts as engaged (fail safe): the pause must
hold even if the file was created by ``touch ~/.hermes/ESTOP``.

Ported from: gastownhall/gastown estop.go (MIT). Related prior art:
#26778 (/panic — kill/exit semantics; deliberately different, ours is
resumable) and #44617 (interrupting in-flight cron; deliberately out of
scope here).

There is also a kanban-scoped pause: ``hermes kanban pause`` / ``hermes kanban resume``
manages a separate ``ESTOP_KANBAN`` sentinel that halts only the kanban dispatcher,
leaving chat turns and cron dispatch running. ``check_paused("kanban", …)`` honors
both sentinels; other components honor only the global one, so the two levers are
independent.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from contextlib import suppress
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

SENTINEL_NAME = "ESTOP"
# Kanban-scoped pause sentinel: `hermes kanban pause` writes this; it gates the
# kanban dispatcher only, leaving chat turns and cron dispatch running. Separate
# from the global ESTOP so the two levers are independent (global = stop all).
KANBAN_SENTINEL_NAME = "ESTOP_KANBAN"

# Per-component "logged already for this engagement" flags so a paused
# dispatch loop logs once per engagement instead of once per tick.
_log_lock = threading.Lock()
_logged_components: set[str] = set()


def _hermes_home() -> Path:
    """Resolve the active HERMES_HOME (profile-aware) at call time."""
    try:
        from hermes_constants import get_hermes_home
        return get_hermes_home()
    except Exception:
        return Path(os.path.expanduser("~/.hermes"))


def _canonical_root() -> Path:
    """Fleet-wide Hermes root, even when this process is a profile gateway.

    Profile gateways launch with HERMES_HOME=~/.hermes/profiles/<name>.
    ``hermes pause`` from an operator seat writes ~/.hermes/ESTOP. If we
    only inspect the profile home, the emergency stop does not bind
    (jarvis-os/t_7b65ff88: fleet-analyst kept dispatching through pause).
    """
    try:
        from hermes_constants import get_default_hermes_root
        return Path(get_default_hermes_root())
    except Exception:
        return Path(os.path.expanduser("~/.hermes"))


def sentinel_path() -> Path:
    """Path of the ESTOP sentinel this process would write on `hermes pause`."""
    return _hermes_home() / SENTINEL_NAME


def kanban_sentinel_path() -> Path:
    """Path of the kanban-only pause sentinel this process would write (``ESTOP_KANBAN``)."""
    return _hermes_home() / KANBAN_SENTINEL_NAME


def _candidate_sentinel_paths() -> list:
    """Profile home first, then the fleet root if it is a different directory."""
    primary = sentinel_path()
    paths = [primary]
    try:
        root = _canonical_root() / SENTINEL_NAME
    except Exception:
        return paths
    try:
        if root.resolve() != primary.resolve():
            paths.append(root)
    except Exception:
        # Non-Path test doubles (fail-safe stat fixture) fail .resolve();
        # the generic comparison below still dedupes plain equal paths.
        if root != primary:
            paths.append(root)
    return paths


def _candidate_kanban_sentinel_paths() -> list:
    """Same profile-first/fleet-root lookup for the kanban-only ``ESTOP_KANBAN`` sentinel."""
    primary = kanban_sentinel_path()
    try:
        root = _canonical_root() / KANBAN_SENTINEL_NAME
    except Exception:
        return [primary]
    try:
        distinct = root.resolve() != primary.resolve()
    except Exception:
        distinct = root != primary
    return [primary, root] if distinct else [primary]


def is_kanban_engaged() -> bool:
    """True if ANY candidate kanban sentinel exists; fail SAFE (True) on stat errors."""
    saw_stat_error = False
    for path in _candidate_kanban_sentinel_paths():
        try:
            if path.exists():
                return True
        except OSError:
            saw_stat_error = True
    return saw_stat_error


def kanban_engage(reason: Optional[str] = None) -> Path:
    """Create the kanban-only pause sentinel. Idempotent; re-engaging updates the file."""
    path = kanban_sentinel_path()
    payload = {"engaged_at": datetime.now(timezone.utc).isoformat(), "reason": reason or None}
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    except OSError:
        with suppress(OSError):  # Best effort: an empty/partial sentinel still pauses (fail safe).
            path.touch(exist_ok=True)
    return path


def kanban_disengage() -> bool:
    """Remove every visible kanban-only sentinel (process-local and fleet-root)."""
    lifted = False
    for path in _candidate_kanban_sentinel_paths():
        try:
            path.unlink()
            lifted = True
        except (OSError, AttributeError):
            continue
    return lifted


def get_kanban_state() -> Optional[dict]:
    """Return ``{"reason", "engaged_at"}`` for the kanban-only pause, or None when not engaged."""
    if not is_kanban_engaged():
        return None
    state = {"reason": None, "engaged_at": None}
    found = False
    for path in _candidate_kanban_sentinel_paths():
        try:
            if not path.exists():
                continue
        except OSError:
            return state
        except AttributeError:
            continue
        found = True
        with suppress(OSError, ValueError, AttributeError):
            raw = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                state = {"reason": raw.get("reason") or None, "engaged_at": raw.get("engaged_at") or None}
                break
    return state if found else None


def is_engaged() -> bool:
    """Cheap check: is the global emergency stop engaged?

    Engaged if ANY candidate sentinel exists: the process HERMES_HOME
    (profile-local) or the fleet canonical root (~/.hermes). Fail SAFE on
    stat errors so an unreadable sentinel still holds the pause.
    """
    saw_stat_error = False
    for path in _candidate_sentinel_paths():
        try:
            if path.exists():
                return True
        except OSError:
            saw_stat_error = True
    return saw_stat_error


def engage(reason: Optional[str] = None) -> Path:
    """Create the ESTOP sentinel. Idempotent; re-engaging updates the file."""
    path = sentinel_path()
    payload = {
        "engaged_at": datetime.now(timezone.utc).isoformat(),
        "reason": reason or None,
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    except OSError:
        # Best effort: an empty/partial sentinel still pauses (fail safe).
        try:
            path.touch(exist_ok=True)
        except OSError:
            pass
    return path


def disengage() -> bool:
    """Remove ESTOP sentinels this process can see.

    Lifts both the process-local sentinel and the fleet-root sentinel so
    ``hermes resume`` from a profile gateway still clears an operator pause
    written at ~/.hermes/ESTOP.
    """
    lifted = False
    for path in _candidate_sentinel_paths():
        try:
            path.unlink()
            lifted = True
        except FileNotFoundError:
            continue
        except (OSError, AttributeError):
            continue
    return lifted


def get_state() -> Optional[dict]:
    """Return ``{"reason": ..., "engaged_at": ...}`` or None when not engaged.

    A sentinel with an unreadable/corrupt body still reports engaged, with
    both fields None — the pause is authoritative, the metadata is not.
    """
    if not is_engaged():
        return None
    reason = None
    engaged_at = None
    found = False
    for path in _candidate_sentinel_paths():
        try:
            exists = path.exists()
        except OSError:
            return {"reason": None, "engaged_at": None}
        except AttributeError:
            continue
        if not exists:
            continue
        found = True
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                reason = raw.get("reason") or None
                engaged_at = raw.get("engaged_at") or None
                break
        except (OSError, ValueError, AttributeError):
            continue
    if not found:
        return None
    return {"reason": reason, "engaged_at": engaged_at}


def check_paused(component: str, logger: logging.Logger) -> bool:
    """Return True when the component is paused, logging once per engagement per component.

    Dispatch loops call this every tick; the log fires on the disengaged→
    engaged transition for that component and re-arms after a resume, so a
    long pause doesn't spam one line per tick.

    The global emergency stop (`hermes pause` → ``ESTOP``) pauses every component.
    The kanban-scoped pause (`hermes kanban pause` → ``ESTOP_KANBAN``) additionally
    pauses only the ``kanban`` component — chat turns and cron dispatch keep running.
    """
    engaged = is_engaged()          # global ESTOP stops everything.
    engaged = engaged or (component == "kanban" and is_kanban_engaged())
    if not engaged:
        with _log_lock:
            _logged_components.discard(component)
        return False
    with _log_lock:
        first = component not in _logged_components
        if first:
            _logged_components.add(component)
    if first:
        reason = (get_state() or get_kanban_state() or {}).get("reason")
        global_paused = is_engaged()
        scope = "global emergency stop" if global_paused else "kanban-only pause"
        suffix = f" (reason: {reason})" if reason else ""
        _path = sentinel_path() if global_paused else kanban_sentinel_path()
        logger.info(
            "%s dispatch paused by %s%s — remove with `hermes %s` (%s)",
            component, scope, suffix,
            "resume" if global_paused else "kanban resume", _path,
        )
    return True


def _reset_log_state_for_tests() -> None:
    """Clear the log-once bookkeeping (test isolation helper)."""
    with _log_lock:
        _logged_components.clear()
