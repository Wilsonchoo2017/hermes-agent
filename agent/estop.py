"""Global emergency stop (ESTOP) — a resumable pause for NEW work only.

``hermes pause`` writes a sentinel at ``$HERMES_HOME/ESTOP``; ``hermes resume``
removes it. While it exists the cron scheduler, kanban dispatcher and new gateway
turns skip work; in-flight work is never killed. The check is one or two uncached
``os.stat`` calls (process home + fleet root when they differ). The body is optional
JSON ``{"reason", "engaged_at"}``; a corrupt/empty file still counts as engaged
(fail safe, e.g. ``touch ~/.hermes/ESTOP``). Ported from gastownhall/gastown estop.go (MIT).

There is also a kanban-scoped pause: ``hermes kanban pause`` / ``hermes kanban resume``
manages a separate ``ESTOP_KANBAN`` sentinel that halts only the kanban dispatcher,
leaving chat turns and cron dispatch running. ``check_paused("kanban", …)`` honors
both sentinels; other components honor only the global one, so the two levers are
independent.
"""

from __future__ import annotations

import json
import logging
import threading
from contextlib import suppress
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# Same profile-aware / fleet-root resolvers the file-safety guards use (fail-open to ~/.hermes).
from agent.file_safety import _hermes_home_path as _hermes_home, _hermes_root_path as _canonical_root

SENTINEL_NAME = "ESTOP"
# Kanban-scoped pause sentinel: `hermes kanban pause` writes this; it gates the
# kanban dispatcher only, leaving chat turns and cron dispatch running. Separate
# from the global ESTOP so the two levers are independent (global = stop all).
KANBAN_SENTINEL_NAME = "ESTOP_KANBAN"

# Per-component "logged already for this engagement" flags: log once per engagement, not per tick.
_log_lock = threading.Lock()
_logged_components: set[str] = set()


def sentinel_path() -> Path:
    """Path of the ESTOP sentinel this process would write on `hermes pause`."""
    return _hermes_home() / SENTINEL_NAME


def kanban_sentinel_path() -> Path:
    """Path of the kanban-only pause sentinel this process would write (``ESTOP_KANBAN``)."""
    return _hermes_home() / KANBAN_SENTINEL_NAME


def _candidate_sentinel_paths() -> list:
    """Profile home first, then the fleet root if it is a different directory: a profile
    gateway (HERMES_HOME=~/.hermes/profiles/<n>) must still honor an operator's ~/.hermes/ESTOP."""
    primary = sentinel_path()
    try:
        root = _canonical_root() / SENTINEL_NAME
    except Exception:
        return [primary]
    try:
        distinct = root.resolve() != primary.resolve()
    except Exception:
        # Non-Path test doubles fail .resolve(); plain equality still dedupes.
        distinct = root != primary
    return [primary, root] if distinct else [primary]


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
    """True if ANY candidate sentinel exists; fail SAFE (True) on stat errors."""
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
    payload = {"engaged_at": datetime.now(timezone.utc).isoformat(), "reason": reason or None}
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    except OSError:
        with suppress(OSError):  # Best effort: an empty/partial sentinel still pauses (fail safe).
            path.touch(exist_ok=True)
    return path


def disengage() -> bool:
    """Remove every visible sentinel (process-local and fleet-root)."""
    lifted = False
    for path in _candidate_sentinel_paths():
        try:
            path.unlink()
            lifted = True
        except (OSError, AttributeError):
            continue
    return lifted


def get_state() -> Optional[dict]:
    """Return ``{"reason", "engaged_at"}`` or None when not engaged; an unreadable/corrupt
    body still reports engaged with both fields None."""
    if not is_engaged():
        return None
    state = {"reason": None, "engaged_at": None}
    found = False
    for path in _candidate_sentinel_paths():
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


def paused_reply() -> Optional[str]:
    """Short user-facing notice for new gateway turns, or None if not paused."""
    state = get_state()
    if state is None:
        return None
    tag = f" ({state['reason']})" if state.get("reason") else ""
    return f"⏸️ Hermes is paused{tag}. New work is on hold; run `hermes resume` to pick things back up."


def check_paused(component: str, logger: logging.Logger) -> bool:
    """Return True when the component is paused, logging once per engagement per component.

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


# ---- BEGIN PLUGIN-COMPAT (revert-scheduled; see COMPAT_MANIFEST.md) ----
# Names external plugins imported from this module before the Sep 2026 decomposition.
# Internal code MUST NOT use these (scripts/check_compat_pointers.py fails CI if it does).
# The whole block is removed by reverting the commit that added it.
import os  # noqa: F401,E402
# ---- END PLUGIN-COMPAT ----
