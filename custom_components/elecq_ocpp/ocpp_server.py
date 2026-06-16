from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

import websockets
from websockets.server import WebSocketServer
from websockets.exceptions import ConnectionClosed

from homeassistant.core import HomeAssistant
from homeassistant.helpers.dispatcher import async_dispatcher_send

from ocpp.routing import on
from ocpp.v201 import ChargePoint as OcppChargePointBase
from ocpp.v201 import call, call_result
from ocpp.v201.enums import (
    RegistrationStatusType,
    RequestStartStopStatusType,
    MessageTriggerEnumType,  # 👈 NEW
    ChargingProfilePurposeType,  # 👈 AJOUT
    ChargingProfileStatusType,
)

from .const import SIGNAL_STATE_UPDATED

_LOGGER = logging.getLogger(__name__)


@dataclass
class ElecqChargerState:
    """Live state from Elecq charger."""

    power_kw: Optional[float] = None
    power_kw_smoothed: Optional[float] = None

    energy_kwh: Optional[float] = None

    session_energy_kwh: Optional[float] = None
    session_start: Optional[datetime] = None
    session_start_meter_kwh: Optional[float] = None
    session_event_type: Optional[str] = None
    session_trigger_reason: Optional[str] = None

    plugged_in: bool = False
    charging: bool = False

    remote_stop_requested: bool = False
    current_limit: float = 32.0

    transaction_id: Optional[str] = None
    last_status: Optional[str] = None
    last_charging_state: Optional[str] = None

    last_transaction_info: Optional[dict[str, Any]] = None
    last_meter_value: Optional[list[dict[str, Any]]] = None

    last_update: Optional[datetime] = None


