"""Tests for the YPL Printer integration scaffold."""

from homeassistant.core import HomeAssistant
from homeassistant.loader import async_get_integration

from custom_components.ypl_printer import async_setup
from custom_components.ypl_printer.const import DOMAIN


async def test_integration_loads(hass: HomeAssistant) -> None:
    """Home Assistant finds the custom integration and its setup succeeds."""
    integration = await async_get_integration(hass, DOMAIN)

    assert integration.domain == DOMAIN
    assert integration.config_flow
    assert await async_setup(hass, {})
