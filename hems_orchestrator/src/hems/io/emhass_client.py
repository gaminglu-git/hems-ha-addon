"""Stateless EMHASS-REST-Client mit kanonischem Parameter-Hashing (docs/03 §2).

Wichtig (Fix B2): EMHASS liefert P_batt > 0 = Entladung, P_batt < 0 = Ladung.
Beim Parsen wird das Vorzeichen zwingend invertiert: battery_w = -P_batt[0].
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from typing import Any, Literal

import httpx
from pydantic import BaseModel, ConfigDict

from hems.config import AppConfig, OperatingMode
from hems.io.market_client import MarketForecast
from hems.models import ProposedSetpoints, SystemInputs

logger = logging.getLogger(__name__)

_PREDICTION_HORIZON = 24
_FEEDIN_TARIFF_EUR_KWH = 0.08
# Modellannahme (docs/03 §2, Modus C): CO₂-Bepreisung für den effektiven Kostenterm.
_CO2_COST_EUR_PER_KG = 0.05
# Thermische Modellkonstanten konsistent zum PlantSimulator (docs/04 §1.2).
_BUFFER_CAPACITY_WH_PER_K = 1500.0
_HEATPUMP_COP = 3.5


class EmhassResult(BaseModel):
    """Ergebnis eines EMHASS-Aufrufs (deterministische Fehlerzustände inklusive)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    status: Literal["ok", "timeout", "error"]
    solve_ms: int | None
    setpoints: ProposedSetpoints | None
    runtimeparams_hash: str | None
    wallbox_schedule_w: list[float] | None = None
    heatpump_schedule_w: list[float] | None = None
    battery_schedule_w: list[float] | None = None


