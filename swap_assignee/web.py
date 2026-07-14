"""Web UI for the Pipefy assignee swap.

Local use:
    python -m swap_assignee.web
then open http://127.0.0.1:5000 in a browser.

Hosted use (Render / Fly.io):
    gunicorn -w 1 --threads 8 -b 0.0.0.0:$PORT 'swap_assignee.web:create_app()'
Set SWAP_WEB_PASSWORD to require a shared password + company email before the
UI (and every API route) can be used. Keep a single worker process — preview
plans and the log ring live in process memory.

This is a thin wrapper around the same functions the CLI uses
(`_resolve_users`, `discover_assignee_fields`, `iter_cards`, `plan_card`,
`execute_plan`).
"""
from __future__ import annotations

import collections
import hashlib
import json
import logging
import os
import secrets
import threading
import time
import webbrowser
from datetime import timedelta
from typing import Any, Deque, Dict, List

try:
    from flask import Flask, jsonify, request, session, Response
    from werkzeug.middleware.proxy_fix import ProxyFix
except ImportError as exc:  # pragma: no cover
    raise SystemExit(
        "Flask is required for the web UI. Install with: pip install -r requirements.txt"
    ) from exc

from .card_iterator import (
    discover_assignee_fields,
    fetch_active_phases,
    iter_cards,
    iter_cards_fast_union,
    probe_card_level_assignee,
    probe_field_value,
)
from .cli import _resolve_users  # reuse exactly the CLI's resolution flow
from .config import load_config, mask_secret
from .papertrail_logger import attach_papertrail
from .pipefy_client import PipefyClient
from .swap_engine import DEFAULT_EXCLUDED_PHASES, execute_plan, plan_card
from .user_resolver import (
    UserResolutionError,
    fetch_org_users,
    fetch_pipe_context,
    resolve_user,
)
from .audit import (
    ALLOWED_EMAIL_DOMAINS,
    AuditStore,
    is_allowed_email,
    normalize_email,
    record_swap,
)
from .sheets_sink import build_sheet_sink


log = logging.getLogger("swap_assignee.web")


# ---------- in-memory log ring for the Developer panel ----------
_LOG_RING: Deque[Dict[str, Any]] = collections.deque(maxlen=500)
_LOG_SEQ = {"n": 0}
_LOG_LOCK = threading.Lock()


class _RingHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = self.format(record)
        except Exception:
            msg = record.getMessage()
        with _LOG_LOCK:
            _LOG_SEQ["n"] += 1
            _LOG_RING.append({
                "seq": _LOG_SEQ["n"],
                "ts": record.created,
                "level": record.levelname,
                "logger": record.name,
                "msg": msg,
            })


def _install_log_capture() -> None:
    """Attach the ring handler once to the swap_assignee logger tree."""
    root = logging.getLogger("swap_assignee")
    for h in root.handlers:
        if isinstance(h, _RingHandler):
            return
    handler = _RingHandler()
    handler.setFormatter(logging.Formatter("%(message)s"))
    root.addHandler(handler)
    if root.level == logging.NOTSET:
        root.setLevel(logging.INFO)

# In-memory store of previewed plans keyed by an opaque token.
# Single-user local tool — fine to hold this in process memory.
_PREVIEW_STORE: Dict[str, Dict[str, Any]] = {}
_PREVIEW_TTL_SECONDS = 30 * 60  # 30 minutes


# Curated list of pipes shown in the "Pipe" dropdown and probed by the
# "Find pipes" discovery scan. Add new entries here (or use the
# "Other (enter ID)…" option in the UI to type one ad-hoc).
# Source of truth lives in PIPES.md.
KNOWN_PIPES: List[Dict[str, str]] = [
    {"id": "305705781", "name": "Account Management"},
    {"id": "306751836", "name": "Always On (Social Organic)"},
    {"id": "305709782", "name": "Creative"},
    {"id": "306828440", "name": "Events and Activations"},
    {"id": "305743818", "name": "Media"},
    {"id": "306683702", "name": "MTN CBU | Social Media"},
    {"id": "306766298", "name": "MTN EBU - Social Media"},
    {"id": "306766315", "name": "MTN MoMo - Social Media"},
    {"id": "306773031", "name": "PR Management"},
    {"id": "305787071", "name": "Production"},
    {"id": "305766460", "name": "Retail"},
    {"id": "305723961", "name": "Social Community Management"},
    {"id": "305709783", "name": "Strategy"},
]


def _gc_preview_store() -> None:
    now = time.time()
    expired = [k for k, v in _PREVIEW_STORE.items() if now - v["created_at"] > _PREVIEW_TTL_SECONDS]
    for k in expired:
        _PREVIEW_STORE.pop(k, None)


def _norm_role_label(label: str) -> str:
    """Normalize an assignee-field label into a role key.

    Backend pipes often carry near-duplicate labels for the same human role
    ("Reviewer" in three phases, "Tester(s)" vs "Tester"). The UI shows ONE
    role per key; at scan time the key expands back to every matching field
    in each pipe. Lowercased, whitespace collapsed, trailing "(s)" dropped.
    """
    s = " ".join((label or "").strip().lower().split())
    if s.endswith("(s)"):
        s = s[:-3].strip()
    return s


def _discover_resolve_user(client, pipes: List[Dict[str, str]], user_input: str, org_id_override):
    """Resolve a user org-wide (no specific pipe chosen yet).

    Pipefy user ids are org-global, so one successful resolution works for
    every pipe. We accumulate member pools pipe by pipe (disk-cached) plus
    the org user list, and stop at the first match. Raises
    UserResolutionError if the user is nowhere to be found.
    """
    members_pool: list = []
    org_users: list = []
    org_id = org_id_override
    last_err: Exception | None = None
    for p in pipes:
        try:
            ctx = fetch_pipe_context(client, p["id"])
        except Exception as exc:  # noqa: BLE001 - skip unreachable pipes
            log.warning("discover: could not fetch context for pipe %s: %s", p["id"], exc)
            last_err = exc
            continue
        members_pool.extend(ctx.get("members") or [])
        if not org_id:
            org_id = ctx.get("org_id")
        if org_id and not org_users:
            try:
                org_users = fetch_org_users(client, org_id)
            except Exception as exc:  # noqa: BLE001
                log.warning("discover: could not fetch org users: %s", exc)
        try:
            return resolve_user(user_input, members_pool, org_users)
        except UserResolutionError as exc:
            last_err = exc
    if isinstance(last_err, UserResolutionError):
        raise last_err
    raise UserResolutionError(f"Could not resolve {user_input!r} in any known pipe.")


