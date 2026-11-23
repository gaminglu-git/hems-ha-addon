"""Universelle 96-Slot-Einzelverbraucher-Prognose & ForecastManager (docs/07 §4)."""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from enum import Enum
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field

from hems.config import AppConfig
from hems.control.device_registry import ControllableDevice, DeviceCategory
from hems.forecast.pattern_learner import (
    DayType,
    LearnedPattern,
    analyze_entities,
    learn_patterns,
)
from hems.forecast.washdata_adapter import (
    WashDataLiveStatus,
    discover_washdata_appliances,
    project_washdata_forecast,
)
from hems.io.emhass_client import EmhassResult
from hems.io.ha_core_client import HaCoreClient, HaEntityState, HaHistoryPoint
from hems.logging.sqlite_store import SqliteAuditStore
from hems.logging.verification import format_utc_ms

logger = logging.getLogger(__name__)


class ForecastSource(str, Enum):
    WASHDATA_ACTIVE_CYCLE = "washdata_active_cycle"
    WASHDATA_HABITUAL = "washdata_habitual"
    MPC_SCHEDULE = "mpc_schedule"
    HABITUAL_PATTERN = "habitual_pattern"
    HISTORICAL_PROFILE = "historical_profile"
    STATIC_BASELINE = "static_baseline"


