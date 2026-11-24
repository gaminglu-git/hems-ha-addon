"""Async MQTT-Ingestion & Aktor-Publisher (docs/03 §1).

Shadow-Mode: Bei dry_run=True wird NIEMALS auf {prefix}/cmd/... publiziert,
ausschließlich auf {prefix}/shadow/cmd/.... Diagnose-Topic status/last_decision
wird immer bedient. Credential-Resolution über die Supervisor-API bei leeren
Zugangsdaten (respx-gemockt in Tests).
"""

from __future__ import annotations

import json
import logging
import os
from typing import TYPE_CHECKING, Any

import aiomqtt
import httpx
from pydantic import BaseModel, ConfigDict

from hems.config import AppConfig, OperatingMode
from hems.models import ControlOutputs, DecisionTraceRecord, SystemInputs

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from hems.control.scheduler import ControlScheduler
    from hems.io.market_client import MarketForecast


class MqttCredentials(BaseModel):
    """Aufgelöste MQTT-Zugangsdaten (konfiguriert oder via Supervisor)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    host: str
    port: int
    username: str
    password: str


async def resolve_mqtt_credentials(config: AppConfig) -> MqttCredentials:
    """Löst leere Credentials einmalig über GET http://supervisor/services/mqtt auf."""
    configured = MqttCredentials(
        host=config.mqtt_host,
        port=config.mqtt_port,
        username=config.mqtt_username,
        password=config.mqtt_password,
    )
    token = os.getenv("SUPERVISOR_TOKEN")
    if config.mqtt_username or config.mqtt_password or not token:
        return configured
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(
                "http://supervisor/services/mqtt",
                headers={"Authorization": f"Bearer {token}"},
            )
            response.raise_for_status()
            payload = response.json()
            data = payload.get("data", payload) if isinstance(payload, dict) else {}
            return MqttCredentials(
                host=str(data.get("host", config.mqtt_host)),
                port=int(data.get("port", config.mqtt_port)),
                username=str(data.get("username", "")),
                password=str(data.get("password", "")),
            )
    except (httpx.HTTPError, ValueError, TypeError) as exc:
        logger.error("MQTT credential resolution via Supervisor failed: %s", exc)
        return configured


def subscribed_topics(prefix: str) -> list[str]:
    return [
        f"{prefix}/sensors/state",
        f"{prefix}/control/mode",
        f"{prefix}/control/dry_run",
        f"{prefix}/dso/14a_signal",
    ]


def actuator_topics(prefix: str, dry_run: bool) -> dict[str, str]:
    """Realbetrieb: {prefix}/cmd/... – Shadow-Mode: {prefix}/shadow/cmd/..."""
    base = f"{prefix}/shadow/cmd" if dry_run else f"{prefix}/cmd"
    return {
        "wallbox": f"{base}/wallbox_w",
        "battery": f"{base}/battery_w",
        "heatpump": f"{base}/heatpump_sg_ready",
    }