def build_runtimeparams(
    inputs: SystemInputs,
    config: AppConfig,
    aggregated_load_w: list[float] | None = None,
    market_forecast: MarketForecast | None = None,
) -> dict[str, Any]:
    """Erzeugt das vollständige runtimeparams-Dict (kein Konfigurations-Drift).

    Ist ``market_forecast`` gesetzt, wird ``load_cost_forecast`` aus dem
    zeitabhängigen 24-Slot-Vektor (EUR/kWh) gebildet; im Modus C zuzüglich der
    zeitabhängigen CO2-Gewichtung. Ohne Forecast bleibt das Dict bitgleich.
    """
    tariff_eur_kwh = inputs.tariff_ct_kwh / 100.0
    prod_price = 0.0 if inputs.tariff_ct_kwh < 0.0 else _FEEDIN_TARIFF_EUR_KWH

    if config.operating_mode is OperatingMode.C_GRID_SUPPORTIVE:
        # Zusätzliche CO₂-Gewichtung über den effektiven Kostenterm (Modus C).
        effective_cost = tariff_eur_kwh + (
            inputs.co2_intensity_g_kwh / 1000.0
        ) * _CO2_COST_EUR_PER_KG
    else:
        effective_cost = tariff_eur_kwh

    # Benötigte Laufstunden der deferrable Lasten (Wallbox, Wärmepumpe).
    wallbox_hours = 0.0
    if inputs.ev_connected and inputs.soc_ev_pct < config.ev_target_soc_pct:
        e_missing_wh = (
            (config.ev_target_soc_pct - inputs.soc_ev_pct) / 100.0
        ) * config.ev_capacity_wh
        wallbox_hours = e_missing_wh / max(config.wallbox_max_power_w, 1.0)
    heatpump_hours = 0.0
    if inputs.t_buffer_c < config.heatpump_temp_min_c and config.heatpump_nominal_power_w > 0.0:
        e_thermal_wh = (config.heatpump_temp_min_c - inputs.t_buffer_c) * _BUFFER_CAPACITY_WH_PER_K
        heatpump_hours = (e_thermal_wh / _HEATPUMP_COP) / config.heatpump_nominal_power_w

    if aggregated_load_w and any(value > 0.0 for value in aggregated_load_w):
        load_forecast = [float(value) for value in aggregated_load_w[:_PREDICTION_HORIZON]]
        load_forecast.extend([load_forecast[-1]] * (_PREDICTION_HORIZON - len(load_forecast)))
    else:
        load_forecast = [inputs.p_house_base_w] * _PREDICTION_HORIZON

    # Ohne Forecast bitgleich: konstanter effektiver Kostenterm über alle Slots.
    # Mit Forecast: zeitabhängiger 24-Slot-Vektor (EUR/kWh), Modus C + CO2-Gewichtung.
    if market_forecast is not None and market_forecast.load_cost_forecast_eur_kwh:
        load_cost_forecast = list(market_forecast.load_cost_forecast_eur_kwh[:_PREDICTION_HORIZON])
        if config.operating_mode is OperatingMode.C_GRID_SUPPORTIVE:
            co2_vector = market_forecast.co2_forecast_g_kwh
            load_cost_forecast = [
                cost + (co2_vector[index] / 1000.0) * _CO2_COST_EUR_PER_KG
                if index < len(co2_vector)
                else cost
                for index, cost in enumerate(load_cost_forecast)
            ]
        load_cost_forecast.extend(
            [load_cost_forecast[-1]] * (_PREDICTION_HORIZON - len(load_cost_forecast))
        )
    else:
        load_cost_forecast = [effective_cost] * _PREDICTION_HORIZON

    params: dict[str, Any] = {
        "prediction_horizon": _PREDICTION_HORIZON,
        "time_step": 15,
        "soc_init": inputs.soc_battery_pct / 100.0,
        "soc_final": 0.20,
        "load_cost_forecast": load_cost_forecast,
        "prod_price_forecast": [prod_price] * _PREDICTION_HORIZON,
        "pv_power_forecast": [inputs.p_pv_w] * _PREDICTION_HORIZON,
        "load_power_forecast": load_forecast,
        "def_total_hours": [wallbox_hours, heatpump_hours],
        "number_of_deferrable_loads": 2,
        "nominal_power_of_deferrable_loads": [
            config.wallbox_max_power_w,
            config.heatpump_nominal_power_w,
        ],
        "alpha": 0.5,
        "beta": 0.5,
        "weight_battery_discharge": config.battery_cycle_cost_ct_kwh / 100.0,
        "costfun": "profit",
    }

    if config.operating_mode is OperatingMode.B_MARKET_ORIENTED:
        params["maximum_power_from_grid"] = config.house_fuse_limit_w
    elif config.operating_mode is OperatingMode.C_GRID_SUPPORTIVE:
        params["maximum_power_from_grid"] = (
            inputs.dso_14a_limit_w
            if inputs.dso_14a_active
            else min(config.house_fuse_limit_w, 8000.0)
        )
        params["maximum_power_to_grid"] = config.pv_peak_w * config.eeg_feedin_limit_pct / 100.0

    return params


