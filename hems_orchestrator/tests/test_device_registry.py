"""Phase-5-Gate: HA Core API, Device Registry & Dispatcher (docs/06)."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast
from urllib.parse import quote

import aiomqtt
import httpx
import pytest
import respx
from pydantic import ValidationError

from hems.config import AppConfig
from hems.control.device_registry import (
    ControllableDevice,
    ControlModeType,
    ControlTransport,
    DeviceCategory,
    DeviceCommandDispatcher,
    DeviceRegistry,
    HaServiceBinding,
    IntegrationSource,
    apply_registry_load_shedding,
)
from hems.io.ha_core_client import HaCoreClient, HaEntityState
from hems.models import ControlOutputs, HeatpumpSGReadyMode, SystemInputs

BASE_URL = "http://supervisor/core/api"


class _FakeMqtt:
    def __init__(self) -> None:
        self.published: list[tuple[str, str]] = []

    async def publish(self, topic: str, payload: str) -> None:
        self.published.append((topic, payload))


class _FakeHaClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    async def call_service(
        self, domain: str, service: str, service_data: dict[str, Any]
    ) -> bool:
        self.calls.append((domain, service, service_data))
        return True


@pytest.fixture
def config(tmp_path: Path) -> AppConfig:
    return AppConfig(
        data_dir=tmp_path / "data",
        tmp_dir=tmp_path / "tmp",
        dry_run=True,
        mqtt_topic_prefix="hems",
    )


@pytest.fixture
def supervisor_token(monkeypatch: pytest.MonkeyPatch) -> str:
    monkeypatch.setenv("SUPERVISOR_TOKEN", "test-supervisor-token")
    return "test-supervisor-token"


def _ha_state(
    entity_id: str, state: str, friendly_name: str, attributes: dict[str, Any] | None = None
) -> HaEntityState:
    return HaEntityState(
        entity_id=entity_id,
        state=state,
        last_changed="2024-01-01T10:00:00.000Z",
        friendly_name=friendly_name,
        domain=entity_id.split(".", 1)[0],
        attributes=attributes or {},
    )


def _inputs(dso_active: bool = True, pv_w: float = 0.0) -> SystemInputs:
    return SystemInputs(
        p_grid_before_w=0.0,
        p_pv_w=pv_w,
        p_house_base_w=0.0,
        soc_battery_pct=50.0,
        soc_ev_pct=50.0,
        ev_connected=False,
        t_buffer_c=50.0,
        tariff_ct_kwh=25.0,
        co2_intensity_g_kwh=300.0,
        dso_14a_active=dso_active,
        dso_14a_limit_w=4200.0,
        eeg_feedin_limit_pct=60.0,
    )


def _outputs() -> ControlOutputs:
    return ControlOutputs(
        cmd_wallbox_w=2100.0,
        cmd_battery_w=0.0,
        cmd_heatpump_sg_ready=HeatpumpSGReadyMode.NORMAL,
        cmd_heatpump_w_est=0.0,
    )


# --- A1: /states ----------------------------------------------------------------


@respx.mock
async def test_get_states_parses_attributes_and_sends_token(
    config: AppConfig, supervisor_token: str
) -> None:
    route = respx.get(f"{BASE_URL}/states").mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "entity_id": "sensor.waschmaschine_progress",
                    "state": "50",
                    "attributes": {
                        "friendly_name": "Waschmaschine Fortschritt",
                        "device_class": "battery",
                        "unit_of_measurement": "%",
                        "projected_energy_kwh": 1.6,
                        "reference_profile": [100.0, 200.0],
                    },
                    "last_changed": "2024-01-01T10:00:00.000Z",
                    "last_updated": "2024-01-01T10:00:00.000Z",
                    "context": {"id": "must-be-dropped"},
                },
                {"unexpected": "entry"},
            ],
        )
    )
    states = await HaCoreClient(config).get_states()

    assert len(states) == 1
    state = states[0]
    assert state.entity_id == "sensor.waschmaschine_progress"
    assert state.domain == "sensor"
    assert state.friendly_name == "Waschmaschine Fortschritt"
    assert state.device_class == "battery"
    assert state.unit_of_measurement == "%"
    assert state.attributes["projected_energy_kwh"] == 1.6
    assert state.attributes["reference_profile"] == [100.0, 200.0]
    assert route.calls.last.request.headers["authorization"] == f"Bearer {supervisor_token}"


# --- A2: /history/period/... ----------------------------------------------------


@respx.mock
async def test_get_history_params_and_ascending_sorting(
    config: AppConfig, supervisor_token: str
) -> None:
    start = datetime(2024, 1, 1, tzinfo=UTC)
    end = start + timedelta(hours=1)
    encoded_start = quote(start.isoformat(), safe="")
    route = respx.get(f"{BASE_URL}/history/period/{encoded_start}").mock(
        return_value=httpx.Response(
            200,
            json=[
                [
                    {
                        "entity_id": "sensor.x",
                        "state": "off",
                        "last_changed": "2024-01-01T00:20:00+00:00",
                    },
                    {
                        "entity_id": "sensor.x",
                        "state": "on",
                        "last_changed": "2024-01-01T00:05:00+00:00",
                    },
                ],
                [
                    {
                        "entity_id": "sensor.y",
                        "state": "on",
                        "last_changed": "2024-01-01T00:10:00+00:00",
                    }
                ],
            ],
        )
    )
    result = await HaCoreClient(config).get_history(["sensor.x", "sensor.y"], start, end)

    assert [point.state for point in result["sensor.x"]] == ["on", "off"]
    assert [point.state for point in result["sensor.y"]] == ["on"]
    params = route.calls.last.request.url.params
    assert params["filter_entity_id"] == "sensor.x,sensor.y"
    assert params["minimal_response"] == "true"
    assert params["no_attributes"] == "true"
    assert params["end_time"] == end.isoformat()
    assert route.calls.last.request.headers["authorization"] == f"Bearer {supervisor_token}"


# --- A3: /services/... ----------------------------------------------------------


@respx.mock
async def test_call_service_returns_true_on_200_and_false_on_500(
    config: AppConfig, supervisor_token: str
) -> None:
    ok_route = respx.post(f"{BASE_URL}/services/switch/turn_on").mock(
        return_value=httpx.Response(200, json={})
    )
    respx.post(f"{BASE_URL}/services/switch/turn_off").mock(
        return_value=httpx.Response(500, json={})
    )
    client = HaCoreClient(config)

    assert await client.call_service("switch", "turn_on", {"entity_id": "switch.x"}) is True
    assert await client.call_service("switch", "turn_off", {"entity_id": "switch.x"}) is False
    assert json.loads(ok_route.calls.last.request.content) == {"entity_id": "switch.x"}
    assert ok_route.calls.last.request.headers["authorization"] == f"Bearer {supervisor_token}"


async def test_missing_token_warns_once_and_returns_safe_defaults(
    config: AppConfig, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
    config.ha_token = ""
    client = HaCoreClient(config)

    with caplog.at_level(logging.WARNING):
        assert await client.get_states() == []
        assert await client.get_history(["sensor.x"], datetime.now(UTC)) == {}
        assert await client.call_service("switch", "turn_on", {}) is False
        assert await client.get_states() == []

    warnings = [record for record in caplog.records if "No Home Assistant token" in record.message]
    assert len(warnings) == 1


# --- A4: Default-Seeding --------------------------------------------------------


async def test_registry_seeds_defaults_and_reloads(config: AppConfig) -> None:
    registry = DeviceRegistry(config)
    await registry.initialize()
    devices = await registry.list_devices()

    assert {device.device_id for device in devices} == {
        "default_wallbox",
        "default_battery",
        "default_heatpump",
    }
    assert all(device.is_controllable and device.enabled for device in devices)
    assert all(device.is_steuve_14a for device in devices)
    assert all(device.transport is ControlTransport.MQTT for device in devices)
    assert registry.path.exists()
    assert len(json.loads(registry.path.read_text(encoding="utf-8"))) == 3
    assert not registry.path.with_name(f".{registry.path.name}.tmp").exists()

    reloaded = DeviceRegistry(config)
    assert len(await reloaded.list_devices()) == 3


# --- A5: CRUD & Validierung -----------------------------------------------------


async def test_registry_crud_for_controllable_and_monitored(config: AppConfig) -> None:
    registry = DeviceRegistry(config)
    await registry.initialize()

    boiler = ControllableDevice(
        device_id="boiler",
        name="Boiler",
        category=DeviceCategory.DEFERRABLE_LOAD,
        transport=ControlTransport.HA_SERVICE,
        ha_binding=HaServiceBinding(
            domain="switch",
            entity_id="switch.boiler",
            service_on_or_set="turn_on",
            service_off="turn_off",
        ),
        min_power_w=0.0,
        max_power_w=2000.0,
        nominal_power_w=2000.0,
    )
    assert (await registry.upsert_device(boiler)).device_id == "boiler"

    fridge = ControllableDevice(
        device_id="fridge",
        name="Kühlschrank",
        category=DeviceCategory.MONITORED_LOAD,
        is_controllable=False,
        control_mode=ControlModeType.NONE,
        is_steuve_14a=True,
        transport=ControlTransport.NONE,
        power_sensor_entity_id="sensor.fridge_power",
        min_power_w=0.0,
        max_power_w=300.0,
        nominal_power_w=150.0,
    )
    await registry.upsert_device(fridge)

    stored_fridge = await registry.get_device("fridge")
    assert stored_fridge is not None
    assert stored_fridge.is_steuve_14a is False
    assert len(await registry.list_devices()) == 5

    updated = await registry.set_enabled("boiler", False)
    assert updated.enabled is False
    assert (await registry.get_device("boiler")).enabled is False  # type: ignore[union-attr]
    assert await registry.delete_device("boiler") is True
    assert await registry.delete_device("boiler") is False
    assert await registry.get_device("boiler") is None


@pytest.mark.parametrize(
    "overrides",
    [
        {"transport": ControlTransport.NONE},
        {"transport": ControlTransport.MQTT, "mqtt_command_topic": ""},
        {"transport": ControlTransport.HA_SERVICE},
        {
            "transport": ControlTransport.MQTT,
            "mqtt_command_topic": "hems/cmd/x",
            "min_power_w": 500.0,
            "max_power_w": 100.0,
        },
        {
            "transport": ControlTransport.MQTT,
            "mqtt_command_topic": "hems/cmd/x",
            "nominal_power_w": 900.0,
            "max_power_w": 100.0,
        },
        {"device_id": "Ungültige ID!"},
    ],
)
def test_controllable_device_validation_rules(overrides: dict[str, Any]) -> None:
    payload: dict[str, Any] = {
        "device_id": "test_device",
        "name": "Testgerät",
        "category": DeviceCategory.DEFERRABLE_LOAD,
        "transport": ControlTransport.MQTT,
        "mqtt_command_topic": "hems/cmd/test_device",
        "min_power_w": 0.0,
        "max_power_w": 1000.0,
        "nominal_power_w": 500.0,
    }
    payload.update(overrides)
    with pytest.raises(ValidationError):
        ControllableDevice.model_validate(payload)


# --- A6: WashData-Auto-Sync -----------------------------------------------------


async def test_sync_washdata_devices_detects_without_overwriting(config: AppConfig) -> None:
    states = [
        _ha_state("sensor.waschmaschine_state", "running", "Waschmaschine"),
        _ha_state("sensor.waschmaschine_time_remaining", "45", "Restzeit"),
        _ha_state("binary_sensor.trockner_running", "on", "Trockner läuft"),
        _ha_state("sensor.trockner_state", "running", "Trockner"),
        _ha_state("light.wohnzimmer", "on", "Wohnzimmer"),
    ]
    registry = DeviceRegistry(config)
    await registry.initialize()

    added = await registry.sync_washdata_devices(states)
    ids = {device.device_id for device in added}
    assert ids == {"washdata_waschmaschine", "washdata_trockner"}

    washer = await registry.get_device("washdata_waschmaschine")
    assert washer is not None
    assert washer.source_integration is IntegrationSource.WASHDATA
    assert washer.category is DeviceCategory.APPLIANCE_CYCLE
    assert washer.is_controllable is False
    assert washer.is_steuve_14a is False
    assert washer.control_mode is ControlModeType.NONE
    assert washer.washdata_prefix == "waschmaschine"
    assert washer.nominal_power_w == 800.0

    assert await registry.sync_washdata_devices(states) == []
    custom = washer.model_copy(update={"name": "Meine Waschmaschine", "nominal_power_w": 950.0})
    await registry.upsert_device(custom)
    await registry.sync_washdata_devices(states)
    stored = await registry.get_device("washdata_waschmaschine")
    assert stored is not None
    assert stored.name == "Meine Waschmaschine"
    assert stored.nominal_power_w == 950.0


async def test_sync_washdata_devices_respects_auto_discover_flag(config: AppConfig) -> None:
    config.auto_discover_washdata = False
    registry = DeviceRegistry(config)
    await registry.initialize()
    states = [
        _ha_state("sensor.waschmaschine_state", "running", "Waschmaschine"),
        _ha_state("sensor.waschmaschine_time_remaining", "45", "Restzeit"),
    ]
    assert await registry.sync_washdata_devices(states) == []
    assert await registry.get_device("washdata_waschmaschine") is None


# --- A7: DEFERRABLE_LOAD im § 14a-Limit -----------------------------------------


def test_deferrable_loads_are_shed_by_priority_rank(config: AppConfig) -> None:
    boiler = ControllableDevice(
        device_id="boiler",
        name="Boiler",
        category=DeviceCategory.DEFERRABLE_LOAD,
        transport=ControlTransport.MQTT,
        mqtt_command_topic="hems/cmd/boiler",
        priority_rank=10,
        max_power_w=2000.0,
        nominal_power_w=2000.0,
    )
    pool = ControllableDevice(
        device_id="pool",
        name="Poolpumpe",
        category=DeviceCategory.DEFERRABLE_LOAD,
        transport=ControlTransport.MQTT,
        mqtt_command_topic="hems/cmd/pool",
        priority_rank=90,
        max_power_w=1000.0,
        nominal_power_w=1000.0,
    )
    setpoints, rules = apply_registry_load_shedding(
        _outputs(), _inputs(), config, [boiler, pool]
    )

    # 2100 (Wallbox) + 2000 + 1000 = 5100 W > 4200 W -> 900 W müssen weg.
    assert setpoints["boiler"] == pytest.approx(1100.0)
    assert setpoints["pool"] == pytest.approx(1000.0)
    assert rules == ["P1_registry_load_shed_boiler"]
    assert 2100.0 + setpoints["boiler"] + setpoints["pool"] == pytest.approx(4200.0)


def test_deferrable_loads_untouched_without_14a_or_with_pv_surplus(
    config: AppConfig,
) -> None:
    device = ControllableDevice(
        device_id="boiler",
        name="Boiler",
        category=DeviceCategory.DEFERRABLE_LOAD,
        transport=ControlTransport.MQTT,
        mqtt_command_topic="hems/cmd/boiler",
        max_power_w=2000.0,
        nominal_power_w=2000.0,
    )
    inactive, rules = apply_registry_load_shedding(
        _outputs(), _inputs(dso_active=False), config, [device]
    )
    assert inactive == {"boiler": 2000.0}
    assert rules == []

    with_surplus, rules = apply_registry_load_shedding(
        _outputs(), _inputs(pv_w=1000.0), config, [device]
    )
    assert with_surplus == {"boiler": 2000.0}
    assert rules == []


# --- A8/A9: Dispatcher & Shadow-Mode --------------------------------------------


async def test_monitored_devices_never_trigger_commands(config: AppConfig) -> None:
    registry = DeviceRegistry(config)
    await registry.initialize()
    mqtt = _FakeMqtt()
    ha = _FakeHaClient()
    dispatcher = DeviceCommandDispatcher(
        config,
        registry,
        mqtt_client=cast(aiomqtt.Client, mqtt),
        ha_client=cast(HaCoreClient, ha),
    )
    monitored = ControllableDevice(
        device_id="dryer",
        name="Trockner",
        category=DeviceCategory.MONITORED_LOAD,
        is_controllable=False,
        control_mode=ControlModeType.NONE,
        power_sensor_entity_id="sensor.dryer_power",
        max_power_w=1000.0,
        nominal_power_w=1000.0,
    )
    monitored_ha = ControllableDevice(
        device_id="oven",
        name="Backofen",
        category=DeviceCategory.MONITORED_LOAD,
        is_controllable=False,
        control_mode=ControlModeType.NONE,
        transport=ControlTransport.HA_SERVICE,
        ha_binding=HaServiceBinding(
            domain="switch", entity_id="switch.oven", service_on_or_set="turn_on"
        ),
        max_power_w=2000.0,
        nominal_power_w=2000.0,
    )
    await dispatcher.dispatch_device(monitored, 1000.0)
    await dispatcher.dispatch_device(monitored_ha, 1000.0)

    assert mqtt.published == []
    assert ha.calls == []


async def test_ha_service_dry_run_logs_only_and_real_mode_calls(
    config: AppConfig, caplog: pytest.LogCaptureFixture
) -> None:
    registry = DeviceRegistry(config)
    await registry.initialize()
    ha = _FakeHaClient()
    dispatcher = DeviceCommandDispatcher(
        config, registry, ha_client=cast(HaCoreClient, ha)
    )
    device = ControllableDevice(
        device_id="boiler",
        name="Boiler",
        category=DeviceCategory.DEFERRABLE_LOAD,
        transport=ControlTransport.HA_SERVICE,
        ha_binding=HaServiceBinding(
            domain="switch",
            entity_id="switch.boiler",
            service_on_or_set="turn_on",
            service_off="turn_off",
        ),
        max_power_w=2000.0,
        nominal_power_w=2000.0,
    )

    with caplog.at_level(logging.INFO):
        await dispatcher.dispatch_device(device, 1000.0, dry_run=True)
    assert ha.calls == []
    assert "Shadow HA service call (dry_run)" in caplog.text

    await dispatcher.dispatch_device(device, 1000.0, dry_run=False)
    assert ha.calls[-1] == ("switch", "turn_on", {"entity_id": "switch.boiler", "value": 1000.0})

    await dispatcher.dispatch_device(device, 0.0, dry_run=False)
    assert ha.calls[-1] == ("switch", "turn_off", {"entity_id": "switch.boiler"})


async def test_mqtt_dispatch_uses_shadow_topic_in_dry_run(config: AppConfig) -> None:
    registry = DeviceRegistry(config)
    await registry.initialize()
    mqtt = _FakeMqtt()
    dispatcher = DeviceCommandDispatcher(
        config, registry, mqtt_client=cast(aiomqtt.Client, mqtt)
    )
    device = ControllableDevice(
        device_id="boiler_mqtt",
        name="Boiler",
        category=DeviceCategory.DEFERRABLE_LOAD,
        transport=ControlTransport.MQTT,
        mqtt_command_topic="hems/cmd/boiler_mqtt",
        max_power_w=2000.0,
        nominal_power_w=2000.0,
    )

    await dispatcher.dispatch_device(device, 1000.0, dry_run=True)
    assert mqtt.published == [("hems/shadow/cmd/boiler_mqtt", "1000.0")]

    await dispatcher.dispatch_device(device, 1000.0, dry_run=False)
    assert mqtt.published[-1] == ("hems/cmd/boiler_mqtt", "1000.0")


async def test_dispatch_outputs_covers_default_devices_and_registry_loads(
    config: AppConfig,
) -> None:
    registry = DeviceRegistry(config)
    await registry.initialize()
    mqtt = _FakeMqtt()
    dispatcher = DeviceCommandDispatcher(
        config, registry, mqtt_client=cast(aiomqtt.Client, mqtt)
    )
    await registry.upsert_device(
        ControllableDevice(
            device_id="boiler",
            name="Boiler",
            category=DeviceCategory.DEFERRABLE_LOAD,
            transport=ControlTransport.MQTT,
            mqtt_command_topic="hems/cmd/boiler",
            max_power_w=2000.0,
            nominal_power_w=2000.0,
        )
    )

    await dispatcher.dispatch_outputs(_outputs(), {"boiler": 500.0}, dry_run=True)
    published = dict(mqtt.published)

    assert published["hems/shadow/cmd/default_wallbox"] == "2100.0"
    assert published["hems/shadow/cmd/default_battery"] == "0.0"
    assert published["hems/shadow/cmd/default_heatpump"] == "0.0"
    assert published["hems/shadow/cmd/boiler"] == "500.0"
