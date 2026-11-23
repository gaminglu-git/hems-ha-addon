"""Zirkuläre 1D-Mustererkennung für Lichter, Schalter und WashData (docs/07 §3).

Ein Zustand gilt als aktiv, wenn ``state.lower()`` in
``{"on", "running", "starting", "ending"}`` liegt. Dadurch arbeitet derselbe
Algorithmus für ``light.*``, ``switch.*``, ``binary_sensor.*_running`` und
``sensor.*_state``.
"""

from __future__ import annotations

import math
from datetime import UTC, date, datetime, timedelta
from enum import Enum
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field

from hems.io.ha_core_client import HaEntityState, HaHistoryPoint
from hems.logging.verification import format_utc_ms

_ACTIVE_STATES = frozenset({"on", "running", "starting", "ending"})
_MINUTE_WINDOW = 30
_SUPPRESSION_DISTANCE_MIN = 60
_CANDIDATE_STEP_MIN = 5


class DayType(str, Enum):
    WEEKDAY = "weekday"
    WEEKEND = "weekend"


class LearnedPattern(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    pattern_id: str
    entity_id: str
    friendly_name: str
    domain: str
    day_type: DayType
    typical_on_minute: int = Field(ge=0, le=1439)
    typical_off_minute: int | None = Field(default=None, ge=0, le=1439)
    avg_duration_min: float = Field(ge=0.0)
    std_dev_min: float = Field(ge=0.0)
    confidence: float = Field(ge=0.0, le=1.0)
    sample_count: int = Field(ge=1)
    estimated_power_w: float = Field(ge=0.0)
    updated_at_utc: str


def _day_type(weekday: int) -> DayType:
    return DayType.WEEKDAY if weekday < 5 else DayType.WEEKEND


def _circular_distance(a: int, b: int) -> int:
    delta = abs(a - b)
    return min(delta, 1440 - delta)


def _circular_stats(minutes: list[int]) -> tuple[int, float]:
    """Zirkulärer Mittelwert (int) und zirkuläre Standardabweichung in Minuten."""
    count = len(minutes)
    sin_sum = sum(math.sin(2.0 * math.pi * minute / 1440.0) for minute in minutes)
    cos_sum = sum(math.cos(2.0 * math.pi * minute / 1440.0) for minute in minutes)
    angle = math.atan2(sin_sum, cos_sum) % (2.0 * math.pi)
    mean = int(round(angle * 1440.0 / (2.0 * math.pi))) % 1440
    length = math.hypot(sin_sum, cos_sum) / count
    if length <= 1e-9:
        return mean, 360.0
    std = math.sqrt(-2.0 * math.log(min(length, 1.0))) * 1440.0 / (2.0 * math.pi)
    return mean, std


def _to_local(timestamp: str, tz: ZoneInfo) -> datetime | None:
    try:
        value = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except ValueError:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(tz)


def analyze_entities(
    states: list[HaEntityState], extra_entity_ids: list[str] | None = None
) -> list[str]:
    """Wählt light/switch (ohne hems_*), WashData-Sensoren und Extra-Entitäten."""
    selected: list[str] = []
    for state in states:
        entity_id = state.entity_id
        if entity_id.startswith("hems_"):
            continue
        if state.domain in {"light", "switch"}:
            selected.append(entity_id)
        elif entity_id.endswith("_running") or entity_id.endswith("_state"):
            if entity_id.startswith(("binary_sensor.", "sensor.")):
                selected.append(entity_id)
    for entity_id in extra_entity_ids or []:
        if entity_id and entity_id not in selected:
            selected.append(entity_id)
    return sorted(selected)


def _extract_events(
    points: list[HaHistoryPoint], tz: ZoneInfo, start_local: datetime
) -> dict[DayType, list[tuple[date, int, float]]]:
    converted: list[tuple[bool, datetime]] = []
    for point in points:
        local = _to_local(point.last_changed_utc, tz)
        if local is not None:
            converted.append((point.state.lower() in _ACTIVE_STATES, local))
    converted.sort(key=lambda item: item[1])
    if converted and converted[0][0]:
        converted.insert(0, (False, start_local))

    events: dict[DayType, list[tuple[date, int, float]]] = {
        DayType.WEEKDAY: [],
        DayType.WEEKEND: [],
    }
    previous = False
    on_dt: datetime | None = None
    for active, local_dt in converted:
        if active and not previous:
            on_dt = local_dt
        elif not active and previous and on_dt is not None:
            duration = (local_dt - on_dt).total_seconds() / 60.0
            duration = min(720.0, duration) if duration > 0.0 else 60.0
            if duration >= 2.0:
                day_type = _day_type(on_dt.weekday())
                events[day_type].append((on_dt.date(), on_dt.hour * 60 + on_dt.minute, duration))
            on_dt = None
        previous = active
    return events


def _total_days(start_local: datetime, end_local: datetime) -> dict[DayType, int]:
    totals = {DayType.WEEKDAY: 0, DayType.WEEKEND: 0}
    day = start_local.date()
    while day <= end_local.date():
        totals[_day_type(day.weekday())] += 1
        day += timedelta(days=1)
    return {key: max(1, value) for key, value in totals.items()}


def _estimate_power(
    entity_id: str,
    domain: str,
    nominal_power: dict[str, float],
    washdata_power: dict[str, float],
    current_power: dict[str, float],
) -> float:
    if entity_id in washdata_power:
        return max(0.0, washdata_power[entity_id])
    if entity_id in nominal_power:
        return max(0.0, nominal_power[entity_id])
    measured = current_power.get(entity_id, 0.0)
    if measured > 0.0:
        return measured
    if domain == "light":
        return 15.0
    if domain == "switch":
        return 100.0
    if entity_id.endswith("_state") or entity_id.endswith("_running"):
        return 800.0
    return 50.0


def _cluster_day_type(
    entity_id: str,
    friendly_name: str,
    domain: str,
    events: list[tuple[date, int, float]],
    day_type: DayType,
    total_days: int,
    now_utc: str,
    estimated_power_w: float,
) -> list[LearnedPattern]:
    candidates: list[tuple[int, float, int, float]] = []
    for center in range(0, 1440, _CANDIDATE_STEP_MIN):
        nearest: dict[date, tuple[int, tuple[date, int, float]]] = {}
        for event in events:
            distance = _circular_distance(event[1], center)
            if distance > _MINUTE_WINDOW:
                continue
            current = nearest.get(event[0])
            if current is None or distance < current[0]:
                nearest[event[0]] = (distance, event)
        if not nearest:
            continue
        selected = [item[1] for item in nearest.values()]
        mean, std = _circular_stats([event[1] for event in selected])
        avg_duration = sum(event[2] for event in selected) / len(selected)
        candidates.append((len(selected), std, mean, avg_duration))

    candidates.sort(key=lambda item: (-item[0], item[1]))
    kept: list[tuple[int, float, int, float]] = []
    for candidate in candidates:
        if all(_circular_distance(candidate[2], other[2]) >= _SUPPRESSION_DISTANCE_MIN
               for other in kept):
            kept.append(candidate)

    patterns: list[LearnedPattern] = []
    for sample_count, std, mean, avg_duration in kept:
        confidence = round(sample_count / total_days, 3)
        if sample_count < 3 or confidence < 0.50 or std > 35.0:
            continue
        typical_off = (mean + round(avg_duration)) % 1440 if avg_duration > 0.0 else None
        patterns.append(
            LearnedPattern(
                pattern_id=f"{entity_id}_{day_type.value}_{mean:04d}",
                entity_id=entity_id,
                friendly_name=friendly_name,
                domain=domain,
                day_type=day_type,
                typical_on_minute=mean,
                typical_off_minute=typical_off,
                avg_duration_min=avg_duration,
                std_dev_min=std,
                confidence=confidence,
                sample_count=sample_count,
                estimated_power_w=estimated_power_w,
                updated_at_utc=now_utc,
            )
        )
    return patterns


def learn_patterns(
    histories: dict[str, list[HaHistoryPoint]],
    entity_meta: dict[str, tuple[str, str]],
    start_local: datetime,
    end_local: datetime,
    tz: ZoneInfo,
    now_utc: datetime | None = None,
    nominal_power: dict[str, float] | None = None,
    washdata_power: dict[str, float] | None = None,
    current_power: dict[str, float] | None = None,
) -> list[LearnedPattern]:
    """Lernt zirkuläre Einschaltmuster pro Entität und Tagtyp."""
    totals = _total_days(start_local, end_local)
    timestamp = format_utc_ms(now_utc or datetime.now(UTC))
    nominal = nominal_power or {}
    washdata = washdata_power or {}
    measured = current_power or {}
    patterns: list[LearnedPattern] = []
    for entity_id, points in histories.items():
        friendly_name, domain = entity_meta.get(entity_id, (entity_id, ""))
        power = _estimate_power(entity_id, domain, nominal, washdata, measured)
        events = _extract_events(points, tz, start_local)
        for day_type in (DayType.WEEKDAY, DayType.WEEKEND):
            patterns.extend(
                _cluster_day_type(
                    entity_id,
                    friendly_name,
                    domain,
                    events[day_type],
                    day_type,
                    totals[day_type],
                    timestamp,
                    power,
                )
            )
    return sorted(patterns, key=lambda item: (item.entity_id, item.day_type.value,
                                              item.typical_on_minute))
