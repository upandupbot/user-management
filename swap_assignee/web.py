"""Local web UI for the Pipefy assignee swap.

Run with:
    python -m swap_assignee.web
then open http://127.0.0.1:5000 in a browser.

This is a thin wrapper around the same functions the CLI uses
(`_resolve_users`, `discover_assignee_fields`, `iter_cards`, `plan_card`,
`execute_plan`). No auth — bind to localhost only.
"""
from __future__ import annotations

import collections
import logging
import secrets
import threading
import time
import webbrowser
from typing import Any, Deque, Dict, List

try:
    from flask import Flask, jsonify, request, Response
except ImportError as exc:  # pragma: no cover
    raise SystemExit(
        "Flask is required for the web UI. Install with: pip install -r requirements.txt"
    ) from exc

from .card_iterator import discover_assignee_fields, iter_cards
from .cli import _resolve_users  # reuse exactly the CLI's resolution flow
from .config import load_config, mask_secret
from .papertrail_logger import attach_papertrail
from .pipefy_client import PipefyClient
from .swap_engine import DEFAULT_EXCLUDED_PHASES, execute_plan, plan_card
from .user_resolver import UserResolutionError


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


def _gc_preview_store() -> None:
    now = time.time()
    expired = [k for k, v in _PREVIEW_STORE.items() if now - v["created_at"] > _PREVIEW_TTL_SECONDS]
    for k in expired:
        _PREVIEW_STORE.pop(k, None)


