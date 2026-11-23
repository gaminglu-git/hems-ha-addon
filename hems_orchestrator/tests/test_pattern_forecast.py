"""Phase-6-Gate: Mustererkennung, WashData & Einzelverbraucher-Prognose (docs/07)."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, cast
from zoneinfo import ZoneInfo

import httpx
import pytest
import respx
from pydantic import ValidationError

from hems.config import AppConfig
from hems.control.device_registry import (
    ControllableDevice,
    ControlTransport,
    DeviceCategory,
    DeviceRegistry,
    IntegrationSource,
)
from hems.forecast.appliance_forecaster import (
    ApplianceForecaster,
    ForecastManager,
    ForecastSource,
    SystemForecastReport,
)
from hems.forecast.pattern_learner import DayType, LearnedPattern, learn_patterns
from hems.forecast.washdata_adapter import (
    WashDataLiveStatus,
    discover_washdata_appliances,
    project_washdata_forecast,
)
from hems.io.emhass_client import EmhassClient, EmhassResult, build_runtimeparams
from hems.io.ha_core_client import HaCoreClient, HaEntityState, HaHistoryPoint
from hems.logging.sqlite_store import SqliteAuditStore
from hems.logging.verification import format_utc_ms
from hems.models import SystemInputs
from test_audit_logging import make_inputs

TZ = ZoneInfo("Europe/Berlin")
EMHASS_URL = "http://emhass-test:5000/action/naive-mpc-optim"


class _FakeHaClient:
    def __init__(
        self,
        states: list[HaEntityState] | None = None,
        history: dict[str, list[HaHistoryPoint]] | None = None,
    ) -> None:
        self.states = states or []
        self.history = history or {}

    async def get_states(self) -> list[HaEntityState]:
        return self.states

    async def get_history(
        self,
        entity_ids: list[str],
        start_utc: datetime,
        end_utc: datetime | None = None,
    ) -> dict[str, list[HaHistoryPoint]]:
        return {entity_id: self.history.get(entity_id, []) for entity_id in entity_ids}


@pytest.fixture
def config(tmp_path: Path) -> AppConfig:
    return AppConfig(
        data_dir=tmp_path / "data",
        tmp_dir=tmp_path / "tmp",
        timezone="Europe/Berlin",
        dry_run=True,
    )


@pytest.fixture
async def store(config: AppConfig) -> SqliteAuditStore:
    instance = SqliteAuditStore.from_config(config)
    await instance.initialize()
    yield instance
    await instance.close()


def _point(state: str, local_dt: datetime) -> HaHistoryPoint:
    return HaHistoryPoint(
        state=state,
        last_changed_utc=format_utc_ms(local_dt.astimezone(UTC)),
    )


def _local(day: date, hour: int, minute: int) -> datetime:
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=TZ)


def _washdata_status(**overrides: Any) -> WashDataLiveStatus:
    payload: dict[str, Any] = {
        "slug": "waschmaschine",
        "friendly_name": "Waschmaschine",
        "state": "running",
        "is_running": True,
        "time_remaining_min": 45.0,
        "progress_pct": 50.0,
        "projected_energy_kwh": 1.6,
    }
    payload.update(overrides)
    return WashDataLiveStatus.model_validate(payload)


def _device(device_id: str, category: DeviceCategory, **overrides: Any) -> ControllableDevice:
    payload: dict[str, Any] = {
        "device_id": device_id,
        "name": device_id,
        "category": category,
        "transport": ControlTransport.MQTT,
        "mqtt_command_topic": f"hems/cmd/{device_id}",
        "min_power_w": 0.0,
        "max_power_w": 2000.0,
        "nominal_power_w": 1000.0,
    }
    payload.update(overrides)
    return ControllableDevice.model_validate(payload)


# --- B1: aktiver WashData-Zyklus -------------------------------------------------


def test_washdata_live_cycle_fills_exact_slots(config: AppConfig) -> None:
    status = _washdata_status()
    slots = project_washdata_forecast(status, [], datetime(2024, 1, 1, tzinfo=UTC), TZ)

    assert len(slots) == 96
    assert all(slot.expected_power_w > 0.0 for slot in slots[:3])
    assert all(
        slot.source is ForecastSource.WASHDATA_ACTIVE_CYCLE and slot.confidence >= 0.90
        for slot in slots[:3]
    )
    assert all(slot.expected_power_w == 0.0 for slot in slots[3:])


# --- B2: exaktes reference_profile ----------------------------------------------


def test_washdata_reference_profile_uses_remaining_entries(config: AppConfig) -> None:
    status = _washdata_status(
        slug="geschirrspueler",
        time_remaining_min=90.0,
        progress_pct=25.0,
        reference_profile_w=[100.0, 200.0, 300.0, 400.0, 500.0, 600.0, 700.0, 800.0],
    )
    slots = project_washdata_forecast(status, [], datetime(2024, 1, 1, tzinfo=UTC), TZ)

    assert [slot.expected_power_w for slot in slots[:6]] == [
        300.0,
        400.0,
        500.0,
        600.0,
        700.0,
        800.0,
    ]
    assert all(slot.source is ForecastSource.WASHDATA_ACTIVE_CYCLE for slot in slots[:6])
    assert all(slot.expected_power_w == 0.0 for slot in slots[6:])


def test_discover_washdata_appliances_from_states(config: AppConfig) -> None:
    states = [
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
            attributes={"power_profile": [100.0, 200.0]},
        ),
    ]
    statuses = discover_washdata_appliances(states)

    assert len(statuses) == 1
    assert statuses[0].slug == "waschmaschine"
    assert statuses[0].is_running is True
    assert statuses[0].program == "Baumwolle 40°C"
    assert statuses[0].reference_profile_w == [100.0, 200.0]


# --- B3: Werktags- vs. Wochenendmuster ------------------------------------------


def test_light_pattern_learning_weekday_vs_weekend(config: AppConfig) -> None:
    start = _local(date(2024, 1, 1), 0, 0)
    end = _local(date(2024, 1, 14), 23, 59)
    points: list[HaHistoryPoint] = []
    weekday_on = {0, 1, 2, 3, 4, 7, 8, 9}
    for offset in range(14):
        day = start.date() + timedelta(days=offset)
        points.append(_point("off", _local(day, 0, 0)))
        is_active = (day.weekday() < 5 and offset in weekday_on) or offset == 12
        if is_active:
            points.append(_point("on", _local(day, 19, 30)))
            points.append(_point("off", _local(day, 22, 30)))

    patterns = learn_patterns(
        {"light.wohnzimmer": points},
        {"light.wohnzimmer": ("Wohnzimmer Deckenlicht", "light")},
        start,
        end,
        TZ,
        now_utc=datetime(2024, 1, 15, tzinfo=UTC),
    )

    assert len(patterns) == 1
    pattern = patterns[0]
    assert pattern.day_type is DayType.WEEKDAY
    assert pattern.sample_count == 8
    assert pattern.confidence == 0.8
    assert pattern.typical_on_minute == 19 * 60 + 30
    assert pattern.estimated_power_w == 15.0


# --- B4: Mitternachts-Wrap & Rauschunterdrückung --------------------------------


def test_midnight_wrap_and_noise_suppression(config: AppConfig) -> None:
    start = _local(date(2024, 1, 1), 0, 0)
    end = _local(date(2024, 1, 14), 23, 59)
    points: list[HaHistoryPoint] = []
    for offset in (0, 1, 2, 3, 4):
        day = start.date() + timedelta(days=offset)
        points.append(_point("off", _local(day, 0, 0)))
        points.append(_point("on", _local(day, 23, 50)))
        points.append(_point("off", _local(day + timedelta(days=1), 0, 30)))
    for offset in (7, 8):
        day = start.date() + timedelta(days=offset)
        points.append(_point("on", _local(day, 12, 0)))
        points.append(_point("off", _local(day, 12, 20)))

    patterns = learn_patterns(
        {"switch.nachtlicht": points},
        {"switch.nachtlicht": ("Nachtlicht", "switch")},
        start,
        end,
        TZ,
        now_utc=datetime(2024, 1, 15, tzinfo=UTC),
    )

    assert len(patterns) == 1
    assert patterns[0].typical_on_minute in {1430, 1435, 0, 5}
    assert patterns[0].day_type is DayType.WEEKDAY
    assert all(not (1100 <= item.typical_on_minute <= 1250) for item in patterns)


# --- SQLite learned_patterns ----------------------------------------------------


async def test_sqlite_replace_and_list_patterns(store: SqliteAuditStore) -> None:
    pattern = LearnedPattern(
        pattern_id="light.wohnzimmer_weekday_1170",
        entity_id="light.wohnzimmer",
        friendly_name="Wohnzimmer",
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
    await store.replace_patterns([pattern])
    assert await store.list_patterns() == [pattern]

    await store.replace_patterns([])
    assert await store.list_patterns() == []


# --- B5: universelle Prognose ---------------------------------------------------


async def test_forecaster_builds_five_consumer_forecasts(config: AppConfig) -> None:
    devices = [
        _device("default_wallbox", DeviceCategory.WALLBOX, is_controllable=True),
        _device("default_heatpump", DeviceCategory.HEATPUMP, is_controllable=True),
        _device(
            "washdata_waschmaschine",
            DeviceCategory.APPLIANCE_CYCLE,
            is_controllable=False,
            washdata_prefix="waschmaschine",
            source_integration=IntegrationSource.WASHDATA,
        ),
        _device(
            "smart_plug_fridge",
            DeviceCategory.MONITORED_LOAD,
            is_controllable=False,
            power_sensor_entity_id="sensor.fridge_power",
            source_integration=IntegrationSource.HA_POWER_SENSOR,
        ),
    ]
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
    history_point = _point("150.0", _local(date(2024, 1, 2), 10, 0))
    schedule = [1200.0] * 24
    emhass_result = EmhassResult(
        status="ok",
        solve_ms=1,
        setpoints=None,
        runtimeparams_hash="abcdef12",
        wallbox_schedule_w=schedule,
        heatpump_schedule_w=[500.0] * 24,
    )
    start = datetime(2024, 1, 15, 10, 0, tzinfo=UTC)
    report = ApplianceForecaster(config).build_forecast(
        devices,
        [_washdata_status()],
        [pattern],
        start,
        emhass_result=emhass_result,
        history={"sensor.fridge_power": [history_point] * 5},
    )

    assert len(report.consumers) == 5
    assert all(len(consumer.slots) == 96 for consumer in report.consumers)
    assert report.consumers[0].slots[0].source is ForecastSource.MPC_SCHEDULE
    assert report.consumers[0].slots[24].source is ForecastSource.STATIC_BASELINE
    assert report.consumers[0].slots[24].confidence == 0.25
    assert all(
        report.aggregated_uncontrollable_w[index]
        == sum(
            consumer.slots[index].expected_power_w
            for consumer in report.consumers
            if not consumer.is_controllable
        )
        for index in range(96)
    )
    assert len(report.aggregated_uncontrollable_w) == 96
    assert len(report.total_house_predicted_w) == 96


# --- B6 & Patch-Spec: EMHASS-Kopplung -------------------------------------------


def test_runtimeparams_time_step_and_aggregated_load() -> None:
    config = AppConfig(emhass_url="http://emhass-test:5000")
    inputs: SystemInputs = make_inputs()
    params = build_runtimeparams(inputs, config)

    assert params["time_step"] == 15
    assert params["load_power_forecast"] == [inputs.p_house_base_w] * 24

    custom = build_runtimeparams(inputs, config, [250.0] * 24)
    assert custom["load_power_forecast"] == [250.0] * 24
    assert build_runtimeparams(inputs, config, [0.0] * 24)["load_power_forecast"] == [
        inputs.p_house_base_w
    ] * 24


@respx.mock
async def test_emhass_result_exposes_24_slot_schedules() -> None:
    config = AppConfig(emhass_url="http://emhass-test:5000")
    respx.post(EMHASS_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "P_batt": [-3000.0, 1000.0] + [500.0] * 22,
                "P_deferrable0": [4200.0, 4000.0] + [3000.0] * 22,
                "P_deferrable1": [200.0, 100.0] + [50.0] * 22,
            },
        )
    )
    result = await EmhassClient(config).optimize(make_inputs())

    assert result.status == "ok"
    assert result.wallbox_schedule_w is not None
    assert len(result.wallbox_schedule_w) == 24
    assert result.wallbox_schedule_w[:2] == [4200.0, 4000.0]
    assert result.heatpump_schedule_w is not None
    assert len(result.heatpump_schedule_w) == 24
    assert result.battery_schedule_w is not None
    assert len(result.battery_schedule_w) == 24
    assert result.battery_schedule_w[:2] == [3000.0, -1000.0]


# --- ForecastManager ------------------------------------------------------------


async def test_forecast_manager_caches_report_and_refreshes_on_registry_change(
    config: AppConfig,
) -> None:
    registry = DeviceRegistry(config)
    await registry.initialize()
    ha = _FakeHaClient()
    manager = ForecastManager(config, registry, cast(HaCoreClient, ha), _store_stub())

    first = await manager.refresh()
    assert manager.get_report() is first
    assert isinstance(first, SystemForecastReport)

    registry.set_change_callback(manager.refresh)
    await registry.upsert_device(
        _device("extra_load", DeviceCategory.MONITORED_LOAD, is_controllable=False)
    )
    assert manager.get_report() is not first
    assert len(manager.get_report().consumers) == len(first.consumers) + 1  # type: ignore[union-attr]


def _store_stub() -> SqliteAuditStore:
    """Minimaler Store: list_patterns liefert keine Muster (kein I/O nötig)."""
    class _Stub:
        async def list_patterns(self) -> list[LearnedPattern]:
            return []

        async def replace_patterns(self, patterns: list[LearnedPattern]) -> None:
            return None

    return cast(SqliteAuditStore, _Stub())


def test_learned_pattern_model_rejects_invalid_confidence() -> None:
    with pytest.raises(ValidationError):
        LearnedPattern(
            pattern_id="x",
            entity_id="light.x",
            friendly_name="X",
            domain="light",
            day_type=DayType.WEEKDAY,
            typical_on_minute=100,
            avg_duration_min=10.0,
            std_dev_min=1.0,
            confidence=1.5,
            sample_count=3,
            estimated_power_w=15.0,
            updated_at_utc="2024-01-15T00:00:00.000Z",
        )
