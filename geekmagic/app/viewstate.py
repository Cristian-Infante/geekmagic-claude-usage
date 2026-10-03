"""What is on the screen: one provider's, or one of the views of two providers at once (the "mode")."""

from __future__ import annotations

from geekmagic.providers import TITLES
from geekmagic.render.views import PANEL_VIEWS

SPLIT = "split"  # both providers on one screen; every view has its own image slots on the device
STATS = "stats"  # activity numbers for both providers
BREAKDOWN = "breakdown"  # which projects and models the requests went to
HOURS = "hours"  # at what hours of the day you work
VIEW_NAMES = {SPLIT: "vista dividida", STATS: "estadísticas", BREAKDOWN: "proyectos y modelos", HOURS: "horas pico"}
assert set(VIEW_NAMES) == set(PANEL_VIEWS), "every view of two needs a name"
STATS_VIEWS = {key for key, view in PANEL_VIEWS.items() if view.needs_activity}  # drawn from the local activity logs


class ViewState:
    def __init__(self, first_run_provider: str | None = None) -> None:
        self.first_run_provider = first_run_provider  # --provider: only the very first run's choice
        self.mode: str | None = None  # a view of two providers instead of one (only the menu turns these on), or None
        self.provider = "claude"  # the last single-provider view, also what the left-click goes on from

    @property
    def key(self) -> str:
        """What the screen should be showing: a provider, or a view of two."""
        return self.mode or self.provider

    # The flag the split view was built on, kept as a property so `view.split` reads and writes naturally.
    @property
    def split(self) -> bool:
        return self.mode == SPLIT

    @split.setter
    def split(self, value: bool) -> None:
        self.mode = SPLIT if value else (None if self.mode == SPLIT else self.mode)

    def restore(self, saved: dict) -> None:
        """The view comes back exactly as it was left; --provider is only the very first run's choice."""
        first_run_provider = self.first_run_provider
        saved_view = saved.get("view") if saved.get("view") in (*TITLES, *PANEL_VIEWS) else None
        saved_provider = saved.get("provider") if saved.get("provider") in TITLES else None
        view = saved_view or first_run_provider or "claude"
        self.mode = view if view in PANEL_VIEWS else None
        self.provider = (saved_provider or first_run_provider or "claude") if self.mode else view

    def snapshot(self) -> dict:
        return {"view": self.key, "provider": self.provider}