class LiveState:
    """Interner mutierbarer Zustandscontainer (Ingestion aus MQTT)."""

    def __init__(self, config: AppConfig) -> None:
        self.inputs: SystemInputs | None = None
        self.operating_mode: OperatingMode = config.operating_mode
        self.dry_run: bool = config.dry_run
        self._pending_dso: dict[str, Any] | None = None

    def apply_dso_signal(self, active: bool, limit_w: float) -> None:
        """§14a-Steuerbox-Signal überschreibt sofort den Zustand."""
        self._pending_dso = {"dso_14a_active": active, "dso_14a_limit_w": limit_w}
        if self.inputs is not None:
            self.inputs = self.inputs.model_copy(update=self._pending_dso)

    def set_inputs(self, inputs: SystemInputs) -> None:
        if self._pending_dso is not None:
            inputs = inputs.model_copy(update=self._pending_dso)
        self.inputs = inputs

    def apply_market_forecast(self, forecast: MarketForecast) -> None:
        """Überschreibt tariff_ct_kwh/co2_intensity_g_kwh mit dem ersten Slot.

        Nur gesetzt, wenn ``inputs`` bereits vorhanden ist; ein leerer Vektor
        (Offline-Fallback) lässt den MQTT-Wert unverändert (docs/09 §6.1).
        """
        if self.inputs is None or not forecast.slots:
            return
        first = forecast.slots[0]
        update: dict[str, Any] = {"tariff_ct_kwh": first.gross_ct_kwh}
        if first.co2_intensity_g_kwh is not None:
            update["co2_intensity_g_kwh"] = first.co2_intensity_g_kwh
        self.inputs = self.inputs.model_copy(update=update)

    @property
    def tariff_ct_kwh(self) -> float | None:
        """Aktueller tariff_ct_kwh aus dem inputs-Snapshot (read-only)."""
        return self.inputs.tariff_ct_kwh if self.inputs is not None else None

    @property
    def co2_intensity_g_kwh(self) -> float | None:
        """Aktuelle co2_intensity_g_kwh aus dem inputs-Snapshot (read-only)."""
        return self.inputs.co2_intensity_g_kwh if self.inputs is not None else None


def handle_message(
    topic: str,
    payload: bytes,
    state: LiveState,
    config: AppConfig,
    scheduler: ControlScheduler | None = None,
) -> None:
    """Verarbeitet eine MQTT-Nachricht; Fehler werden strukturiert geloggt."""
    prefix = config.mqtt_topic_prefix
    try:
        if topic == f"{prefix}/sensors/state":
            state.set_inputs(SystemInputs.model_validate_json(payload))
        elif topic == f"{prefix}/control/mode":
            mode = OperatingMode(payload.decode("utf-8").strip())
            if scheduler is not None:
                scheduler.set_operating_mode(mode)
            else:
                state.operating_mode = mode
        elif topic == f"{prefix}/control/dry_run":
            value = payload.decode("utf-8").strip().lower()
            if value not in ("true", "false"):
                raise ValueError(f"invalid dry_run payload: {value!r}")
            if scheduler is not None:
                scheduler.set_dry_run(value == "true")
            else:
                state.dry_run = value == "true"
        elif topic == f"{prefix}/dso/14a_signal":
            signal = json.loads(payload)
            state.apply_dso_signal(active=bool(signal["active"]), limit_w=float(signal["limit_w"]))
        else:
            logger.warning("Ignoring unknown MQTT topic: %s", topic)
    except (ValueError, KeyError, TypeError) as exc:
        logger.error("MQTT message rejected on topic %s: %s", topic, exc)


class ActuatorPublisher:
    """Publiziert Aktor-Sollwerte strikt gemäß Shadow-Mode-Regel."""

    def __init__(self, prefix: str) -> None:
        self._prefix = prefix

    async def publish_outputs(
        self, client: aiomqtt.Client, outputs: ControlOutputs, dry_run: bool
    ) -> None:
        topics = actuator_topics(self._prefix, dry_run)
        await client.publish(topics["wallbox"], str(outputs.cmd_wallbox_w))
        await client.publish(topics["battery"], str(outputs.cmd_battery_w))
        await client.publish(topics["heatpump"], outputs.cmd_heatpump_sg_ready.value)

    async def publish_status(self, client: aiomqtt.Client, record: DecisionTraceRecord) -> None:
        await client.publish(f"{self._prefix}/status/last_decision", record.model_dump_json())


async def run_ingestion(
    client: aiomqtt.Client,
    state: LiveState,
    config: AppConfig,
    scheduler: ControlScheduler | None = None,
) -> None:
    """Subscribed alle Eingangs-Topics und verarbeitet Nachrichten endlos."""
    for topic in subscribed_topics(config.mqtt_topic_prefix):
        await client.subscribe(topic)
    async for message in client.messages:
        payload = message.payload if isinstance(message.payload, bytes) else b""
        handle_message(str(message.topic), payload, state, config, scheduler)
