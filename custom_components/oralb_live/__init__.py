"""Oral-B Live: hybrid passive/active integration for Oral-B toothbrushes."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_ADDRESS, Platform
from homeassistant.core import HomeAssistant

from .const import CONF_CONNECTION_MODE, DEFAULT_CONNECTION_MODE, DOMAIN
from .coordinator import OralBLiveCoordinator

PLATFORMS: list[Platform] = [Platform.SENSOR]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Oral-B Live from a config entry."""
    address: str = entry.data[CONF_ADDRESS]
    mode: str = entry.options.get(CONF_CONNECTION_MODE, DEFAULT_CONNECTION_MODE)
    coordinator = OralBLiveCoordinator(hass, address, entry.title, mode)
    coordinator.async_start()
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))
    return True


async def _async_options_updated(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload the entry when the connection mode changes."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    if unload_ok := await hass.config_entries.async_unload_platforms(
        entry, PLATFORMS
    ):
        coordinator: OralBLiveCoordinator = hass.data[DOMAIN].pop(entry.entry_id)
        await coordinator.async_stop()
    return unload_ok