class ForecastSlot(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    timestamp_utc: str
    expected_power_w: float = Field(ge=0.0)
    confidence: float = Field(ge=0.0, le=1.0)
    source: ForecastSource


class ConsumerForecast(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    consumer_id: str
    name: str
    category: str
    is_controllable: bool
    source_integration: str
    active_status_label: str | None = None
    total_predicted_kwh_24h: float
    peak_power_w: float
    slots: list[ForecastSlot]


class SystemForecastReport(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    generated_at_utc: str
    horizon_hours: int = 24
    resolution_minutes: int = 15
    consumers: list[ConsumerForecast]
    aggregated_uncontrollable_w: list[float]
    total_house_predicted_w: list[float]


def _slot(
    start_utc: datetime, index: int, power_w: float, confidence: float, source: ForecastSource
) -> ForecastSlot:
    return ForecastSlot(
        timestamp_utc=format_utc_ms(start_utc + timedelta(minutes=15 * index)),
        expected_power_w=max(0.0, float(power_w)),
        confidence=min(1.0, max(0.0, float(confidence))),
        source=source,
    )


def _pattern_slots(
    patterns: list[LearnedPattern], start_utc: datetime, tz: ZoneInfo, default_power_w: float
) -> list[ForecastSlot]:
    slots = [_slot(start_utc, index, 0.0, 0.0, ForecastSource.STATIC_BASELINE)
             for index in range(96)]
    for index in range(96):
        local = (start_utc + timedelta(minutes=15 * index)).astimezone(tz)
        day_type = DayType.WEEKDAY if local.weekday() < 5 else DayType.WEEKEND
        minute = local.hour * 60 + local.minute
        for pattern in patterns:
            if pattern.day_type is not day_type:
                continue
            end_minute = pattern.typical_on_minute + pattern.avg_duration_min
            active = (
                pattern.typical_on_minute <= minute < end_minute
                if end_minute <= 1440
                else minute >= pattern.typical_on_minute or minute < end_minute - 1440
            )
            if active:
                slots[index] = _slot(
                    start_utc,
                    index,
                    pattern.estimated_power_w * pattern.confidence or default_power_w,
                    pattern.confidence,
                    ForecastSource.HABITUAL_PATTERN,
                )
                break
    return slots


def _history_slots(
    points: list[HaHistoryPoint], start_utc: datetime, tz: ZoneInfo, fallback_w: float
) -> list[ForecastSlot]:
    """15-Minuten-Slot-Mittelwerte getrennt nach weekday/weekend."""
    buckets: dict[tuple[DayType, int], list[float]] = defaultdict(list)
    for point in points:
        try:
            local = datetime.fromisoformat(point.last_changed_utc.replace("Z", "+00:00"))
        except ValueError:
            continue
        if local.tzinfo is None:
            local = local.replace(tzinfo=UTC)
        local = local.astimezone(tz)
        try:
            power = float(point.state)
        except ValueError:
            continue
        day_type = DayType.WEEKDAY if local.weekday() < 5 else DayType.WEEKEND
        buckets[(day_type, local.hour * 4 + local.minute // 15)].append(max(0.0, power))

    slots: list[ForecastSlot] = []
    for index in range(96):
        local = (start_utc + timedelta(minutes=15 * index)).astimezone(tz)
        day_type = DayType.WEEKDAY if local.weekday() < 5 else DayType.WEEKEND
        values = buckets.get((day_type, index), [])
        if len(values) >= 3:
            slots.append(
                _slot(start_utc, index, sum(values) / len(values), 0.75,
                      ForecastSource.HISTORICAL_PROFILE)
            )
        else:
            slots.append(_slot(start_utc, index, fallback_w, 0.30,
                               ForecastSource.STATIC_BASELINE))
    return slots


class ApplianceForecaster:
    """Baut die 96-Slot-Prognose für jeden eingebundenen Verbraucher."""

    def __init__(self, config: AppConfig) -> None:
        self._config = config

    def build_forecast(
        self,
        devices: list[ControllableDevice],
        washdata_statuses: list[WashDataLiveStatus],
        patterns: list[LearnedPattern],
        start_utc: datetime,
        emhass_result: EmhassResult | None = None,
        history: dict[str, list[HaHistoryPoint]] | None = None,
        states: list[HaEntityState] | None = None,
    ) -> SystemForecastReport:
        tz = ZoneInfo(self._config.timezone)
        history = history or {}
        statuses = {status.slug: status for status in washdata_statuses}
        forecasts: list[ConsumerForecast] = []
        covered_entities: set[str] = set()

        for device in devices:
            if not device.enabled:
                continue
            device_entities = {
                value
                for value in (
                    device.power_sensor_entity_id,
                    device.state_entity_id,
                )
                if value
            }
            if device.washdata_prefix:
                device_entities.update(
                    {
                        f"sensor.{device.washdata_prefix}_state",
                        f"binary_sensor.{device.washdata_prefix}_running",
                    }
                )
            covered_entities.update(device_entities)
            device_patterns = [
                pattern for pattern in patterns if pattern.entity_id in device_entities
            ]
            status = statuses.get(device.washdata_prefix or "")

            label: str | None = None
            slots: list[ForecastSlot]
            if status is not None:
                if status.is_running:
                    label = (
                        f"Läuft ({status.program or 'Programm'}, "
                        f"noch {int(status.time_remaining_min)} min)"
                    )
                slots = project_washdata_forecast(status, device_patterns, start_utc, tz)
            elif device.is_controllable:
                schedule = self._schedule_for(device, emhass_result)
                if schedule is not None:
                    slots = [
                        _slot(start_utc, index, schedule[index], 0.90, ForecastSource.MPC_SCHEDULE)
                        for index in range(24)
                    ]
                    slots.extend(
                        _slot(start_utc, index, device.min_power_w, 0.25,
                              ForecastSource.STATIC_BASELINE)
                        for index in range(24, 96)
                    )
                else:
                    slots = [
                        _slot(start_utc, index, device.min_power_w, 0.25,
                              ForecastSource.STATIC_BASELINE)
                        for index in range(96)
                    ]
            elif device_patterns:
                slots = _pattern_slots(
                    device_patterns, start_utc, tz, device.nominal_power_w
                )
            elif device.power_sensor_entity_id and device.power_sensor_entity_id in history:
                slots = _history_slots(
                    history[device.power_sensor_entity_id],
                    start_utc,
                    tz,
                    device.min_power_w,
                )
            else:
                slots = [
                    _slot(start_utc, index, device.min_power_w, 0.20,
                          ForecastSource.STATIC_BASELINE)
                    for index in range(96)
                ]

            forecasts.append(
                ConsumerForecast(
                    consumer_id=device.device_id,
                    name=device.name,
                    category=device.category.value,
                    is_controllable=device.is_controllable,
                    source_integration=device.source_integration.value,
                    active_status_label=label,
                    total_predicted_kwh_24h=sum(slot.expected_power_w for slot in slots)
                    * 0.25 / 1000.0,
                    peak_power_w=max(slot.expected_power_w for slot in slots),
                    slots=slots,
                )
            )

        # Nicht in der Registry hinterlegte Entitäten mit gelerntem Muster.
        for pattern in patterns:
            if pattern.entity_id in covered_entities:
                continue
            covered_entities.add(pattern.entity_id)
            slots = _pattern_slots([pattern], start_utc, tz, pattern.estimated_power_w)
            forecasts.append(
                ConsumerForecast(
                    consumer_id=pattern.entity_id,
                    name=pattern.friendly_name,
                    category="light" if pattern.domain == "light" else "switch",
                    is_controllable=False,
                    source_integration="ha_pattern",
                    total_predicted_kwh_24h=sum(slot.expected_power_w for slot in slots)
                    * 0.25 / 1000.0,
                    peak_power_w=max(slot.expected_power_w for slot in slots),
                    slots=slots,
                )
            )

        aggregated = [0.0] * 96
        total = [0.0] * 96
        for forecast in forecasts:
            for index, slot in enumerate(forecast.slots):
                total[index] += slot.expected_power_w
                if not forecast.is_controllable:
                    aggregated[index] += slot.expected_power_w

        return SystemForecastReport(
            generated_at_utc=format_utc_ms(start_utc),
            consumers=forecasts,
            aggregated_uncontrollable_w=aggregated,
            total_house_predicted_w=total,
        )

    @staticmethod
    def _schedule_for(
        device: ControllableDevice, emhass_result: EmhassResult | None
    ) -> list[float] | None:
        if emhass_result is None:
            return None
        if device.category is DeviceCategory.HEATPUMP:
            return emhass_result.heatpump_schedule_w
        if device.category is DeviceCategory.BATTERY:
            return emhass_result.battery_schedule_w
        return emhass_result.wallbox_schedule_w


class ForecastManager:
    """Hält den gecachten SystemForecastReport und aktualisiert ihn deterministisch."""

    def __init__(
        self,
        config: AppConfig,
        registry: object,
        ha_client: HaCoreClient,
        store: SqliteAuditStore,
        forecaster: ApplianceForecaster | None = None,
    ) -> None:
        self._config = config
        self._registry = registry
        self._ha_client = ha_client
        self._store = store
        self._forecaster = forecaster or ApplianceForecaster(config)
        self._report: SystemForecastReport | None = None
        self._emhass_result: EmhassResult | None = None
        self._lock = asyncio.Lock()

    def get_report(self) -> SystemForecastReport | None:
        return self._report

    def set_emhass_result(self, result: EmhassResult | None) -> None:
        self._emhass_result = result

    async def refresh(self) -> SystemForecastReport:
        """Berechnet den Report neu (inkl. Live-States) und legt ihn im Cache ab."""
        async with self._lock:
            now = datetime.now(UTC)
            states = await self._ha_client.get_states()
            statuses = discover_washdata_appliances(states)
            patterns = await self._store.list_patterns()
            devices = await self._registry.list_devices()  # type: ignore[attr-defined]
            entity_ids = analyze_entities(
                states,
                [
                    value
                    for device in devices
                    for value in (device.power_sensor_entity_id, device.state_entity_id)
                    if value
                ],
            )
            history: dict[str, list[HaHistoryPoint]] = {}
            if entity_ids:
                history = await self._ha_client.get_history(
                    entity_ids, now - timedelta(days=self._config.pattern_lookback_days)
                )
            self._report = self._forecaster.build_forecast(
                devices,
                statuses,
                patterns,
                now,
                emhass_result=self._emhass_result,
                history=history,
                states=states,
            )
            return self._report

    async def retrain_patterns(self) -> int:
        """Führt die Musteranalyse aus, speichert die Muster und aktualisiert den Cache."""
        now = datetime.now(UTC)
        tz = ZoneInfo(self._config.timezone)
        start_local = (now - timedelta(days=self._config.pattern_lookback_days)).astimezone(tz)
        states = await self._ha_client.get_states()
        entity_ids = analyze_entities(states)
        history = await self._ha_client.get_history(
            entity_ids, now - timedelta(days=self._config.pattern_lookback_days)
        ) if entity_ids else {}
        meta = {
            state.entity_id: (state.friendly_name, state.domain)
            for state in states
        }
        discovered = await self._registry.list_devices()  # type: ignore[attr-defined]
        nominal_power = {
            device.state_entity_id: device.nominal_power_w
            for device in discovered
            if device.state_entity_id
        }
        patterns = await asyncio.to_thread(
            learn_patterns,
            history,
            meta,
            start_local,
            now.astimezone(tz),
            tz,
            now_utc=now,
            nominal_power=nominal_power,
        )
        await self._store.replace_patterns(patterns)
        await self.refresh()
        return len(patterns)

    async def run_background(self) -> None:
        """Hält WashData-Restlaufzeiten alle 60 s aktuell."""
        while True:
            await asyncio.sleep(60.0)
            try:
                await self.refresh()
            except Exception as exc:  # noqa: BLE001 - Hintergrund-Loop muss weiterlaufen
                logger.error("Background forecast refresh failed: %s", exc)

    async def run_retrain_loop(self) -> None:
        while True:
            await asyncio.sleep(self._config.pattern_retrain_interval_s)
            try:
                await self.retrain_patterns()
            except Exception as exc:  # noqa: BLE001 - Hintergrund-Loop muss weiterlaufen
                logger.error("Periodic pattern retraining failed: %s", exc)
