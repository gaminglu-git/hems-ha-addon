"""Universelle Consumer- & Device-Registry inkl. Aktor-Dispatcher (docs/06, Phase 5).

Persistenz liegt unter ``{data_dir}/config/devices.json`` und wird atomar über
eine Temp-Datei im selben Zielverzeichnis + ``os.replace`` geschrieben (begründete
/tmp-Ausnahme, siehe AGENTS.md §3.3). Alle Operationen laufen nicht-blockierend.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from collections.abc import Awaitable, Callable
from enum import Enum
from pathlib import Path
from typing import Any

import aiomqtt
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from hems.config import AppConfig
from hems.io.ha_core_client import HaCoreClient, HaEntityState
from hems.models import ControlOutputs, SystemInputs

logger = logging.getLogger(__name__)

# Verbindliche WashData-Zustände aus docs/07 §2.1.
_WASHDATA_STATES = frozenset(
    {
        "off",
        "idle",
        "starting",
        "running",
        "paused",
        "user_paused",
        "ending",
        "finished",
        "anti_wrinkle",
        "interrupted",
        "force_stopped",
        "clean",
        "delay_wait",
    }
)


class DeviceCategory(str, Enum):
    """Kategorie eines eingebundenen Verbrauchers."""

    WALLBOX = "wallbox"
    BATTERY = "battery"
    HEATPUMP = "heatpump"
    DEFERRABLE_LOAD = "deferrable_load"
    APPLIANCE_CYCLE = "appliance_cycle"
    MONITORED_LOAD = "monitored_load"


class ControlModeType(str, Enum):
    """Regelungsart eines steuerbaren Geräts."""

    NONE = "none"
    CONTINUOUS_WATT = "continuous_watt"
    BINARY_SWITCH = "binary_switch"
    SG_READY = "sg_ready"


class ControlTransport(str, Enum):
    """Transportweg für Aktor-Kommandos."""

    NONE = "none"
    MQTT = "mqtt"
    HA_SERVICE = "ha_service"


class IntegrationSource(str, Enum):
    """Herkunft des Registry-Eintrags."""

    MANUAL = "manual"
    WASHDATA = "washdata"
    HA_POWER_SENSOR = "ha_power_sensor"
    HA_PATTERN = "ha_pattern"


class HaServiceBinding(BaseModel):
    """Bindung an einen Home-Assistant-Service (nur bei HA_SERVICE)."""

    model_config = ConfigDict(extra="forbid")

    domain: str
    entity_id: str
    service_on_or_set: str
    service_off: str | None = None
    value_field: str = "value"


class ControllableDevice(BaseModel):
    """Steuerbares Gerät oder rein überwachter Verbraucher."""

    model_config = ConfigDict(extra="forbid")

    device_id: str = Field(min_length=1, max_length=64, pattern=r"^[a-z0-9_-]+$")
    name: str = Field(min_length=1, max_length=100)
    category: DeviceCategory
    is_controllable: bool = True
    control_mode: ControlModeType = ControlModeType.CONTINUOUS_WATT
    enabled: bool = True
    is_steuve_14a: bool = True
    priority_rank: int = Field(default=50, ge=1, le=100)
    min_power_w: float = Field(default=0.0, ge=0.0)
    max_power_w: float = Field(gt=0.0)
    nominal_power_w: float = Field(gt=0.0)
    power_sensor_entity_id: str | None = None
    state_entity_id: str | None = None
    soc_sensor_entity_id: str | None = None
    washdata_prefix: str | None = None
    source_integration: IntegrationSource = IntegrationSource.MANUAL
    transport: ControlTransport = ControlTransport.NONE
    mqtt_command_topic: str | None = None
    ha_binding: HaServiceBinding | None = None

    @model_validator(mode="after")
    def _validate_control_binding(self) -> ControllableDevice:
        if not self.is_controllable:
            # Nicht steuerbare Verbraucher können nicht gedimmt werden.
            self.is_steuve_14a = False
            return self
        if self.transport is ControlTransport.NONE:
            raise ValueError("controllable devices require a transport other than 'none'")
        if (
            self.transport is ControlTransport.MQTT
            and not (self.mqtt_command_topic or "").strip()
        ):
            raise ValueError("MQTT transport requires a non-empty mqtt_command_topic")
        if self.transport is ControlTransport.HA_SERVICE and self.ha_binding is None:
            raise ValueError("HA_SERVICE transport requires a ha_binding")
        if self.min_power_w > self.max_power_w:
            raise ValueError("min_power_w must be <= max_power_w")
        if self.nominal_power_w > self.max_power_w:
            raise ValueError("nominal_power_w must be <= max_power_w")
        return self


def _load_devices(path: Path) -> dict[str, ControllableDevice] | None:
    """Liest die Datei; None bedeutet 'nicht vorhanden' -> Default-Seeding."""
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.error("Could not read device registry %s: %s", path, exc)
        return None
    if not isinstance(payload, list):
        logger.error("Device registry %s is not a JSON list", path)
        return None
    devices: dict[str, ControllableDevice] = {}
    for item in payload:
        try:
            device = ControllableDevice.model_validate(item)
        except ValidationError as exc:
            logger.error("Skipping invalid registry entry in %s: %s", path, exc)
            continue
        devices[device.device_id] = device
    return devices


def _write_devices_atomic(path: Path, payload: list[dict[str, Any]]) -> None:
    """Atomares Schreiben: Temp-Datei im Zielverzeichnis + os.replace."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f".{path.name}.tmp")
    tmp_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp_path, path)


