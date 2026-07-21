"""Iterate cards in a Pipefy pipe with cursor pagination + fallback."""
from __future__ import annotations

import logging
from typing import Any, Dict, Iterable, Iterator, Optional, Set


log = logging.getLogger(__name__)


def _norm_phase(s: Optional[str]) -> str:
    return " ".join((s or "").strip().lower().split())


PIPE_CARDS_QUERY = """
query PipeCards($pipeId: ID!, $cursor: String) {
  cards(pipe_id: $pipeId, first: 20, after: $cursor) {
    pageInfo { hasNextPage endCursor }
    edges {
      node {
        id
        title
        url
        current_phase { id name }
        assignees { id name email }
        fields {
          name
          value
          array_value
          field { id label type }
        }
      }
    }
  }
}
"""

SINGLE_CARD_QUERY = """
query SingleCard($id: ID!) {
  card(id: $id) {
    id
    title
    url
    current_phase { id name }
    assignees { id name email }
    fields {
      name
      value
      array_value
      field { id label type }
    }
  }
}
"""


def fetch_card(client, card_id: str) -> Optional[Dict[str, Any]]:
    """Fetch one card in the same shape iter_cards yields, or None if gone."""
    data = client.execute(SINGLE_CARD_QUERY, {"id": str(card_id)})
    return (data or {}).get("card") or None


# Server-side filter: only cards where `assignee_id` is on the card-level
# assignees. Massively faster than scanning the whole pipe.
#
# We intentionally do NOT pass `include_done: true`. On high-volume pipes
# (e.g. Retail) a single user can be the assignee on tens of thousands of
# historical/done cards; pulling them all back exhausts the API budget and
# is pointless because done cards aren't usually re-assigned.
PIPE_CARDS_BY_ASSIGNEE_QUERY = """
query PipeCardsByAssignee($pipeId: ID!, $cursor: String, $assigneeId: ID!) {
  cards(
    pipe_id: $pipeId
    first: 50
    after: $cursor
    search: { assignee_ids: [$assigneeId] }
  ) {
    pageInfo { hasNextPage endCursor }
    edges {
      node {
        id
        title
        url
        current_phase { id name }
        assignees { id name email }
        fields {
          name
          value
          array_value
          field { id label type }
        }
      }
    }
  }
}
"""

PIPE_PHASES_FALLBACK_QUERY = """
query PipePhases($pipeId: ID!) {
  pipe(id: $pipeId) {
    phases {
      id
      name
      cards(first: 20) {
        pageInfo { hasNextPage endCursor }
        edges {
          node {
            id
            title
            url
            current_phase { id name }
            assignees { id name email }
            fields {
              name
              value
              array_value
              field { id label type }
            }
          }
        }
      }
    }
  }
}
"""

# Server-side filter on a single field's stored value. Used for phase form
# fields of type assignee_select / user / member when we need to find cards
# where the user is in that specific field (not just card-level assignees).
FIND_CARDS_BY_FIELD_QUERY = """
query FindCardsByField($pipeId: ID!, $cursor: String, $fieldId: String!, $fieldValue: String!) {
  findCards(
    pipeId: $pipeId
    first: 20
    after: $cursor
    search: { fieldId: $fieldId, fieldValue: $fieldValue }
  ) {
    pageInfo { hasNextPage endCursor }
    edges {
      node {
        id
        title
        url
        current_phase { id name }
        assignees { id name email }
        fields {
          name
          value
          array_value
          field { id label type }
        }
      }
    }
  }
}
"""

PHASE_CARDS_PAGE_QUERY = """
query PhaseCards($phaseId: ID!, $cursor: String) {
  phase(id: $phaseId) {
    cards(first: 20, after: $cursor) {
      pageInfo { hasNextPage endCursor }
      edges {
        node {
          id
          title
          url
          current_phase { id name }
          assignees { id name email }
          fields {
            name
            value
            array_value
            field { id label type }
          }
        }
      }
    }
  }
}
"""


PIPE_FIELDS_QUERY = """
query PipeFields($pipeId: ID!) {
  pipe(id: $pipeId) {
    id
    name
    start_form_fields { id internal_id label type }
    phases { id name fields { id internal_id label type } }
  }
}
"""