class ElecqOcppManager:
    """Manager running OCPP server & storing charger state."""

    def __init__(
        self,
        hass: HomeAssistant,
        port: int,
        id_token: str,
        evse_id: int,
        connector_id: int,
    ) -> None:
        self.hass = hass
        self.port = port
        self.id_token = id_token
        self.evse_id = evse_id
        self.connector_id = connector_id

        self._cp: Optional[ElecqChargePoint] = None
        self._server: Optional[WebSocketServer] = None

        self.state = ElecqChargerState()

        self._power_window: list[float] = []
        self._max_power_samples: int = 5

    def _notify(self) -> None:
        async_dispatcher_send(self.hass, SIGNAL_STATE_UPDATED)

    def _update_power_smoothing(self, power_kw: float) -> None:
        self._power_window.append(power_kw)
        if len(self._power_window) > self._max_power_samples:
            self._power_window.pop(0)
        self.state.power_kw_smoothed = sum(self._power_window) / len(self._power_window)

    def _update_session_energy(self, total_kwh: float) -> None:
        st = self.state
        if st.session_start is None:
            return
        if st.session_start_meter_kwh is None:
            st.session_start_meter_kwh = total_kwh
        st.session_energy_kwh = max(0.0, total_kwh - st.session_start_meter_kwh)

    def update_meter_values(self, meter_value: list[dict[str, Any]]) -> None:
        """Parse meterValue[] from TransactionEvent."""
        st = self.state
        power_kw = st.power_kw
        total_kwh = st.energy_kwh

        for mv in meter_value:
            for sv in mv.get("sampled_value", []):
                measurand = sv.get("measurand")
                val_raw = sv.get("value")
                uom = sv.get("unit_of_measure", {}) or {}
                unit = uom.get("unit")
                mult = uom.get("multiplier", 0) or 0

                try:
                    value = float(val_raw) * (10 ** mult)
                except (TypeError, ValueError):
                    continue

                if measurand == "Power.Active.Import":
                    if unit == "W":
                        power_kw = value / 1000.0
                    else:
                        power_kw = value
                elif measurand == "Energy.Active.Import.Register":
                    total_kwh = value

        st.power_kw = power_kw
        if power_kw is not None:
            self._update_power_smoothing(power_kw)

        st.energy_kwh = total_kwh
        if total_kwh is not None:
            self._update_session_energy(total_kwh)

        st.last_meter_value = meter_value
        st.last_update = datetime.now(timezone.utc)
        self._notify()

    def update_transaction_event(
        self,
        event_type: str,
        trigger_reason: str | None,
        transaction_info: dict[str, Any] | None,
        meter_value: list[dict[str, Any]] | None,
    ) -> None:
        """Handle TransactionEvent from charger."""
        st = self.state

        st.session_event_type = event_type
        st.session_trigger_reason = trigger_reason
        st.last_transaction_info = transaction_info

        if meter_value:
            self.update_meter_values(meter_value)

        if transaction_info:
            charging_state = (
                transaction_info.get("charging_state")
                or transaction_info.get("chargingState")
            )
            transaction_id = (
                transaction_info.get("transaction_id")
                or transaction_info.get("transactionId")
            )
            stopped_reason = (
                transaction_info.get("stopped_reason")
                or transaction_info.get("stoppedReason")
            )

            if stopped_reason == "EVDisconnected":
                _LOGGER.info(
                    "EV disconnected (stoppedReason=EVDisconnected). "
                    "Marking unplugged and clearing session."
                )
                st.plugged_in = False
                st.charging = False
                st.transaction_id = None
                st.last_charging_state = "Idle"
                st.remote_stop_requested = False
                st.session_start = None
                st.session_start_meter_kwh = None
                st.last_update = datetime.now(timezone.utc)
                self._notify()
                return

            st.last_charging_state = charging_state
            st.transaction_id = transaction_id

            # Treat EVConnected as "charging session active" for UI
            if charging_state in ("Charging", "EVConnected"):
                if st.remote_stop_requested:
                    _LOGGER.info(
                        "Charger reports %s but remote stop requested; "
                        "keeping charging=False.",
                        charging_state,
                    )
                    st.charging = False
                else:
                    st.charging = True
            elif charging_state in ("Idle", "Finished", "SuspendedEV", "SuspendedEVSE"):
                st.charging = False

        if event_type == "Started":
            st.session_start = datetime.now(timezone.utc)
            st.session_start_meter_kwh = st.energy_kwh
            st.session_energy_kwh = 0.0
            st.remote_stop_requested = False
        elif event_type in ("Ended", "Stopped"):
            st.session_start = None
            st.session_start_meter_kwh = None
            st.remote_stop_requested = False
            st.charging = False

        st.last_update = datetime.now(timezone.utc)
        self._notify()

    @property
    def is_available(self) -> bool:
        return self._cp is not None

    async def async_request_start(self) -> bool:
        if self._cp is None:
            _LOGGER.warning("Cannot start transaction: no charger connected.")
            return False

        request = call.RequestStartTransaction(
            evse_id=self.evse_id,
            id_token={"idToken": self.id_token, "type": "Local"},
            remote_start_id=int(datetime.now().timestamp()),
        )
        _LOGGER.info("Sending RequestStartTransaction: %s", request)
        try:
            response = await self._cp.call(request)
        except Exception as err:  # noqa: BLE001
            _LOGGER.exception("Error sending RequestStartTransaction: %s", err)
            return False

        _LOGGER.info("RequestStartTransaction response: %s", response)
        ok = (
            getattr(response, "status", None)
            == RequestStartStopStatusType.accepted
        )
        if ok:
            self.state.remote_stop_requested = False
            self._notify()
        return ok

    async def async_request_stop(self) -> bool:
        st = self.state
        if self._cp is None or not st.transaction_id:
            _LOGGER.warning(
                "Cannot stop transaction: no active transaction_id or CP."
            )
            return False

        request = call.RequestStopTransaction(transaction_id=st.transaction_id)
        _LOGGER.info("Sending RequestStopTransaction: %s", request)
        try:
            response = await self._cp.call(request)
        except Exception as err:  # noqa: BLE001
            _LOGGER.exception("Error sending RequestStopTransaction: %s", err)
            return False

        _LOGGER.info("RequestStopTransaction response: %s", response)
        ok = (
            getattr(response, "status", None)
            == RequestStartStopStatusType.accepted
        )
        if ok:
            st.remote_stop_requested = True
            st.charging = False
            self._notify()
        return ok

    async def async_request_refresh(self) -> None:
        """Ask the charger to send a fresh StatusNotification via TriggerMessage."""
        if self._cp is None:
            _LOGGER.debug(
                "Refresh request skipped: no charge point connected yet."
            )
            return

        try:
            # NOTE: do NOT pass evse_id here; this ocpp version doesn't accept it
            req = call.TriggerMessage(
                requested_message=MessageTriggerEnumType.status_notification,
            )
            _LOGGER.info(
                "Sending TriggerMessage(StatusNotification) to Elecq for manual refresh: %s",
                req,
            )
            resp = await self._cp.call(req)
            _LOGGER.info(
                "TriggerMessage(StatusNotification) response from Elecq: %s", resp
            )
        except Exception:  # noqa: BLE001
            _LOGGER.exception("Error sending TriggerMessage(StatusNotification)")

    async def async_start_server(self) -> None:
        async def _on_connect(websocket):
            req = getattr(websocket, "request", None)
            if websocket.subprotocol != "ocpp2.0.1":
                _LOGGER.warning(
                    "Client did not negotiate ocpp2.0.1 (got %s) - closing.",
                    websocket.subprotocol,
                )
                await websocket.close()
                return

            path = req.path if req is not None else "/"
            cp_id = path.strip("/") or "unknown"
            _LOGGER.info("Elecq OCPP: new connection id=%s path=%s", cp_id, path)

            cp = ElecqChargePoint(cp_id, websocket, self)
            self._cp = cp

            try:
                await cp.start()
            except ConnectionClosed:
                _LOGGER.info("Elecq OCPP: connection closed for %s", cp_id)
            finally:
                st = self.state
                st.charging = False
                st.plugged_in = False
                st.transaction_id = None
                st.last_charging_state = None
                st.last_status = "Disconnected"
                st.remote_stop_requested = False
                st.last_update = datetime.now(timezone.utc)
                self._notify()

        self._server = await websockets.serve(
            _on_connect,
            host="0.0.0.0",
            port=self.port,
            subprotocols=["ocpp2.0.1"],
        )
        _LOGGER.info(
            "Elecq OCPP 2.0.1 server listening on 0.0.0.0:%s",
            self.port,
        )

    async def async_stop_server(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
            _LOGGER.info("Elecq OCPP server stopped.")
    
    
    async def async_set_charge_rate(self, limit_amps: int) -> bool:
        """Envoie une consigne de délestage via un ChargingProfile OCPP 2.0.1."""
        if self._cp is None:
            _LOGGER.warning("Impossible d'ajuster l'intensité : aucune borne connectée.")
            return False

        # Construction du profil de charge absolu conforme aux specs OCPP 2.0.1
        request = call.SetChargingProfile(
            evse_id=self.evse_id,
            charging_profile={
                "id": 100,  # ID unique arbitraire pour ce profil de délestage
                "stackLevel": 1,  # Priorité supérieure aux profils par défaut
                "chargingProfilePurpose": ChargingProfilePurposeType.tx_profile,
                "chargingProfileKind": "Absolute",
                "chargingSchedule": {
                    "id": 101,
                    "chargingRateUnit": "A",  # Limitation exprimée en Ampères
                    "chargingSchedulePeriod": [
                        {
                            "startPeriod": 0,  # S'applique immédiatement
                            "limit": float(limit_amps)
                        }
                    ]
                }
            }
        )

        _LOGGER.info("Envoi du ChargingProfile (Délestage) -> %d A : %s", limit_amps, request)
        
        try:
            response = await self._cp.call(request)
        except Exception as err:
            _LOGGER.exception("Erreur lors de l'envoi du ChargingProfile : %s", err)
            return False

        _LOGGER.info("Réponse SetChargingProfile reçue : %s", response)
        
        # Vérification si la borne Elecq accepte la consigne
        ok = (
            getattr(response, "status", None)
            == ChargingProfileStatusType.accepted
        )
        
        if ok:
            # On mémorise la nouvelle limite validée et on avertit HA pour mettre à jour l'UI
            self.state.current_limit = float(limit_amps)
            self._notify()
            
        return ok


class ElecqChargePoint(OcppChargePointBase):
    """OCPP 2.0.1 ChargePoint handlers."""

    def __init__(self, cp_id: str, websocket, manager: ElecqOcppManager) -> None:
        super().__init__(cp_id, websocket)
        self._manager = manager

    @on("BootNotification")
    async def on_boot(self, charging_station, reason, **kwargs):
        _LOGGER.info(
            "BootNotification: model=%s, vendor=%s, reason=%s",
            charging_station.get("model"),
            charging_station.get("vendor_name")
            or charging_station.get("vendorName"),
            reason,
        )

        return call_result.BootNotification(
            current_time=datetime.now(timezone.utc).isoformat(),
            interval=60,
            status=RegistrationStatusType.accepted,
        )

    @on("Heartbeat")
    async def on_heartbeat(self, **kwargs):
        return call_result.Heartbeat(
            current_time=datetime.now(timezone.utc).isoformat(),
        )

    @on("StatusNotification")
    async def on_status(
        self,
        timestamp,
        evse_id,
        connector_id,
        connector_status,
        **kwargs,
    ):
        st = self._manager.state
        status_upper = (connector_status or "").upper()
        st.last_status = connector_status

        if status_upper in ("AVAILABLE", "FAULTED"):
            st.plugged_in = False
        else:
            st.plugged_in = True

        if st.last_charging_state in ("Charging", "EVConnected"):
            st.charging = not st.remote_stop_requested
        elif st.last_charging_state in ("Idle", "Finished", "SuspendedEV", "SuspendedEVSE"):
            st.charging = False
        else:
            st.charging = (
                status_upper == "CHARGING"
                and not st.remote_stop_requested
            )

        st.last_update = datetime.now(timezone.utc)
        self._manager._notify()

        return call_result.StatusNotification()

    @on("TransactionEvent")
    async def on_transaction_event(
        self,
        event_type,
        timestamp,
        trigger_reason,
        seq_no,
        transaction_info,
        evse=None,
        id_token=None,
        meter_value=None,
        **kwargs,
    ):
        _LOGGER.debug(
            "TransactionEvent: event_type=%s trigger_reason=%s seq_no=%s "
            "transaction_info=%s meter_value=%s",
            event_type,
            trigger_reason,
            seq_no,
            transaction_info,
            meter_value,
        )

        self._manager.update_transaction_event(
            event_type=event_type,
            trigger_reason=trigger_reason,
            transaction_info=transaction_info,
            meter_value=meter_value,
        )

        return call_result.TransactionEvent()
    

