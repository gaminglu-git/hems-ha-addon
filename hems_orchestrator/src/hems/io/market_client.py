"""Asynchroner, cachender Client für externe Strompreis- & EE-Prognosen (docs/09, Phase 8).

Primärquelle Energy-Charts (Fraunhofer ISE), Fallback aWATTar, optional HA-Sensor.
Alle HTTP-Calls laufen nicht-blockierend über ``httpx.AsyncClient(timeout=10.0)``;
die deterministische Zeitquelle ist das injizierte ``Clock``-Protocol
(``hems.logging.verification``). Fehler werden strukturiert geloggt und führen
deterministisch auf den letzten Cache bzw. den MQTT-Wert zurück (offlinefähig).
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

import httpx
from pydantic import BaseModel, ConfigDict

from hems.config import AppConfig
from hems.logging.verification import Clock, RealClock

logger = logging.getLogger(__name__)

_ENERGY_CHARTS_PRICE_URL = "https://api.energy-charts.info/price"
_ENERGY_CHARTS_REN_URL = "https://api.energy-charts.info/ren_share_forecast"
_AWATTAR_URL = "https://api.awattar.de/v1/marketdata"

_SLOT_COUNT = 24
_SLOT_MINUTES = 15
# Untergrenze/Maximalwert der CO₂-Intensität in g/kWh (docs/09 §5.3).
_CO2_MIN_G_KWH = 40.0
_CO2_MAX_G_KWH = 650.0


class MarketSlot(BaseModel):
    """Einzelner 15-Minuten-Slot des Marktprognose-Vektors (docs/09 §5.1)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    start_utc: str
    spot_eur_mwh: float
    gross_ct_kwh: float
    renewable_share_pct: float | None = None
    co2_intensity_g_kwh: float | None = None