# Phase metadata used to skip done / completed phases without downloading
# their cards. `done: true` flags Pipefy's terminal phase; we also match
# common name patterns because pipes often have multiple "completed-style"
# phases (e.g. Retail: "Completed Review Tasks" + "Completed Jobs").
PIPE_PHASES_META_QUERY = """
query PipePhasesMeta($pipeId: ID!) {
  pipe(id: $pipeId) {
    id
    phases { id name done cards_count }
  }
}
"""

# Try to filter a single phase's cards by assignee server-side. Pipefy may
# or may not accept `search` on phase.cards depending on schema version; we
# fall back gracefully if it errors.
PHASE_CARDS_BY_ASSIGNEE_QUERY = """
query PhaseCardsByAssignee($phaseId: ID!, $cursor: String, $assigneeId: ID!) {
  phase(id: $phaseId) {
    cards(first: 50, after: $cursor, search: { assignee_ids: [$assigneeId] }) {
      pageInfo { hasNextPage endCursor }
      edges {
        node {
          id
          title
          url
          current_phase { id name }
          assignees { id name email }
          fields {
            name
            value
            array_value
            field { id label type }
          }
        }
      }
    }
  }
}
"""


# Names that almost certainly mean "this phase is terminal — don't touch
# old cards living here". Matched case-insensitive, substring; combined
# with the user-supplied `exclude_phases` list and Pipefy's `done` flag.
_DONE_PHASE_PATTERNS: tuple = (
    "completed",      # Completed, Completed Jobs, Completed Review Tasks
    "cancelled",
    "canceled",
    "finance recon",
    "archived",
    "closed",
)


def _is_skippable_phase(name: Optional[str], done: bool, excluded: Set[str]) -> bool:
    """True if we should NOT download cards from this phase."""
    if done:
        return True
    n = _norm_phase(name)
    if not n:
        return False
    if n in excluded:
        return True
    return any(pat in n for pat in _DONE_PHASE_PATTERNS)


# Tiny TTL cache for the pipe phases meta query. A single preview can
# call `fetch_active_phases` up to 3x (UI badges, per-phase assignee
# scan, per-phase field fallback). The pipe's phase list doesn't change
# inside one preview run, so we memo the raw GraphQL result for ~30s
# keyed by pipe_id. Filtering by `exclude_phases` is cheap and done
# from the cached payload.
_PHASES_META_CACHE: Dict[str, tuple] = {}  # pipe_id -> (timestamp, raw_pipe_dict)
_PHASES_META_TTL_SEC: float = 30.0


def fetch_active_phases(
    client,
    pipe_id: str,
    *,
    exclude_phases: Optional[Iterable[str]] = None,
) -> Dict[str, list]:
    """Return {"active": [...], "skipped": [...]} phase metadata dicts.

    Each item: {"id", "name", "done", "cards_count"}.
    """
    import time as _time
    excluded: Set[str] = {_norm_phase(p) for p in (exclude_phases or []) if p}
    pipe_key = str(pipe_id)
    now = _time.time()
    cached = _PHASES_META_CACHE.get(pipe_key)
    if cached and (now - cached[0]) < _PHASES_META_TTL_SEC:
        pipe = cached[1]
    else:
        data = client.execute(PIPE_PHASES_META_QUERY, {"pipeId": pipe_key})
        pipe = data.get("pipe") or {}
        _PHASES_META_CACHE[pipe_key] = (now, pipe)
    active: list = []
    skipped: list = []
    for ph in pipe.get("phases") or []:
        item = {
            "id": str(ph.get("id") or ""),
            "name": ph.get("name") or "",
            "done": bool(ph.get("done")),
            "cards_count": int(ph.get("cards_count") or 0),
        }
        if _is_skippable_phase(item["name"], item["done"], excluded):
            skipped.append(item)
        else:
            active.append(item)
    return {"active": active, "skipped": skipped}


# Memo of pipes where `phase.cards(search:...)` is NOT supported (server
# returns "Unknown argument" or similar). We fall back to plain phase
# pagination + client-side filter for these.
_PHASE_SEARCH_UNSUPPORTED: set = set()



