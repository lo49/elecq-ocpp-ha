from __future__ import annotations

import logging

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.exceptions import HomeAssistantError

from .const import DOMAIN, SIGNAL_STATE_UPDATED
from .ocpp_server import ElecqOcppManager

_LOGGER = logging.getLogger(__name__)

async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    data = hass.data[DOMAIN][entry.entry_id]
    manager: ElecqOcppManager = data["manager"]

    device_info = DeviceInfo(
        identifiers={(DOMAIN, "elecq_au101")},
        name="Elecq AU101 Charger",
        manufacturer="Elecq",
        model="AU101",
    )

    async_add_entities([ElecqChargeRateNumber(manager, device_info)])


class ElecqChargeRateNumber(NumberEntity):
    """Curseur pour ajuster dynamiquement l'ampérage (Délestage OCPP 2.0.1)."""

    _attr_has_entity_name = True
    _attr_name = "Dynamic Charge Limit"
    _attr_unique_id = "elecq_au101_dynamic_charge_limit"
    
    # Configuration du curseur selon les standards des VE
    _attr_native_min_value = 6.0
    _attr_native_max_value = 32.0
    _attr_native_step = 1.0
    _attr_native_unit_of_measurement = "A"
    _attr_mode = NumberMode.SLIDER

    def __init__(self, manager: ElecqOcppManager, device_info: DeviceInfo) -> None:
        self._manager = manager
        self._attr_device_info = device_info
        self._busy: bool = False

    async def async_added_to_hass(self) -> None:
        async def _handle_update() -> None:
            # Re-sync le curseur si la borne change sa valeur ou confirme l'état
            self.async_write_ha_state()

        self.async_on_remove(
            async_dispatcher_connect(self.hass, SIGNAL_STATE_UPDATED, _handle_update)
        )

    @property
    def native_value(self) -> float | None:
        """La valeur affichée provient de l'état actuel mémorisé par le manager."""
        # On récupère la limite courante stockée dans le manager (ex: st.current_limit)
        # À adapter selon le nom exact de la variable dans ton fichier ocpp_server.py
        return getattr(self._manager.state, "current_limit", 32.0)

    @property
    def available(self) -> bool:
        """Grise le curseur pendant que la borne traite le changement de puissance."""
        return self._manager.is_available and not self._busy

    async def async_set_native_value(self, value: float) -> None:
        """L'utilisateur (ou l'automatisation de délestage) change la valeur du curseur."""
        st = self._manager.state
        target_amps = int(value)

        # Sécurité : Si la voiture n'est même pas branchée, on bloque
        if not st.plugged_in:
            raise HomeAssistantError(
                "Cannot adjust charge rate: EV is not plugged in."
            )

        # Verrouillage visuel du curseur (greyout)
        self._busy = True
        self.async_write_ha_state()

        try:
            # Appel de la fonction OCPP 2.0.1 du manager pour envoyer le ChargingProfile / SetVariables
            # À adapter selon la méthode exacte de ton ElecqOcppManager (ex: async_set_charging_profile)
            ok = await self._manager.async_set_charge_rate(target_amps)
        except Exception as e:
            _LOGGER.error("Error sending OCPP 2.0.1 charge rate: %s", e)
            ok = False
        finally:
            self._busy = False
            self.async_write_ha_state()

        if not ok:
            _LOGGER.warning("Charger rejected the new charge rate limit: %dA", target_amps)