INDEX_HTML = r"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>slipstream — Assignee Swap</title>
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Bodoni+Moda:ital,wght@0,500;0,700;1,500&family=Inter:wght@400;500;600&display=swap">
  <style>
    :root {
      --bg: #FBF7F2;
      --surface: #FFFFFF;
      --surface-2: #FAF4EE;
      --ink: #1F1A17;
      --ink-2: #4B403A;
      --muted: #8A7E76;
      --border: #ECE2D7;
      --border-strong: #D9CABB;
      --coral: #E94B3C;
      --coral-deep: #C8392B;
      --coral-soft: #F8B5AC;
      --blush: #F2A8A2;
      --ok: #2F7D4F;
      --ok-bg: #E6F4EC;
      --warn: #9A6B14;
      --warn-bg: #FBEFD4;
      --danger: #C8392B;
      --danger-bg: #FBE3DE;
      --shadow: 0 1px 2px rgba(31,26,23,0.04), 0 6px 18px rgba(31,26,23,0.06);
      --radius: 10px;
    }
    * { box-sizing: border-box; }
    html, body { margin: 0; padding: 0; }
    body {
      background: var(--bg);
      color: var(--ink);
      font-family: "Inter", -apple-system, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
      font-size: 14px;
      line-height: 1.5;
      -webkit-font-smoothing: antialiased;
    }
    .serif {
      font-family: "Bodoni Moda", "Bodoni MT", Didot, "Playfair Display", Georgia, serif;
      font-weight: 700;
      letter-spacing: -0.005em;
    }

    /* ---------- Header ---------- */
    header {
      background: var(--bg);
      padding: 28px 32px 20px;
      border-bottom: 1px solid var(--border);
    }
    .wrap { max-width: 1080px; margin: 0 auto; }
    .brand {
      font-family: "Bodoni Moda", "Bodoni MT", Didot, Georgia, serif;
      font-weight: 700;
      font-size: 44px;
      line-height: 1;
      letter-spacing: -0.01em;
      user-select: none;
    }
    .brand .b1 { color: var(--coral); }
    .brand .b2 { color: var(--blush); }
    .sub {
      margin-top: 6px;
      color: var(--ink-2);
      font-size: 13px;
      letter-spacing: 0.08em;
      text-transform: uppercase;
    }

    /* ---------- Layout ---------- */
    main { padding: 28px 32px 96px; }
    .panel {
      background: var(--surface);
      border: 1px solid var(--border);
      border-radius: var(--radius);
      box-shadow: var(--shadow);
      padding: 24px 26px;
      margin-bottom: 20px;
    }
    .panel-head {
      display: flex; align-items: baseline; gap: 14px;
      margin-bottom: 18px;
    }
    .panel-head .step {
      font-family: "Bodoni Moda", Georgia, serif;
      font-weight: 500;
      font-style: italic;
      font-size: 28px;
      line-height: 1;
      color: var(--coral);
    }
    .panel-head h2 {
      margin: 0;
      font-family: "Bodoni Moda", Georgia, serif;
      font-weight: 700;
      font-size: 22px;
      color: var(--ink);
      letter-spacing: -0.01em;
    }
    .panel-head .hint {
      margin-left: auto;
      font-size: 12px;
      color: var(--muted);
    }

    /* ---------- Form ---------- */
    .row { display: flex; gap: 14px; flex-wrap: wrap; }
    .row > .col { flex: 1 1 240px; min-width: 0; }
    label {
      display: block;
      font-size: 12px;
      color: var(--ink-2);
      margin-bottom: 6px;
      font-weight: 500;
    }
    input[type=text], input[type=number], textarea, select {
      width: 100%;
      padding: 10px 12px;
      border: 1px solid var(--border-strong);
      border-radius: 8px;
      font-size: 14px;
      font-family: inherit;
      background: var(--surface);
      color: var(--ink);
      transition: border-color .12s, box-shadow .12s;
    }
    input:focus, textarea:focus, select:focus {
      outline: none;
      border-color: var(--coral);
      box-shadow: 0 0 0 3px rgba(233,75,60,0.15);
    }
    textarea { resize: vertical; min-height: 70px; font-family: ui-monospace, "SF Mono", Consolas, monospace; font-size: 12.5px; }

    /* ---------- Buttons ---------- */
    button {
      cursor: pointer;
      border: 1px solid var(--border-strong);
      border-radius: 8px;
      padding: 9px 18px;
      font-size: 14px;
      font-weight: 500;
      font-family: inherit;
      background: var(--surface);
      color: var(--ink);
      transition: background .12s, border-color .12s, transform .04s;
    }
    button:hover { border-color: var(--ink-2); }
    button:active { transform: translateY(1px); }
    button.primary {
      background: var(--coral);
      color: #fff;
      border-color: var(--coral);
    }
    button.primary:hover { background: var(--coral-deep); border-color: var(--coral-deep); }
    button.ghost { background: transparent; }
    button.danger {
      background: var(--coral-deep);
      color: #fff;
      border-color: var(--coral-deep);
    }
    button.danger:hover { background: #a82e22; border-color: #a82e22; }
    button:disabled { opacity: 0.45; cursor: not-allowed; }
    button:disabled:hover { border-color: var(--border-strong); background: var(--surface); }
    button.primary:disabled:hover { background: var(--coral); border-color: var(--coral); }
    button.danger:disabled:hover { background: var(--coral-deep); border-color: var(--coral-deep); }
    button + button { margin-left: 8px; }

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

    .badge { display: inline-block; padding: 3px 10px; border-radius: 999px; font-size: 11.5px; font-weight: 500; letter-spacing: 0.02em; }
    .badge.ok { background: var(--ok-bg); color: var(--ok); }
    .badge.warn { background: var(--warn-bg); color: var(--warn); }
    .badge.err { background: var(--danger-bg); color: var(--danger); }

    .alert {
      padding: 11px 14px; border-radius: 8px; margin-top: 12px;
      font-size: 13px; border: 1px solid transparent;
    }
    .alert.error { background: var(--danger-bg); border-color: #f3c5bd; color: var(--danger); }
    .alert.warn  { background: var(--warn-bg); border-color: #f0d99f; color: var(--warn); }
    .alert.ok    { background: var(--ok-bg); border-color: #c3e4d0; color: var(--ok); }

    /* ---------- Resolved users summary ---------- */
    .resolved {
      background: var(--surface-2);
      border: 1px solid var(--border);
      border-radius: 8px;
      padding: 12px 14px;
      font-size: 13.5px;
    }
    .resolved .label { color: var(--muted); font-size: 11.5px; text-transform: uppercase; letter-spacing: 0.06em; }
    .resolved .swap {
      display: flex; align-items: center; gap: 14px; margin-top: 4px;
      flex-wrap: wrap;
    }
    .resolved .user { display: flex; flex-direction: column; }
    .resolved .user b { font-weight: 600; color: var(--ink); }
    .resolved .user .email { color: var(--muted); font-size: 12.5px; }
    .resolved .arrow { color: var(--coral); font-size: 20px; line-height: 1; }

    /* ---------- Field list ---------- */
    .field-list {
      max-height: 300px; overflow-y: auto;
      border: 1px solid var(--border);
      border-radius: 8px;
      background: var(--surface-2);
      padding: 4px 0;
    }
    .field-list .row-item {
      display: flex; align-items: center; gap: 10px;
      padding: 8px 14px;
      border-bottom: 1px solid var(--border);
      cursor: pointer;
    }
    .field-list .row-item:last-child { border-bottom: 0; }
    .field-list .row-item:hover { background: #F5EDE4; }
    .field-list input[type=checkbox] { margin: 0; accent-color: var(--coral); width: 16px; height: 16px; }
    .field-list .name { font-size: 13.5px; color: var(--ink); flex: 1; }
    .field-list .pill {
      font-size: 11px; color: var(--muted);
      background: #fff; border: 1px solid var(--border);
      padding: 1px 7px; border-radius: 999px;
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
      display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr)); gap: 12px;
      margin-top: 4px;
    }
    .tile {
      background: var(--surface-2);
      border: 1px solid var(--border);
      border-radius: 8px;
      padding: 14px;
      text-align: center;
    }
    .tile .val {
      font-family: "Bodoni Moda", Georgia, serif;
      font-size: 30px; font-weight: 700; line-height: 1;
      color: var(--ink);
    }
    .tile .lbl {
      font-size: 11px; color: var(--muted);
      margin-top: 6px;
      text-transform: uppercase; letter-spacing: 0.06em;
    }
    .tile.accent .val { color: var(--coral); }

    table { width: 100%; border-collapse: collapse; font-size: 13px; }
    th, td { text-align: left; padding: 9px 12px; border-bottom: 1px solid var(--border); vertical-align: top; }
    th {
      background: var(--surface-2);
      font-weight: 600; color: var(--muted);
      text-transform: uppercase; font-size: 10.5px; letter-spacing: 0.06em;
      position: sticky; top: 0; z-index: 1;
    }
    tbody tr:hover { background: #FCF7F1; }
    td.num { text-align: right; font-variant-numeric: tabular-nums; color: var(--muted); }
    a { color: var(--coral); text-decoration: none; }
    a:hover { text-decoration: underline; }

    .hidden { display: none !important; }
    .small { font-size: 12px; color: var(--muted); }
    .mono { font-family: ui-monospace, Consolas, monospace; font-size: 12.5px; }

    /* ---------- Developer panel ---------- */
    .dev {
      margin-top: 28px;
      border: 1px solid var(--border);
      border-radius: var(--radius);
      background: var(--surface);
    }
    .dev > summary {
      cursor: pointer;
      padding: 14px 20px;
      list-style: none;
      display: flex; align-items: center; gap: 10px;
      font-size: 13px;
      color: var(--ink-2);
      user-select: none;
    }
    .dev > summary::-webkit-details-marker { display: none; }
    .dev > summary::before {
      content: "›";
      display: inline-block;
      font-size: 18px;
      color: var(--muted);
      transition: transform .15s;
    }
    .dev[open] > summary::before { transform: rotate(90deg); }
    .dev > summary .tag {
      margin-left: auto;
      color: var(--muted);
      font-family: ui-monospace, Consolas, monospace;
      font-size: 11.5px;
    }
    .dev-body { padding: 4px 20px 20px; border-top: 1px solid var(--border); }
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
      background: #1B1410;
      color: #F4E4D6;
      font-family: ui-monospace, "SF Mono", Consolas, monospace;
      font-size: 12px;
      line-height: 1.5;
      padding: 12px 14px;
      border-radius: 8px;
      height: 240px;
      overflow-y: auto;
      white-space: pre-wrap;
      word-break: break-word;
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
      position: fixed; inset: 0; background: rgba(31,26,23,0.45);
      display: flex; align-items: center; justify-content: center;
      z-index: 100;
      backdrop-filter: blur(2px);
    }
    .modal {
      background: var(--surface);
      border-radius: var(--radius);
      padding: 26px 28px;
      max-width: 460px;
      width: 92%;
      box-shadow: 0 24px 48px rgba(0,0,0,0.25);
    }
    .modal h3 {
      margin: 0 0 10px;
      font-family: "Bodoni Moda", Georgia, serif;
      font-weight: 700;
      font-size: 22px;
    }
    .modal p { margin: 0 0 18px; color: var(--ink-2); }
    .modal .actions { justify-content: flex-end; margin-top: 0; }
  </style>
</head>
<body>

<header>
  <div class="wrap">
    <div class="brand"><span class="b1">slip</span><span class="b2">stream</span></div>
    <div class="sub">Pipefy Assignee Swap</div>
  </div>
</header>

<main class="wrap">

  <!-- Step 1 -->
  <section class="panel">
    <div class="panel-head">
      <span class="step">01</span>
      <h2>Choose your pipe</h2>
      <span class="hint">Email is preferred — names can be ambiguous</span>
    </div>
    <div class="row">
      <div class="col">
        <label for="pipeId">Pipe ID</label>
        <input id="pipeId" type="text" placeholder="305705781" autocomplete="off">
      </div>
      <div class="col">
        <label for="oldUser">Remove user</label>
        <input id="oldUser" type="text" placeholder="leaving@dydx.digital" autocomplete="off">
      </div>
      <div class="col">
        <label for="newUser">Add user</label>
        <input id="newUser" type="text" placeholder="replacement@dydx.digital" autocomplete="off">
      </div>
    </div>
    <div class="actions">
      <button id="loadBtn" class="primary">Continue</button>
      <span id="loadSpin" class="spinner"></span>
      <span id="loadStatus" class="small"></span>
    </div>
    <div id="loadAlerts"></div>
  </section>

  <!-- Step 2 -->
  <section id="optionsPanel" class="panel hidden">
    <div class="panel-head">
      <span class="step">02</span>
      <h2>Pick what to swap</h2>
      <span class="hint">Card-level is the top-of-card assignee list</span>
    </div>

    <div class="resolved" style="margin-bottom: 18px;">
      <div class="label">Swap</div>
      <div id="resolvedUsers" class="swap"></div>
    </div>

    <div class="row" style="margin-bottom: 14px;">
      <div class="col" style="flex: 2 1 380px;">
        <label>Assignee fields</label>
        <div id="fieldList" class="field-list"></div>
      </div>
      <div class="col" style="flex: 1 1 220px;">
        <label for="excludePhases">Skip cards in these phases</label>
        <textarea id="excludePhases" placeholder="Cancelled, Completed, Finance Recon"></textarea>
        <div class="small" style="margin-top: 6px;">Comma-separated, case-insensitive.</div>
      </div>
    </div>

    <div class="actions">
      <button id="previewBtn" class="primary">Test run</button>
      <button id="applyBtn" class="danger" disabled>Apply changes…</button>
      <span id="runSpin" class="spinner"></span>
      <span id="runStatus" class="small"></span>
    </div>
  </section>

  <!-- Step 3 -->
  <section id="resultsPanel" class="panel hidden">
    <div class="panel-head">
      <span class="step">03</span>
      <h2 id="resultsTitle">Results</h2>
      <span class="hint" id="resultsHint"></span>
    </div>

    <div id="resultMode" class="alert ok"></div>
    <div class="summary-grid" id="summaryGrid" style="margin-top: 14px;"></div>

    <div id="errorBox" class="alert error hidden"></div>

    <div id="planTableWrap" style="margin-top: 18px;">
      <h4 style="margin: 0 0 8px; font-size: 11px; text-transform: uppercase; letter-spacing: 0.06em; color: var(--muted); font-weight: 600;">
        Affected cards <span id="planCount" class="small" style="font-weight: 400;"></span>
      </h4>
      <div style="max-height: 460px; overflow-y: auto; border: 1px solid var(--border); border-radius: 8px;">
        <table>
          <thead>
            <tr>
              <th style="width: 36px;">#</th>
              <th>Card</th>
              <th style="width: 180px;">Phase</th>
              <th>Change</th>
              <th style="width: 96px;">Status</th>
            </tr>
          </thead>
          <tbody id="planTbody"></tbody>
        </table>
      </div>
    </div>
  </section>

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

<script>
const $ = (id) => document.getElementById(id);

let previewToken = null;
let lastPlanCount = 0;

// ---------- helpers ----------
async function api(path, body) {
  const r = await fetch(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body || {}),
  });
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.error || ('HTTP ' + r.status));
  return data;
}
function setBusy(spinnerId, statusId, busy, msg) {
  $(spinnerId).classList.toggle('on', !!busy);
  $(statusId).textContent = msg || '';
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

// ---------- initial info ----------
async function loadInitial() {
  try {
    const info = await api('/api/info', {});
    if (info.default_excluded_phases) {
      $('excludePhases').value = info.default_excluded_phases.join(', ');
    }
  } catch (e) {
    /* ignore — non-fatal */
  }
}

// ---------- step 1: load ----------
$('loadBtn').addEventListener('click', async () => {
  clearAlerts('loadAlerts');
  $('optionsPanel').classList.add('hidden');
  $('resultsPanel').classList.add('hidden');
  $('applyBtn').disabled = true;
  previewToken = null;

  const pipeId = $('pipeId').value.trim();
  const oldUser = $('oldUser').value.trim();
  const newUser = $('newUser').value.trim();
  const orgId = $('orgId').value.trim();
  if (!pipeId || !oldUser || !newUser) {
    $('loadAlerts').appendChild(alertEl('error', 'Pipe ID, remove user, and add user are required.'));
    return;
  }

  setBusy('loadSpin', 'loadStatus', true, 'Fetching pipe and resolving users…');
  try {
    const data = await api('/api/load', {
      pipe_id: pipeId, old_user: oldUser, new_user: newUser, org_id: orgId || null,
    });
    setBusy('loadSpin', 'loadStatus', false, '');

    const oldU = data.old_user, newU = data.new_user;
    $('resolvedUsers').innerHTML = `
      <div class="user">
        <b>${escapeHtml(oldU.name || '(no name)')}</b>
        <span class="email">${escapeHtml(oldU.email)}</span>
      </div>
      <span class="arrow">→</span>
      <div class="user">
        <b>${escapeHtml(newU.name || '(no name)')}</b>
        <span class="email">${escapeHtml(newU.email)}</span>
      </div>
      <div class="user" style="margin-left:auto;">
        <span class="email">on pipe</span>
        <b>${escapeHtml(data.pipe_name || data.pipe_id)}</b>
      </div>
    `;
    (data.warnings || []).forEach(w => $('loadAlerts').appendChild(alertEl('warn', escapeHtml(w))));

    const fl = $('fieldList');
    fl.innerHTML = '';
    fl.classList.toggle('show-meta', $('showMetaCb').checked);

    const mkRow = (id, name, pill, meta, checked) => {
      const row = document.createElement('label');
      row.className = 'row-item';
      row.innerHTML = `
        <input type="checkbox" data-field="${escapeAttr(id)}" ${checked?'checked':''}>
        <span class="name">${escapeHtml(name)}</span>
        ${pill ? `<span class="pill">${escapeHtml(pill)}</span>` : ''}
        ${meta ? `<span class="meta">${escapeHtml(meta)}</span>` : ''}
      `;
      return row;
    };
    fl.appendChild(mkRow('__card__', 'Card-level assignees', 'card', '(top-of-card assignee list)', true));
    (data.fields || []).forEach(f => {
      const meta = `${f.id} · internal_id=${f.internal_id || '?'} · ${f.type}`;
      fl.appendChild(mkRow(f.id, f.label || '(no label)', 'field', meta, false));
    });
    if (!data.fields || data.fields.length === 0) {
      const d = document.createElement('div');
      d.className = 'small';
      d.style.padding = '10px 14px';
      d.textContent = 'No assignee fields discovered on this pipe.';
      fl.appendChild(d);
    }

    $('optionsPanel').classList.remove('hidden');
    $('optionsPanel').scrollIntoView({ behavior: 'smooth', block: 'start' });
  } catch (e) {
    setBusy('loadSpin', 'loadStatus', false, '');
    $('loadAlerts').appendChild(alertEl('error', escapeHtml(e.message)));
  }
});

// ---------- step 2: preview / apply ----------
function gatherRunPayload() {
  const fieldIds = Array.from(document.querySelectorAll('#fieldList input[type=checkbox]:checked'))
    .map(cb => cb.dataset.field);
  const includeCardLevel = fieldIds.includes('__card__');
  const realFieldIds = fieldIds.filter(x => x !== '__card__');
  const excluded = $('excludePhases').value.split(/[,\n]/).map(s => s.trim()).filter(Boolean);
  const throttleMs = parseInt($('throttleMs').value, 10) || 0;
  return {
    pipe_id: $('pipeId').value.trim(),
    old_user: $('oldUser').value.trim(),
    new_user: $('newUser').value.trim(),
    org_id: $('orgId').value.trim() || null,
    include_card_level: includeCardLevel,
    field_ids: realFieldIds,
    exclude_phases: excluded,
    throttle_ms: throttleMs,
  };
}

$('previewBtn').addEventListener('click', async () => {
  $('resultsPanel').classList.add('hidden');
  $('applyBtn').disabled = true;
  setBusy('runSpin', 'runStatus', true, 'Scanning cards — large pipes can take a minute…');
  startLogPolling();
  try {
    const data = await api('/api/preview', gatherRunPayload());
    previewToken = data.token;
    lastPlanCount = data.plans.length;
    renderResults(data, 'preview');
    $('applyBtn').disabled = data.plans.length === 0;
    setBusy('runSpin', 'runStatus', false, 'Test run complete. Plans cached for ~30 min.');
  } catch (e) {
    setBusy('runSpin', 'runStatus', false, '');
    alert('Test run failed: ' + e.message);
  }
});

$('applyBtn').addEventListener('click', () => {
  if (!previewToken) { alert('Run a test run first.'); return; }
  $('confirmText').textContent =
    `This will modify ${lastPlanCount} card${lastPlanCount === 1 ? '' : 's'} in Pipefy. ` +
    `This action cannot be undone. Are you sure?`;
  $('confirmModal').classList.remove('hidden');
});

$('confirmCancel').addEventListener('click', () => $('confirmModal').classList.add('hidden'));

$('confirmGo').addEventListener('click', async () => {
  $('confirmModal').classList.add('hidden');
  setBusy('runSpin', 'runStatus', true, 'Applying changes…');
  $('applyBtn').disabled = true;
  $('previewBtn').disabled = true;
  startLogPolling();
  try {
    const data = await api('/api/apply', { token: previewToken });
    renderResults(data, 'apply');
    setBusy('runSpin', 'runStatus', false, 'Apply complete.');
    previewToken = null;
  } catch (e) {
    setBusy('runSpin', 'runStatus', false, '');
    alert('Apply failed: ' + e.message);
  } finally {
    $('previewBtn').disabled = false;
  }
});

// ---------- results render ----------
function renderResults(data, mode) {
  const panel = $('resultsPanel');
  panel.classList.remove('hidden');
  const m = $('resultMode');
  if (mode === 'preview') {
    m.className = 'alert ok';
    m.textContent = data.plans.length === 0
      ? 'Test run: no cards would be modified — the old user is not assigned anywhere matching your scope.'
      : `Test run: ${data.plans.length} card(s) would be modified. Nothing has been changed in Pipefy yet.`;
    $('resultsTitle').textContent = 'Test run results';
    $('resultsHint').textContent = 'Nothing has been written to Pipefy.';
  } else {
    const errCount = (data.errors || []).length;
    m.className = errCount ? 'alert error' : 'alert ok';
    m.textContent = errCount
      ? `Applied with ${data.cards_changed} card(s) changed and ${errCount} error(s).`
      : `Applied. ${data.cards_changed} card(s) changed successfully.`;
    $('resultsTitle').textContent = 'Apply results';
    $('resultsHint').textContent = errCount ? 'See errors below.' : 'All changes written.';
  }

  const grid = $('summaryGrid');
  grid.innerHTML = '';
  const tiles = [
    ['Scanned',        data.cards_scanned, false],
    ['Excluded phase', data.cards_in_excluded, false],
    ['Already done',   data.cards_already_swapped, false],
    ['To swap',        data.plans.length, true],
    ['Card-level',     data.card_level_changes, false],
    ['Field-level',    data.field_level_changes, false],
  ];
  if (mode === 'apply') {
    tiles.push(['Changed', data.cards_changed, true]);
    tiles.push(['Errors', (data.errors || []).length, false]);
  }
  tiles.forEach(([lbl, val, accent]) => {
    const t = document.createElement('div');
    t.className = 'tile' + (accent ? ' accent' : '');
    t.innerHTML = `<div class="val">${val ?? 0}</div><div class="lbl">${escapeHtml(lbl)}</div>`;
    grid.appendChild(t);
  });

  const errBox = $('errorBox');
  if (mode === 'apply' && (data.errors || []).length) {
    errBox.classList.remove('hidden');
    errBox.innerHTML = '<b>Errors:</b><br>' + data.errors.map(e => escapeHtml(e)).join('<br>');
  } else {
    errBox.classList.add('hidden');
  }

  const tbody = $('planTbody');
  tbody.innerHTML = '';
  $('planCount').textContent = `(${data.plans.length})`;
  const showMeta = $('showMetaCb').checked;
  data.plans.forEach((p, i) => {
    const tr = document.createElement('tr');
    const acts = (p.actions || []).map(a => {
      const scope = a.scope === 'card' ? 'card-level' : `field ${a.field_id}`;
      return `<div>${escapeHtml(scope)}</div>`;
    }).join('');
    let statusHtml = '<span class="badge ok">planned</span>';
    if (mode === 'apply') {
      if (p.status === 'ok') statusHtml = '<span class="badge ok">swapped</span>';
      else if (p.status === 'error') statusHtml = '<span class="badge err">error</span>';
      else statusHtml = '<span class="badge warn">' + escapeHtml(p.status || '?') + '</span>';
    }
    const titleHtml = p.card_url
      ? `<a href="${escapeAttr(p.card_url)}" target="_blank" rel="noopener">${escapeHtml(p.title || '(untitled)')}</a>`
      : escapeHtml(p.title || '(untitled)');
    tr.innerHTML = `
      <td class="num">${i + 1}</td>
      <td>${titleHtml}${showMeta ? `<div class="small mono">id ${escapeHtml(p.card_id)}</div>` : ''}</td>
      <td>${escapeHtml(p.phase_name || '?')}</td>
      <td>${acts}</td>
      <td>${statusHtml}</td>
    `;
    tbody.appendChild(tr);
  });
}

// ---------- developer panel: field meta toggle ----------
$('showMetaCb').addEventListener('change', () => {
  $('fieldList').classList.toggle('show-meta', $('showMetaCb').checked);
});

// ---------- developer panel: log polling ----------
let logSince = 0;
let logTimer = null;
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
  if (logTimer) return;
  logTimer = setInterval(pollLogs, 1500);
  pollLogs();
}
$('devPanel').addEventListener('toggle', () => {
  if ($('devPanel').open) startLogPolling();
});
$('logClear').addEventListener('click', () => { $('logView').innerHTML = ''; });