def discover_assignee_fields(client, pipe_id: str) -> Dict[str, Dict[str, str]]:
    """Return {field_id: {"id":..., "internal_id":..., "label":..., "type":..., "phase":...}}
    for assignee-bearing fields.

    Keyed by `id` (the slug, e.g. "team_member_to_work_on_support_issue") because that's what
    per-card payloads use. `internal_id` (numeric) is preserved for reference.
    `phase` records where the field lives ("Start form" or the phase name).
    """
    assignee_types = {"assignee_select", "user", "member"}
    data = client.execute(PIPE_FIELDS_QUERY, {"pipeId": str(pipe_id)})
    pipe = data.get("pipe") or {}
    out: Dict[str, Dict[str, str]] = {}

    def _add(f: Dict[str, Any], phase_name: str) -> None:
        if not f or f.get("type") not in assignee_types:
            return
        key = str(f.get("id"))
        if key in out:  # keep the first sighting's provenance
            return
        out[key] = {
            "id": key,
            "internal_id": str(f.get("internal_id") or ""),
            "label": f.get("label") or "",
            "type": f["type"],
            "phase": phase_name,
        }

    for f in pipe.get("start_form_fields") or []:
        _add(f, "Start form")
    for ph in pipe.get("phases") or []:
        for f in (ph or {}).get("fields") or []:
            _add(f, (ph or {}).get("name") or "")
    return out


# Process-level memory of pipes whose top-level cards() query timed out.
# We skip straight to the per-phase fallback for these on subsequent calls.
_SKIP_TOP_LEVEL_CARDS: set = set()


def iter_cards(
    client,
    pipe_id: str,
    *,
    exclude_phases: Optional[Iterable[str]] = None,
) -> Iterator[Dict[str, Any]]:
    """Yield card dicts. Tries top-level cards(pipe_id:...) then falls back to phases.

    On large pipes Pipefy times out the top-level query; once that happens we
    remember the pipe id and go straight to the per-phase path for the rest
    of this process's lifetime.

    When `exclude_phases` is supplied (case-insensitive phase names), excluded
    phases are skipped entirely in the per-phase fallback — no card downloads
    from "Completed" / "Cancelled" / "Finance Recon" etc. Cards yielded from
    the top-level query are filtered by `current_phase.name` instead.
    """
    excluded: Set[str] = {_norm_phase(p) for p in (exclude_phases or []) if p}
    yielded_ids: Set[str] = set()  # belt-and-braces dedupe across phases

    def _ok(card: Dict[str, Any]) -> bool:
        if not card:
            return False
        cid = str(card.get("id") or "")
        if cid and cid in yielded_ids:
            return False
        if excluded:
            ph_name = _norm_phase((card.get("current_phase") or {}).get("name"))
            if ph_name in excluded:
                return False
        if cid:
            yielded_ids.add(cid)
        return True

    cursor = None
    skip_top = str(pipe_id) in _SKIP_TOP_LEVEL_CARDS
    if not skip_top:
        try:
            while True:
                data = client.execute(PIPE_CARDS_QUERY, {"pipeId": str(pipe_id), "cursor": cursor})
                conn = data.get("cards") or {}
                for edge in conn.get("edges") or []:
                    node = (edge or {}).get("node")
                    if _ok(node):
                        yield node
                page = conn.get("pageInfo") or {}
                if not page.get("hasNextPage"):
                    return
                cursor = page.get("endCursor")
                if not cursor:
                    return
        except Exception as exc:
            log.info("cards(pipe_id:) failed (%s); falling back to per-phase iteration.", exc)
            if "timeout" in str(exc).lower() or "complexity" in str(exc).lower():
                _SKIP_TOP_LEVEL_CARDS.add(str(pipe_id))
                log.info("Pipe %s added to skip-list; future iterations will go straight to per-phase.", pipe_id)
    else:
        log.info("Pipe %s on skip-list; using per-phase iteration directly.", pipe_id)

    # Fallback: discover phases then page through each (skipping excluded phases entirely).
    data = client.execute(PIPE_PHASES_FALLBACK_QUERY, {"pipeId": str(pipe_id)})
    pipe = data.get("pipe") or {}
    skipped_phase_names = []
    for phase in pipe.get("phases") or []:
        phase_id = (phase or {}).get("id")
        if not phase_id:
            continue
        phase_name = (phase or {}).get("name") or ""
        if excluded and _norm_phase(phase_name) in excluded:
            skipped_phase_names.append(phase_name)
            continue
        conn = (phase or {}).get("cards") or {}
        for edge in conn.get("edges") or []:
            node = (edge or {}).get("node")
            if _ok(node):
                yield node
        page = conn.get("pageInfo") or {}
        cursor = page.get("endCursor") if page.get("hasNextPage") else None
        while cursor:
            data2 = client.execute(PHASE_CARDS_PAGE_QUERY, {"phaseId": str(phase_id), "cursor": cursor})
            ph = data2.get("phase") or {}
            conn2 = ph.get("cards") or {}
            for edge in conn2.get("edges") or []:
                node = (edge or {}).get("node")
                if _ok(node):
                    yield node
            page2 = conn2.get("pageInfo") or {}
            cursor = page2.get("endCursor") if page2.get("hasNextPage") else None
    if skipped_phase_names:
        log.info("Skipped %d excluded phase(s) entirely: %s", len(skipped_phase_names), ", ".join(skipped_phase_names))