class MarketForecast(BaseModel):
    """24-Slot-Prognosevektor für EMHASS (6 h, 15-Minuten-Raster)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    generated_at_utc: str
    source: Literal["energy_charts", "awattar", "ha_sensor", "cache"]
    slots: list[MarketSlot]
    load_cost_forecast_eur_kwh: list[float]
    co2_forecast_g_kwh: list[float]


def _floor_15min(value: datetime) -> datetime:
    """Rundet einen UTC-Zeitstempel auf den laufenden 15-Minuten-Raster ab."""
    return value.replace(
        minute=(value.minute // _SLOT_MINUTES) * _SLOT_MINUTES, second=0, microsecond=0
    )


def _parse_series(payload: Any, value_keys: tuple[str, ...]) -> dict[datetime, float | None]:
    """Extrahiert eine ``{unix_seconds: [...], <value_key>: [...]}``-Serie.

    Defensiv gegenüber dem Energy-Charts-Schema: Werte können direkt oder unter
    einem ``forecast``-Unterarray stehen; ungültige Einträge werden verworfen.
    """
    if not isinstance(payload, dict):
        return {}
    raw_ts = payload.get("unix_seconds")
    if not isinstance(raw_ts, list) or not raw_ts:
        return {}
    raw_values: list[Any] | None = None
    for key in value_keys:
        candidate = payload.get(key)
        if isinstance(candidate, list):
            raw_values = candidate
            break
    if raw_values is None:
        return {}
    series: dict[datetime, float | None] = {}
    for index, raw_ts_value in enumerate(raw_ts):
        if index >= len(raw_values):
            break
        try:
            timestamp = datetime.fromtimestamp(float(raw_ts_value), tz=UTC)
        except (TypeError, ValueError, OverflowError, OSError) as exc:
            logger.warning("Energy-Charts: skipping invalid timestamp %r: %s", raw_ts_value, exc)
            continue
        raw_value = raw_values[index]
        if raw_value is None:
            series[timestamp] = None
            continue
        try:
            series[timestamp] = float(raw_value)
        except (TypeError, ValueError) as exc:
            logger.warning("Energy-Charts: skipping invalid value %r: %s", raw_value, exc)
            series[timestamp] = None
    return series


class MarketDataClient:
    """Cachender Client für Day-Ahead-Preis & EE-Anteil (docs/09 §5)."""

    def __init__(
        self,
        config: AppConfig,
        clock: Clock | None = None,
        ha_client: Any | None = None,
    ) -> None:
        self._config = config
        self._clock: Clock = clock if clock is not None else RealClock()
        self._ha_client = ha_client
        self._lock = asyncio.Lock()
        self._forecast: MarketForecast | None = None
        self._last_refresh_utc: datetime | None = None

    # -- öffentliche API ------------------------------------------------------------

    def get_forecast(self) -> MarketForecast | None:
        """Liefert den zuletzt erzeugten Forecast (kein HTTP, MPC-Pfad-tauglich)."""
        return self._forecast

    async def refresh(self, *, force: bool = False) -> MarketForecast | None:
        """Aktualisiert den Cache; respektiert ``market_refresh_interval_s``.

        Bei Ablauf/``force`` wird die Fallback-Kette durchlaufen. Bei Fehler wird
        der letzte erfolgreiche Vektor (source="cache") beibehalten.
        """
        if self._config.tariff_source == "mqtt_only":
            return self._forecast
        async with self._lock:
            now = self._clock.now_utc()
            if (
                not force
                and self._forecast is not None
                and self._last_refresh_utc is not None
                and (now - self._last_refresh_utc).total_seconds()
                < self._config.market_refresh_interval_s
            ):
                return self._forecast
            forecast = await self._build_forecast(now)
            if forecast is not None:
                self._forecast = forecast
                self._last_refresh_utc = now
            return self._forecast

    # -- Fallback-Kette -------------------------------------------------------------

    async def _build_forecast(self, now: datetime) -> MarketForecast | None:
        source = self._config.tariff_source
        if source == "ha_sensor":
            forecast = await self._fetch_ha_sensor(now)
            if forecast is not None:
                return forecast
            return self._as_cache()
        if source == "awattar":
            forecast = await self._fetch_awattar(now)
            if forecast is not None:
                return forecast
            return self._as_cache()
        # Primärquelle energy_charts, automatischer Fallback aWATTar.
        forecast = await self._fetch_energy_charts(now)
        if forecast is not None:
            return forecast
        forecast = await self._fetch_awattar(now)
        if forecast is not None:
            return forecast
        return self._as_cache()

    def _as_cache(self) -> MarketForecast | None:
        """Markiert den letzten erfolgreichen Vektor als Cache-Quelle."""
        if self._forecast is None:
            return None
        cached = self._forecast
        return cached.model_copy(update={"source": "cache"})

    # -- Quellen --------------------------------------------------------------------

    async def _fetch_energy_charts(self, now: datetime) -> MarketForecast | None:
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                price_response = await client.get(
                    _ENERGY_CHARTS_PRICE_URL, params={"bzn": "DE-LU"}
                )
                price_response.raise_for_status()
                price_payload = price_response.json()
                ren_response = await client.get(
                    _ENERGY_CHARTS_REN_URL, params={"country": "de"}
                )
                ren_response.raise_for_status()
                ren_payload = ren_response.json()
        except httpx.HTTPError as exc:
            logger.error("Energy-Charts request failed: %s", exc)
            return None
        except ValueError as exc:
            logger.error("Energy-Charts returned invalid JSON: %s", exc)
            return None

        price_series = _parse_series(price_payload, ("price", "values"))
        ren_series = _parse_series(ren_payload, ("ren_share", "values", "forecast"))
        if not price_series:
            logger.error("Energy-Charts price series empty or invalid")
            return None

        points: list[tuple[datetime, float, float | None]] = [
            (timestamp, spot, ren_series.get(timestamp))
            for timestamp, spot in price_series.items()
            if spot is not None
        ]
        if not points:
            logger.error("Energy-Charts price series has no valid values")
            return None
        return self._assemble(now, "energy_charts", points)

    async def _fetch_awattar(self, now: datetime) -> MarketForecast | None:
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.get(_AWATTAR_URL)
                response.raise_for_status()
                payload = response.json()
        except httpx.HTTPError as exc:
            logger.error("aWATTar request failed: %s", exc)
            return None
        except ValueError as exc:
            logger.error("aWATTar returned invalid JSON: %s", exc)
            return None

        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, list) or not data:
            logger.error("aWATTar payload missing 'data' array")
            return None

        points: list[tuple[datetime, float, float | None]] = []
        for entry in data:
            if not isinstance(entry, dict):
                continue
            try:
                start_ms = float(entry["start_timestamp"])
                marketprice = float(entry["marketprice"])
            except (KeyError, TypeError, ValueError) as exc:
                logger.warning("aWATTar: skipping invalid entry %r: %s", entry, exc)
                continue
            start = datetime.fromtimestamp(start_ms / 1000.0, tz=UTC)
            # Stundenwert auf 4 × 15-Minuten-Slots abbilden (docs/09 §3.3).
            for quarter in range(4):
                points.append(
                    (start + timedelta(minutes=_SLOT_MINUTES * quarter), marketprice, None)
                )
        if not points:
            logger.error("aWATTar data array has no valid entries")
            return None
        return self._assemble(now, "awattar", points)

    async def _fetch_ha_sensor(self, now: datetime) -> MarketForecast | None:
        entity_id = self._config.ha_tariff_entity_id
        if not entity_id or self._ha_client is None:
            logger.error("ha_sensor source selected but no entity_id/ha_client configured")
            return None
        try:
            states = await self._ha_client.get_states()
        except Exception as exc:  # noqa: BLE001 - HA-Fehler -> Fallback
            logger.error("ha_sensor state read failed: %s", exc)
            return None
        gross_ct_kwh: float | None = None
        for state in states:
            if state.entity_id != entity_id:
                continue
            try:
                gross_ct_kwh = float(state.state)
            except (TypeError, ValueError) as exc:
                logger.error(
                    "ha_sensor entity %s has non-numeric state %r: %s",
                    entity_id,
                    state.state,
                    exc,
                )
                return None
            break
        if gross_ct_kwh is None:
            logger.error("ha_sensor entity %s not found", entity_id)
            return None

        start = _floor_15min(now)
        slots: list[MarketSlot] = []
        for index in range(_SLOT_COUNT):
            slot_start = start + timedelta(minutes=_SLOT_MINUTES * index)
            slots.append(
                MarketSlot(
                    start_utc=slot_start.isoformat(),
                    spot_eur_mwh=gross_ct_kwh * 10.0,
                    gross_ct_kwh=gross_ct_kwh,
                    renewable_share_pct=None,
                    # Kein EE-Anteil aus HA-Sensor -> CO2 aus MQTT/Cache (None).
                    co2_intensity_g_kwh=None,
                )
            )
        return self._finalize(now, "ha_sensor", slots)

    def _ha_co2_default(self) -> float:
        """CO₂-Fallback für ha_sensor: letzter Cache-Wert oder 0.0 (unbekannt)."""
        if self._forecast is not None and self._forecast.co2_forecast_g_kwh:
            return self._forecast.co2_forecast_g_kwh[0]
        return 0.0

    # -- Aufbereitung ---------------------------------------------------------------

    def _gross_ct_kwh(self, spot_eur_mwh: float) -> float:
        """Brutto-Endkundenpreis in ct/kWh (docs/09 §3.1), deterministisch >= 0."""
        return max(
            0.0,
            (spot_eur_mwh / 10.0 + self._config.tariff_grid_fees_ct_kwh)
            * (1.0 + self._config.tariff_vat_pct / 100.0),
        )

    def _co2_g_kwh(self, renewable_share_pct: float) -> float:
        """CO₂-Intensität in g/kWh aus EE-Anteil (docs/09 §5.3)."""
        return max(
            _CO2_MIN_G_KWH,
            min(_CO2_MAX_G_KWH, _CO2_MAX_G_KWH * (1.0 - renewable_share_pct / 100.0)),
        )

    def _assemble(
        self,
        now: datetime,
        source: Literal["energy_charts", "awattar"],
        points: list[tuple[datetime, float, float | None]],
    ) -> MarketForecast | None:
        """Baut 24 an das aktuelle 15-Minuten-Raster gekoppelte Slots."""
        lookup: dict[datetime, tuple[float, float | None]] = {
            _floor_15min(timestamp): (spot, ren) for timestamp, spot, ren in points
        }
        start = _floor_15min(now)
        slots: list[MarketSlot] = []
        last_spot: float | None = None
        last_ren: float | None = None
        for index in range(_SLOT_COUNT):
            slot_start = start + timedelta(minutes=_SLOT_MINUTES * index)
            if slot_start in lookup:
                spot, ren = lookup[slot_start]
                last_spot = spot
                if ren is not None:
                    last_ren = ren
            else:
                # Fortschreibung des letzten bekannten Slotwerts (docs/09 §3.2).
                spot = last_spot
            if spot is None:
                # Noch kein Datenpunkt am/ab Startzeitpunkt -> keine Slots.
                break
            slots.append(
                MarketSlot(
                    start_utc=slot_start.isoformat(),
                    spot_eur_mwh=spot,
                    gross_ct_kwh=self._gross_ct_kwh(spot),
                    renewable_share_pct=last_ren,
                    co2_intensity_g_kwh=(
                        self._co2_g_kwh(last_ren) if last_ren is not None else None
                    ),
                )
            )
        if not slots:
            logger.error("Market forecast assembly produced no slots")
            return None
        return self._finalize(now, source, slots)

    def _finalize(
        self,
        now: datetime,
        source: Literal["energy_charts", "awattar", "ha_sensor"],
        slots: list[MarketSlot],
    ) -> MarketForecast:
        load_cost = [slot.gross_ct_kwh / 100.0 for slot in slots]
        co2_forecast: list[float] = []
        last_co2 = 0.0
        for slot in slots:
            if slot.co2_intensity_g_kwh is not None:
                last_co2 = slot.co2_intensity_g_kwh
            co2_forecast.append(last_co2)
        # Vektor auf exakt 24 Werte auffüllen (Fortschreibung des letzten Werts).
        while len(load_cost) < _SLOT_COUNT:
            load_cost.append(load_cost[-1])
            co2_forecast.append(co2_forecast[-1])
        return MarketForecast(
            generated_at_utc=now.isoformat(),
            source=source,
            slots=slots,
            load_cost_forecast_eur_kwh=load_cost[:_SLOT_COUNT],
            co2_forecast_g_kwh=co2_forecast[:_SLOT_COUNT],
        )