loadInitial();
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
    attach_papertrail(cfg.papertrail_token, cfg.papertrail_host, cfg.papertrail_port)
    _install_log_capture()
    client = PipefyClient(cfg.pipefy_api_url, cfg.pipefy_api_token)

    @app.get("/")
    def index() -> Response:  # type: ignore[override]
        return Response(INDEX_HTML, mimetype="text/html; charset=utf-8")

    @app.post("/api/info")
    def info():
        return jsonify({
            "token_mask": mask_secret(cfg.pipefy_api_token),
            "default_excluded_phases": list(DEFAULT_EXCLUDED_PHASES),
        })

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

    def _run_preview(body: Dict[str, Any]) -> Dict[str, Any]:
        pipe_id = (body.get("pipe_id") or "").strip()
        old_user = (body.get("old_user") or "").strip()
        new_user = (body.get("new_user") or "").strip()
        org_id_override = (body.get("org_id") or "").strip() or cfg.org_id
        include_card_level = bool(body.get("include_card_level", True))
        field_ids = list(body.get("field_ids") or [])
        exclude_phases = list(body.get("exclude_phases") or DEFAULT_EXCLUDED_PHASES)

        # Re-resolve (cheap; ensures the IDs we apply against are current).
        pipe_ctx, old_u, new_u = _resolve_users(client, pipe_id, old_user, new_user, org_id_override)

        # The UI shows an explicit checkbox per discovered field; we pass exactly
        # the user's selection. Empty set => field-level off (card-level may still run).
        field_filter = set(field_ids)

        plans = []
        cards_scanned = 0
        cards_in_excluded = 0
        cards_already_swapped = 0
        log.info("Web preview: scanning pipe %s ...", pipe_id)
        for card in iter_cards(client, pipe_id):
            cards_scanned += 1
            plan = plan_card(
                card,
                old_id=old_u.id,
                new_id=new_u.id,
                excluded_phases=exclude_phases,
                include_card_level=include_card_level,
                field_filter=field_filter,
            )
            if plan.skipped_reason:
                cards_in_excluded += 1
                continue
            if not plan.actions:
                cards_already_swapped += 1
                continue
            plans.append(plan)
            if len(plans) % 100 == 0:
                log.info("  %d plans, %d scanned so far", len(plans), cards_scanned)
        log.info("Web preview: %d scanned, %d to swap", cards_scanned, len(plans))

        card_level_changes = sum(1 for p in plans for a in p.actions if a.scope == "card")
        field_level_changes = sum(1 for p in plans for a in p.actions if a.scope.startswith("field:"))

        token = secrets.token_urlsafe(16)
        _gc_preview_store()
        _PREVIEW_STORE[token] = {
            "created_at": time.time(),
            "plans": plans,
            "throttle_ms": int(body.get("throttle_ms", 250)),
            "summary": {
                "cards_scanned": cards_scanned,
                "cards_in_excluded": cards_in_excluded,
                "cards_already_swapped": cards_already_swapped,
                "card_level_changes": card_level_changes,
                "field_level_changes": field_level_changes,
            },
        }

        return {
            "token": token,
            "cards_scanned": cards_scanned,
            "cards_in_excluded": cards_in_excluded,
            "cards_already_swapped": cards_already_swapped,
            "card_level_changes": card_level_changes,
            "field_level_changes": field_level_changes,
            "plans": [_plan_to_dict(p) for p in plans],
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

    @app.post("/api/apply")
    def api_apply():
        body = request.get_json(force=True, silent=True) or {}
        token = body.get("token")
        entry = _PREVIEW_STORE.pop(token, None) if token else None
        if not entry:
            return jsonify({"error": "Preview expired or token unknown. Run Test run again."}), 400

        plans = entry["plans"]
        throttle_ms = entry["throttle_ms"]
        summary = entry["summary"]

        errors: List[str] = []
        cards_changed = 0
        out_plans: List[Dict[str, Any]] = []
        for plan in plans:
            results = execute_plan(client, plan, throttle_ms=throttle_ms)
            any_ok = False
            any_err = False
            for action, err in results:
                if err:
                    any_err = True
                    errors.append(f"{plan.card_url or plan.card_id}: {err}")
                else:
                    any_ok = True
            if any_ok:
                cards_changed += 1
            d = _plan_to_dict(plan)
            d["status"] = "ok" if any_ok and not any_err else ("partial" if any_ok else "error")
            out_plans.append(d)

        return jsonify({
            **summary,
            "cards_changed": cards_changed,
            "errors": errors,
            "plans": out_plans,
        })

    return app


def _plan_to_dict(plan) -> Dict[str, Any]:
    return {
        "card_id": plan.card_id,
        "card_url": plan.card_url,
        "title": plan.title,
        "phase_name": plan.phase_name,
        "actions": [
            {
                "scope": a.scope,
                "field_id": a.field_id,
                "old_ids": list(a.old_ids),
                "new_ids": list(a.new_ids),
            }
            for a in plan.actions
        ],
    }


def main() -> int:
    import argparse
    parser = argparse.ArgumentParser(prog="swap-assignee-web", description="Local web UI for the Pipefy assignee swap.")
    parser.add_argument("--config", default="config.ini")
    parser.add_argument("--host", default="127.0.0.1", help="Bind host (default 127.0.0.1, do not change for non-local use).")
    parser.add_argument("--port", type=int, default=5000)
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