def iter_cards_assigned_to(
    client,
    pipe_id: str,
    assignee_id: str,
    *,
    exclude_phases: Optional[Iterable[str]] = None,
) -> Iterator[Dict[str, Any]]:
    """Server-side filtered card iterator.

    Returns only cards where `assignee_id` is on the card-level assignees list.
    This is what powers the Pipefy UI's instant "filter by assignee" — the
    server walks its index instead of us downloading every card.

    NOTE: this filter only matches card-level (`card.assignees`). Cards where
    the user is only inside a phase form field of type `assignee_select` /
    `user` / `member` will NOT appear. Use `iter_cards()` for that case.
    """
    excluded: Set[str] = {_norm_phase(p) for p in (exclude_phases or []) if p}
    yielded_ids: Set[str] = set()
    cursor = None
    while True:
        data = client.execute(
            PIPE_CARDS_BY_ASSIGNEE_QUERY,
            {"pipeId": str(pipe_id), "cursor": cursor, "assigneeId": str(assignee_id)},
        )
        conn = data.get("cards") or {}
        for edge in conn.get("edges") or []:
            node = (edge or {}).get("node")
            if not node:
                continue
            cid = str(node.get("id") or "")
            if cid and cid in yielded_ids:
                continue
            if excluded:
                ph_name = _norm_phase((node.get("current_phase") or {}).get("name"))
                if ph_name in excluded:
                    continue
            if cid:
                yielded_ids.add(cid)
            yield node
        page = conn.get("pageInfo") or {}
        if not page.get("hasNextPage"):
            return
        cursor = page.get("endCursor")
        if not cursor:
            return


def iter_cards_by_field_value(
    client,
    pipe_id: str,
    field_id: str,
    field_value: str,
    *,
    exclude_phases: Optional[Iterable[str]] = None,
) -> Iterator[Dict[str, Any]]:
    """Server-side filter via `findCards`: cards where `field_id == field_value`."""
    excluded: Set[str] = {_norm_phase(p) for p in (exclude_phases or []) if p}
    yielded_ids: Set[str] = set()
    cursor = None
    while True:
        data = client.execute(
            FIND_CARDS_BY_FIELD_QUERY,
            {
                "pipeId": str(pipe_id),
                "cursor": cursor,
                "fieldId": str(field_id),
                "fieldValue": str(field_value),
            },
        )
        conn = data.get("findCards") or {}
        for edge in conn.get("edges") or []:
            node = (edge or {}).get("node")
            if not node:
                continue
            cid = str(node.get("id") or "")
            if cid and cid in yielded_ids:
                continue
            if excluded:
                ph_name = _norm_phase((node.get("current_phase") or {}).get("name"))
                if ph_name in excluded:
                    continue
            if cid:
                yielded_ids.add(cid)
            yield node
        page = conn.get("pageInfo") or {}
        if not page.get("hasNextPage"):
            return
        cursor = page.get("endCursor")
        if not cursor:
            return


