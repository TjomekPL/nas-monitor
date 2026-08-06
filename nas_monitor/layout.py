"""
nas_monitor.layout
---------------------
Persisted card ordering for the "Podsumowanie" (Summary) tab -
purely cosmetic state (which order the cards appear in), stored the
same way everything else that isn't derivable from the live system is
(see state_store.py). Server-side rather than the browser's own
localStorage on purpose: this is a single-admin tool, but that admin
may open the dashboard from more than one browser or device, and a
server-side order stays consistent across all of them the same way
the session-duration setting does.

Sections are independent of each other - reordering the "disks"
section never touches any other section's saved order, and dragging a
card is only ever a rewrite of the full order *within* the section
being reordered (see set_order below), never a way to move a card
between sections.
"""

from __future__ import annotations

from typing import Any

from nas_monitor import state_store

LAYOUT_FILE = "dashboard-layout.json"


def get_order(section: str) -> list[str]:
    """Saved card-id order for a section (e.g. "disks"), or an empty
    list if nothing's been saved yet - the frontend falls back to
    whatever order the API returned the items in for a section with no
    saved order, and any card id not present in a saved order (new
    disk since the order was last saved) is appended after everything
    that is, not dropped."""
    data = state_store.load(LAYOUT_FILE, default={})
    order = data.get(section)
    return list(order) if isinstance(order, list) else []


def set_order(section: str, order: list[str]) -> dict[str, Any]:
    data = state_store.load(LAYOUT_FILE, default={})
    if not isinstance(data, dict):
        data = {}
    data[section] = [str(item) for item in order]
    return state_store.save(LAYOUT_FILE, data)
