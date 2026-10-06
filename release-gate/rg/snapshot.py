"""World snapshot: candidate + currently-live manifest and derived diff data.
One snapshot per run => every check observes the same point in time."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_LINK_RE = re.compile(r'\[[^\]]*\]\(([^)]+)\)')
_RENAME_SLUG_THRESHOLD = 0  # exact slug remapping is supplied by redirects

EMPTY_BASELINE: dict[str, Any] = {
    "schema": "docrelease/manifest.v1",
    "release_id": None,
    "pages": [],
    "redirects": {},
    "assets": [],
    "feedback": [],
    "build": {"artifacts": [], "search_index": {"routes": []},
              "routes_table": {"routes": []}},
    "migrations": {"deleted_pages": []},
}


@dataclass
class Snapshot:
    candidate: dict
    live: dict
    source_root: str | None
    links: dict[str, list[str]] = field(default_factory=dict)
    # derived diffs, filled in by checks order but computed once
    page_set_old: set = field(default_factory=set)
    page_set_new: set = field(default_factory=set)
    added: set = field(default_factory=set)
    deleted: set = field(default_factory=set)
    common: set = field(default_factory=set)
    old_by_route: dict = field(default_factory=dict)
    new_by_route: dict = field(default_factory=dict)

    def diff_pages(self) -> tuple[set, set, set]:
        """(added, deleted, common) relative to live. A deleted route covered by
        a redirect in the candidate is treated as a *rename/move* and changes the
        broken-link outcome downstream."""
        if self.page_set_new:
            return self.added, self.deleted, self.common
        old = {p["route"] for p in self.live.get("pages", [])}
        new = {p["route"] for p in self.candidate.get("pages", [])}
        self.page_set_old, self.page_set_new = old, new
        self.added = new - old
        self.deleted = old - new
        self.common = old & new
        self.old_by_route = {p["route"]: p for p in self.live.get("pages", [])}
        self.new_by_route = {p["route"]: p for p in self.candidate.get("pages", [])}
        return self.added, self.deleted, self.common

    def renamed(self) -> dict[str, str]:
        """Deleted routes that have a redirect to a route that exists now."""
        _, deleted, _ = self.diff_pages()
        out = {}
        for d in sorted(deleted):
            tgt = self.candidate.get("redirects", {}).get(d)
            if tgt and tgt in self.page_set_new:
                out[d] = tgt
        return out

    def truly_removed(self) -> set:
        _, deleted, _ = self.diff_pages()
        return deleted - set(self.renamed())

    def changed_routes(self) -> set:
        added, deleted, common = self.diff_pages()
        changed = set(added) | set(deleted)
        for r in common:
            if self.old_by_route[r].get("content_sha") != self.new_by_route[r].get("content_sha"):
                changed.add(r)
        return changed

    def page_links(self) -> dict[str, list[str]]:
        """Internal outbound links parsed from candidate source markdown."""
        if self.links or not self.source_root:
            return self.links
        root = Path(self.source_root)
        for p in self.candidate.get("pages", []):
            fp = root / p["source"]
            links: list[str] = []
            if fp.exists():
                text = fp.read_text(encoding="utf-8", errors="replace")
                for target in _LINK_RE.findall(text):
                    if target.startswith("http") or target.startswith("mailto:"):
                        continue
                    links.append(target.split("#", 1)[0])
            self.links[p["route"]] = links
        return self.links