def iter_cards_assigned_to_by_phases(
    client,
    pipe_id: str,
    assignee_id: str,
    *,
    exclude_phases: Optional[Iterable[str]] = None,
) -> Iterator[Dict[str, Any]]:
    """Card-level assignee iterator that walks the pipe one phase at a time.

    This is the key perf path for high-volume pipes (e.g. Retail with 36k+
    cards in Completed Review Tasks): we enumerate the pipe's phases, drop
    any phase that's `done` / matches a known done-name / is user-excluded,
    and only download cards from the phases that remain. For each remaining
    phase we try `phase.cards(search:{assignee_ids:[user]})` server-side
    first; if Pipefy rejects the `search` arg on `phase.cards` we fall back
    to plain pagination + client-side assignee filter for THAT phase only
    (still bounded by that one phase's `cards_count`).
    """
    phases_info = fetch_active_phases(client, pipe_id, exclude_phases=exclude_phases)
    active = phases_info["active"]
    skipped = phases_info["skipped"]
    skipped_total = sum(p["cards_count"] for p in skipped)
    active_total = sum(p["cards_count"] for p in active)
    log.info(
        "Phase scan: %d active phase(s) (~%d cards), skipped %d phase(s) (~%d cards): %s",
        len(active), active_total, len(skipped), skipped_total,
        ", ".join(f"{p['name']} ({p['cards_count']})" for p in skipped) or "none",
    )

    yielded_ids: Set[str] = set()
    aid = str(assignee_id)
    pipe_key = str(pipe_id)
    use_server_filter = pipe_key not in _PHASE_SEARCH_UNSUPPORTED

    for ph in active:
        phase_id = ph["id"]
        phase_name = ph["name"]
        cursor = None
        scanned_in_phase = 0
        matched_in_phase = 0

        while True:
            data = None
            if use_server_filter:
                try:
                    data = client.execute(
                        PHASE_CARDS_BY_ASSIGNEE_QUERY,
                        {"phaseId": str(phase_id), "cursor": cursor, "assigneeId": aid},
                    )
                except Exception as exc:  # noqa: BLE001
                    msg = str(exc).lower()
                    if "unknown argument" in msg or ("argument 'search'" in msg) or ("search" in msg and "phase" in msg):
                        log.info(
                            "phase.cards(search:) not supported on pipe %s; falling back to client-side filter for this run.",
                            pipe_key,
                        )
                        _PHASE_SEARCH_UNSUPPORTED.add(pipe_key)
                        use_server_filter = False
                        data = None  # falls through to fallback below
                    else:
                        # Per-phase failure (e.g. timeout): skip THIS phase
                        # and move on so one slow phase doesn't sink the
                        # whole preview.
                        log.warning(
                            "Phase '%s' (id=%s) failed; skipping. %s",
                            phase_name, phase_id, exc,
                        )
                        break
            if data is None:
                # Fallback: plain phase pagination + client-side assignee filter.
                try:
                    data = client.execute(
                        PHASE_CARDS_PAGE_QUERY,
                        {"phaseId": str(phase_id), "cursor": cursor},
                    )
                except Exception as exc:  # noqa: BLE001
                    log.warning(
                        "Phase '%s' (id=%s) fallback page failed; skipping. %s",
                        phase_name, phase_id, exc,
                    )
                    break
            ph_node = data.get("phase") or {}
            conn = ph_node.get("cards") or {}
            edges = conn.get("edges") or []
            for edge in edges:
                node = (edge or {}).get("node")
                if not node:
                    continue
                scanned_in_phase += 1
                cid = str(node.get("id") or "")
                if cid and cid in yielded_ids:
                    continue
                if not use_server_filter:
                    # client-side filter: keep only cards assigned to user
                    assignees = node.get("assignees") or []
                    if not any(str((a or {}).get("id")) == aid for a in assignees):
                        continue
                if cid:
                    yielded_ids.add(cid)
                matched_in_phase += 1
                yield node
            page = conn.get("pageInfo") or {}
            if not page.get("hasNextPage"):
                break
            cursor = page.get("endCursor")
            if not cursor:
                break

        log.info(
            "Phase '%s' (id=%s): %d scanned, %d matched",
            phase_name, phase_id, scanned_in_phase, matched_in_phase,
        )


def _field_matches(card: Dict[str, Any], field_id: str, accepted_values: Set[str]) -> bool:
    """True if `card.fields[field with id=field_id]` value matches anything in `accepted_values`.

    Accepts both scalar `value` and `array_value` (multi-select fields).
    Comparison is case-insensitive on stringified values.
    """
    fid = str(field_id)
    wants = {str(v).strip().lower() for v in accepted_values if v is not None and str(v).strip()}
    if not wants:
        return False
    for f in card.get("fields") or []:
        meta = (f or {}).get("field") or {}
        if str(meta.get("id") or "") != fid:
            continue
        v = f.get("value")
        if v is not None and str(v).strip().lower() in wants:
            return True
        for item in (f.get("array_value") or []):
            if item is not None and str(item).strip().lower() in wants:
                return True
    return False