INDEX_HTML = r"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>slipstream — Assignee Swap</title>
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 64 64'%3E%3Crect width='64' height='64' rx='14' fill='%23FFFFFF'/%3E%3Ctext x='32' y='50' font-family='Bodoni MT,Didot,Georgia,serif' font-size='52' font-weight='800' text-anchor='middle'%3E%3Ctspan fill='%23F2623A'%3Es%3C/tspan%3E%3Ctspan fill='%23F9BEC0'%3Es%3C/tspan%3E%3C/text%3E%3C/svg%3E">
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Bodoni+Moda:ital,wght@0,500;0,700;0,800;0,900;1,500;1,700;1,900&family=Inter:wght@400;500;600;700&display=swap">
  <style>
    :root {
      --bg: #FFFFFF;           /* the site is white-first */
      --surface: #FFFFFF;
      --surface-2: #F4F1ED;    /* warm neutral, like their imagery backdrops */
      --ink: #131211;          /* near-black, Up&Up logo */
      --ink-2: #363330;
      --muted: #8A847C;
      --rule: #E6E2DC;         /* hairline rules — light, airy */
      --border: #E6E2DC;
      --border-strong: #131211;
      --coral: #F2623A;        /* slipstream coral */
      --coral-deep: #C8392B;
      --coral-soft: #FBD3CB;
      --orange: #F2623A;       /* selection accent */
      --blush: #F9BEC0;        /* slipstream blush pink */
      --ok: #2E2B28;           /* success reads calm ink on blush — no traffic-light green */
      --ok-bg: #FBF0EB;
      --warn: #96652F;         /* warm terracotta-tan */
      --warn-bg: #F7EDDD;
      --danger: #C8392B;       /* deep coral — errors stay in the brand family */
      --danger-bg: #F2623A;
      --shadow: 0 24px 64px rgba(20, 18, 16, 0.10);
      --radius: 20px;          /* soft, rounded — like their pill menu */
    }
    * { box-sizing: border-box; }
    html, body { margin: 0; padding: 0; }
    body {
      background: var(--bg);
      color: var(--ink);
      font-family: "Inter", -apple-system, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
      font-size: 14px;
      line-height: 1.55;
      -webkit-font-smoothing: antialiased;
    }
    .serif {
      font-family: "Bodoni Moda", "Bodoni MT", Didot, Georgia, serif;
      font-weight: 800;
      letter-spacing: -0.01em;
    }

    /* ---------- Header ---------- */
    header {
      background: var(--bg);
      padding: 30px 40px 24px;
      border-bottom: 1px solid var(--rule);
    }
    .wrap { max-width: 1140px; margin: 0 auto; }
    .topbar { display: flex; align-items: flex-end; justify-content: space-between; gap: 24px; flex-wrap: wrap; }
    .brand {
      font-family: "Bodoni Moda", "Bodoni MT", Didot, Georgia, serif;
      font-weight: 800;
      font-size: 64px;
      line-height: 0.9;
      letter-spacing: -0.03em;
      user-select: none;
      color: var(--ink);
      text-transform: lowercase;
    }
    .brand .b1 { color: var(--coral); }
    .brand .b2 {
      color: var(--blush);
      font-family: "Inter", -apple-system, "Segoe UI", sans-serif;
      font-weight: 800;
      letter-spacing: -0.05em;
    }
    .brand .amp {
      font-style: italic;
      font-weight: 700;
      color: var(--ink);
      padding: 0 2px;
    }
    .sub {
      margin: 0;
      color: var(--ink);
      font-size: 11px;
      font-weight: 600;
      letter-spacing: 0.22em;
      text-transform: uppercase;
      text-align: right;
      max-width: 280px;
    }
    .sub .dot { color: var(--coral); margin: 0 6px; }

    /* Hero strip — sans-led statement with a serif accent word,
       echoing "CREATIVITY ELEVATES everything" on theupandupgroup.com */
    .hero {
      padding: 72px 40px 56px;
      border-bottom: 1px solid var(--rule);
      background: var(--bg);
    }
    .hero .wrap { display: grid; grid-template-columns: 1fr auto; gap: 40px; align-items: end; }
    .hero h1 {
      margin: 0;
      font-family: "Inter", -apple-system, "Segoe UI", sans-serif;
      font-weight: 800;
      font-size: clamp(48px, 7.5vw, 104px);
      line-height: 0.94;
      letter-spacing: -0.04em;
      text-transform: uppercase;
      color: var(--ink);
    }
    .hero h1 em {
      font-family: "Bodoni Moda", Georgia, serif;
      font-style: normal;
      font-weight: 700;
      letter-spacing: -0.02em;
      color: var(--coral);
    }
    .hero .kicker {
      font-size: 12px;
      font-weight: 600;
      letter-spacing: 0.2em;
      text-transform: uppercase;
      color: var(--ink);
      max-width: 220px;
      text-align: right;
      line-height: 1.5;
      padding-bottom: 6px;
    }
    .hero .kicker .rule { display: block; height: 2px; background: var(--ink); margin-bottom: 12px; width: 56px; margin-left: auto; }

    /* ---------- Layout ---------- */
    main { padding: 0 40px 96px; }
    .panel {
      background: transparent;
      border: 0;
      border-bottom: 1px solid var(--rule);
      border-radius: 0;
      box-shadow: none;
      padding: 36px 0 40px;
      margin: 0;
    }
    .panel:last-of-type { border-bottom: 0; }
    .panel-head {
      display: grid;
      grid-template-columns: 92px 1fr auto;
      align-items: baseline;
      gap: 20px;
      margin-bottom: 24px;
    }
    .panel-head .step {
      font-family: "Bodoni Moda", Georgia, serif;
      font-weight: 900;
      font-style: italic;
      font-size: 72px;
      line-height: 0.8;
      color: var(--coral);
      letter-spacing: -0.04em;
    }
    .panel-head h2 {
      margin: 0;
      font-family: "Inter", -apple-system, "Segoe UI", sans-serif;
      font-weight: 800;
      font-size: 30px;
      line-height: 1;
      color: var(--ink);
      letter-spacing: -0.03em;
      text-transform: uppercase;
    }
    .panel-head .hint {
      font-size: 10.5px;
      font-weight: 600;
      color: var(--muted);
      text-transform: uppercase;
      letter-spacing: 0.18em;
      text-align: right;
      max-width: 220px;
      line-height: 1.5;
    }

    /* ---------- Form ---------- */
    .row { display: flex; gap: 18px; flex-wrap: wrap; }
    .row > .col { flex: 1 1 240px; min-width: 0; }
    .operation-picker {
      margin-bottom: 24px;
      max-width: 420px;
    }
    .operation-picker label { margin-bottom: 8px; }
    .operation-select-wrap { position: relative; }
    .operation-select-wrap::after {
      content: "↓";
      position: absolute;
      right: 18px;
      top: 50%;
      transform: translateY(-50%);
      color: var(--coral);
      font-size: 16px;
      font-weight: 800;
      pointer-events: none;
    }
    #operationSelect {
      appearance: none;
      padding: 14px 48px 14px 20px;
      border: 1.5px solid var(--coral);
      background: var(--coral-soft);
      color: var(--ink);
      font-weight: 800;
      cursor: pointer;
    }
    #operationSelect:hover { background: #fff; }
    #operationSelect:focus-visible {
      outline: 3px solid var(--coral-soft);
      outline-offset: 3px;
    }
    .operation-description {
      margin: 8px 4px 0;
      color: var(--muted);
      font-size: 12.5px;
      line-height: 1.45;
    }
    label {
      display: block;
      font-size: 10.5px;
      color: var(--ink);
      margin-bottom: 8px;
      font-weight: 700;
      text-transform: uppercase;
      letter-spacing: 0.18em;
    }
    input[type=text], input[type=email], input[type=password], input[type=search], input[type=number], textarea, select {
      width: 100%;
      padding: 13px 20px;
      border: 1px solid var(--ink);
      border-radius: 999px;
      font-size: 14px;
      font-family: inherit;
      background: var(--surface);
      color: var(--ink);
      transition: border-color .12s, box-shadow .12s;
    }
    input:focus, textarea:focus, select:focus {
      outline: none;
      border-color: var(--coral);
      box-shadow: 0 0 0 3px var(--coral-soft);
    }
    textarea { resize: vertical; min-height: 84px; border-radius: 16px; font-family: ui-monospace, "SF Mono", Consolas, monospace; font-size: 12.5px; }

    /* ---------- Buttons — pill-shaped, like the site's floating menu ---------- */
    button {
      cursor: pointer;
      border: 1px solid var(--ink);
      border-radius: 999px;
      padding: 13px 26px;
      font-size: 11.5px;
      font-weight: 700;
      font-family: "Inter", sans-serif;
      text-transform: uppercase;
      letter-spacing: 0.18em;
      background: var(--surface);
      color: var(--ink);
      transition: background .12s, color .12s, border-color .12s, transform .04s;
    }
    button:hover { background: var(--ink); color: var(--bg); }
    button:active { transform: translateY(1px); }
    button.primary {
      background: var(--ink);
      color: var(--bg);
      border-color: var(--ink);
    }
    button.primary:hover { background: var(--coral); color: #fff; border-color: var(--coral); }
    button.ghost { background: transparent; border-color: var(--ink); }
    button.danger {
      background: var(--coral);
      color: #fff;
      border-color: var(--coral);
    }
    button.danger:hover { background: var(--ink); color: var(--bg); border-color: var(--ink); }
    button:disabled { opacity: 0.35; cursor: not-allowed; }
    button:disabled:hover { border-color: var(--ink); background: var(--surface); color: var(--ink); }
    button.primary:disabled:hover { background: var(--ink); color: var(--bg); border-color: var(--ink); }
    button.danger:disabled:hover { background: var(--coral); color: #fff; border-color: var(--coral); }
    button + button { margin-left: 10px; }

    /* Pipe chips — horizontal, wrapping selector replacing the legacy <select> */
    .pipe-chips {
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      margin-top: 4px;
    }
    .pipe-chip {
      display: inline-block;
      padding: 8px 14px;
      border: 1px solid var(--border-strong);
      background: var(--surface);
      color: var(--ink);
      font-size: 12px;
      font-weight: 600;
      letter-spacing: 0.04em;
      cursor: pointer;
      user-select: none;
      transition: background .12s, color .12s;
      font-family: inherit;
    }
    .pipe-chip:hover {
      background: var(--coral);
      color: #fff;
      border-color: var(--coral);
    }
    .pipe-chip.active {
      background: var(--ink);
      color: var(--bg);
      border-color: var(--ink);
    }
    .pipe-chip.active:hover { background: var(--coral); color: #fff; border-color: var(--coral); }

    .actions { margin-top: 18px; display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
    .spinner {
      display: none;
      width: 14px; height: 14px;
      border: 2px solid var(--coral-soft);
      border-top-color: var(--coral);
      border-radius: 50%;
      animation: spin 0.8s linear infinite;
    }
    .spinner.on { display: inline-block; }
    @keyframes spin { to { transform: rotate(360deg); } }

    .badge { display: inline-block; padding: 4px 12px; border-radius: 999px; font-size: 10px; font-weight: 700; letter-spacing: 0.16em; text-transform: uppercase; border: 1px solid currentColor; }
    .badge.ok { background: transparent; color: var(--coral); }
    .badge.warn { background: transparent; color: var(--warn); }

    /* Phase scan strip — compact two-row table so long pipes
       don't produce a 10-line vertical staircase. */
    .phase-strip {
      display: grid;
      grid-template-columns: auto 1fr;
      gap: 6px 14px;
      align-items: baseline;
      margin: 14px 0 4px;
      padding: 10px 14px;
      border: 1px solid var(--border);
      background: var(--surface);
    }
    .phase-strip .ps-label {
      font-size: 10px;
      font-weight: 700;
      letter-spacing: 0.16em;
      text-transform: uppercase;
      color: var(--muted);
      white-space: nowrap;
    }
    .phase-strip .ps-pills {
      display: flex;
      flex-wrap: wrap;
      gap: 6px;
      min-width: 0;
    }
    .phase-strip .ps-pill {
      display: inline-flex;
      align-items: baseline;
      gap: 6px;
      padding: 3px 8px;
      font-size: 10.5px;
      font-weight: 600;
      letter-spacing: 0.04em;
      border: 1px solid currentColor;
      line-height: 1.3;
      white-space: nowrap;
    }
    .phase-strip .ps-pill.warn { color: var(--warn); }
    .phase-strip .ps-pill.ok { color: var(--ok); }
    .phase-strip .ps-pill .ps-count {
      font-weight: 700;
      opacity: 0.75;
      font-variant-numeric: tabular-nums;
    }
    .badge.err { background: transparent; color: var(--danger); }

    .alert {
      padding: 14px 18px; border-radius: 14px; margin-top: 14px;
      font-size: 13px; border: 1px solid var(--border); border-left-width: 5px;
      font-weight: 500;
    }
    .alert.error { background: var(--danger-bg); border-color: var(--danger); color: var(--danger); }
    .alert.warn  { background: var(--warn-bg); border-color: var(--warn); color: var(--warn); }
    .alert.ok    { background: var(--ok-bg); border-color: var(--coral); color: var(--ink); }

    /* ---------- Resolved users summary ---------- */
    .resolved {
      background: var(--surface-2);
      border: 1px solid var(--border);
      border-radius: var(--radius);
      padding: 20px 24px;
      font-size: 13.5px;
    }
    .resolved .label { color: var(--ink); font-size: 10.5px; text-transform: uppercase; letter-spacing: 0.18em; font-weight: 700; }
    .resolved .swap {
      display: flex; align-items: center; gap: 18px; margin-top: 10px;
      flex-wrap: wrap;
    }
    .resolved .user { display: flex; flex-direction: column; }
    .resolved .user b { font-family: "Bodoni Moda", Georgia, serif; font-weight: 700; font-size: 18px; color: var(--ink); letter-spacing: -0.01em; }
    .resolved .user .email { color: var(--muted); font-size: 12px; font-family: ui-monospace, Consolas, monospace; }
    .resolved .arrow { color: var(--coral); font-size: 28px; line-height: 1; font-family: "Bodoni Moda", Georgia, serif; font-style: italic; font-weight: 700; }

    /* ---------- Field list ---------- */
    .field-list {
      max-height: 300px; overflow-y: auto;
      border: 1px solid var(--border);
      border-radius: var(--radius);
      background: var(--surface);
      padding: 0;
    }
    .field-list .row-head {
      padding: 8px 14px;
      background: var(--surface-2);
      font-size: 10px;
      font-weight: 700;
      text-transform: uppercase;
      letter-spacing: 0.16em;
      color: var(--muted);
      border-bottom: 1px solid var(--border);
    }
    .field-list .row-item {
      display: flex; align-items: center; gap: 12px;
      padding: 10px 14px;
      border-bottom: 1px solid var(--border);
      cursor: pointer;
    }
    .field-list .row-item:last-child { border-bottom: 0; }
    .field-list .row-item:hover { background: var(--surface-2); }
    .field-list input[type=checkbox] { margin: 0; accent-color: var(--coral); width: 16px; height: 16px; }
    /* Per-card selection checkboxes in the preview/results table use orange so
       they read as a distinct "what will I apply" control, separate from the
       coral scope checkboxes above. */
    .card-pick, #selectAllCards { accent-color: var(--orange); width: 16px; height: 16px; cursor: pointer; }
    .card-pick:disabled { accent-color: var(--muted); cursor: default; }

    /* Preview filter toolbar: text search + per-client / per-phase toggle chips */
    .plan-filters {
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      gap: 10px 16px;
      margin: 0 0 10px;
      padding: 10px 12px;
      background: var(--surface-2);
      border: 1px solid var(--border);
      border-radius: 8px;
    }
    .plan-filters input[type=search] {
      flex: 1 1 240px;
      min-width: 180px;
      padding: 8px 16px;
      border: 1px solid var(--border-strong);
      border-radius: 999px;
      background: var(--surface);
      color: var(--ink);
      font-family: inherit;
      font-size: 13px;
    }
    .filter-group { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
    .fg-label {
      font-size: 10px; text-transform: uppercase; letter-spacing: 0.06em;
      color: var(--muted); font-weight: 700;
    }

    /* Dropdown multiselect filters (Client / Phase) */
    .filter-dd { position: relative; }
    .filter-dd-btn {
      display: inline-flex; align-items: center; gap: 8px;
      padding: 7px 12px;
      border: 1px solid var(--border-strong);
      border-radius: 6px;
      background: var(--surface);
      color: var(--ink);
      font-family: inherit; font-size: 13px; font-weight: 600;
      cursor: pointer;
    }
    .filter-dd-btn:hover { border-color: var(--coral); }
    .filter-dd-btn .dd-badge {
      font-size: 11px; font-weight: 700;
      padding: 1px 7px; border-radius: 999px;
      background: var(--orange); color: #fff;
    }
    .filter-dd-btn .dd-caret { color: var(--muted); font-size: 11px; }
    .filter-dd-panel {
      position: absolute; z-index: 40; top: calc(100% + 4px); left: 0;
      min-width: 220px; max-width: 320px;
      background: var(--surface);
      border: 1px solid var(--border-strong);
      border-radius: 8px;
      box-shadow: 0 8px 24px rgba(14,14,14,0.18);
      padding: 8px;
    }
    .dd-actions { display: flex; gap: 6px; margin-bottom: 6px; }
    .dd-actions button {
      flex: 1; padding: 5px 8px;
      border: 1px solid var(--border); border-radius: 5px;
      background: var(--surface-2); color: var(--ink);
      font-family: inherit; font-size: 11px; font-weight: 600; cursor: pointer;
    }
    .dd-actions button:hover { border-color: var(--coral); color: var(--coral); }
    .dd-list { max-height: 260px; overflow-y: auto; display: flex; flex-direction: column; gap: 2px; }
    .dd-opt {
      display: flex; align-items: center; gap: 8px;
      padding: 5px 6px; border-radius: 5px;
      font-size: 13px; cursor: pointer; user-select: none;
    }
    .dd-opt:hover { background: var(--surface-2); }
    .dd-opt input { accent-color: var(--orange); width: 15px; height: 15px; cursor: pointer; }
    .dd-opt .dd-opt-name { flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
    .dd-opt .dd-opt-n { color: var(--muted); font-size: 11px; font-variant-numeric: tabular-nums; }

    /* Expandable per-card "what changes" detail row */
    .row-exp {
      border: none; background: none; cursor: pointer;
      color: var(--muted); font-size: 12px; line-height: 1;
      padding: 0 6px 0 0; font-family: inherit;
    }
    .row-exp:hover { color: var(--coral); }
    .row-exp[aria-expanded="true"] { color: var(--coral); }
    tr.detail-row > td { background: var(--surface-2); padding: 8px 12px; }
    .chg { display: flex; flex-wrap: wrap; align-items: baseline; gap: 6px; margin: 2px 0; font-size: 12px; }
    .chg-scope { font-weight: 700; color: var(--ink); }
    .chg-before { color: var(--muted); }
    .chg-arrow { color: var(--coral); font-weight: 700; }
    .chg-after { color: var(--ink); font-weight: 600; }
    .card-client { color: var(--muted); font-size: 11px; }
    .field-list .name { font-size: 13.5px; color: var(--ink); flex: 1; font-weight: 500; }
    .field-list .pill {
      font-size: 9.5px; color: var(--coral); font-weight: 700;
      background: transparent; border: 1px solid currentColor;
      padding: 2px 10px; border-radius: 999px;
      text-transform: uppercase; letter-spacing: 0.14em;
    }
    .field-list .meta {
      display: none;
      color: var(--muted);
      font-family: ui-monospace, Consolas, monospace;
      font-size: 11.5px;
      margin-left: 8px;
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
    }
    .field-list.show-meta .meta { display: inline; }

    /* ---------- Results ---------- */
    .summary-grid {
      display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); gap: 0;
      margin-top: 4px;
      border: 1.5px solid var(--ink);
    }
    .tile {
      background: var(--surface);
      border: 0;
      border-right: 1px solid var(--ink);
      border-radius: 0;
      padding: 22px 18px;
      text-align: left;
    }
    .tile:last-child { border-right: 0; }
    .tile .val {
      font-family: "Bodoni Moda", Georgia, serif;
      font-size: 56px; font-weight: 900; line-height: 0.9;
      color: var(--ink);
      letter-spacing: -0.03em;
    }
    .tile .lbl {
      font-size: 10px; color: var(--ink); font-weight: 700;
      margin-top: 10px;
      text-transform: uppercase; letter-spacing: 0.2em;
    }
    .tile.accent .val { color: var(--coral); font-style: italic; }

    table { width: 100%; border-collapse: collapse; font-size: 13px; }
    th, td { text-align: left; padding: 12px 14px; border-bottom: 1px solid var(--border); vertical-align: top; }
    th {
      background: var(--surface-2);
      font-weight: 700; color: var(--muted);
      text-transform: uppercase; font-size: 10px; letter-spacing: 0.16em;
      position: sticky; top: 0; z-index: 1;
      border-bottom: 1px solid var(--border);
    }
    tbody tr:hover { background: var(--surface-2); }
    td.num { text-align: right; font-variant-numeric: tabular-nums; color: var(--muted); font-family: ui-monospace, Consolas, monospace; }
    a { color: var(--coral); text-decoration: none; font-weight: 600; border-bottom: 1px solid transparent; }
    a:hover { border-bottom-color: var(--coral); }

    .hidden { display: none !important; }
    .small { font-size: 12px; color: var(--muted); }
    .mono { font-family: ui-monospace, Consolas, monospace; font-size: 12.5px; }

    /* ---------- Developer panel ---------- */
    .dev {
      margin-top: 36px;
      border: 1px solid var(--border);
      border-radius: var(--radius);
      background: var(--surface);
      overflow: hidden;
    }
    .dev > summary {
      cursor: pointer;
      padding: 16px 22px;
      list-style: none;
      display: flex; align-items: center; gap: 12px;
      font-size: 11px;
      font-weight: 700;
      letter-spacing: 0.2em;
      text-transform: uppercase;
      color: var(--ink);
      user-select: none;
      background: var(--bg);
    }
    .dev > summary::-webkit-details-marker { display: none; }
    .dev > summary::before {
      content: "+";
      display: inline-block;
      font-family: "Bodoni Moda", Georgia, serif;
      font-size: 22px;
      line-height: 0.6;
      color: var(--coral);
      transition: transform .15s;
      font-weight: 700;
    }
    .dev[open] > summary::before { content: "–"; }
    .dev > summary .tag {
      margin-left: auto;
      color: var(--muted);
      font-family: ui-monospace, Consolas, monospace;
      font-size: 10.5px;
      letter-spacing: 0.08em;
      text-transform: none;
      font-weight: 400;
    }
    .dev-body { padding: 8px 22px 24px; border-top: 1px solid var(--border); }
    .dev-body h4 {
      margin: 18px 0 8px;
      font-size: 11px;
      text-transform: uppercase;
      letter-spacing: 0.08em;
      color: var(--muted);
      font-weight: 600;
    }
    .dev-body h4:first-child { margin-top: 14px; }
    .check {
      display: inline-flex; align-items: center; gap: 6px;
      font-size: 13px; cursor: pointer;
    }
    .check input { accent-color: var(--coral); }
    .log {
      background: #1B1917;
      color: #F2EFEA;
      font-family: ui-monospace, "SF Mono", Consolas, monospace;
      font-size: 12px;
      line-height: 1.55;
      padding: 14px 16px;
      border-radius: 14px;
      height: 260px;
      overflow-y: auto;
      white-space: pre-wrap;
      word-break: break-word;
      border: 1px solid var(--border);
    }
    .log .lvl-WARNING { color: #F8C572; }
    .log .lvl-ERROR { color: #F49890; }
    .log .lvl-DEBUG { color: #9D8A7C; }
    .log .ts { color: #8A7E76; }
    .log-bar {
      display: flex; align-items: center; gap: 10px;
      margin-bottom: 8px;
    }
    .log-bar .small { margin-left: auto; }

    /* ---------- Modal ---------- */
    .modal-backdrop {
      position: fixed; inset: 0; background: rgba(20, 18, 16, 0.45);
      display: flex; align-items: center; justify-content: center;
      z-index: 100;
      backdrop-filter: blur(3px);
    }
    .modal {
      background: var(--surface);
      border: 1px solid var(--border);
      border-radius: 28px;
      padding: 36px 38px;
      max-width: 480px;
      width: 92%;
      box-shadow: var(--shadow);
    }
    .modal h3 {
      margin: 0 0 14px;
      font-family: "Inter", -apple-system, sans-serif;
      font-weight: 800;
      font-size: 28px;
      line-height: 1;
      text-transform: uppercase;
      letter-spacing: -0.03em;
    }
    .modal p { margin: 0 0 22px; color: var(--ink-2); font-size: 14px; line-height: 1.55; }
    .modal .actions { justify-content: flex-end; margin-top: 0; }
    .modal.modal-wide { max-width: 920px; }
    .modal.modal-wide .actions { margin-top: 18px; align-items: center; }

    /* ---------- scan summary card (step 04 header) ---------- */
    .scan-card {
      border: 1px solid var(--border);
      border-top: 4px solid var(--coral);
      border-radius: var(--radius);
      background: var(--surface);
      box-shadow: 0 10px 30px rgba(20, 18, 16, 0.06);
      overflow: hidden;
      margin-bottom: 24px;
    }
    .scan-card .sc-body {
      display: flex;
      gap: 22px;
      align-items: flex-start;
      padding: 26px 28px 22px;
    }
    .scan-card .sc-count {
      flex: 0 0 auto;
      width: 64px; height: 64px;
      border-radius: 18px;
      background: var(--coral);
      color: #fff;
      display: flex; align-items: center; justify-content: center;
      font-family: "Inter", sans-serif;
      font-weight: 800;
      font-size: 26px;
      letter-spacing: -0.02em;
    }
    .scan-card .sc-head {
      font-size: 19px;
      font-weight: 700;
      letter-spacing: -0.01em;
      color: var(--ink);
      line-height: 1.3;
    }
    .scan-card .sc-head .who { color: var(--coral); }
    .scan-card .sc-sub { color: var(--muted); font-size: 13.5px; margin-top: 4px; line-height: 1.5; }
    .scan-card .sc-chips { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 14px; }
    .scan-card .sc-chip {
      display: inline-flex; align-items: center; gap: 8px;
      padding: 7px 15px;
      border: 0;
      border-radius: 999px;
      background: var(--coral-soft);
      color: var(--coral-deep);
      font-family: inherit;
      font-size: 12.5px;
      font-weight: 600;
      letter-spacing: 0.01em;
      text-transform: none;
      cursor: pointer;
    }
    .scan-card .sc-chip:hover { background: var(--coral); color: #fff; }
    .scan-card .sc-chip .dot { width: 6px; height: 6px; border-radius: 50%; background: currentColor; }
    .scan-card .sc-chip b { font-weight: 800; }
    .scan-card .sc-foot {
      display: flex; align-items: center; justify-content: space-between; gap: 16px;
      padding: 14px 28px;
      background: var(--surface-2);
      border-top: 1px solid var(--border);
    }
    .scan-card .sc-foot .note { color: var(--muted); font-size: 13px; }
    .scan-card .sc-foot button { padding: 10px 22px; }

    /* ---------- results grouped by pipe ---------- */
    .pipe-group {
      border: 1px solid var(--border);
      border-radius: var(--radius);
      background: var(--surface);
      margin-bottom: 24px;
      overflow: hidden;
      box-shadow: 0 10px 30px rgba(20, 18, 16, 0.05);
    }
    .pipe-group .pg-head {
      display: flex;
      align-items: baseline;
      gap: 14px;
      flex-wrap: wrap;
      padding: 14px 18px;
      border-bottom: 1px solid var(--border);
    }
    .pipe-group .pg-head h3 {
      margin: 0;
      font-family: "Bodoni Moda", Georgia, serif;
      font-weight: 800;
      font-size: 22px;
      letter-spacing: -0.01em;
    }
    .pipe-group .pg-sub { color: var(--muted); font-size: 12px; }
    .pipe-group .pg-status { margin-left: auto; font-size: 12px; color: var(--muted); }
    .pipe-group .pg-body { max-height: 420px; overflow-y: auto; }
    .pipe-group .pg-actions {
      display: flex;
      align-items: center;
      gap: 12px;
      padding: 12px 18px;
      border-top: 1px solid var(--border);
    }
    .pipe-group .pg-empty { padding: 14px 18px; color: var(--muted); font-size: 13px; }
    .pipe-group .pg-alert { margin: 12px 18px; }

    @media (max-width: 640px) {
      header, .hero { padding-left: 20px; padding-right: 20px; }
      main { padding-left: 20px; padding-right: 20px; }
      .operation-picker { max-width: none; }
      .panel-head { grid-template-columns: 64px 1fr; }
      .panel-head .step { font-size: 54px; }
      .panel-head .hint { grid-column: 2; text-align: left; max-width: none; }
      .scan-card .sc-body { padding: 22px 20px; }
      .scan-card .sc-foot { align-items: flex-start; flex-direction: column; padding: 14px 20px; }
    }
  </style>
</head>
<body>

<!-- Sign-in gate — blocks the whole app until a valid company email (and,
     when hosted, the shared password) is accepted by the server. The server
     enforces this on every API route; the overlay is just the front door. -->
<div id="gateBackdrop" class="modal-backdrop">
  <div class="modal">
    <h3>Who's swapping?</h3>
    <p>Enter your company email so we can keep an audit trail of who changed what.
       Only approved Up&amp;Up group domains are allowed.</p>
    <div class="col" style="margin-bottom: 14px;">
      <label for="gateEmail">Company email</label>
      <input id="gateEmail" type="email" placeholder="you@upandupgroup.com" autocomplete="email">
    </div>
    <div class="col hidden" id="gatePwRow" style="margin-bottom: 14px;">
      <label for="gatePw">Access password</label>
      <input id="gatePw" type="password" placeholder="Shared team password" autocomplete="current-password">
    </div>
    <div id="gateError" class="alert error hidden" style="margin-bottom: 14px;"></div>
    <div class="actions">
      <button id="gateGo" class="primary">Continue</button>
      <span id="gateSpin" class="spinner"></span>
    </div>
    <p class="small" id="gateDomains" style="margin: 16px 0 0; color: var(--muted);"></p>
  </div>
</div>

<header>
  <div class="wrap topbar">
    <div class="brand"><span class="b1">slip</span><span class="b2">stream</span></div>
    <div class="sub">An Up<span class="amp">&amp;</span>Up Tool <span class="dot">&bull;</span> Pipefy Operations</div>
  </div>
</header>

<section class="hero">
  <div class="wrap">
    <h1>Swap <em>people</em>,<br>not pipelines.</h1>
    <div class="kicker"><span class="rule"></span>Bulk reassignment for Pipefy &mdash; preview every change before a single card moves.</div>
  </div>
</section>

<main class="wrap">

  <!-- Step 1: who -->
  <section class="panel">
    <div class="panel-head">
      <span class="step">01</span>
      <h2 id="step1Title">Who's being replaced?</h2>
      <span class="hint">Email is preferred — names can be ambiguous</span>
    </div>
    <div class="operation-picker">
      <label for="operationSelect">What do you want to do?</label>
      <div class="operation-select-wrap">
        <select id="operationSelect" aria-describedby="operationDescription">
          <option value="replace">Replace assignee</option>
          <option value="add">Add co-assignee</option>
        </select>
      </div>
      <div id="operationDescription" class="operation-description">
        Move selected role assignments from one person to another.
      </div>
    </div>
    <div class="row">
      <div class="col">
        <label for="oldUser" id="oldUserLabel">Remove user</label>
        <input id="oldUser" type="text" placeholder="leaving@dydx.digital" autocomplete="off">
      </div>
      <div class="col">
        <label for="newUser" id="newUserLabel">Add user</label>
        <input id="newUser" type="text" placeholder="replacement@dydx.digital" autocomplete="off">
      </div>
    </div>
    <div class="actions">
      <button id="usersBtn" class="primary">Continue</button>
      <span id="usersSpin" class="spinner"></span>
      <span id="usersStatus" class="small"></span>
    </div>
    <div id="usersAlerts"></div>
  </section>

  <!-- Step 2: pipes -->
  <section id="pipesPanel" class="panel hidden">
    <div class="panel-head">
      <span class="step">02</span>
      <h2>Pick pipes to check</h2>
      <span class="hint">Tick the pipes you want to scan</span>
    </div>
    <div class="resolved" style="margin-bottom: 18px;">
      <div class="label" id="resolvedOperationLabel">Swap</div>
      <div id="resolvedUsers" class="swap"></div>
    </div>
    <div class="col" style="max-width: 640px;">
      <label>Pipes to scan <span class="small" id="pipePickCount"></span></label>
      <div id="pipePickList" class="field-list"></div>
      <!--div class="small" style="margin-top: 8px; color: var(--muted);">
        Pipefy can't reliably tell us which pipes someone works in (user-group
        access is invisible to the API) — so the scan simply checks every pipe
        you leave ticked.
      </div-->
    </div>
    <div class="actions">
      <button id="pipesBtn" class="primary">Continue</button>
      <span id="pipesSpin" class="spinner"></span>
      <span id="pipesStatus" class="small"></span>
    </div>
    <div id="pipesAlerts"></div>
  </section>

  <!-- Step 3: roles + phases -->
  <section id="phasesPanel" class="panel hidden">
    <div class="panel-head">
      <span class="step">03</span>
      <h2>Narrow the search</h2>
      <span class="hint">Fewer roles = faster scan · ticked phases are skipped</span>
    </div>
    <div class="row">
      <div class="col" style="flex: 1 1 300px; max-width: 520px;">
        <label id="roleListLabel">Roles to swap <span class="small" id="rolePickCount"></span></label>
        <div id="rolePickList" class="field-list"></div>
        <div class="small" id="roleHelp" style="margin-top: 8px; color: var(--muted);">
          Tick the role(s) this person holds — e.g. Account Manager. Ticking
          everything searches every assignee field, which is slower.
        </div>
      </div>
      <div class="col" style="flex: 1 1 300px; max-width: 520px;">
        <label>Skip these phases <span class="small" id="phasePickCount"></span></label>
        <div id="phasePickList" class="field-list"></div>
        <div class="small" style="margin-top: 8px; color: var(--muted);">
          Cards sitting in a ticked phase are left untouched (Completed, Cancelled, etc.).
        </div>
      </div>
    </div>
    <div class="actions">
      <button id="scanBtn" class="primary">Find their jobs</button>
      <span id="scanSpin" class="spinner"></span>
      <span id="scanStatus" class="small"></span>
    </div>
    <div id="phasesAlerts"></div>
  </section>

  <!-- Step 4: results, grouped by pipe -->
  <section id="resultsPanel" class="panel hidden">
    <div class="panel-head">
      <span class="step">04</span>
      <h2 id="resultsTitle">Their jobs, by pipe</h2>
      <span class="hint" id="resultsHint">Nothing changes in Pipefy until you click Apply on a pipe.</span>
    </div>
    <div id="scanSummary" class="alert hidden" style="margin-bottom: 16px;"></div>
    <div id="pipeGroups"></div>
  </section>

  <!-- History panel -->
  <details class="dev" id="historyPanel">
    <summary>
      <span>History</span>
      <span class="tag">recent applies &middot; last 30 days</span>
    </summary>
    <div class="dev-body">
      <div class="log-bar">
        <button id="historyRefresh" class="ghost" style="padding: 4px 10px; font-size: 12px;">Refresh</button>
        <span class="small" id="historyStatus">&mdash;</span>
      </div>
      <div style="max-height: 360px; overflow-y: auto; border: 1px solid var(--border); border-radius: 8px;">
        <table>
          <thead>
            <tr>
              <th style="width: 150px;">When</th>
              <th>Operator</th>
              <th>Card</th>
              <th style="width: 90px;">Scope</th>
              <th>Before &rarr; After</th>
              <th style="width: 80px;">Status</th>
            </tr>
          </thead>
          <tbody id="historyTbody"></tbody>
        </table>
      </div>
    </div>
  </details>

  <!-- Developer panel -->
  <details class="dev" id="devPanel">
    <summary>
      <span>Developer</span>
      <span class="tag">advanced settings · live log</span>
    </summary>
    <div class="dev-body">

      <h4>Advanced settings</h4>
      <div class="row">
        <div class="col">
          <label for="orgId">Org ID override <span class="small">(optional)</span></label>
          <input id="orgId" type="text" placeholder="auto-detected" autocomplete="off">
        </div>
        <div class="col" style="flex: 0 0 180px;">
          <label for="throttleMs">Throttle between writes (ms)</label>
          <input id="throttleMs" type="number" min="0" step="50" value="250">
        </div>
        <div class="col" style="flex: 0 0 200px;">
          <label>Field display</label>
          <label class="check" style="margin-top: 4px;">
            <input id="showMetaCb" type="checkbox">
            <span>Show field slug &amp; internal_id</span>
          </label>
        </div>
      </div>

      <h4>Activity log</h4>
      <div class="log-bar">
        <label class="check">
          <input id="logFollow" type="checkbox" checked>
          <span>Auto-scroll</span>
        </label>
        <label class="check">
          <input id="logDebug" type="checkbox">
          <span>Show DEBUG</span>
        </label>
        <button id="logClear" class="ghost" style="padding: 4px 10px; font-size: 12px;">Clear view</button>
        <span class="small" id="logStatus">idle</span>
      </div>
      <div class="log" id="logView"></div>

    </div>
  </details>

</main>

<!-- Confirm modal -->
<div id="confirmModal" class="modal-backdrop hidden">
  <div class="modal">
    <h3>Apply changes?</h3>
    <p id="confirmText"></p>
    <div class="actions">
      <button id="confirmCancel" class="ghost">Cancel</button>
      <button id="confirmGo" class="danger">Yes, apply</button>
    </div>
  </div>
</div>

<!-- Recent applies modal -->
<div id="recentModal" class="modal-backdrop hidden">
  <div class="modal modal-wide">
    <h3>Recent applies</h3>
    <p class="small" style="margin: 0 0 14px; color: var(--muted);">
      Last 30 days from this machine's local audit log.
      <span id="recentSyncNote"></span>
    </p>
    <div id="recentBody" style="max-height: 52vh; overflow-y: auto; border: 1px solid var(--border); border-radius: 8px;">
      <table>
        <thead>
          <tr>
            <th style="width: 150px;">When</th>
            <th>Operator</th>
            <th>Card</th>
            <th style="width: 90px;">Scope</th>
            <th>Before &rarr; After</th>
            <th style="width: 80px;">Status</th>
          </tr>
        </thead>
        <tbody id="recentTbody"></tbody>
      </table>
    </div>
    <div class="actions">
      <span class="small" id="recentStatus" style="margin-right: auto; color: var(--muted);"></span>
      <button id="recentClose" class="ghost">Close</button>
    </div>
  </div>
</div>

<script>
const $ = (id) => document.getElementById(id);

let operatorEmail = '';  // set once the sign-in gate is cleared

// ---------- helpers ----------
async function api(path, body) {
  const r = await fetch(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body || {}),
  });
  const data = await r.json().catch(() => ({}));
  if (r.status === 401) { showGate(); throw new Error('Session expired — sign in again.'); }
  if (!r.ok) throw new Error(data.error || ('HTTP ' + r.status));
  return data;
}
function setBusy(spinnerId, statusId, busy, msg) {
  $(spinnerId).classList.toggle('on', !!busy);
  const st = statusId && $(statusId);
  if (st) st.textContent = msg || '';
}
function clearAlerts(id) { $(id).innerHTML = ''; }
function alertEl(kind, html) {
  const d = document.createElement('div');
  d.className = 'alert ' + kind;
  d.innerHTML = html;
  return d;
}
function escapeHtml(s) {
  return String(s ?? '').replace(/[&<>"']/g, c => ({
    '&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'
  }[c]));
}
function escapeAttr(s) { return escapeHtml(s); }

// ---------- wizard state ----------
// One group per scanned pipe: token + plan rows + apply controls.
const wizGroups = new Map();
let wizUsers = null;   // response of /api/wizard/users (resolved users + membership)
let wizOperation = 'replace';

function setWizardOperation(operation) {
  wizOperation = operation === 'add' ? 'add' : 'replace';
  $('operationSelect').value = wizOperation;
  const adding = wizOperation === 'add';
  $('operationDescription').textContent = adding
    ? 'Add someone to matching roles without removing any existing assignees.'
    : 'Move selected role assignments from one person to another.';
  $('step1Title').textContent = adding ? 'Add someone alongside an assignee' : "Who's being replaced?";
  $('oldUserLabel').textContent = adding ? 'Existing assignee' : 'Remove user';
  $('newUserLabel').textContent = adding ? 'Assignee to add' : 'Add user';
  $('oldUser').placeholder = adding ? 'existing.assignee@dydx.digital' : 'leaving@dydx.digital';
  $('newUser').placeholder = adding ? 'new.assignee@dydx.digital' : 'replacement@dydx.digital';
  $('resolvedOperationLabel').textContent = adding ? 'Add alongside' : 'Swap';
  $('roleListLabel').childNodes[0].nodeValue = adding ? 'Roles to add ' : 'Roles to swap ';
  $('roleHelp').textContent = adding
    ? 'Tick the role(s) where the new person should be added. Existing assignees remain in place.'
    : "Tick the role(s) this person holds — e.g. Account Manager. Ticking everything searches every assignee field, which is slower.";
  $('oldUser').value = '';
  $('newUser').value = '';
  clearAlerts('usersAlerts');
  $('usersStatus').textContent = '';
  $('pipePickList').innerHTML = '';
  $('rolePickList').innerHTML = '';
  $('phasePickList').innerHTML = '';
  $('pipeGroups').innerHTML = '';
  $('scanSummary').className = 'alert hidden';
  ['pipesPanel', 'phasesPanel', 'resultsPanel'].forEach(id => $(id).classList.add('hidden'));
  wizGroups.clear();
  wizUsers = null;
  $('oldUser').focus();
}

$('operationSelect').addEventListener('change', (event) => {
  setWizardOperation(event.target.value);
});

// Upgrade a busy-status message if a single call drags on — the free-tier
// server sleeps after ~15 min idle and takes ~30–60s to wake.
function slowHint(statusId, baseMsg) {
  const t = setTimeout(() => {
    const el = $(statusId);
    if (el && el.textContent === baseMsg) {
      el.textContent = baseMsg + ' Still working \u2014 the server may be waking up (~30\u201360s).';
    }
  }, 8000);
  return () => clearTimeout(t);
}

// ---------- step 1: resolve users ----------
$('usersBtn').addEventListener('click', async () => {
  clearAlerts('usersAlerts');
  ['pipesPanel', 'phasesPanel', 'resultsPanel'].forEach(id => $(id).classList.add('hidden'));
  const oldUser = $('oldUser').value.trim();
  const newUser = $('newUser').value.trim();
  if (!oldUser || !newUser) {
    $('usersAlerts').appendChild(alertEl('error', wizOperation === 'add'
      ? 'Enter both the person whose jobs should be found and the person to add.'
      : 'Both the remove user and the add user are required.'));
    return;
  }
  const baseMsg = 'Resolving users\u2026';
  setBusy('usersSpin', 'usersStatus', true, baseMsg);
  const cancelHint = slowHint('usersStatus', baseMsg);
  try {
    const data = await api('/api/wizard/users', {
      old_user: oldUser, new_user: newUser, operation: wizOperation,
      org_id: $('orgId').value.trim() || null,
    });
    wizUsers = data;
    setBusy('usersSpin', 'usersStatus', false, '');
    renderResolvedUsers(data);
    renderPipePick(data);
    $('pipesPanel').classList.remove('hidden');
    $('pipesPanel').scrollIntoView({ behavior: 'smooth', block: 'start' });
  } catch (e) {
    setBusy('usersSpin', 'usersStatus', false, '');
    $('usersAlerts').appendChild(alertEl('error', escapeHtml(e.message)));
  } finally {
    cancelHint();
  }
});

function renderResolvedUsers(data) {
  const oldU = data.old_user, newU = data.new_user;
  const operation = data.operation || wizOperation;
  $('resolvedUsers').innerHTML = `
    <div class="user">
      <b>${escapeHtml(oldU.name || '(no name)')}</b>
      <span class="email">${escapeHtml(oldU.email)}</span>
    </div>
    <span class="arrow">${operation === 'add' ? '+' : '→'}</span>
    <div class="user">
      <b>${escapeHtml(newU.name || '(no name)')}</b>
      <span class="email">${escapeHtml(newU.email)}</span>
    </div>
  `;
}

function renderPipePick(data) {
  const host = $('pipePickList');
  host.innerHTML = '';
  const pipes = data.pipes || [];
  pipes.forEach(p => {
    const row = document.createElement('label');
    row.className = 'row-item';
    row.innerHTML = `
      <input type="checkbox" data-pipe="${escapeAttr(p.id)}">
      <span class="name">${escapeHtml(p.name)}</span>
    `;
    host.appendChild(row);
  });
  $('pipePickCount').textContent = pipes.length
    ? `(${pipes.length} pipe${pipes.length === 1 ? '' : 's'} — tick the ones to search)` : '';
  $('pipesBtn').disabled = pipes.length === 0;
}

function getSelectedPipeIds() {
  return Array.from(document.querySelectorAll('#pipePickList input[type=checkbox]:checked'))
    .map(cb => cb.dataset.pipe);
}

// ---------- step 2 -> 3: phases across the selected pipes ----------
$('pipesBtn').addEventListener('click', async () => {
  clearAlerts('pipesAlerts');
  ['phasesPanel', 'resultsPanel'].forEach(id => $(id).classList.add('hidden'));
  const pipeIds = getSelectedPipeIds();
  if (!pipeIds.length) {
    $('pipesAlerts').appendChild(alertEl('error', 'Tick at least one pipe.'));
    return;
  }
  const baseMsg = 'Collecting phases across the selected pipes\u2026';
  setBusy('pipesSpin', 'pipesStatus', true, baseMsg);
  const cancelHint = slowHint('pipesStatus', baseMsg);
  try {
    const data = await api('/api/wizard/phases', { pipe_ids: pipeIds });
    setBusy('pipesSpin', 'pipesStatus', false, '');
    (data.errors || []).forEach(w => $('pipesAlerts').appendChild(alertEl('warn', escapeHtml(w))));
    renderPhasePick(data.phases_by_pipe || []);
    renderRolePick(data.roles_by_pipe || []);
    $('phasesPanel').classList.remove('hidden');
    $('phasesPanel').scrollIntoView({ behavior: 'smooth', block: 'start' });
  } catch (e) {
    setBusy('pipesSpin', 'pipesStatus', false, '');
    $('pipesAlerts').appendChild(alertEl('error', escapeHtml(e.message)));
  } finally {
    cancelHint();
  }
});

// Phases grouped by pipe: exclusions apply per pipe, so skipping
// "Completed" under Creative doesn't touch other pipes' phases.
function renderPhasePick(phasesByPipe) {
  const host = $('phasePickList');
  host.innerHTML = '';
  let total = 0, sug = 0;
  phasesByPipe.forEach(entry => {
    if (!entry.phases || !entry.phases.length) return;
    const head = document.createElement('div');
    head.className = 'row-head';
    head.textContent = entry.pipe_name;
    host.appendChild(head);
    entry.phases.forEach(ph => {
      total++;
      if (ph.suggested) sug++;
      const row = document.createElement('label');
      row.className = 'row-item';
      row.innerHTML = `
        <input type="checkbox" data-pipe="${escapeAttr(entry.pipe_id)}" data-phase="${escapeAttr(ph.name)}" ${ph.suggested ? 'checked' : ''}>
        <span class="name">${escapeHtml(ph.name)}</span>
        ${ph.suggested ? '<span class="pill">auto</span>' : ''}
        <span class="meta" style="display:inline;">~${Number(ph.cards_count || 0).toLocaleString()} cards</span>
      `;
      host.appendChild(row);
    });
  });
  if (!total) {
    host.innerHTML = '<div class="small" style="padding:10px 14px;">No phases found on the selected pipes.</div>';
  }
  $('phasePickCount').textContent = total ? `(${total} phases, ${sug} pre-ticked)` : '';
}

// {pipe_id: [phase names]} — ticked = excluded, per pipe.
function getExcludedPhasesByPipe() {
  const map = {};
  document.querySelectorAll('#phasePickList input[type=checkbox]').forEach(cb => {
    const pid = cb.dataset.pipe;
    map[pid] = map[pid] || [];
    if (cb.checked) map[pid].push(cb.dataset.phase);
  });
  return map;
}

// Roles grouped by pipe: the same human role (e.g. Reviewer) can exist on
// several pipes — ticking it under Creative narrows only Creative's scan.
function renderRolePick(rolesByPipe) {
  const host = $('rolePickList');
  host.innerHTML = '';
  let any = false;
  rolesByPipe.forEach(entry => {
    if (!entry.roles || !entry.roles.length) return;
    const head = document.createElement('div');
    head.className = 'row-head';
    head.textContent = entry.pipe_name;
    host.appendChild(head);
    entry.roles.forEach(r => {
      any = true;
      const row = document.createElement('label');
      row.className = 'row-item';
      const phases = (r.phases || []).join(', ');
      const meta = [phases, r.fields > 1 ? `${r.fields} fields` : '']
        .filter(Boolean).join(' \u00b7 ');
      row.innerHTML = `
        <input type="checkbox" data-pipe="${escapeAttr(entry.pipe_id)}" data-role="${escapeAttr(r.key)}">
        <span class="name">${escapeHtml(r.label)}</span>
        ${meta ? `<span class="meta" style="display:inline;">${escapeHtml(meta)}</span>` : ''}
      `;
      host.appendChild(row);
    });
  });
  if (!any) {
    host.innerHTML = '<div class="small" style="padding:10px 14px;">No assignee fields found on the selected pipes.</div>';
  }
  updateRoleCount();
  host.addEventListener('change', updateRoleCount);
}

function updateRoleCount() {
  const all = document.querySelectorAll('#rolePickList input[type=checkbox]');
  const on = document.querySelectorAll('#rolePickList input[type=checkbox]:checked');
  $('rolePickCount').textContent = all.length
    ? (on.length === all.length ? `(all ${all.length} ticked)` : `(${on.length} of ${all.length} ticked)`)
    : '';
}

// {pipe_id: [role keys]} — only ticked roles, per pipe.
function getSelectedRolesByPipe() {
  const map = {};
  document.querySelectorAll('#rolePickList input[type=checkbox]:checked').forEach(cb => {
    (map[cb.dataset.pipe] = map[cb.dataset.pipe] || []).push(cb.dataset.role);
  });
  return map;
}

// ---------- step 3 -> 4: multi-pipe field-level scan ----------
let _scanError = null;

$('scanBtn').addEventListener('click', runWizardScan);

// Live progress state for the scan status line.
const _scanProg = { startedAt: 0, lastEventAt: 0, stage: '', ticker: null };
const SCAN_STALL_LIMIT_MS = 120000;  // no data for 2 min = give up

function _fmtElapsed(ms) {
  const s = Math.floor(ms / 1000);
  return s < 60 ? `${s}s` : `${Math.floor(s / 60)}m ${s % 60}s`;
}

function _scanTick() {
  const now = Date.now();
  const idleMs = now - _scanProg.lastEventAt;
  let msg = `${_scanProg.stage || 'Working'} \u00b7 ${_fmtElapsed(now - _scanProg.startedAt)}`;
  if (idleMs > 15000) {
    msg += ' \u00b7 still waiting on Pipefy\u2026';
  }
  $('scanStatus').textContent = msg;
}

function _scanStage(stage) {
  _scanProg.stage = stage;
  _scanProg.lastEventAt = Date.now();
  _scanTick();
}

async function runWizardScan() {
  clearAlerts('phasesAlerts');
  const pipeIds = getSelectedPipeIds();
  if (!pipeIds.length) {
    $('phasesAlerts').appendChild(alertEl('error', 'No pipes selected — go back to step 02.'));
    return;
  }
  const roleBoxes = document.querySelectorAll('#rolePickList input[type=checkbox]');
  if (roleBoxes.length && !document.querySelectorAll('#rolePickList input[type=checkbox]:checked').length) {
    $('phasesAlerts').appendChild(alertEl('error', 'Tick at least one role to search.'));
    return;
  }
  wizGroups.clear();
  _scanError = null;
  _appliedTotal = 0;
  $('pipeGroups').innerHTML = '';
  const sum = $('scanSummary');
  sum.className = 'alert';
  sum.classList.remove('hidden');
  sum.textContent = wizOperation === 'add'
    ? 'Scanning for jobs where a co-assignee can be added…'
    : 'Scanning — jobs stream in below as they are found…';
  $('resultsPanel').classList.remove('hidden');
  $('resultsTitle').textContent = wizOperation === 'add' ? 'Jobs ready for co-assignment' : 'Their jobs, by pipe';
  $('resultsHint').textContent = wizOperation === 'add'
    ? 'Existing assignees remain in place until you review and apply.'
    : 'Nothing changes in Pipefy until you click Apply on a pipe.';
  $('resultsPanel').scrollIntoView({ behavior: 'smooth', block: 'start' });
  setBusy('scanSpin', 'scanStatus', true, 'Contacting server…');
  $('scanBtn').disabled = true;
  startLogPolling();

  // Progress ticker + stall watchdog: the elapsed clock reassures the user
  // during long stretches (cold start, big pipes); if the stream goes fully
  // silent for 2 minutes we abort instead of spinning forever.
  _scanProg.startedAt = _scanProg.lastEventAt = Date.now();
  _scanProg.stage = 'Contacting server (may take ~30\u201360s if it was asleep)';
  const aborter = new AbortController();
  _scanProg.ticker = setInterval(() => {
    _scanTick();
    if (Date.now() - _scanProg.lastEventAt > SCAN_STALL_LIMIT_MS) {
      aborter.abort();
    }
  }, 1000);

  try {
    const resp = await fetch('/api/wizard/scan/stream', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      signal: aborter.signal,
      body: JSON.stringify({
        old_user: $('oldUser').value.trim(),
        new_user: $('newUser').value.trim(),
        operation: wizOperation,
        org_id: $('orgId').value.trim() || null,
        pipe_ids: pipeIds,
        exclude_phases_by_pipe: getExcludedPhasesByPipe(),
        roles_by_pipe: getSelectedRolesByPipe(),
        throttle_ms: parseInt($('throttleMs').value, 10) || 0,
      }),
    });
    if (resp.status === 401) { showGate(); return; }
    if (!resp.ok || !resp.body) {
      const data = await resp.json().catch(() => ({}));
      throw new Error(data.error || ('HTTP ' + resp.status));
    }
    _scanStage('Resolving user');
    const reader = resp.body.getReader();
    const decoder = new TextDecoder('utf-8');
    let buf = '';
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      _scanProg.lastEventAt = Date.now();
      buf += decoder.decode(value, { stream: true });
      let nl;
      while ((nl = buf.indexOf('\n')) >= 0) {
        const line = buf.slice(0, nl).trim();
        buf = buf.slice(nl + 1);
        if (!line) continue;
        try { handleScanEvent(JSON.parse(line)); } catch (_) {}
      }
    }
    buf = buf.trim();
    if (buf) { try { handleScanEvent(JSON.parse(buf)); } catch (_) {} }
    if (_scanError) throw new Error(_scanError);
    setBusy('scanSpin', 'scanStatus', false,
      `Scan complete in ${_fmtElapsed(Date.now() - _scanProg.startedAt)} — review below.`);
  } catch (e) {
    setBusy('scanSpin', 'scanStatus', false, '');
    $('scanSummary').className = 'alert error';
    $('scanSummary').textContent = (e && e.name === 'AbortError')
      ? `Scan stopped: no response from the server for ${_fmtElapsed(SCAN_STALL_LIMIT_MS)}. `
        + 'The results below may be incomplete — try again, or scan fewer pipes at once.'
      : 'Scan failed: ' + (e.message || e);
  } finally {
    clearInterval(_scanProg.ticker);
    _scanProg.ticker = null;
    $('scanBtn').disabled = false;
  }
}

// Summary card shown when a scan completes with matches (replaces the
// plain alert). Flat slipstream coral — count badge, coral name, one chip
// per pipe (click scrolls to that pipe's group).
let _appliedTotal = 0;

function renderScanCard(host, totalPlans, withJobs) {
  const who = wizUsers ? (wizUsers.old_user.name || wizUsers.old_user.email) : 'This user';
  const target = wizUsers ? (wizUsers.new_user.name || wizUsers.new_user.email) : 'the new assignee';
  const sub = (withJobs.length === 1 ? 'All within one pipe. ' : `Across ${withJobs.length} pipes. `)
    + (wizOperation === 'add'
      ? `Review each match before adding ${target}; client and product filters are optional.`
      : 'Review each match and apply where the fit is right.');
  const headline = wizOperation === 'add'
    ? `<span class="who">${escapeHtml(target)}</span> can be added to ${totalPlans} job${totalPlans === 1 ? '' : 's'} held by ${escapeHtml(who)}`
    : `<span class="who">${escapeHtml(who)}</span> matched ${totalPlans} job${totalPlans === 1 ? '' : 's'}`;
  host.className = 'scan-card';
  host.innerHTML = `
    <div class="sc-body">
      <div class="sc-count">${totalPlans}</div>
      <div>
        <div class="sc-head">${headline}</div>
        <div class="sc-sub">${escapeHtml(sub)}</div>
        <div class="sc-chips">
          ${withJobs.map(g => `
            <button type="button" class="sc-chip" data-pipe="${escapeAttr(g.id)}">
              <span class="dot"></span>${escapeHtml(g.name)}&nbsp;<b>${g.plans}</b>
            </button>
          `).join('')}
        </div>
      </div>
    </div>
    <div class="sc-foot">
      <span class="note" id="scFootNote">Nothing has been submitted yet.</span>
      <button type="button" class="danger" id="scReviewBtn">Review matches</button>
    </div>
  `;
  host.querySelectorAll('.sc-chip').forEach(chip => {
    chip.addEventListener('click', () => {
      const g = wizGroups.get(chip.dataset.pipe);
      if (g) g.el.scrollIntoView({ behavior: 'smooth', block: 'start' });
    });
  });
  const rb = host.querySelector('#scReviewBtn');
  rb.addEventListener('click', () => {
    const first = withJobs[0] && wizGroups.get(withJobs[0].id);
    if (first) first.el.scrollIntoView({ behavior: 'smooth', block: 'start' });
  });
}

function handleScanEvent(evt) {
  const g = evt.pipe_id ? wizGroups.get(evt.pipe_id) : null;
  switch (evt.type) {
    case 'pipe_start':
      createPipeGroup(evt);
      _scanStage(`Scanning ${evt.pipe_name}`);
      break;
    case 'phase_skip':
      if (g) { g.skipped++; updateGroupStatus(g); }
      break;
    case 'phase_scan_start':
      if (g) { g.scanning++; updateGroupStatus(g); }
      break;
    case 'plan':
      if (g) addPlanToGroup(g, evt.plan);
      break;
    case 'progress':
    case 'summary':
      if (g) {
        g.scanned = evt.cards_scanned ?? g.scanned;
        updateGroupStatus(g);
        _scanStage(`Scanning ${g.name} \u2014 ${g.scanned} card${g.scanned === 1 ? '' : 's'} checked`);
      }
      break;
    case 'pipe_done':
      if (g) finalizePipeGroup(g, evt);
      break;
    case 'pipe_error':
      pipeGroupError(evt);
      break;
    case 'all_done': {
      const s = $('scanSummary');
      const withJobs = Array.from(wizGroups.values()).filter(x => x.plans > 0);
      if (evt.total_plans) {
        renderScanCard(s, evt.total_plans, withJobs);
      } else {
        s.className = 'alert ok';
        s.textContent = wizOperation === 'add'
          ? 'No matching jobs need this co-assignee (outside the excluded phases).'
          : 'No jobs found in the selected pipes (outside the excluded phases).';
      }
      break;
    }
    case 'error':
      _scanError = evt.message || 'Scan failed.';
      break;
    default:
      break;
  }
}

// ---------- per-pipe result groups ----------
function createPipeGroup(evt) {
  const el = document.createElement('section');
  el.className = 'pipe-group';
  el.dataset.pipeId = evt.pipe_id;
  el.innerHTML = `
    <div class="pg-head">
      <h3>${escapeHtml(evt.pipe_name)}</h3>
      ${evt.role ? `<span class="pill">${escapeHtml(evt.role)}</span>` : ''}
      <span class="pg-sub pg-count">scanning…</span>
      <span class="pg-status"></span>
    </div>
    <div class="pg-alert alert error hidden"></div>
    <div class="plan-filters pg-filters hidden" style="margin: 12px 18px 0;">
      <input type="search" class="pg-search" placeholder="Search cards…" autocomplete="off">
      <div class="filter-dd" data-attr="client">
        <button type="button" class="filter-dd-btn" aria-haspopup="true" aria-expanded="false">
          Client <span class="dd-badge">All</span> <span class="dd-caret">▾</span>
        </button>
        <div class="filter-dd-panel" hidden><div class="dd-list"></div></div>
      </div>
      <div class="filter-dd" data-attr="product">
        <button type="button" class="filter-dd-btn" aria-haspopup="true" aria-expanded="false">
          Product <span class="dd-badge">All</span> <span class="dd-caret">▾</span>
        </button>
        <div class="filter-dd-panel" hidden><div class="dd-list"></div></div>
      </div>
      <span class="small pg-filter-count"></span>
    </div>
    <div class="pg-body hidden">
      <table>
        <thead>
          <tr>
            <th style="width: 34px; text-align: center;"><input type="checkbox" class="pg-select-all" checked title="Select / deselect all"></th>
            <th style="width: 36px;">#</th>
            <th>Card</th>
            <th style="width: 180px;">Phase</th>
            <th>Change</th>
            <th style="width: 96px;">Status</th>
          </tr>
        </thead>
        <tbody></tbody>
      </table>
    </div>
    <div class="pg-empty hidden"></div>
    <div class="pg-actions hidden">
      <button class="danger pg-apply" disabled>${wizOperation === 'add' ? 'Add co-assignee…' : 'Apply changes…'}</button>
      <span class="spinner pg-spin"></span>
      <span class="small pg-apply-status"></span>
    </div>
  `;
  $('pipeGroups').appendChild(el);
  const g = {
    id: evt.pipe_id, name: evt.pipe_name, role: evt.role || '',
    token: null, plans: 0, scanned: 0, skipped: 0, scanning: 0, done: false,
    el,
    tbody: el.querySelector('tbody'),
    body: el.querySelector('.pg-body'),
    empty: el.querySelector('.pg-empty'),
    actions: el.querySelector('.pg-actions'),
    applyBtn: el.querySelector('.pg-apply'),
    applyStatus: el.querySelector('.pg-apply-status'),
    spin: el.querySelector('.pg-spin'),
    alertBox: el.querySelector('.pg-alert'),
    count: el.querySelector('.pg-count'),
    status: el.querySelector('.pg-status'),
    filters: el.querySelector('.pg-filters'),
    filterText: '',
    hidden: { client: new Set(), product: new Set() },
  };
  g.applyBtn.addEventListener('click', () => askApplyGroup(g));
  el.querySelector('.pg-select-all').addEventListener('change', (e) => {
    g.tbody.querySelectorAll('tr.plan-row .card-pick:not(:disabled)').forEach(b => {
      if (b.closest('tr').style.display !== 'none') b.checked = e.target.checked;
    });
    updateGroupSelection(g);
  });
  g.tbody.addEventListener('change', (e) => {
    if (e.target.classList && e.target.classList.contains('card-pick')) updateGroupSelection(g);
  });
  // Filter wiring: text search + client/product dropdowns, all scoped to this group.
  el.querySelector('.pg-search').addEventListener('input', (e) => {
    g.filterText = (e.target.value || '').toLowerCase();
    applyGroupFilter(g);
  });
  el.querySelectorAll('.filter-dd').forEach(dd => {
    const btn = dd.querySelector('.filter-dd-btn');
    const panel = dd.querySelector('.filter-dd-panel');
    btn.addEventListener('click', (e) => {
      e.stopPropagation();
      const willOpen = panel.hidden;
      document.querySelectorAll('.filter-dd-panel').forEach(p => { p.hidden = true; });
      document.querySelectorAll('.filter-dd-btn').forEach(b => b.setAttribute('aria-expanded', 'false'));
      panel.hidden = !willOpen;
      btn.setAttribute('aria-expanded', willOpen ? 'true' : 'false');
    });
  });
  wizGroups.set(evt.pipe_id, g);
  return g;
}

// ---------- per-group filtering (search + client/product) ----------
function _groupDistinct(g, attr) {
  const map = new Map();
  g.tbody.querySelectorAll('tr.plan-row').forEach(tr => {
    const v = tr.dataset[attr] || '';
    map.set(v, (map.get(v) || 0) + 1);
  });
  return map;
}

function refreshGroupFilters(g) {
  // Only surface the bar when there's something worth filtering.
  g.filters.classList.toggle('hidden', g.plans < 2);
  g.el.querySelectorAll('.filter-dd').forEach(dd => {
    const attr = dd.dataset.attr;
    const counts = _groupDistinct(g, attr);
    const hiddenSet = g.hidden[attr];
    const list = dd.querySelector('.dd-list');
    const badge = dd.querySelector('.dd-badge');
    const keys = Array.from(counts.keys()).sort((a, b) => (a || '~').localeCompare(b || '~'));
    list.innerHTML = '';
    keys.forEach(k => {
      const row = document.createElement('label');
      row.className = 'dd-opt';
      const label = k || `(no ${attr})`;
      row.innerHTML = `
        <input type="checkbox" ${hiddenSet.has(k) ? '' : 'checked'}>
        <span class="dd-opt-name" title="${escapeAttr(label)}">${escapeHtml(label)}</span>
        <span class="dd-opt-n">${counts.get(k)}</span>
      `;
      row.querySelector('input').addEventListener('change', (e) => {
        if (e.target.checked) hiddenSet.delete(k); else hiddenSet.add(k);
        applyGroupFilter(g);
      });
      list.appendChild(row);
    });
    const active = keys.length - keys.filter(k => hiddenSet.has(k)).length;
    badge.textContent = active >= keys.length ? 'All' : (active === 0 ? 'None' : `${active}/${keys.length}`);
    dd.style.display = keys.length > 1 ? '' : 'none';  // hide pointless single-value dropdowns
  });
}

function applyGroupFilter(g) {
  let shown = 0, total = 0;
  g.tbody.querySelectorAll('tr.plan-row').forEach(tr => {
    total++;
    const visible = (!g.filterText || (tr.dataset.search || '').includes(g.filterText))
      && !g.hidden.client.has(tr.dataset.client || '')
      && !g.hidden.product.has(tr.dataset.product || '');
    tr.style.display = visible ? '' : 'none';
    const detail = tr.nextElementSibling;
    if (detail && detail.classList.contains('detail-row')) {
      detail.style.display = (visible && detail.dataset.open === '1') ? '' : 'none';
    }
    if (visible) shown++;
  });
  g.el.querySelector('.pg-filter-count').textContent =
    shown < total ? `showing ${shown} of ${total}` : '';
  refreshGroupFilters(g);
  updateGroupSelection(g);
}

// Close any open filter dropdown when clicking elsewhere.
document.addEventListener('click', (e) => {
  if (!e.target.closest || !e.target.closest('.filter-dd')) {
    document.querySelectorAll('.filter-dd-panel').forEach(p => { p.hidden = true; });
    document.querySelectorAll('.filter-dd-btn').forEach(b => b.setAttribute('aria-expanded', 'false'));
  }
});

function updateGroupStatus(g) {
  if (g.done) return;
  const bits = [];
  if (g.scanned) bits.push(`scanned ${g.scanned}`);
  if (g.skipped) bits.push(`${g.skipped} phase(s) skipped`);
  g.status.textContent = bits.join(' · ');
}

function addPlanToGroup(g, plan) {
  g.plans++;
  g.body.classList.remove('hidden');
  const showMeta = $('showMetaCb').checked;
  const { main, detail } = buildPlanRows(plan, g.plans, 'preview', showMeta);
  g.tbody.appendChild(main);
  g.tbody.appendChild(detail);
  g.count.textContent = `${g.plans} job${g.plans === 1 ? '' : 's'}`;
}

function finalizePipeGroup(g, evt) {
  g.done = true;
  g.token = evt.token || null;
  g.status.textContent = '';
  if (evt.no_fields) {
    g.count.textContent = evt.no_matching_roles ? 'no roles ticked' : 'no assignee fields';
    g.empty.textContent = evt.no_matching_roles
      ? 'No roles ticked for this pipe — nothing searched.'
      : `This pipe has no assignee fields — nothing to ${wizOperation === 'add' ? 'add' : 'swap'}.`;
    g.empty.classList.remove('hidden');
    return;
  }
  if (!g.plans) {
    g.count.textContent = 'no jobs';
    g.empty.textContent = 'No jobs found for this user (outside the excluded phases).';
    g.empty.classList.remove('hidden');
    return;
  }
  g.count.textContent = `${g.plans} job${g.plans === 1 ? '' : 's'}`;
  g.actions.classList.remove('hidden');
  refreshGroupFilters(g);
  updateGroupSelection(g);
}

function pipeGroupError(evt) {
  let g = wizGroups.get(evt.pipe_id);
  if (!g) g = createPipeGroup({ pipe_id: evt.pipe_id, pipe_name: evt.pipe_name || evt.pipe_id, role: null });
  g.done = true;
  g.count.textContent = 'error';
  g.status.textContent = '';
  g.alertBox.textContent = evt.message || 'Scan failed for this pipe.';
  g.alertBox.classList.remove('hidden');
}

function groupSelectedIds(g) {
  return Array.from(g.tbody.querySelectorAll('.card-pick:checked:not(:disabled)'))
    .filter(c => { const tr = c.closest('tr'); return tr && tr.style.display !== 'none'; })
    .map(c => c.dataset.cardId);
}

function updateGroupSelection(g) {
  const boxes = Array.from(g.tbody.querySelectorAll('.card-pick:not(:disabled)'))
    .filter(b => { const tr = b.closest('tr'); return tr && tr.style.display !== 'none'; });
  const checked = boxes.filter(b => b.checked);
  const sa = g.el.querySelector('.pg-select-all');
  if (sa) {
    sa.checked = boxes.length > 0 && checked.length === boxes.length;
    sa.indeterminate = checked.length > 0 && checked.length < boxes.length;
  }
  if (g.token && g.plans) {
    g.count.textContent = checked.length === g.plans
      ? `${g.plans} job${g.plans === 1 ? '' : 's'}`
      : `${checked.length} of ${g.plans} selected`;
  }
  g.applyBtn.disabled = !g.token || checked.length === 0;
}

// ---------- per-pipe apply (shared confirm modal) ----------
let _pendingApplyGroup = null;

function askApplyGroup(g) {
  const ids = groupSelectedIds(g);
  if (!g.token) { alert('Run the scan first.'); return; }
  if (!ids.length) { alert('Tick at least one card to change.'); return; }
  _pendingApplyGroup = g;
  if (wizOperation === 'add') {
    const target = wizUsers ? (wizUsers.new_user.name || wizUsers.new_user.email) : 'the new assignee';
    $('confirmText').textContent =
      `This will add ${target} to the selected role fields on ${ids.length} card${ids.length === 1 ? '' : 's'} in “${g.name}”. ` +
      'All existing assignees will remain. Are you sure?';
  } else {
    $('confirmText').textContent =
      `This will modify ${ids.length} card${ids.length === 1 ? '' : 's'} in “${g.name}”. ` +
      'This action cannot be undone. Are you sure?';
  }
  $('confirmModal').classList.remove('hidden');
}

$('confirmCancel').addEventListener('click', () => {
  _pendingApplyGroup = null;
  $('confirmModal').classList.add('hidden');
});

$('confirmGo').addEventListener('click', async () => {
  const g = _pendingApplyGroup;
  _pendingApplyGroup = null;
  $('confirmModal').classList.add('hidden');
  if (!g || !g.token) return;
  g.applyBtn.disabled = true;
  g.spin.classList.add('on');
  g.applyStatus.textContent = wizOperation === 'add' ? 'Adding co-assignee…' : 'Applying…';
  startLogPolling();
  try {
    const data = await api('/api/apply', {
      token: g.token, card_ids: groupSelectedIds(g), operator_email: operatorEmail,
    });
    renderGroupApply(g, data);
  } catch (e) {
    g.applyStatus.textContent = '';
    g.alertBox.textContent = 'Apply failed: ' + (e.message || e);
    g.alertBox.classList.remove('hidden');
    g.applyBtn.disabled = false;
  } finally {
    g.spin.classList.remove('on');
  }
});

function renderGroupApply(g, data) {
  const showMeta = $('showMetaCb').checked;
  g.tbody.innerHTML = '';
  (data.plans || []).forEach((p, i) => {
    const { main, detail } = buildPlanRows(p, i + 1, 'apply', showMeta);
    g.tbody.appendChild(main);
    g.tbody.appendChild(detail);
  });
  const errs = data.errors || [];
  if (errs.length) {
    g.alertBox.innerHTML = '<b>Errors:</b><br>' + errs.map(x => escapeHtml(x)).join('<br>');
    g.alertBox.classList.remove('hidden');
  } else {
    g.alertBox.classList.add('hidden');
  }
  const remaining = data.remaining || 0;
  const verb = data.operation === 'add' ? 'updated with the co-assignee' : 'changed';
  g.applyStatus.innerHTML =
    `${data.cards_changed} card(s) ${verb}.` +
    (remaining ? ` ${remaining} left unticked — tick and Apply again.` : '') +
    ' <button type="button" class="ghost pg-recent" style="padding:2px 8px;font-size:12px;">Recent applies</button>';
  g.applyStatus.querySelector('.pg-recent').addEventListener('click', openRecentModal);
  g.count.textContent = data.operation === 'add'
    ? `${data.cards_changed} added`
    : `${data.cards_changed} changed`;
  // Keep the summary card's footer honest across multiple per-pipe applies.
  _appliedTotal += data.cards_changed || 0;
  const note = document.getElementById('scFootNote');
  if (note && _appliedTotal > 0) {
    note.textContent = `${_appliedTotal} card${_appliedTotal === 1 ? '' : 's'} `
      + (data.operation === 'add' ? 'updated with the co-assignee.' : 'changed so far.');
  }
  if (!remaining) {
    g.token = null;
  }
  applyGroupFilter(g);
}

// Expand / collapse a card's "what changes" detail row (all groups).
$('pipeGroups').addEventListener('click', (e) => {
  const btn = e.target.closest && e.target.closest('.row-exp');
  if (!btn) return;
  const tr = btn.closest('tr');
  const detail = tr && tr.nextElementSibling;
  if (!detail || !detail.classList.contains('detail-row')) return;
  const open = detail.dataset.open === '1';
  detail.dataset.open = open ? '' : '1';
  detail.style.display = open ? 'none' : '';
  btn.setAttribute('aria-expanded', open ? 'false' : 'true');
  btn.textContent = open ? '▸' : '▾';
});

function actionsCellHtml(p, showMeta) {
  return (p.actions || []).map(a => {
    if (a.scope === 'card') {
      if (a.mode === 'add_only') {
        return '<div>Card assignees <span class="pill" style="background:var(--warn-bg);color:var(--warn);">add-only</span></div>';
      }
      return '<div>Card assignees</div>';
    }
    const label = a.field_label || a.field_id || 'field';
    const slug = showMeta && a.field_id && a.field_id !== a.field_label
      ? ` <span class="small mono">${escapeHtml(a.field_id)}</span>`
      : '';
    const mode = a.mode === 'add_only'
      ? ' <span class="pill" style="background:var(--coral-soft);color:var(--coral-deep);">add</span>'
      : '';
    return `<div>${escapeHtml(label)}${slug}${mode}</div>`;
  }).join('');
}

function scopeLabel(a) {
  if (a.scope === 'card') return 'Card assignees';
  return a.field_label || a.field_id || 'Field';
}

function detailHtml(p) {
  const rows = (p.actions || []).map(a => {
    const before = (a.old_names && a.old_names.length ? a.old_names : (a.old_ids || []));
    const after = (a.new_names && a.new_names.length ? a.new_names : (a.new_ids || []));
    return `<div class="chg">
      <span class="chg-scope">${escapeHtml(scopeLabel(a))}:</span>
      <span class="chg-before">${escapeHtml(before.join(', ') || '—')}</span>
      <span class="chg-arrow">→</span>
      <span class="chg-after">${escapeHtml(after.join(', ') || '—')}</span>
    </div>`;
  }).join('');
  return rows || '<div class="small muted">No detail.</div>';
}

// Build the main row + its (hidden) detail row for one plan.
function buildPlanRows(p, i, mode, showMeta) {
  const acts = actionsCellHtml(p, showMeta);
  const addOnly = (p.actions || []).some(a => a.mode === 'add_only');
  let statusHtml = `<span class="badge ok">${addOnly ? 'to add' : 'planned'}</span>`;
  let stillPlanned = true;
  if (mode === 'apply') {
    stillPlanned = p.status === 'planned';
    if (p.status === 'ok') statusHtml = `<span class="badge ok">${addOnly ? 'added' : 'swapped'}</span>`;
    else if (p.status === 'error') statusHtml = '<span class="badge err">error</span>';
    else if (p.status === 'planned') statusHtml = '<span class="badge ok">planned</span>';
    else statusHtml = '<span class="badge warn">' + escapeHtml(p.status || '?') + '</span>';
  }
  const titleHtml = p.card_url
    ? `<a href="${escapeAttr(p.card_url)}" target="_blank" rel="noopener">${escapeHtml(p.title || '(untitled)')}</a>`
    : escapeHtml(p.title || '(untitled)');
  const client = p.client || '';
  const product = p.product || '';
  const subline = [client, product].filter(Boolean).join(' \u00b7 ');
  const pickCell = stillPlanned
    ? `<td style="text-align:center;"><input type="checkbox" class="card-pick" data-card-id="${escapeAttr(p.card_id)}" checked></td>`
    : `<td style="text-align:center;"><input type="checkbox" disabled ${p.status === 'ok' ? 'checked' : ''}></td>`;

  const main = document.createElement('tr');
  main.className = 'plan-row';
  main.dataset.phase = p.phase_name || '';
  main.dataset.client = client;
  main.dataset.product = product;
  main.dataset.search = `${p.title || ''} ${client} ${product}`.toLowerCase();
  main.innerHTML = `
    ${pickCell}
    <td class="num">${i}</td>
    <td>
      <button type="button" class="row-exp" aria-expanded="false" title="Show what changes">▸</button>
      ${titleHtml}
      ${subline ? `<div class="card-client">${escapeHtml(subline)}</div>` : ''}
      ${showMeta ? `<div class="small mono">id ${escapeHtml(p.card_id)}</div>` : ''}
    </td>
    <td>${escapeHtml(p.phase_name || '?')}</td>
    <td>${acts}</td>
    <td>${statusHtml}</td>
  `;
  const detail = document.createElement('tr');
  detail.className = 'detail-row';
  detail.style.display = 'none';
  detail.innerHTML = `<td></td><td></td><td colspan="4">${detailHtml(p)}</td>`;
  return { main, detail };
}

// ---------- developer panel: log polling ----------
let logSince = 0;
let logTimer = null;
let logsEnabled = true;  // switched off in hosted mode (dev panel hidden)
function fmtTs(t) {
  const d = new Date(t * 1000);
  const hh = String(d.getHours()).padStart(2, '0');
  const mm = String(d.getMinutes()).padStart(2, '0');
  const ss = String(d.getSeconds()).padStart(2, '0');
  return `${hh}:${mm}:${ss}`;
}
async function pollLogs() {
  try {
    const data = await api('/api/logs', { since: logSince });
    const entries = data.entries || [];
    if (entries.length) {
      const showDebug = $('logDebug').checked;
      const view = $('logView');
      const follow = $('logFollow').checked;
      const wasAtBottom = follow || (view.scrollTop + view.clientHeight >= view.scrollHeight - 10);
      const frag = document.createDocumentFragment();
      entries.forEach(e => {
        logSince = Math.max(logSince, e.seq);
        if (!showDebug && e.level === 'DEBUG') return;
        const line = document.createElement('div');
        line.className = 'lvl-' + e.level;
        line.innerHTML = `<span class="ts">${fmtTs(e.ts)}</span> ${escapeHtml(e.level)} ${escapeHtml(e.msg)}`;
        frag.appendChild(line);
      });
      view.appendChild(frag);
      // cap rendered lines so DOM doesn't grow forever
      const max = 800;
      while (view.children.length > max) view.removeChild(view.firstChild);
      if (wasAtBottom) view.scrollTop = view.scrollHeight;
    }
    $('logStatus').textContent = entries.length ? `+${entries.length}` : 'idle';
  } catch (e) {
    $('logStatus').textContent = 'log error';
  }
}
function startLogPolling() {
  if (!logsEnabled || logTimer) return;
  logTimer = setInterval(pollLogs, 1500);
  pollLogs();
}
$('devPanel').addEventListener('toggle', () => {
  if ($('devPanel').open) startLogPolling();
});
$('logClear').addEventListener('click', () => { $('logView').innerHTML = ''; });

// ---------- sign-in gate ----------
const GATE_KEY = 'slipstream.operatorEmail';
let gatePasswordRequired = false;

async function tryAuth(email, password) {
  try {
    const r = await fetch('/api/auth', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ email, password: password || '' }),
    });
    const data = await r.json().catch(() => ({}));
    return { ok: r.ok && data.ok, data };
  } catch (e) {
    return { ok: false, data: { error: 'Network error — is the server running?' } };
  }
}

function showGate() {
  $('gatePwRow').classList.toggle('hidden', !gatePasswordRequired);
  $('gateBackdrop').classList.remove('hidden');
  ($('gateEmail').value ? $('gatePw') : $('gateEmail')).focus();
}

function unlockApp(email) {
  operatorEmail = email;
  $('gateBackdrop').classList.add('hidden');
  $('oldUser').focus();
}

async function submitGate() {
  const email = ($('gateEmail').value || '').trim().toLowerCase();
  const password = $('gatePw').value || '';
  const errBox = $('gateError');
  errBox.classList.add('hidden');
  if (!email) { errBox.textContent = 'Enter your company email.'; errBox.classList.remove('hidden'); return; }
  if (gatePasswordRequired && !password) { errBox.textContent = 'Enter the access password.'; errBox.classList.remove('hidden'); return; }
  setBusy('gateSpin', null, true);
  const { ok, data } = await tryAuth(email, password);
  setBusy('gateSpin', null, false);
  if (ok) {
    try { localStorage.setItem(GATE_KEY, email); } catch (e) {}
    unlockApp(email);
  } else {
    errBox.textContent = (data && data.error) || 'That email is not on an approved domain.';
    errBox.classList.remove('hidden');
    /* if (data && data.allowed_domains) {
      $('gateDomains').textContent = 'Approved domains: ' + data.allowed_domains.join(', ');
    }*/
  }
}

$('gateGo').addEventListener('click', submitGate);
$('gateEmail').addEventListener('keydown', (e) => { if (e.key === 'Enter') submitGate(); });
$('gatePw').addEventListener('keydown', (e) => { if (e.key === 'Enter') submitGate(); });

async function initGate() {
  let saved = '';
  try { saved = localStorage.getItem(GATE_KEY) || ''; } catch (e) {}
  if (saved) $('gateEmail').value = saved;
  // Ask the server whether we already have a signed session cookie.
  try {
    const r = await fetch('/api/session');
    const s = await r.json();
    gatePasswordRequired = !!s.password_required;
    // Hosted mode: operators don't need the developer terminal.
    if (gatePasswordRequired) {
      logsEnabled = false;
      $('devPanel').classList.add('hidden');
    }
    if (s.authed) { unlockApp(s.email); return; }
  } catch (e) { /* fall through to the gate */ }
  // Local no-password mode: a previously saved email is enough.
  if (saved && !gatePasswordRequired) {
    const { ok } = await tryAuth(saved);
    if (ok) { unlockApp(saved); return; }
    try { localStorage.removeItem(GATE_KEY); } catch (e) {}
  }
  showGate();
}

// ---------- history ----------
function fmtHistTs(ts) {
  if (!ts) return '';
  const d = new Date(ts);
  if (isNaN(d.getTime())) return ts;
  return d.toLocaleString();
}

async function loadHistory() {
  $('historyStatus').textContent = 'Loading…';
  try {
    const r = await fetch('/api/history');
    const data = await r.json();
    const rows = data.records || [];
    const n = renderHistoryInto(rows, $('historyTbody'));
    const sync = data.sheet_sync
      ? 'mirrored to the shared Google Sheet'
      : 'local to this server only — resets on redeploy';
    $('historyStatus').textContent = `${n} record${n === 1 ? '' : 's'} · ${sync}`;
  } catch (e) {
    $('historyStatus').textContent = 'load error';
  }
}

// Shared renderer for both the History panel and the "Recent applies" modal.
function renderHistoryInto(rows, tbody) {
  tbody.innerHTML = '';
  if (!rows.length) {
    tbody.innerHTML = '<tr><td colspan="6" class="small" style="padding:14px;">No applies recorded yet.</td></tr>';
    return 0;
  }
  const frag = document.createDocumentFragment();
  rows.forEach(rec => {
    const tr = document.createElement('tr');
    const baseScope = rec.scope === 'card' ? 'Card-level' : (rec.field_label || rec.scope || '');
    const scope = rec.operation === 'add' ? `${baseScope} (add)` : baseScope;
    const change = `${escapeHtml(rec.before || '—')} <span style="color:var(--muted);">→</span> ${escapeHtml(rec.after || '—')}`;
    const cardCell = rec.card_url
      ? `<a href="${escapeHtml(rec.card_url)}" target="_blank" rel="noopener">${escapeHtml(rec.title || rec.card_id || 'card')}</a>`
      : escapeHtml(rec.title || rec.card_id || '');
    const stKind = rec.status === 'ok' ? 'ok' : (rec.status === 'error' ? 'err' : 'warn');
    tr.innerHTML =
      `<td class="small">${escapeHtml(fmtHistTs(rec.ts))}</td>` +
      `<td class="small">${escapeHtml(rec.operator_email || rec.os_user || '')}</td>` +
      `<td>${cardCell}</td>` +
      `<td class="small">${escapeHtml(scope)}</td>` +
      `<td class="small">${change}</td>` +
      `<td><span class="badge ${stKind}">${escapeHtml(rec.status || '')}</span></td>`;
    frag.appendChild(tr);
  });
  tbody.appendChild(frag);
  return rows.length;
}

// "Recent applies" modal — opened from the Apply results header.
async function openRecentModal() {
  $('recentModal').classList.remove('hidden');
  $('recentStatus').textContent = 'Loading…';
  $('recentSyncNote').textContent = '';
  try {
    const r = await fetch('/api/history');
    const data = await r.json();
    const rows = data.records || [];
    const n = renderHistoryInto(rows, $('recentTbody'));
    $('recentStatus').textContent = `${n} record${n === 1 ? '' : 's'} · last 30 days`;
    $('recentSyncNote').textContent = data.sheet_sync
      ? 'Also mirrored to the shared Google Sheet.'
      : 'Local only — set [Audit] webhook_url to also mirror to a shared Sheet.';
  } catch (e) {
    $('recentStatus').textContent = 'load error';
  }
}

function closeRecentModal() { $('recentModal').classList.add('hidden'); }
$('recentClose').addEventListener('click', closeRecentModal);
$('recentModal').addEventListener('click', (e) => { if (e.target === $('recentModal')) closeRecentModal(); });

$('historyRefresh').addEventListener('click', loadHistory);
$('historyPanel').addEventListener('toggle', () => {
  if ($('historyPanel').open) loadHistory();
});

initGate();
</script>
</body>
</html>
"""


def create_app(config_path: str = "config.ini") -> Flask:
    app = Flask(__name__)
    cfg = load_config(config_path)
    if not cfg.pipefy_api_token:
        raise SystemExit(
            "Pipefy API token missing. Set [Pipefy] pipefy_api_token in config.ini or PIPEFY_API_TOKEN env var."
        )

    # ---------- auth / session setup ----------
    # When SWAP_WEB_PASSWORD (or [Web] password) is set, every API route
    # requires a signed-in session (company email + shared password).
    password_required = bool(cfg.web_password)
    if cfg.web_secret_key:
        app.secret_key = cfg.web_secret_key
    elif cfg.web_password:
        # Stable across restarts/workers without a second secret to manage.
        app.secret_key = hashlib.sha256(b"swap-web-session:" + cfg.web_password.encode()).hexdigest()
    else:
        app.secret_key = secrets.token_hex(32)  # local, no-auth mode
    app.permanent_session_lifetime = timedelta(days=14)
    app.config.update(
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        # HTTPS-only cookies when hosted; opt out for local testing with a
        # password by setting SWAP_WEB_ALLOW_INSECURE_COOKIE=1.
        SESSION_COOKIE_SECURE=password_required
        and not os.environ.get("SWAP_WEB_ALLOW_INSECURE_COOKIE"),
    )
    # Render/Fly terminate TLS at a proxy; trust one hop of X-Forwarded-*.
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)  # type: ignore[method-assign]

    _AUTH_FAIL_LOCK = threading.Lock()
    _auth_failures = {"count": 0, "window_start": 0.0}

    def _throttle_failed_auth() -> bool:
        """Cheap brute-force guard: after 10 failures in 10 min, refuse."""
        now = time.time()
        with _AUTH_FAIL_LOCK:
            if now - _auth_failures["window_start"] > 600:
                _auth_failures["window_start"] = now
                _auth_failures["count"] = 0
            _auth_failures["count"] += 1
            return _auth_failures["count"] > 10

    attach_papertrail(cfg.papertrail_token, cfg.papertrail_host, cfg.papertrail_port)
    _install_log_capture()
    client = PipefyClient(cfg.pipefy_api_url, cfg.pipefy_api_token)
    audit_store = AuditStore()
    audit_sheet_sink = build_sheet_sink(
        cfg.audit_sheet_id,
        cfg.audit_service_account_file,
        service_account_json=cfg.audit_service_account_json,
    )

    @app.before_request
    def _require_session():
        """Server-side gate: with a password configured, every API route
        except sign-in/status needs an authenticated session."""
        if not password_required:
            return None
        path = request.path
        if not path.startswith("/api/") or path in ("/api/auth", "/api/session"):
            return None
        if session.get("operator_email"):
            return None
        return jsonify({"error": "Not signed in.", "auth_required": True}), 401

    @app.get("/")
    def index() -> Response:  # type: ignore[override]
        return Response(INDEX_HTML, mimetype="text/html; charset=utf-8")

    @app.get("/api/session")
    def api_session():
        email = normalize_email(session.get("operator_email") or "")
        return jsonify({
            "authed": bool(email) and is_allowed_email(email),
            "email": email,
            "password_required": password_required,
        })

    @app.post("/api/info")
    def info():
        return jsonify({
            "token_mask": mask_secret(cfg.pipefy_api_token),
            "default_excluded_phases": list(DEFAULT_EXCLUDED_PHASES),
            "allowed_domains": sorted(ALLOWED_EMAIL_DOMAINS),
        })

    @app.post("/api/auth")
    def api_auth():
        """Sign in: validate the operator email against the company allow-list
        and, when configured, the shared password. On success a signed session
        cookie is issued; every other API route requires it (see
        `_require_session`). Apply requests also re-check the email.
        """
        body = request.get_json(force=True, silent=True) or {}
        email = normalize_email(body.get("email"))
        if not is_allowed_email(email):
            return jsonify({
                "ok": False,
                "error": "Enter a valid company email on an approved domain.",
                "allowed_domains": sorted(ALLOWED_EMAIL_DOMAINS),
            }), 403
        if password_required:
            supplied = str(body.get("password") or "")
            if not secrets.compare_digest(supplied, cfg.web_password or ""):
                if _throttle_failed_auth():
                    log.warning("auth: too many failed password attempts")
                    return jsonify({"ok": False, "error": "Too many attempts. Try again later."}), 429
                time.sleep(0.5)  # slow down guessing
                return jsonify({"ok": False, "error": "Wrong password."}), 403
        session.permanent = True
        session["operator_email"] = email
        log.info("auth: %s signed in", email)
        return jsonify({"ok": True, "email": email})

    @app.get("/api/history")
    def api_history():
        rows = audit_store.recent(limit=300, days=30)
        return jsonify({
            "records": rows,
            "count": len(rows),
            "sheet_sync": bool(cfg.audit_webhook_url) or audit_sheet_sink is not None,
        })

    @app.get("/api/pipes")
    def api_pipes():
        return jsonify({"pipes": KNOWN_PIPES})

    @app.post("/api/logs")
    def api_logs():
        body = request.get_json(force=True, silent=True) or {}
        try:
            since = int(body.get("since") or 0)
        except (TypeError, ValueError):
            since = 0
        with _LOG_LOCK:
            entries = [dict(e) for e in _LOG_RING if e["seq"] > since]
            latest = _LOG_SEQ["n"]
        return jsonify({"entries": entries, "latest": latest})

    @app.post("/api/load")
    def api_load():
        body = request.get_json(force=True, silent=True) or {}
        pipe_id = (body.get("pipe_id") or "").strip()
        old_user = (body.get("old_user") or "").strip()
        new_user = (body.get("new_user") or "").strip()
        org_id_override = (body.get("org_id") or "").strip() or cfg.org_id

        if not pipe_id or not old_user or not new_user:
            return jsonify({"error": "pipe_id, old_user, and new_user are required."}), 400
        if old_user.lower() == new_user.lower():
            return jsonify({"error": "Old user and new user are the same value."}), 400

        try:
            pipe_ctx, old_u, new_u = _resolve_users(client, pipe_id, old_user, new_user, org_id_override)
        except UserResolutionError as exc:
            return jsonify({"error": str(exc)}), 400
        except Exception as exc:  # network / API failure
            log.exception("load failed")
            return jsonify({"error": f"Pipefy API error: {exc}"}), 500

        if old_u.id == new_u.id:
            return jsonify({"error": "Old user and new user resolved to the same Pipefy user id."}), 400

        warnings: List[str] = []
        member_ids = {m.id for m in pipe_ctx.get("members", [])}
        if new_u.id not in member_ids:
            warnings.append(
                f"New user {new_u.name} <{new_u.email}> is NOT listed as a member of this pipe. "
                f"Pipefy may reject the assignment. Add them to the pipe (any role) before applying."
            )

        try:
            fields_meta = discover_assignee_fields(client, pipe_id)
        except Exception as exc:
            return jsonify({"error": f"Could not discover fields: {exc}"}), 500

        fields = sorted(
            ({"id": fid, **meta} for fid, meta in fields_meta.items()),
            key=lambda f: (str(f.get("label") or "").lower(), f["id"]),
        )

        return jsonify({
            "pipe_id": pipe_ctx.get("pipe_id") or pipe_id,
            "pipe_name": pipe_ctx.get("pipe_name") or "",
            "old_user": {"id": old_u.id, "name": old_u.name, "email": old_u.email},
            "new_user": {"id": new_u.id, "name": new_u.name, "email": new_u.email},
            "fields": fields,
            "warnings": warnings,
        })

    def _iter_preview_events(body: Dict[str, Any]):
        """Generator that yields preview events as dicts.

        Event types:
          - {"type":"resolved", "old_user":..., "new_user":..., "pipe_name":...}
          - {"type":"phase_skip", "phase":..., "cards_count":...}
          - {"type":"phase_scan_start", "phase":..., "cards_count":...}
          - {"type":"plan", "plan":{...}}
          - {"type":"progress", "cards_scanned":N, "plans":M, "in_excluded":X, "already_swapped":Y}
          - {"type":"summary", ...}
          - {"type":"done", "token":...}
          - {"type":"error", "message":...}

        Designed so both the JSON `/api/preview` and the NDJSON streaming
        endpoint can drive identical work.
        """
        pipe_id = (body.get("pipe_id") or "").strip()
        old_user = (body.get("old_user") or "").strip()
        new_user = (body.get("new_user") or "").strip()
        org_id_override = (body.get("org_id") or "").strip() or cfg.org_id
        include_card_level = bool(body.get("include_card_level", True))
        card_add_only = bool(body.get("card_add_only", False))
        operation = str(body.get("operation") or "replace").strip().lower()
        if operation not in {"replace", "add"}:
          raise ValueError(f"Unsupported assignment operation: {operation}")
        field_ids = list(body.get("field_ids") or [])
        exclude_phases = list(body.get("exclude_phases") or DEFAULT_EXCLUDED_PHASES)

        pipe_ctx, old_u, new_u = _resolve_users(client, pipe_id, old_user, new_user, org_id_override)
        yield {
            "type": "resolved",
            "pipe_id": pipe_ctx.get("pipe_id") or pipe_id,
            "pipe_name": pipe_ctx.get("pipe_name") or "",
            "old_user": {"id": old_u.id, "name": old_u.name, "email": old_u.email},
            "new_user": {"id": new_u.id, "name": new_u.name, "email": new_u.email},
        }

        field_filter = set(field_ids)
        use_fast_path = include_card_level or bool(field_filter)

        # Emit phase metadata up-front so the UI can show what we're
        # skipping vs. scanning before any cards arrive. We do this even
        # for field-only scans so the user can see which phases match
        # the done-name patterns / `done` flag — for field scans these
        # are filtered client-side after findCards returns, but knowing
        # what's classified as terminal is still useful.
        if use_fast_path:
            try:
                phases_info = fetch_active_phases(client, pipe_id, exclude_phases=exclude_phases)
                for ph in phases_info["skipped"]:
                    yield {"type": "phase_skip", "phase": ph["name"], "cards_count": ph["cards_count"], "done": ph["done"]}
                for ph in phases_info["active"]:
                    yield {"type": "phase_scan_start", "phase": ph["name"], "cards_count": ph["cards_count"]}
            except Exception as exc:  # noqa: BLE001
                log.warning("phase meta probe failed: %s", exc)

        plans: List[Any] = []
        cards_scanned = 0
        cards_in_excluded = 0
        cards_already_swapped = 0

        if use_fast_path:
            log.info(
                "Web preview: fast scan pipe %s (card_level=%s, fields=%d) old_user=%s",
                pipe_id, include_card_level, len(field_filter), old_u.id,
            )
            card_source = iter_cards_fast_union(
                client, pipe_id, old_u.id,
                include_card_level=include_card_level,
                field_ids=field_filter,
                old_user_email=old_u.email,
                exclude_phases=exclude_phases,
            )
        else:
            log.info("Web preview: nothing selected; falling back to full scan of %s", pipe_id)
            card_source = iter_cards(client, pipe_id, exclude_phases=exclude_phases)

        for card in card_source:
            cards_scanned += 1
            plan = plan_card(
                card,
                old_id=old_u.id,
                new_id=new_u.id,
                excluded_phases=exclude_phases,
                include_card_level=include_card_level,
                field_filter=field_filter,
                card_add_only=card_add_only,
                operation=operation,
                id_names={old_u.id: old_u.name, new_u.id: new_u.name},
            )
            if plan.skipped_reason:
                cards_in_excluded += 1
            elif not plan.actions:
                cards_already_swapped += 1
            else:
                plans.append(plan)
                yield {"type": "plan", "plan": _plan_to_dict(plan)}

            # Heartbeat every card so the browser progress UI feels alive,
            # but throttle on big runs to keep payload sane.
            if cards_scanned <= 50 or cards_scanned % 25 == 0:
                yield {
                    "type": "progress",
                    "cards_scanned": cards_scanned,
                    "plans": len(plans),
                    "in_excluded": cards_in_excluded,
                    "already_swapped": cards_already_swapped,
                }

        log.info("Web preview: %d scanned, %d to swap", cards_scanned, len(plans))

        card_level_changes = sum(1 for p in plans for a in p.actions if a.scope == "card")
        field_level_changes = sum(1 for p in plans for a in p.actions if a.scope.startswith("field:"))

        summary = {
            "cards_scanned": cards_scanned,
            "cards_in_excluded": cards_in_excluded,
            "cards_already_swapped": cards_already_swapped,
            "card_level_changes": card_level_changes,
            "field_level_changes": field_level_changes,
        }

        token = secrets.token_urlsafe(16)
        _gc_preview_store()
        _PREVIEW_STORE[token] = {
            "created_at": time.time(),
            "plans": plans,
            "throttle_ms": int(body.get("throttle_ms", 250)),
            "summary": summary,
            "pipe_id": pipe_ctx.get("pipe_id") or pipe_id,
            "pipe_name": pipe_ctx.get("pipe_name") or "",
            "old_user": f"{old_u.name} <{old_u.email}>".strip(),
            "new_user": f"{new_u.name} <{new_u.email}>".strip(),
            "operation": operation,
        }

        yield {"type": "summary", **summary}
        yield {"type": "done", "token": token, "operation": operation}

    def _run_preview(body: Dict[str, Any]) -> Dict[str, Any]:
        """Back-compat single-shot preview — drains the event stream."""
        resolved: Dict[str, Any] = {}
        plan_dicts: List[Dict[str, Any]] = []
        summary: Dict[str, Any] = {}
        token: str = ""
        for evt in _iter_preview_events(body):
            t = evt.get("type")
            if t == "resolved":
                resolved = evt
            elif t == "plan":
                plan_dicts.append(evt["plan"])
            elif t == "summary":
                summary = {k: v for k, v in evt.items() if k != "type"}
            elif t == "done":
                token = evt.get("token", "")
        return {
            "token": token,
            **summary,
            "plans": plan_dicts,
            "pipe_id": resolved.get("pipe_id", ""),
            "pipe_name": resolved.get("pipe_name", ""),
            "old_user": resolved.get("old_user"),
            "new_user": resolved.get("new_user"),
        }

    @app.post("/api/preview")
    def api_preview():
        body = request.get_json(force=True, silent=True) or {}
        try:
            return jsonify(_run_preview(body))
        except UserResolutionError as exc:
            return jsonify({"error": str(exc)}), 400
        except Exception as exc:
            log.exception("preview failed")
            return jsonify({"error": f"Preview failed: {exc}"}), 500

    @app.post("/api/preview/stream")
    def api_preview_stream():
        body = request.get_json(force=True, silent=True) or {}

        def _gen():
            try:
                for evt in _iter_preview_events(body):
                    yield json.dumps(evt) + "\n"
            except UserResolutionError as exc:
                yield json.dumps({"type": "error", "message": str(exc), "kind": "resolution"}) + "\n"
            except Exception as exc:  # noqa: BLE001
                log.exception("preview stream failed")
                yield json.dumps({"type": "error", "message": f"Preview failed: {exc}"}) + "\n"

        # text/plain-friendly NDJSON; disable proxy buffering when we can.
        resp = Response(_gen(), mimetype="application/x-ndjson")
        resp.headers["Cache-Control"] = "no-cache, no-transform"
        resp.headers["X-Accel-Buffering"] = "no"
        return resp

    @app.post("/api/discover/stream")
    def api_discover_stream():
        """Probe every curated pipe for the given user (NDJSON stream).

        Cheap `first:1` index lookups only — one card-level probe per pipe
        plus one findCards probe per assignee field (retried with the email
        when the id finds nothing). No full scans, no fallbacks: probe
        failures surface as "unknown" rather than eating the API budget.
        """
        body = request.get_json(force=True, silent=True) or {}
        user_input = (body.get("old_user") or "").strip()
        org_id_override = (body.get("org_id") or "").strip() or cfg.org_id
        if not user_input:
            return jsonify({"error": "old_user is required."}), 400

        def _gen():
            try:
                try:
                    user = _discover_resolve_user(client, KNOWN_PIPES, user_input, org_id_override)
                except UserResolutionError as exc:
                    yield json.dumps({"type": "error", "message": str(exc), "kind": "resolution"}) + "\n"
                    return
                yield json.dumps({
                    "type": "start",
                    "user": {"id": user.id, "name": user.name, "email": user.email},
                    "pipes": len(KNOWN_PIPES),
                }) + "\n"
                hits = 0
                for p in KNOWN_PIPES:
                    pid = p["id"]
                    role = None
                    try:
                        ctx = fetch_pipe_context(client, pid)
                        for m in ctx.get("members") or []:
                            if str(m.id) == str(user.id):
                                role = m.role
                                break
                    except Exception as exc:  # noqa: BLE001
                        log.warning("discover: pipe context failed for %s: %s", pid, exc)
                    card_hit = probe_card_level_assignee(client, pid, user.id)
                    fields_out = []
                    try:
                        fields = discover_assignee_fields(client, pid)
                    except Exception as exc:  # noqa: BLE001
                        log.warning("discover: field discovery failed for %s: %s", pid, exc)
                        fields = {}
                    for fid, meta in fields.items():
                        hit = probe_field_value(client, pid, fid, user.id)
                        if hit is False and user.email:
                            hit = probe_field_value(client, pid, fid, user.email)
                        fields_out.append({"id": fid, "label": meta.get("label") or fid, "hit": hit})
                    any_hit = bool(card_hit) or any(f["hit"] for f in fields_out)
                    unknown = card_hit is None or any(f["hit"] is None for f in fields_out)
                    if any_hit:
                        hits += 1
                    yield json.dumps({
                        "type": "pipe",
                        "pipe_id": pid,
                        "pipe_name": p["name"],
                        "role": role,
                        "card_level": card_hit,
                        "fields": fields_out,
                        "any_hit": any_hit,
                        "unknown": unknown,
                    }) + "\n"
                yield json.dumps({"type": "done", "hits": hits, "checked": len(KNOWN_PIPES)}) + "\n"
            except Exception as exc:  # noqa: BLE001
                log.exception("discover stream failed")
                yield json.dumps({"type": "error", "message": f"Discovery failed: {exc}"}) + "\n"

        resp = Response(_gen(), mimetype="application/x-ndjson")
        resp.headers["Cache-Control"] = "no-cache, no-transform"
        resp.headers["X-Accel-Buffering"] = "no"
        return resp

    # ---------- user-first wizard ----------

    @app.post("/api/wizard/users")
    def api_wizard_users():
        """Step 1: resolve both users org-wide.

        Returns the curated pipe list for the picker. We deliberately do NOT
        report per-pipe membership: most people get pipe access through
        Pipefy user groups ("restricted view"), which the API's
        `pipe.members` list does not include — filtering on it silently
        misses real assignees. The operator picks which pipes to scan.
        """
        body = request.get_json(force=True, silent=True) or {}
        old_input = (body.get("old_user") or "").strip()
        new_input = (body.get("new_user") or "").strip()
        operation = str(body.get("operation") or "replace").strip().lower()
        if operation not in {"replace", "add"}:
            return jsonify({"error": "operation must be 'replace' or 'add'."}), 400
        org_id_override = (body.get("org_id") or "").strip() or cfg.org_id
        if not old_input or not new_input:
            return jsonify({"error": "old_user and new_user are required."}), 400
        if old_input.lower() == new_input.lower():
            return jsonify({"error": "Old user and new user are the same value."}), 400
        try:
            old_u = _discover_resolve_user(client, KNOWN_PIPES, old_input, org_id_override)
            new_u = _discover_resolve_user(client, KNOWN_PIPES, new_input, org_id_override)
        except UserResolutionError as exc:
            return jsonify({"error": str(exc)}), 400
        except Exception as exc:  # noqa: BLE001 - network / API failure
            log.exception("wizard users failed")
            return jsonify({"error": f"Pipefy API error: {exc}"}), 500
        if old_u.id == new_u.id:
            return jsonify({"error": "Old user and new user resolved to the same Pipefy user id."}), 400

        return jsonify({
            "old_user": {"id": old_u.id, "name": old_u.name, "email": old_u.email},
            "new_user": {"id": new_u.id, "name": new_u.name, "email": new_u.email},
            "operation": operation,
            "pipes": [{"id": p["id"], "name": p["name"]} for p in KNOWN_PIPES],
        })

    @app.post("/api/wizard/phases")
    def api_wizard_phases():
        """Step 2: phases AND roles across the selected pipes.

        Phases: union of names with card counts; `suggested` marks the ones
        we recommend excluding. Roles: assignee-field labels deduped via
        `_norm_role_label` — the UI offers them as a narrowing filter so a
        scan for e.g. "Account Manager" only queries those fields.
        """
        body = request.get_json(force=True, silent=True) or {}
        pipe_ids = [str(x) for x in (body.get("pipe_ids") or []) if x]
        if not pipe_ids:
            return jsonify({"error": "pipe_ids is required."}), 400

        def _norm(s):
            return " ".join((s or "").strip().lower().split())

        default_norm = {_norm(n) for n in DEFAULT_EXCLUDED_PHASES}
        phases_by_pipe: List[Dict[str, Any]] = []
        roles_by_pipe: List[Dict[str, Any]] = []
        errors: List[str] = []
        names = {p["id"]: p["name"] for p in KNOWN_PIPES}
        for pid in pipe_ids:
            try:
                info = fetch_active_phases(client, pid)
            except Exception as exc:  # noqa: BLE001
                log.warning("wizard: phase fetch failed for %s: %s", pid, exc)
                errors.append(f"Could not fetch phases for {names.get(pid, pid)}.")
                continue
            pipe_phases: List[Dict[str, Any]] = []
            for kind in ("active", "skipped"):
                for ph in info[kind]:
                    pipe_phases.append({
                        "name": ph["name"],
                        "cards_count": int(ph.get("cards_count") or 0),
                        "suggested": kind == "skipped" or _norm(ph["name"]) in default_norm,
                    })
            pipe_phases.sort(key=lambda e: (not e["suggested"], str(e["name"]).lower()))
            phases_by_pipe.append({
                "pipe_id": pid,
                "pipe_name": names.get(pid, pid),
                "phases": pipe_phases,
            })
            try:
                fields = discover_assignee_fields(client, pid)
            except Exception as exc:  # noqa: BLE001
                log.warning("wizard: field discovery failed for %s: %s", pid, exc)
                errors.append(f"Could not list roles for {names.get(pid, pid)}.")
                continue
            # Dedupe near-identical labels WITHIN this pipe ("Reviewer" +
            # "Reviewer(s)" -> one entry) but keep pipes separate so the
            # operator sees exactly which pipe each role belongs to.
            pipe_roles: Dict[str, Dict[str, Any]] = {}
            for meta in fields.values():
                label = meta.get("label") or meta.get("id") or ""
                key = _norm_role_label(label)
                if not key:
                    continue
                ent = pipe_roles.setdefault(key, {
                    "key": key, "label": label, "fields": 0, "phases": [],
                })
                ent["fields"] += 1
                ph_name = meta.get("phase") or ""
                if ph_name and ph_name not in ent["phases"]:
                    ent["phases"].append(ph_name)
            roles_by_pipe.append({
                "pipe_id": pid,
                "pipe_name": names.get(pid, pid),
                "roles": sorted(pipe_roles.values(), key=lambda e: str(e["label"]).lower()),
            })
        return jsonify({
            "phases_by_pipe": phases_by_pipe,
            "roles_by_pipe": roles_by_pipe,
            "errors": errors,
        })

    @app.post("/api/wizard/scan/stream")
    def api_wizard_scan_stream():
        """Step 3: field-level scan of the selected pipes (NDJSON stream).

        Runs the existing single-pipe preview machinery once per pipe with
        ALL of that pipe's assignee fields selected and card-level OFF.
        Every event is tagged with its pipe_id; each pipe finishes with a
        `pipe_done` event carrying that pipe's apply token, so the existing
        /api/apply endpoint works per pipe unchanged.
        """
        body = request.get_json(force=True, silent=True) or {}
        old_input = (body.get("old_user") or "").strip()
        new_input = (body.get("new_user") or "").strip()
        operation = str(body.get("operation") or "replace").strip().lower()
        if operation not in {"replace", "add"}:
          return jsonify({"error": "operation must be 'replace' or 'add'."}), 400
        org_id_override = (body.get("org_id") or "").strip() or None
        pipe_ids = [str(x) for x in (body.get("pipe_ids") or []) if x]
        exclude_phases = list(body.get("exclude_phases") or [])
        # Per-pipe phase exclusions {pipe_id: [names]}; falls back to the
        # flat `exclude_phases` list for any pipe not in the map.
        raw_excl = body.get("exclude_phases_by_pipe") or {}
        exclude_by_pipe = {
            str(pid): [str(n) for n in (names_ or []) if n]
            for pid, names_ in raw_excl.items()
        } if isinstance(raw_excl, dict) else {}
        # Optional role narrowing. Preferred: `roles_by_pipe` {pipe_id: [keys]}
        # so a role ticked under Creative doesn't widen the Media scan.
        # Legacy: flat `roles` list applied to every pipe.
        raw_by_pipe = body.get("roles_by_pipe") or {}
        roles_by_pipe = {
            str(pid): {_norm_role_label(r) for r in (keys or []) if r}
            for pid, keys in raw_by_pipe.items()
        } if isinstance(raw_by_pipe, dict) else {}
        global_role_keys = {
            _norm_role_label(r) for r in (body.get("roles") or []) if r
        }
        roles_narrowed = bool(roles_by_pipe) or bool(global_role_keys)
        throttle_ms = body.get("throttle_ms", 250)
        if not old_input or not new_input or not pipe_ids:
            return jsonify({"error": "old_user, new_user, and pipe_ids are required."}), 400
        names = {p["id"]: p["name"] for p in KNOWN_PIPES}

        def _gen():
            try:
                try:
                    # Early validation: fail fast with a clean error before any
                    # pipe work if the leaver can't be resolved at all.
                    _discover_resolve_user(
                        client, KNOWN_PIPES, old_input, org_id_override or cfg.org_id
                    )
                except UserResolutionError as exc:
                    yield json.dumps({"type": "error", "message": str(exc), "kind": "resolution"}) + "\n"
                    return
                total_plans = 0
                pipes_scanned = 0
                for pid in pipe_ids:
                    pname = names.get(pid, pid)
                    try:
                        fields = discover_assignee_fields(client, pid)
                    except Exception as exc:  # noqa: BLE001
                        yield json.dumps({
                            "type": "pipe_error", "pipe_id": pid, "pipe_name": pname,
                            "message": f"Could not discover fields: {exc}",
                        }) + "\n"
                        continue
                    # Role narrowing: keep only fields whose label matches a
                    # role selected for THIS pipe. Fewer fields = fewer calls.
                    if roles_narrowed:
                        keys = roles_by_pipe.get(pid, global_role_keys)
                        fields = {
                            fid: m for fid, m in fields.items()
                            if _norm_role_label(m.get("label") or fid) in keys
                        }
                    yield json.dumps({
                        "type": "pipe_start", "pipe_id": pid, "pipe_name": pname,
                        "fields": len(fields),
                    }) + "\n"
                    if not fields:
                        yield json.dumps({
                            "type": "pipe_done", "pipe_id": pid, "token": None,
                            "plans": 0, "no_fields": True,
                            "no_matching_roles": roles_narrowed,
                        }) + "\n"
                        pipes_scanned += 1
                        continue
                    sub_body = {
                        "pipe_id": pid,
                        "old_user": old_input,
                        "new_user": new_input,
                        "org_id": org_id_override,
                        "include_card_level": False,   # wizard is field-level only
                        "card_add_only": False,
                        "operation": operation,
                        "field_ids": list(fields.keys()),
                        "exclude_phases": exclude_by_pipe.get(pid, exclude_phases),
                        "throttle_ms": throttle_ms,
                    }
                    plans_n = 0
                    try:
                        for evt in _iter_preview_events(sub_body):
                            t = evt.get("type")
                            if t == "plan":
                                plans_n += 1
                            if t == "done":
                                yield json.dumps({
                                    "type": "pipe_done", "pipe_id": pid,
                                    "token": evt.get("token"), "plans": plans_n,
                                    "operation": operation,
                                }) + "\n"
                            else:
                                yield json.dumps({**evt, "pipe_id": pid}) + "\n"
                        total_plans += plans_n
                        pipes_scanned += 1
                    except UserResolutionError as exc:
                        yield json.dumps({
                            "type": "pipe_error", "pipe_id": pid, "pipe_name": pname,
                            "message": str(exc),
                        }) + "\n"
                    except Exception as exc:  # noqa: BLE001
                        log.exception("wizard scan failed for pipe %s", pid)
                        yield json.dumps({
                            "type": "pipe_error", "pipe_id": pid, "pipe_name": pname,
                            "message": f"Scan failed: {exc}",
                        }) + "\n"
                yield json.dumps({
                  "type": "all_done", "pipes_scanned": pipes_scanned,
                  "total_plans": total_plans, "operation": operation,
                }) + "\n"
            except Exception as exc:  # noqa: BLE001
                log.exception("wizard scan stream failed")
                yield json.dumps({"type": "error", "message": f"Scan failed: {exc}"}) + "\n"

        resp = Response(_gen(), mimetype="application/x-ndjson")
        resp.headers["Cache-Control"] = "no-cache, no-transform"
        resp.headers["X-Accel-Buffering"] = "no"
        return resp

    @app.post("/api/apply")
    def api_apply():
        body = request.get_json(force=True, silent=True) or {}

        # Hard gate: a valid company email is required to mutate anything.
        # Prefer the signed session identity; fall back to the request body
        # only in local no-password mode.
        operator_email = normalize_email(
            session.get("operator_email") or body.get("operator_email")
        )
        if not is_allowed_email(operator_email):
            return jsonify({
                "error": "A valid company email is required before applying changes.",
                "allowed_domains": sorted(ALLOWED_EMAIL_DOMAINS),
            }), 403

        token = body.get("token")
        entry = _PREVIEW_STORE.get(token) if token else None
        if not entry:
            return jsonify({"error": "Preview expired or token unknown. Run Test run again."}), 400

        all_plans = entry["plans"]
        throttle_ms = entry["throttle_ms"]
        summary = entry["summary"]
        pipe_id = entry.get("pipe_id", "")
        pipe_name = entry.get("pipe_name", "")
        old_user = entry.get("old_user", "")
        new_user = entry.get("new_user", "")
        operation = entry.get("operation", "replace")

        # Optional per-card selection: apply only the ticked cards and keep the
        # rest in the preview store for a later batch.
        raw_ids = body.get("card_ids")
        if isinstance(raw_ids, list) and raw_ids:
            wanted = {str(x) for x in raw_ids}
            selected = [p for p in all_plans if str(p.card_id) in wanted]
            remaining = [p for p in all_plans if str(p.card_id) not in wanted]
        else:
            selected = list(all_plans)
            remaining = []

        errors: List[str] = []
        cards_changed = 0
        out_plans: List[Dict[str, Any]] = []
        for plan in selected:
            results = execute_plan(client, plan, throttle_ms=throttle_ms)
            any_ok = False
            any_err = False
            err_msgs: List[str] = []
            for action, err in results:
                if err:
                    any_err = True
                    errors.append(f"{plan.card_url or plan.card_id}: {err}")
                    err_msgs.append(str(err))
                else:
                    any_ok = True
            status = "ok" if any_ok and not any_err else ("partial" if any_ok else "error")
            d = _plan_to_dict(plan)
            d["status"] = status
            out_plans.append(d)
            if any_ok:
                cards_changed += 1
            # Audit every applied card (local store + optional Google Sheet).
            try:
                record_swap(
                    store=audit_store,
                    webhook_url=cfg.audit_webhook_url,
                    operator_email=operator_email,
                    source="web",
                    pipe_id=pipe_id,
                    pipe_name=pipe_name,
                    old_user=old_user,
                    new_user=new_user,
                    plan_dict=d,
                    status=status,
                    error="; ".join(err_msgs),
                    operation=operation,
                    sheet_sink=audit_sheet_sink,
                )
            except Exception:  # noqa: BLE001 - audit must never block apply
                log.exception("audit logging failed for card %s", plan.card_id)

        # Surface the untouched cards as still-planned so the UI can keep them
        # selectable for a follow-up Apply.
        for plan in remaining:
            d = _plan_to_dict(plan)
            d["status"] = "planned"
            out_plans.append(d)

        if remaining:
            entry["plans"] = remaining
            entry["created_at"] = time.time()
        elif token:
            _PREVIEW_STORE.pop(token, None)

        return jsonify({
            **summary,
            "cards_changed": cards_changed,
            "errors": errors,
            "plans": out_plans,
            "remaining": len(remaining),
            "operation": operation,
        })

    return app


def _plan_to_dict(plan) -> Dict[str, Any]:
    return {
        "card_id": plan.card_id,
        "card_url": plan.card_url,
        "title": plan.title,
        "phase_name": plan.phase_name,
        "client": getattr(plan, "client", "") or "",
        "product": getattr(plan, "product", "") or "",
        "actions": [
            {
                "scope": a.scope,
                "field_id": a.field_id,
                "field_label": a.field_label,
                "old_ids": list(a.old_ids),
                "new_ids": list(a.new_ids),
                "old_names": list(getattr(a, "old_names", []) or []),
                "new_names": list(getattr(a, "new_names", []) or []),
                "mode": getattr(a, "mode", "swap"),
            }
            for a in plan.actions
        ],
    }


def main() -> int:
    import argparse
    parser = argparse.ArgumentParser(prog="swap-assignee-web", description="Web UI for the Pipefy assignee swap (local dev server; use gunicorn when hosting).")
    parser.add_argument("--config", default="config.ini")
    parser.add_argument("--host", default=os.environ.get("HOST", "127.0.0.1"), help="Bind host (default 127.0.0.1; set SWAP_WEB_PASSWORD before exposing beyond localhost).")
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", "5000")))
    parser.add_argument("--no-browser", action="store_true", help="Do not auto-open browser.")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    app = create_app(args.config)

    url = f"http://{args.host}:{args.port}/"
    if not args.no_browser:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    print(f"\nPipefy Assignee Swap UI running at {url}\nPress Ctrl+C to stop.\n")
    app.run(host=args.host, port=args.port, debug=False, use_reloader=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