def _default_devices(config: AppConfig) -> dict[str, ControllableDevice]:
    """Seedet die drei steuerbaren Standardgeräte aus der AppConfig."""
    prefix = config.mqtt_topic_prefix
    devices = [
        ControllableDevice(
            device_id="default_wallbox",
            name="Wallbox",
            category=DeviceCategory.WALLBOX,
            control_mode=ControlModeType.CONTINUOUS_WATT,
            transport=ControlTransport.MQTT,
            mqtt_command_topic=f"{prefix}/cmd/wallbox_w",
            min_power_w=config.wallbox_min_power_w,
            max_power_w=config.wallbox_max_power_w,
            nominal_power_w=config.wallbox_max_power_w,
        ),
        ControllableDevice(
            device_id="default_battery",
            name="Batteriespeicher",
            category=DeviceCategory.BATTERY,
            control_mode=ControlModeType.CONTINUOUS_WATT,
            transport=ControlTransport.MQTT,
            mqtt_command_topic=f"{prefix}/cmd/battery_w",
            min_power_w=0.0,
            max_power_w=max(config.battery_max_charge_w, config.battery_max_discharge_w),
            nominal_power_w=config.battery_max_charge_w,
        ),
        ControllableDevice(
            device_id="default_heatpump",
            name="Wärmepumpe",
            category=DeviceCategory.HEATPUMP,
            control_mode=ControlModeType.SG_READY,
            transport=ControlTransport.MQTT,
            mqtt_command_topic=f"{prefix}/cmd/heatpump_sg_ready",
            min_power_w=0.0,
            max_power_w=config.heatpump_nominal_power_w,
            nominal_power_w=config.heatpump_nominal_power_w,
        ),
    ]
    return {device.device_id: device for device in devices}