def iter_cards_by_field_value_via_phases(
    client,
    pipe_id: str,
    field_id: str,
    accepted_values: Iterable[str],
    *,
    exclude_phases: Optional[Iterable[str]] = None,
) -> Iterator[Dict[str, Any]]:
    """Fallback for `findCards` timeouts: walk active phases and filter client-side.

    This is bounded by the active phases' card counts (we already drop the
    huge done/completed phases), so it's typically fast enough even on
    pipes where Pipefy's `findCards` index is missing or slow.
    """
    accepted_set: Set[str] = {str(v) for v in accepted_values if v}
    if not accepted_set:
        return
    phases_info = fetch_active_phases(client, pipe_id, exclude_phases=exclude_phases)
    active = phases_info["active"]
    log.info(
        "Per-phase field fallback: scanning %d phase(s) (~%d cards) for fieldId=%s",
        len(active), sum(p["cards_count"] for p in active), field_id,
    )

    yielded_ids: Set[str] = set()
    for ph in active:
        phase_id = ph["id"]
        phase_name = ph["name"]
        cursor = None
        scanned = 0
        matched = 0
        while True:
            try:
                data = client.execute(
                    PHASE_CARDS_PAGE_QUERY,
                    {"phaseId": str(phase_id), "cursor": cursor},
                )
            except Exception as exc:  # noqa: BLE001
                log.warning(
                    "Per-phase field fallback: phase '%s' page failed; skipping. %s",
                    phase_name, exc,
                )
                break
            ph_node = data.get("phase") or {}
            conn = ph_node.get("cards") or {}
            for edge in conn.get("edges") or []:
                node = (edge or {}).get("node")
                if not node:
                    continue
                scanned += 1
                cid = str(node.get("id") or "")
                if cid and cid in yielded_ids:
                    continue
                if not _field_matches(node, field_id, accepted_set):
                    continue
                if cid:
                    yielded_ids.add(cid)
                matched += 1
                yield node
            page = conn.get("pageInfo") or {}
            if not page.get("hasNextPage"):
                break
            cursor = page.get("endCursor")
            if not cursor:
                break
        log.info(
            "Per-phase field fallback: phase '%s' %d scanned, %d matched (fieldId=%s)",
            phase_name, scanned, matched, field_id,
        )


