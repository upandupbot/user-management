"""Iterate cards in a Pipefy pipe with cursor pagination + fallback."""
from __future__ import annotations

import logging
from typing import Any, Dict, Iterator


log = logging.getLogger(__name__)


PIPE_CARDS_QUERY = """
query PipeCards($pipeId: ID!, $cursor: String) {
  cards(pipe_id: $pipeId, first: 50, after: $cursor) {
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
      cards(first: 50) {
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

PHASE_CARDS_PAGE_QUERY = """
query PhaseCards($phaseId: ID!, $cursor: String) {
  phase(id: $phaseId) {
    cards(first: 50, after: $cursor) {
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


def discover_assignee_fields(client, pipe_id: str) -> Dict[str, Dict[str, str]]:
    """Return {field_id: {"id":..., "internal_id":..., "label":..., "type":...}} for assignee-bearing fields.

    Keyed by `id` (the slug, e.g. "team_member_to_work_on_support_issue") because that's what
    per-card payloads use. `internal_id` (numeric) is preserved for reference.
    """
    assignee_types = {"assignee_select", "user", "member"}
    data = client.execute(PIPE_FIELDS_QUERY, {"pipeId": str(pipe_id)})
    pipe = data.get("pipe") or {}
    out: Dict[str, Dict[str, str]] = {}

    def _add(f: Dict[str, Any]) -> None:
        if not f or f.get("type") not in assignee_types:
            return
        key = str(f.get("id"))
        out[key] = {
            "id": key,
            "internal_id": str(f.get("internal_id") or ""),
            "label": f.get("label") or "",
            "type": f["type"],
        }

    for f in pipe.get("start_form_fields") or []:
        _add(f)
    for ph in pipe.get("phases") or []:
        for f in (ph or {}).get("fields") or []:
            _add(f)
    return out


def iter_cards(client, pipe_id: str) -> Iterator[Dict[str, Any]]:
    """Yield card dicts. Tries top-level cards(pipe_id:...) then falls back to phases."""
    cursor = None
    try:
        while True:
            data = client.execute(PIPE_CARDS_QUERY, {"pipeId": str(pipe_id), "cursor": cursor})
            conn = data.get("cards") or {}
            for edge in conn.get("edges") or []:
                node = (edge or {}).get("node")
                if node:
                    yield node
            page = conn.get("pageInfo") or {}
            if not page.get("hasNextPage"):
                return
            cursor = page.get("endCursor")
            if not cursor:
                return
    except Exception as exc:
        log.info("cards(pipe_id:) failed (%s); falling back to per-phase iteration.", exc)

    # Fallback: discover phases then page through each.
    data = client.execute(PIPE_PHASES_FALLBACK_QUERY, {"pipeId": str(pipe_id)})
    pipe = data.get("pipe") or {}
    for phase in pipe.get("phases") or []:
        phase_id = (phase or {}).get("id")
        if not phase_id:
            continue
        conn = (phase or {}).get("cards") or {}
        for edge in conn.get("edges") or []:
            node = (edge or {}).get("node")
            if node:
                yield node
        page = conn.get("pageInfo") or {}
        cursor = page.get("endCursor") if page.get("hasNextPage") else None
        while cursor:
            data2 = client.execute(PHASE_CARDS_PAGE_QUERY, {"phaseId": str(phase_id), "cursor": cursor})
            ph = data2.get("phase") or {}
            conn2 = ph.get("cards") or {}
            for edge in conn2.get("edges") or []:
                node = (edge or {}).get("node")
                if node:
                    yield node
            page2 = conn2.get("pageInfo") or {}
            cursor = page2.get("endCursor") if page2.get("hasNextPage") else None
