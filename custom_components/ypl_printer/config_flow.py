"""Config flow for the YPL Printer integration."""

from __future__ import annotations

from homeassistant.config_entries import ConfigFlow

from .const import DOMAIN


class YplPrinterConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for YPL Printer."""

    VERSION = 1