class DeviceRegistry:
    """Async-sichere Registry für steuerbare Geräte und überwachte Verbraucher."""

    def __init__(self, config: AppConfig) -> None:
        self._config = config
        self._path = config.data_dir / "config" / "devices.json"
        self._lock = asyncio.Lock()
        self._devices: dict[str, ControllableDevice] = {}
        self._loaded = False
        self._change_callback: Callable[[], Awaitable[None]] | None = None

    def set_change_callback(self, callback: Callable[[], Awaitable[None]] | None) -> None:
        """Registriert den ForecastManager-Refresh nach Registry-Änderungen."""
        self._change_callback = callback

    async def _notify_changed(self) -> None:
        if self._change_callback is not None:
            await self._change_callback()

    @property
    def path(self) -> Path:
        return self._path

    async def initialize(self) -> None:
        """Lädt die Registry bzw. seedet die Standardgeräte beim Erststart."""
        async with self._lock:
            await self._ensure_loaded_locked()

    async def _ensure_loaded_locked(self) -> None:
        if self._loaded:
            return
        devices = await asyncio.to_thread(_load_devices, self._path)
        seeded = devices is None
        if seeded:
            devices = _default_devices(self._config)
        self._devices = devices
        if seeded:
            await self._persist_locked()
        self._loaded = True

    async def _persist_locked(self) -> None:
        payload = [device.model_dump(mode="json") for device in self._devices.values()]
        await asyncio.to_thread(_write_devices_atomic, self._path, payload)

    async def list_devices(self) -> list[ControllableDevice]:
        async with self._lock:
            await self._ensure_loaded_locked()
            return list(self._devices.values())

    async def get_device(self, device_id: str) -> ControllableDevice | None:
        async with self._lock:
            await self._ensure_loaded_locked()
            return self._devices.get(device_id)

    async def upsert_device(self, device: ControllableDevice) -> ControllableDevice:
        async with self._lock:
            await self._ensure_loaded_locked()
            self._devices[device.device_id] = device
            await self._persist_locked()
        await self._notify_changed()
        return device

    async def delete_device(self, device_id: str) -> bool:
        async with self._lock:
            await self._ensure_loaded_locked()
            if device_id not in self._devices:
                return False
            del self._devices[device_id]
            await self._persist_locked()
        await self._notify_changed()
        return True

    async def set_enabled(self, device_id: str, enabled: bool) -> ControllableDevice:
        async with self._lock:
            await self._ensure_loaded_locked()
            device = self._devices.get(device_id)
            if device is None:
                raise KeyError(device_id)
            updated = device.model_copy(update={"enabled": enabled})
            self._devices[device_id] = updated
            await self._persist_locked()
        await self._notify_changed()
        return updated

    async def sync_washdata_devices(
        self, states: list[HaEntityState]
    ) -> list[ControllableDevice]:
        """Importiert neu erkannte WashData-Geräte ohne bestehende Einträge zu ändern."""
        if not self._config.auto_discover_washdata:
            return []
        by_id = {state.entity_id: state for state in states}
        slugs: dict[str, str] = {}
        for state in states:
            entity_id = state.entity_id
            if not entity_id.startswith("sensor.") or not entity_id.endswith("_state"):
                continue
            if state.state.lower() not in _WASHDATA_STATES:
                continue
            slug = entity_id.removeprefix("sensor.").removesuffix("_state")
            if not slug:
                continue
            companions = (
                f"sensor.{slug}_time_remaining",
                f"binary_sensor.{slug}_running",
                f"sensor.{slug}_progress",
                f"sensor.{slug}_program",
            )
            if any(companion in by_id for companion in companions):
                slugs.setdefault(slug, state.friendly_name)

        async with self._lock:
            await self._ensure_loaded_locked()
            added: list[ControllableDevice] = []
            for slug, friendly_name in slugs.items():
                device_id = f"washdata_{slug}"
                if device_id in self._devices:
                    continue
                power_sensor = f"sensor.{slug}_power"
                try:
                    device = ControllableDevice(
                        device_id=device_id,
                        name=friendly_name or slug,
                        category=DeviceCategory.APPLIANCE_CYCLE,
                        is_controllable=False,
                        control_mode=ControlModeType.NONE,
                        transport=ControlTransport.NONE,
                        is_steuve_14a=False,
                        washdata_prefix=slug,
                        state_entity_id=f"sensor.{slug}_state",
                        power_sensor_entity_id=(
                            power_sensor if power_sensor in by_id else None
                        ),
                        source_integration=IntegrationSource.WASHDATA,
                        min_power_w=0.0,
                        max_power_w=800.0,
                        nominal_power_w=800.0,
                    )
                except ValidationError as exc:
                    logger.error("Rejecting invalid WashData slug %r: %s", slug, exc)
                    continue
                self._devices[device_id] = device
                added.append(device)
            if added:
                await self._persist_locked()
        if added:
            await self._notify_changed()
        return added


def apply_registry_load_shedding(
    outputs: ControlOutputs,
    inputs: SystemInputs,
    config: AppConfig,
    devices: list[ControllableDevice],
) -> tuple[dict[str, float], list[str]]:
    """Separat von evaluate_cascade: wirft SteuVE-Zusatzlasten nach Rang ab.

    Rückgabe: (Sollwerte je device_id, angewandte Regeln). ``evaluate_cascade``
    bleibt dadurch in Signatur und Verhalten exakt unverändert.
    """
    eligible = [
        device
        for device in devices
        if device.enabled
        and device.is_controllable
        and device.is_steuve_14a
        and device.category is DeviceCategory.DEFERRABLE_LOAD
    ]
    setpoints = {device.device_id: device.nominal_power_w for device in eligible}
    if not eligible or not inputs.dso_14a_active:
        return setpoints, []

    limit_14a = max(inputs.dso_14a_limit_w, config.dso_14a_min_power_w)
    pv_offset = max(0.0, inputs.p_pv_w - inputs.p_house_base_w)

    def grid_import_w() -> float:
        return max(
            0.0,
            outputs.cmd_wallbox_w
            + outputs.cmd_heatpump_w_est
            + max(0.0, outputs.cmd_battery_w)
            + sum(setpoints.values())
            - pv_offset,
        )

    rules: list[str] = []
    if grid_import_w() <= limit_14a + 1e-6:
        return setpoints, rules
    # Abwurf absteigend nach Rang: niedrigster Rang zuerst.
    for device in sorted(eligible, key=lambda item: item.priority_rank):
        excess = grid_import_w() - limit_14a
        if excess <= 1e-6:
            break
        current = setpoints[device.device_id]
        if current <= 0.0:
            continue
        setpoints[device.device_id] = max(0.0, current - excess)
        rules.append(f"P1_registry_load_shed_{device.device_id}")
    return setpoints, rules