def iter_cards_fast_union(
    client,
    pipe_id: str,
    old_user_id: str,
    *,
    include_card_level: bool,
    field_ids: Iterable[str],
    old_user_email: Optional[str] = None,
    exclude_phases: Optional[Iterable[str]] = None,
) -> Iterator[Dict[str, Any]]:
    """Union of all server-side filtered sources for one swap target.

    Yields each matching card exactly once. Covers:
      - card-level assignees (if `include_card_level`) via per-phase scan
        with `phase.cards(search:{assignee_ids:[user]})` — skips done /
        completed phases entirely so we never download from them.
      - each selected phase form field via `findCards(search:{fieldId,fieldValue})`
        — tried with both the user's ID and (if provided) their email so that
        fields storing values either way are caught. `findCards` doesn't
        accept a phase filter, so excluded phases are dropped client-side.
    """
    seen: Set[str] = set()

    def _emit(stream: Iterator[Dict[str, Any]]) -> Iterator[Dict[str, Any]]:
        for card in stream:
            cid = str((card or {}).get("id") or "")
            if not cid or cid in seen:
                continue
            seen.add(cid)
            yield card

    if include_card_level:
        log.info("Fast scan: per-phase assignee scan for user=%s", old_user_id)

        def _safe_phase_scan():
            try:
                for card in iter_cards_assigned_to_by_phases(
                    client, pipe_id, old_user_id, exclude_phases=exclude_phases
                ):
                    yield card
            except Exception as exc:  # noqa: BLE001
                log.warning(
                    "Per-phase assignee scan failed; continuing with field-level only. %s",
                    exc,
                )

        yield from _emit(_safe_phase_scan())

    # Field-level lookups via findCards can time out on big pipes for
    # specific field types (e.g. plain-text fields with no index). When
    # that happens we MUST NOT silently report zero — we fall back to
    # per-phase pagination + client-side field filtering (bounded by
    # active phases' card counts since done phases are already skipped).
    def _safe_field_scan(label: str, value: str, failure_flag: list):
        log.info("Fast scan: findCards fieldId=%s fieldValue=<%s>", fid_s, label)
        try:
            for card in iter_cards_by_field_value(
                client, pipe_id, fid_s, value, exclude_phases=exclude_phases
            ):
                yield card
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "findCards fieldId=%s (<%s>) failed; will fall back to per-phase scan. %s",
                fid_s, label, exc,
            )
            failure_flag.append(label)

    for fid in field_ids:
        fid_s = str(fid)
        findcards_failed: list = []  # collects labels of failed findCards attempts
        yield from _emit(_safe_field_scan("user_id", old_user_id, findcards_failed))
        if old_user_email:
            yield from _emit(_safe_field_scan("user_email", old_user_email, findcards_failed))

        # If ANY findCards attempt for this field errored, do per-phase
        # fallback once with all accepted values so we don't miss matches.
        if findcards_failed:
            log.info(
                "Field %s: findCards failed for %s; running per-phase fallback.",
                fid_s, "+".join(findcards_failed),
            )
            values = [old_user_id]
            if old_user_email:
                values.append(old_user_email)
            try:
                yield from _emit(
                    iter_cards_by_field_value_via_phases(
                        client, pipe_id, fid_s, values, exclude_phases=exclude_phases
                    )
                )
            except Exception as exc:  # noqa: BLE001
                log.warning(
                    "Per-phase fallback for field %s also failed: %s", fid_s, exc,
                )


# ---------------------------------------------------------------------------
# Discovery probes — cheap "does this user appear here at all?" checks.
#
# Both queries ask the server's index for at most ONE matching card, so a
# probe costs a single tiny API call regardless of pipe size. Used by the
# web UI's "Find pipes" feature to spot which pipes need a swap without
# running a full scan on each. NO fallbacks on purpose: if the server-side
# search errors (e.g. findCards timeout on an unindexed field type) we
# return None ("unknown") instead of burning the API budget — the full
# per-pipe scan will settle it later.
# ---------------------------------------------------------------------------

PROBE_CARDS_BY_ASSIGNEE_QUERY = """
query ProbeCardsByAssignee($pipeId: ID!, $assigneeId: ID!) {
  cards(pipe_id: $pipeId, first: 1, search: { assignee_ids: [$assigneeId] }) {
    edges { node { id } }
  }
}
"""

PROBE_FIND_CARDS_QUERY = """
query ProbeFindCards($pipeId: ID!, $fieldId: String!, $fieldValue: String!) {
  findCards(pipeId: $pipeId, first: 1, search: { fieldId: $fieldId, fieldValue: $fieldValue }) {
    edges { node { id } }
  }
}
"""


def probe_card_level_assignee(client, pipe_id: str, assignee_id: str) -> Optional[bool]:
    """True/False = at least one / no card with this card-level assignee.

    None = probe failed (treat as "unknown", not "no").
    """
    try:
        data = client.execute(
            PROBE_CARDS_BY_ASSIGNEE_QUERY,
            {"pipeId": str(pipe_id), "assigneeId": str(assignee_id)},
        )
        return bool((data.get("cards") or {}).get("edges"))
    except Exception as exc:  # noqa: BLE001
        log.warning("Card-level probe failed for pipe %s: %s", pipe_id, exc)
        return None


def probe_field_value(client, pipe_id: str, field_id: str, field_value: str) -> Optional[bool]:
    """True/False = at least one / no card where `field_id == field_value`.

    None = probe failed (treat as "unknown", not "no").
    """
    try:
        data = client.execute(
            PROBE_FIND_CARDS_QUERY,
            {"pipeId": str(pipe_id), "fieldId": str(field_id), "fieldValue": str(field_value)},
        )
        return bool((data.get("findCards") or {}).get("edges"))
    except Exception as exc:  # noqa: BLE001
        log.warning("Field probe failed for pipe %s field %s: %s", pipe_id, field_id, exc)
        return None

