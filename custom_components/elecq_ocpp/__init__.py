# --- HACK DE COMPATIBILITÉ JSONSCHEMA (Python 3.14 / HA Core) ---
import sys
import jsonschema

if not hasattr(jsonschema, "_validators") and hasattr(jsonschema, "validators"):
    jsonschema._validators = jsonschema.validators
    sys.modules["jsonschema._validators"] = jsonschema.validators
# ----------------------------------------------------------------

from __future__ import annotations

import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.typing import ConfigType

from .const import (
    DOMAIN,
    CONF_PORT,
    CONF_ID_TOKEN,
    CONF_EVSE_ID,
    CONF_CONNECTOR_ID,
)
from .ocpp_server import ElecqOcppManager

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[str] = ["sensor", "binary_sensor", "switch", "number"]


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Set up Elecq OCPP integration (YAML not used)."""
    hass.data.setdefault(DOMAIN, {})
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Elecq OCPP from a config entry."""
    hass.data.setdefault(DOMAIN, {})

    port: int = entry.data[CONF_PORT]
    id_token: str = entry.data[CONF_ID_TOKEN]
    evse_id: int = entry.data[CONF_EVSE_ID]
    connector_id: int = entry.data[CONF_CONNECTOR_ID]

    manager = ElecqOcppManager(
        hass=hass,
        port=port,
        id_token=id_token,
        evse_id=evse_id,
        connector_id=connector_id,
    )

    hass.data[DOMAIN][entry.entry_id] = {
        "manager": manager,
    }

    # Start the OCPP WebSocket server
    hass.async_create_task(manager.async_start_server())

    # Forward entry setup to platforms
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    data = hass.data[DOMAIN].get(entry.entry_id)
    manager: ElecqOcppManager | None = None
    if isinstance(data, dict):
        manager = data.get("manager")

    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)

    if unload_ok:
        if manager is not None:
            await manager.async_stop_server()
        hass.data[DOMAIN].pop(entry.entry_id, None)

    return unload_ok