class DeviceCommandDispatcher:
    """Dispatcht Registry-Sollwerte strikt nach Shadow-Mode und Transport."""

    def __init__(
        self,
        config: AppConfig,
        registry: DeviceRegistry,
        mqtt_client: aiomqtt.Client | None = None,
        ha_client: HaCoreClient | None = None,
    ) -> None:
        self._config = config
        self._registry = registry
        self._mqtt = mqtt_client
        self._ha_client = ha_client

    def attach_mqtt(self, client: aiomqtt.Client) -> None:
        self._mqtt = client

    def attach_ha_client(self, client: HaCoreClient) -> None:
        self._ha_client = client

    async def dispatch_cascade(
        self,
        outputs: ControlOutputs,
        inputs: SystemInputs,
        config: AppConfig,
        dry_run: bool | None = None,
    ) -> None:
        """§ 14a-Nachbearbeitung + Dispatch (wird vom Scheduler aufgerufen)."""
        devices = await self._registry.list_devices()
        setpoints, rules = apply_registry_load_shedding(outputs, inputs, config, devices)
        if rules:
            logger.info("Registry load shedding applied: %s", rules)
        await self.dispatch_outputs(outputs, setpoints, dry_run=dry_run)

    async def dispatch_outputs(
        self,
        outputs: ControlOutputs,
        load_setpoints_w: dict[str, float] | None = None,
        dry_run: bool | None = None,
    ) -> None:
        """Dispatchet die Kaskadenausgaben plus Registry-Zusatzlasten."""
        merged: dict[str, float] = {
            "default_wallbox": outputs.cmd_wallbox_w,
            "default_battery": outputs.cmd_battery_w,
            "default_heatpump": outputs.cmd_heatpump_w_est,
        }
        if load_setpoints_w:
            merged.update(load_setpoints_w)
        for device in await self._registry.list_devices():
            if not device.enabled or not device.is_controllable:
                continue
            target = merged.get(device.device_id)
            if target is None:
                continue
            await self.dispatch_device(device, target, dry_run=dry_run)

    async def dispatch_device(
        self, device: ControllableDevice, target_w: float, dry_run: bool | None = None
    ) -> None:
        """Schaltet genau ein Gerät; nicht-steuerbare Geräte werden ignoriert."""
        if not device.enabled or not device.is_controllable:
            return
        dry = self._config.dry_run if dry_run is None else dry_run
        value = max(0.0, float(target_w))
        if device.control_mode is ControlModeType.BINARY_SWITCH:
            value = device.nominal_power_w if value > 0.0 else 0.0

        if device.transport is ControlTransport.MQTT:
            base = f"{self._config.mqtt_topic_prefix}/shadow/cmd" if dry else (
                f"{self._config.mqtt_topic_prefix}/cmd"
            )
            topic = f"{base}/{device.device_id}"
            if self._mqtt is None:
                logger.warning("No MQTT client attached; skipping dispatch to %s", topic)
                return
            await self._mqtt.publish(topic, str(value))
            return

        if device.transport is ControlTransport.HA_SERVICE:
            binding = device.ha_binding
            if binding is None:  # durch den Validator bereits ausgeschlossen
                logger.error("HA_SERVICE device %s has no binding", device.device_id)
                return
            is_off = value <= 0.0 and binding.service_off is not None
            service = binding.service_off if is_off else binding.service_on_or_set
            data: dict[str, Any] = {"entity_id": binding.entity_id}
            if not is_off:
                data[binding.value_field] = value
            if dry:
                logger.info(
                    "Shadow HA service call (dry_run): %s.%s %s (device=%s)",
                    binding.domain,
                    service,
                    data,
                    device.device_id,
                )
                return
            if self._ha_client is None:
                logger.error("No HA client attached; skipping HA call for %s", device.device_id)
                return
            await self._ha_client.call_service(binding.domain, service, data)
