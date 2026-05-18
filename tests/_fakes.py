"""Test helpers: a fake Pipefy GraphQL client driven by a routing table."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional


FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"


def load_fixture(name: str) -> Dict[str, Any]:
    with open(FIXTURES / name, "r", encoding="utf-8") as fh:
        return json.load(fh)


class FakeClient:
    """A pluggable GraphQL client. You either provide a `router(query, variables) -> data`
    callable, or pre-stage responses keyed by a substring of the query."""

    def __init__(
        self,
        router: Optional[Callable[[str, Dict[str, Any]], Dict[str, Any]]] = None,
        responses: Optional[Dict[str, Any]] = None,
    ) -> None:
        self.router = router
        self.responses = responses or {}
        self.calls: List[Dict[str, Any]] = []

    def execute(self, query: str, variables: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        v = variables or {}
        self.calls.append({"query": query, "variables": v})
        if self.router:
            return self.router(query, v)
        for key, val in self.responses.items():
            if key in query:
                return val() if callable(val) else val
        raise AssertionError(f"FakeClient: no response staged for query: {query[:80]}...")
