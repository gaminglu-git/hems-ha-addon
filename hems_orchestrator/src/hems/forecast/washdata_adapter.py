"""Auswertung der Home-Assistant-Custom-Integration WashData (docs/07 §2)."""

from __future__ import annotations

import logging
import math
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field

from hems.forecast.pattern_learner import DayType, LearnedPattern
from hems.io.ha_core_client import HaEntityState
from hems.logging.verification import format_utc_ms

if TYPE_CHECKING:
    from hems.forecast.appliance_forecaster import ForecastSlot

logger = logging.getLogger(__name__)

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
_RUNNING_STATES = frozenset({"starting", "running", "ending"})
_IGNORED_PROGRAMS = frozenset({"unknown", "detecting", "unavailable", "none", ""})


class WashDataLiveStatus(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    slug: str
    friendly_name: str
    state: str
    is_running: bool
    program: str | None = None
    time_remaining_min: float = Field(default=0.0, ge=0.0)
    progress_pct: float = Field(default=0.0, ge=0.0, le=100.0)
    projected_energy_kwh: float | None = None
    last_cycle_energy_kwh: float | None = None
    last_cycle_duration_min: float | None = None
    current_power_w: float = Field(default=0.0, ge=0.0)
    reference_profile_w: list[float] | None = None


def _number(value: Any) -> float | None:
    """Parst HA-Zustände robust; ungültige Werte werden zu None."""
    if value is None:
        return None
    try:
        number = float(str(value))
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return number


def discover_washdata_appliances(states: list[HaEntityState]) -> list[WashDataLiveStatus]:
    """Erkennt WashData-Geräte anhand von state + Restzeit-/Lauf-Sensor."""
    by_id = {state.entity_id: state for state in states}
    result: list[WashDataLiveStatus] = []
    for state in states:
        entity_id = state.entity_id
        if not entity_id.startswith("sensor.") or not entity_id.endswith("_state"):
            continue
        if state.state.lower() not in _WASHDATA_STATES:
            continue
        slug = entity_id.removeprefix("sensor.").removesuffix("_state")
        if not slug:
            continue
        time_remaining = by_id.get(f"sensor.{slug}_time_remaining")
        binary_running = by_id.get(f"binary_sensor.{slug}_running")
        if time_remaining is None and binary_running is None:
            continue

        program_state = by_id.get(f"sensor.{slug}_program")
        program = None
        if program_state is not None and program_state.state.lower() not in _IGNORED_PROGRAMS:
            program = program_state.state

        progress_state = by_id.get(f"sensor.{slug}_progress")
        attributes = progress_state.attributes if progress_state is not None else {}
        program_attributes = program_state.attributes if program_state is not None else {}
        profile_raw = program_attributes.get("reference_profile")
        if profile_raw is None:
            profile_raw = program_attributes.get("power_profile")
        if profile_raw is None and progress_state is not None:
            profile_raw = progress_state.attributes.get("reference_profile")
        profile: list[float] | None = None
        if isinstance(profile_raw, list) and profile_raw:
            parsed = [_number(value) for value in profile_raw]
            if all(value is not None for value in parsed):
                profile = [max(0.0, float(value)) for value in parsed]

        last_energy = by_id.get(f"sensor.{slug}_last_cycle_energy")
        last_duration = by_id.get(f"sensor.{slug}_last_cycle_duration")
        power_state = by_id.get(f"sensor.{slug}_power")
        is_running = state.state.lower() in _RUNNING_STATES or (
            binary_running is not None and binary_running.state.lower() == "on"
        )
        try:
            status = WashDataLiveStatus(
                slug=slug,
                friendly_name=state.friendly_name or slug,
                state=state.state,
                is_running=is_running,
                program=program,
                time_remaining_min=max(
                    0.0, _number(time_remaining.state if time_remaining else None) or 0.0
                ),
                progress_pct=min(
                    100.0,
                    max(0.0, _number(progress_state.state if progress_state else None) or 0.0),
                ),
                projected_energy_kwh=_number(attributes.get("projected_energy_kwh")),
                last_cycle_energy_kwh=_number(last_energy.state if last_energy else None),
                last_cycle_duration_min=_number(last_duration.state if last_duration else None),
                current_power_w=max(
                    0.0, _number(power_state.state if power_state else None) or 0.0
                ),
                reference_profile_w=profile,
            )
        except (ValueError, TypeError) as exc:
            logger.error("Rejecting invalid WashData status for %s: %s", slug, exc)
            continue
        result.append(status)
    return result


def _estimated_power_w(status: WashDataLiveStatus) -> float:
    if status.last_cycle_energy_kwh is not None and status.last_cycle_duration_min:
        hours = status.last_cycle_duration_min / 60.0
        if hours > 0.0:
            return (status.last_cycle_energy_kwh * 1000.0) / hours
    return 800.0


def project_washdata_forecast(
    status: WashDataLiveStatus,
    habitual_patterns: list[LearnedPattern],
    start_utc: datetime,
    tz: ZoneInfo,
) -> list[ForecastSlot]:
    """Erzeugt exakt 96 Slots: aktiver Zyklus + zukünftige Gewohnheitszyklen."""
    from hems.forecast.appliance_forecaster import ForecastSlot, ForecastSource

    slots: list[ForecastSlot] = [
        ForecastSlot(
            timestamp_utc=format_utc_ms(start_utc + timedelta(minutes=15 * index)),
            expected_power_w=0.0,
            confidence=0.0,
            source=ForecastSource.STATIC_BASELINE,
        )
        for index in range(96)
    ]

    remaining_slots = 0
    if status.is_running and status.time_remaining_min > 0.0:
        remaining_slots = min(96, max(1, math.ceil(status.time_remaining_min / 15.0)))
        profile = status.reference_profile_w
        if profile:
            idx_start = min(
                len(profile) - 1, int(round((status.progress_pct / 100.0) * len(profile)))
            )
            for offset in range(min(remaining_slots, len(profile) - idx_start)):
                slots[offset] = ForecastSlot(
                    timestamp_utc=slots[offset].timestamp_utc,
                    expected_power_w=max(0.0, profile[idx_start + offset]),
                    confidence=0.95,
                    source=ForecastSource.WASHDATA_ACTIVE_CYCLE,
                )
        else:
            total_kwh = (
                status.projected_energy_kwh
                or status.last_cycle_energy_kwh
                or 1.2
            )
            remaining_wh = max(
                50.0, total_kwh * 1000.0 * max(0.05, 1.0 - status.progress_pct / 100.0)
            )
            average_w = min(
                3680.0, max(50.0, remaining_wh / (status.time_remaining_min / 60.0))
            )
            for index in range(remaining_slots):
                slots[index] = ForecastSlot(
                    timestamp_utc=slots[index].timestamp_utc,
                    expected_power_w=average_w,
                    confidence=0.90,
                    source=ForecastSource.WASHDATA_ACTIVE_CYCLE,
                )

    for index in range(remaining_slots, 96):
        local = (start_utc + timedelta(minutes=15 * index)).astimezone(tz)
        day_type = DayType.WEEKDAY if local.weekday() < 5 else DayType.WEEKEND
        minute = local.hour * 60 + local.minute
        for pattern in habitual_patterns:
            if pattern.day_type is not day_type:
                continue
            end_minute = pattern.typical_on_minute + pattern.avg_duration_min
            active = (
                pattern.typical_on_minute <= minute < end_minute
                if end_minute <= 1440
                else minute >= pattern.typical_on_minute or minute < end_minute - 1440
            )
            if active:
                slots[index] = ForecastSlot(
                    timestamp_utc=slots[index].timestamp_utc,
                    expected_power_w=max(
                        0.0, _estimated_power_w(status) * pattern.confidence
                    ),
                    confidence=pattern.confidence,
                    source=ForecastSource.WASHDATA_HABITUAL,
                )
                break
    return slots
