"""Exceptions raised by the print action (spec §7)."""

from __future__ import annotations

from homeassistant.exceptions import HomeAssistantError


class PrintOutcomeUncertainError(HomeAssistantError):
    """A job failed after the transmission boundary.

    The printer may or may not have produced some or all of the label, so the
    job must never be replayed automatically.
    """
