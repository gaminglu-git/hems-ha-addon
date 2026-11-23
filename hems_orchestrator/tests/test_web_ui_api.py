"""Phase-7-Gate: Ingress-Web-UI & REST-API (docs/08)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime
from pathlib import Path
from typing import Any, cast

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError

from hems.config import AppConfig, OperatingMode
from hems.control.device_registry import ControllableDevice, DeviceRegistry
from hems.control.scheduler import ControlScheduler
from hems.forecast.appliance_forecaster import ForecastManager
from hems.forecast.pattern_learner import DayType, LearnedPattern
from hems.io.emhass_client import EmhassClient
from hems.io.ha_core_client import HaCoreClient, HaEntityState, HaHistoryPoint
from hems.io.mqtt_client import LiveState
from hems.logging.sqlite_store import SqliteAuditStore
from hems.web.app import create_app


class _FakeHaClient:
    def __init__(self) -> None:
        self.states = [
            HaEntityState(
                entity_id="sensor.waschmaschine_state",
                state="running",
                last_changed="2024-01-01T10:00:00.000Z",
                friendly_name="Waschmaschine",
                domain="sensor",
            ),
            HaEntityState(
                entity_id="sensor.waschmaschine_time_remaining",
                state="45",
                last_changed="2024-01-01T10:00:00.000Z",
                friendly_name="Restzeit",
                domain="sensor",
            ),
            HaEntityState(
                entity_id="sensor.waschmaschine_program",
                state="Baumwolle 40°C",
                last_changed="2024-01-01T10:00:00.000Z",
                friendly_name="Programm",
                domain="sensor",
                attributes={"power_profile": [800.0] * 8},
            ),
            HaEntityState(
                entity_id="light.wohnzimmer",
                state="on",
                last_changed="2024-01-01T10:00:00.000Z",
                friendly_name="Wohnzimmerlicht",
                domain="light",
            ),
            HaEntityState(
                entity_id="sensor.fridge_power",
                state="150.0",
                last_changed="2024-01-01T10:00:00.000Z",
                friendly_name="Kühlschrank Leistung",
                device_class="power",
                unit_of_measurement="W",
                domain="sensor",
            ),
        ]

    async def get_states(self) -> list[HaEntityState]:
        return self.states

    async def get_history(
        self,
        entity_ids: list[str],
        start_utc: datetime,
        end_utc: datetime | None = None,
    ) -> dict[str, list[HaHistoryPoint]]:
        return {}


class _Env:
    def __init__(
        self,
        config: AppConfig,
        registry: DeviceRegistry,
        store: SqliteAuditStore,
        scheduler: ControlScheduler,
        manager: ForecastManager,
        app: Any,
        client: AsyncClient,
    ) -> None:
        self.config = config
        self.registry = registry
        self.store = store
        self.scheduler = scheduler
        self.manager = manager
        self.app = app
        self.client = client


@pytest.fixture
async def env(tmp_path: Path) -> AsyncIterator[_Env]:
    config = AppConfig(
        data_dir=tmp_path / "data",
        tmp_dir=tmp_path / "tmp",
        dry_run=True,
        ingress_only=True,
    )
    registry = DeviceRegistry(config)
    await registry.initialize()
    store = SqliteAuditStore.from_config(config)
    await store.initialize()
    state = LiveState(config)
    scheduler = ControlScheduler(
        config,
        state,
        cast(Any, None),
        store,
        cast(Any, None),
        EmhassClient(config),
    )
    manager = ForecastManager(config, registry, cast(HaCoreClient, _FakeHaClient()), store)
    registry.set_change_callback(manager.refresh)
    await manager.refresh()
    app = create_app(
        config, scheduler, registry, cast(HaCoreClient, _FakeHaClient()), store, manager
    )
    client = AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
    try:
        yield _Env(config, registry, store, scheduler, manager, app, client)
    finally:
        await client.aclose()
        await store.close()


# --- C1 & C6: Ingress-Pfadsicherheit & lokale Assets -----------------------------


async def test_index_uses_relative_paths_and_no_external_assets(env: _Env) -> None:
    response = await env.client.get("/")

    assert response.status_code == 200
    assert "<html" in response.text.lower()
    for forbidden in (
        'fetch("/api',
        "fetch('/api",
        '<base href="/">',
        "url(/",
        "http://",
        "https://",
    ):
        assert forbidden not in response.text, forbidden
    assert 'fetch("api/dashboard")' in response.text
    assert "<svg" in response.text


async def test_ingress_only_middleware_rejects_foreign_host(env: _Env) -> None:
    client = AsyncClient(
        transport=ASGITransport(app=env.app, client=("8.8.8.8", 1234)),
        base_url="http://test",
    )
    try:
        response = await client.get("/api/dashboard")
    finally:
        await client.aclose()
    assert response.status_code == 403


# --- C2: Geräte-CRUD ------------------------------------------------------------


async def test_device_crud_over_api(env: _Env) -> None:
    controllable = {
        "device_id": "boiler",
        "name": "Boiler",
        "category": "deferrable_load",
        "is_controllable": True,
        "transport": "mqtt",
        "mqtt_command_topic": "hems/cmd/boiler",
        "min_power_w": 0.0,
        "max_power_w": 2000.0,
        "nominal_power_w": 2000.0,
    }
    monitored = {
        "device_id": "fridge",
        "name": "Kühlschrank",
        "category": "monitored_load",
        "is_controllable": False,
        "control_mode": "none",
        "transport": "none",
        "power_sensor_entity_id": "sensor.fridge_power",
        "min_power_w": 0.0,
        "max_power_w": 300.0,
        "nominal_power_w": 150.0,
    }
    assert (await env.client.post("/api/devices", json=controllable)).status_code == 201
    assert (await env.client.post("/api/devices", json=monitored)).status_code == 201

    devices = (await env.client.get("/api/devices")).json()
    assert {device["device_id"] for device in devices} >= {"boiler", "fridge", "default_wallbox"}

    mismatch = dict(controllable, device_id="other")
    assert (await env.client.put("/api/devices/boiler", json=mismatch)).status_code == 400

    patched = await env.client.patch(
        "/api/devices/boiler/enabled", json={"enabled": False}
    )
    assert patched.status_code == 200
    assert patched.json()["enabled"] is False

    assert (await env.client.delete("/api/devices/boiler")).status_code == 200
    remaining = (await env.client.get("/api/devices")).json()
    assert "boiler" not in {device["device_id"] for device in remaining}
    assert (await env.client.delete("/api/devices/does_not_exist")).status_code == 404
    assert (await env.client.patch(
        "/api/devices/fridge/enabled", json={"enabled": True}
    )).status_code == 200


# --- C3: WashData-Integration ---------------------------------------------------


async def test_washdata_detection_and_sync_over_api(env: _Env) -> None:
    detected = (await env.client.get("/api/integrations/washdata")).json()
    assert len(detected) == 1
    assert detected[0]["slug"] == "waschmaschine"

    first = await env.client.post("/api/integrations/washdata/sync")
    assert first.status_code == 200
    assert first.json() == {"status": "ok", "imported_count": 1}

    second = await env.client.post("/api/integrations/washdata/sync")
    assert second.json()["imported_count"] == 0
    stored = await env.registry.get_device("washdata_waschmaschine")
    assert stored is not None
    assert stored.is_controllable is False


# --- C4: Muster & Prognosen -----------------------------------------------------


async def test_patterns_and_forecasts_over_api(env: _Env) -> None:
    retrain = await env.client.post("/api/patterns/retrain")
    assert retrain.status_code == 200
    assert "patterns_found" in retrain.json()

    pattern = LearnedPattern(
        pattern_id="light.wohnzimmer_weekday_1170",
        entity_id="light.wohnzimmer",
        friendly_name="Wohnzimmerlicht",
        domain="light",
        day_type=DayType.WEEKDAY,
        typical_on_minute=1170,
        typical_off_minute=1350,
        avg_duration_min=180.0,
        std_dev_min=2.0,
        confidence=0.8,
        sample_count=8,
        estimated_power_w=15.0,
        updated_at_utc="2024-01-15T00:00:00.000Z",
    )
    await env.store.replace_patterns([pattern])
    patterns = (await env.client.get("/api/patterns")).json()
    assert any(item["entity_id"] == "light.wohnzimmer" for item in patterns)

    await env.client.post("/api/integrations/washdata/sync")
    forecast = (await env.client.get("/api/forecasts")).json()
    assert len(forecast["aggregated_uncontrollable_w"]) == 96
    assert len(forecast["total_house_predicted_w"]) == 96
    assert all(len(consumer["slots"]) == 96 for consumer in forecast["consumers"])
    assert any(
        consumer["source_integration"] == "washdata" for consumer in forecast["consumers"]
    )


# --- C5: Laufzeitsteuerung ------------------------------------------------------


async def test_control_mode_and_dry_run_reflected_in_dashboard(env: _Env) -> None:
    mode = await env.client.post(
        "/api/control/mode", json={"mode": OperatingMode.A_BASELINE.value}
    )
    assert mode.json() == {"status": "ok", "mode": "A_BASELINE"}
    assert env.config.operating_mode is OperatingMode.A_BASELINE

    dry = await env.client.post("/api/control/dry_run", json={"dry_run": False})
    assert dry.json() == {"status": "ok", "dry_run": False}
    assert env.config.dry_run is False

    dashboard = (await env.client.get("/api/dashboard")).json()
    assert dashboard["operating_mode"] == "A_BASELINE"
    assert dashboard["dry_run"] is False
    assert dashboard["active_devices"] == 3
    assert dashboard["washdata_devices"]


async def test_ha_entity_filter_and_request_validation(env: _Env) -> None:
    all_entities = (await env.client.get("/api/ha/entities")).json()
    lights = (await env.client.get("/api/ha/entities", params={"domain": "light"})).json()
    assert len(lights) == 1
    assert lights[0]["entity_id"] == "light.wohnzimmer"
    assert len(all_entities) > len(lights)

    invalid = await env.client.post("/api/devices", json={"device_id": "bad", "name": "X"})
    assert invalid.status_code == 422


def test_request_models_forbid_extra_fields() -> None:
    from hems.web.app import ModeRequest

    with pytest.raises(ValidationError):
        ModeRequest.model_validate({"mode": "A_BASELINE", "unexpected": "value"})


def test_controllable_device_model_still_strict() -> None:
    with pytest.raises(ValidationError):
        ControllableDevice.model_validate({"device_id": "x", "name": "X"})