def canonical_runtimeparams_hash(runtimeparams: dict[str, Any]) -> str:
    """Erste 8 Zeichen des SHA-256 Hex-Digests der kanonischen runtimeparams."""
    canonical_json = json.dumps(runtimeparams, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()[:8]


def _parse_setpoints(response_json: Any) -> ProposedSetpoints | None:
    if not isinstance(response_json, dict):
        return None
    columns: dict[str, float] = {}
    for key in ("P_batt", "P_deferrable0", "P_deferrable1"):
        value = response_json.get(key)
        if not isinstance(value, list) or not value:
            return None
        try:
            columns[key] = float(value[0])
        except (TypeError, ValueError):
            return None
    return ProposedSetpoints(
        # Fix B2: EMHASS P_batt > 0 = Entladung -> Vorzeicheninvertierung.
        battery_w=-columns["P_batt"],
        wallbox_w=max(0.0, columns["P_deferrable0"]),
        heatpump_w=max(0.0, columns["P_deferrable1"]),
    )


def _parse_schedule(
    response_json: Any, key: str, *, clamp_positive: bool, invert: bool = False
) -> list[float] | None:
    """Extrahiert einen 24-Slot-Fahrplan aus einer EMHASS-Spalte."""
    if not isinstance(response_json, dict):
        return None
    raw = response_json.get(key)
    if not isinstance(raw, list) or not raw:
        return None
    try:
        values = [float(value) for value in raw[:_PREDICTION_HORIZON]]
    except (TypeError, ValueError):
        return None
    if invert:
        values = [-value for value in values]
    elif clamp_positive:
        values = [max(0.0, value) for value in values]
    values.extend([values[-1]] * (_PREDICTION_HORIZON - len(values)))
    return values


class EmhassClient:
    """Async-HTTP-Client für POST /action/naive-mpc-optim mit Timeout-Handling."""

    def __init__(self, config: AppConfig) -> None:
        self._config = config

    def set_operating_mode(self, mode: OperatingMode) -> None:
        """Hält die aktive Config-Referenz synchron zur Laufzeitumschaltung."""
        self._config.operating_mode = mode

    def set_dry_run(self, dry_run: bool) -> None:
        """Hält die aktive Config-Referenz synchron zur Laufzeitumschaltung."""
        self._config.dry_run = dry_run

    async def optimize(
        self,
        inputs: SystemInputs,
        aggregated_load_w: list[float] | None = None,
        market_forecast: MarketForecast | None = None,
    ) -> EmhassResult:
        runtimeparams = build_runtimeparams(
            inputs, self._config, aggregated_load_w, market_forecast
        )
        params_hash = canonical_runtimeparams_hash(runtimeparams)
        url = f"{self._config.emhass_url}/action/naive-mpc-optim"
        start_ns = time.perf_counter_ns()
        try:
            async with httpx.AsyncClient(timeout=self._config.emhass_timeout_s) as client:
                response = await client.post(url, json=runtimeparams)
        except httpx.TimeoutException as exc:
            solve_ms = (time.perf_counter_ns() - start_ns) // 1_000_000
            logger.error("EMHASS timeout after %s ms: %s", solve_ms, exc)
            return EmhassResult(
                status="timeout", solve_ms=solve_ms, setpoints=None, runtimeparams_hash=params_hash
            )
        except httpx.HTTPError as exc:
            solve_ms = (time.perf_counter_ns() - start_ns) // 1_000_000
            logger.error("EMHASS HTTP error: %s", exc)
            return EmhassResult(
                status="error", solve_ms=solve_ms, setpoints=None, runtimeparams_hash=params_hash
            )

        solve_ms = (time.perf_counter_ns() - start_ns) // 1_000_000
        if response.status_code != 200:
            logger.error("EMHASS returned HTTP %s", response.status_code)
            return EmhassResult(
                status="error", solve_ms=solve_ms, setpoints=None, runtimeparams_hash=params_hash
            )
        try:
            response_json = response.json()
        except ValueError as exc:
            logger.error("EMHASS returned invalid JSON: %s", exc)
            return EmhassResult(
                status="error", solve_ms=solve_ms, setpoints=None, runtimeparams_hash=params_hash
            )

        setpoints = _parse_setpoints(response_json)
        if setpoints is None:
            logger.error("EMHASS response missing mandatory columns or empty lists")
            return EmhassResult(
                status="error", solve_ms=solve_ms, setpoints=None, runtimeparams_hash=params_hash
            )
        return EmhassResult(
            status="ok",
            solve_ms=solve_ms,
            setpoints=setpoints,
            runtimeparams_hash=params_hash,
            wallbox_schedule_w=_parse_schedule(response_json, "P_deferrable0", clamp_positive=True),
            heatpump_schedule_w=_parse_schedule(
                response_json, "P_deferrable1", clamp_positive=True
            ),
            battery_schedule_w=_parse_schedule(
                response_json, "P_batt", clamp_positive=False, invert=True
            ),
        )
