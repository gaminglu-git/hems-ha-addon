"""Phase-8-Gate: MarketDataClient (Energy-Charts/aWATTar/HA-Sensor) & EMHASS-Kopplung.

Ausschließlich respx-gemockte HTTP-Calls (keine echten Netzwerkzugriffe) und eine
deterministische Fake-Uhr. Deckt die Kriterien D1–D8 aus docs/09 §7 ab.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
import respx

from hems.config import AppConfig
from hems.io.emhass_client import EmhassClient, build_runtimeparams
from hems.io.market_client import MarketDataClient
from test_audit_logging import make_inputs

PRICE_URL = "https://api.energy-charts.info/price"
REN_URL = "https://api.energy-charts.info/ren_share_forecast"
AWATTAR_URL = "https://api.awattar.de/v1/marketdata"
EMHASS_URL = "http://emhass-test:5000/action/naive-mpc-optim"

_NOW = datetime(2026, 11, 24, 10, 7, 0, tzinfo=UTC)


class FakeClock:
    """Minimale Clock-Implementierung mit manuell setzbarer UTC-Zeit."""

    def __init__(self, start: datetime) -> None:
        self.current = start

    def now_utc(self) -> datetime:
        return self.current

    async def sleep(self, seconds: float) -> None:  # pragma: no cover - nicht genutzt
        self.current = self.current + timedelta(seconds=seconds)


def _make_config(**overrides: Any) -> AppConfig:
    base: dict[str, Any] = {"emhass_url": "http://emhass-test:5000"}
    base.update(overrides)
    return AppConfig.model_validate(base)


def _epoch_floor(dt: datetime) -> int:
    return int(dt.replace(second=0, microsecond=0).timestamp())


def _price_payload(start: datetime, count: int, price: float) -> dict[str, Any]:
    stamps = [_epoch_floor(start) + 900 * i for i in range(count)]
    return {"unix_seconds": stamps, "price": [price] * count}


def _ren_payload(start: datetime, count: int, share: float) -> dict[str, Any]:
    stamps = [_epoch_floor(start) + 900 * i for i in range(count)]
    return {"unix_seconds": stamps, "ren_share": [share] * count}


def _floor_15(dt: datetime) -> datetime:
    return dt.replace(minute=(dt.minute // 15) * 15, second=0, microsecond=0)


# --- D1: Energy-Charts-Parsing & Brutto-Preis/CO2-Umrechnung ------------------------


@respx.mock
async def test_energy_charts_parsing_and_conversion() -> None:
    start = _floor_15(_NOW)
    respx.get(PRICE_URL).mock(
        return_value=httpx.Response(200, json=_price_payload(start, 30, 100.0))
    )
    respx.get(REN_URL).mock(return_value=httpx.Response(200, json=_ren_payload(start, 30, 50.0)))

    client = MarketDataClient(_make_config(), clock=FakeClock(_NOW))
    forecast = await client.refresh(force=True)

    assert forecast is not None
    assert forecast.source == "energy_charts"
    assert len(forecast.slots) == 24
    assert len(forecast.load_cost_forecast_eur_kwh) == 24
    # Brutto: (100/10 + 18) * 1.19 = 33.32 ct/kWh; load_cost = ct/100.
    assert forecast.slots[0].gross_ct_kwh == pytest.approx(33.32)
    assert forecast.load_cost_forecast_eur_kwh[0] == pytest.approx(0.3332)
    # CO2: 650 * (1 - 0.5) = 325 g/kWh.
    assert forecast.slots[0].co2_intensity_g_kwh == pytest.approx(325.0)
    assert forecast.co2_forecast_g_kwh[0] == pytest.approx(325.0)


# --- D2: EE-Anteil -> CO2-Untergrenze/Obergrenze ------------------------------------


@pytest.mark.parametrize(
    ("share", "expected"),
    [(100.0, 40.0), (0.0, 650.0)],
)
@respx.mock
async def test_renewable_share_to_co2(share: float, expected: float) -> None:
    start = _floor_15(_NOW)
    respx.get(PRICE_URL).mock(
        return_value=httpx.Response(200, json=_price_payload(start, 30, 50.0))
    )
    respx.get(REN_URL).mock(
        return_value=httpx.Response(200, json=_ren_payload(start, 30, share))
    )
    client = MarketDataClient(_make_config(), clock=FakeClock(_NOW))
    forecast = await client.refresh(force=True)
    assert forecast is not None
    assert forecast.slots[0].co2_intensity_g_kwh == pytest.approx(expected)


# --- D3: Negative Spot-Preise -> realistischer Brutto-Endkundenpreis ----------------


@respx.mock
async def test_negative_price_gross_clamp() -> None:
    start = _floor_15(_NOW)
    respx.get(PRICE_URL).mock(
        return_value=httpx.Response(200, json=_price_payload(start, 30, -100.0))
    )
    respx.get(REN_URL).mock(return_value=httpx.Response(200, json=_ren_payload(start, 30, 50.0)))
    client = MarketDataClient(_make_config(), clock=FakeClock(_NOW))
    forecast = await client.refresh(force=True)
    assert forecast is not None
    # (-100/10 + 18) * 1.19 = 9.52 ct/kWh (kein negativer Endkundenpreis).
    assert forecast.slots[0].gross_ct_kwh == pytest.approx(9.52)


# --- D4: Fallback aWATTar bei HTTP-500 + Stundenwert -> 4 gleiche Slots -------------


@respx.mock
async def test_fallback_awattar_on_energy_charts_500() -> None:
    start = _floor_15(_NOW)
    respx.get(PRICE_URL).mock(return_value=httpx.Response(500, text="boom"))
    respx.get(REN_URL).mock(return_value=httpx.Response(500, text="boom"))
    hour_start_ms = int(start.timestamp() * 1000)
    respx.get(AWATTAR_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "data": [
                    {"start_timestamp": hour_start_ms, "marketprice": 80.0},
                    {"start_timestamp": hour_start_ms + 3_600_000, "marketprice": 90.0},
                ]
            },
        )
    )
    client = MarketDataClient(_make_config(), clock=FakeClock(_NOW))
    forecast = await client.refresh(force=True)

    assert forecast is not None
    assert forecast.source == "awattar"
    assert len(forecast.slots) == 24
    # Stundenwert 80 EUR/MWh -> 4 gleiche Viertelstunden-Slots.
    first_four = [slot.spot_eur_mwh for slot in forecast.slots[:4]]
    assert first_four == [80.0, 80.0, 80.0, 80.0]
    # (80/10 + 18) * 1.19 = 30.94 ct/kWh.
    assert forecast.slots[0].gross_ct_kwh == pytest.approx(30.94)
    # Kein EE-Anteil bei aWATTar -> CO2 aus Fallback (0.0 wenn kein Cache).
    assert forecast.slots[0].co2_intensity_g_kwh is None
    assert forecast.co2_forecast_g_kwh[0] == 0.0


@respx.mock
async def test_fallback_awattar_on_timeout() -> None:
    start = _floor_15(_NOW)
    respx.get(PRICE_URL).mock(side_effect=httpx.ConnectTimeout("timeout"))
    respx.get(REN_URL).mock(side_effect=httpx.ConnectTimeout("timeout"))
    hour_start_ms = int(start.timestamp() * 1000)
    respx.get(AWATTAR_URL).mock(
        return_value=httpx.Response(
            200, json={"data": [{"start_timestamp": hour_start_ms, "marketprice": 50.0}]}
        )
    )
    client = MarketDataClient(_make_config(), clock=FakeClock(_NOW))
    forecast = await client.refresh(force=True)
    assert forecast is not None
    assert forecast.source == "awattar"


# --- Fortschreibung über Mitternacht (fehlende Folgetag-Slots) ----------------------


@respx.mock
async def test_forward_fill_after_midnight() -> None:
    late = datetime(2026, 11, 24, 23, 52, 0, tzinfo=UTC)
    start = _floor_15(late)  # 23:45
    # Nur ein Slot (23:45) vorhanden, Rest des Horizonts (Folgetag) fehlt.
    respx.get(PRICE_URL).mock(
        return_value=httpx.Response(200, json=_price_payload(start, 1, 120.0))
    )
    respx.get(REN_URL).mock(return_value=httpx.Response(200, json=_ren_payload(start, 1, 40.0)))
    client = MarketDataClient(_make_config(), clock=FakeClock(late))
    forecast = await client.refresh(force=True)

    assert forecast is not None
    assert len(forecast.slots) == 24
    assert forecast.slots[0].start_utc == "2026-11-24T23:45:00+00:00"
    # Alle Slots über Mitternacht tragen den letzten bekannten Wert fort.
    assert all(slot.spot_eur_mwh == 120.0 for slot in forecast.slots)
    assert forecast.slots[-1].start_utc == "2026-11-25T05:30:00+00:00"


# --- D5: Offline-Fallback (Cache bzw. None -> MQTT-Wert) ----------------------------


@respx.mock
async def test_offline_returns_cache_then_none() -> None:
    start = _floor_15(_NOW)
    price_route = respx.get(PRICE_URL).mock(
        return_value=httpx.Response(200, json=_price_payload(start, 30, 100.0))
    )
    respx.get(REN_URL).mock(return_value=httpx.Response(200, json=_ren_payload(start, 30, 50.0)))
    clock = FakeClock(_NOW)
    client = MarketDataClient(_make_config(), clock=clock)

    first = await client.refresh(force=True)
    assert first is not None and first.source == "energy_charts"

    # Beides Quellen down -> letzter Cache (source="cache").
    price_route.mock(return_value=httpx.Response(503, text="down"))
    clock.current = _NOW + timedelta(seconds=2000)
    respx.get(REN_URL).mock(return_value=httpx.Response(503, text="down"))
    respx.get(AWATTAR_URL).mock(return_value=httpx.Response(503, text="down"))
    cached = await client.refresh(force=True)
    assert cached is not None
    assert cached.source == "cache"
    assert cached.load_cost_forecast_eur_kwh[0] == pytest.approx(0.3332)


@respx.mock
async def test_offline_no_cache_returns_none_no_crash() -> None:
    respx.get(PRICE_URL).mock(side_effect=httpx.ConnectError("down"))
    respx.get(REN_URL).mock(side_effect=httpx.ConnectError("down"))
    respx.get(AWATTAR_URL).mock(side_effect=httpx.ConnectError("down"))
    client = MarketDataClient(_make_config(), clock=FakeClock(_NOW))
    # Kein Crash, aber kein Forecast -> LiveState/EmhassClient nutzen MQTT-Wert.
    assert await client.refresh(force=True) is None
    assert client.get_forecast() is None


# --- D6: Caching (kein HTTP innerhalb market_refresh_interval_s) --------------------


@respx.mock
async def test_caching_respects_refresh_interval() -> None:
    start = _floor_15(_NOW)
    price_route = respx.get(PRICE_URL).mock(
        return_value=httpx.Response(200, json=_price_payload(start, 30, 100.0))
    )
    respx.get(REN_URL).mock(return_value=httpx.Response(200, json=_ren_payload(start, 30, 50.0)))
    clock = FakeClock(_NOW)
    client = MarketDataClient(_make_config(), clock=clock)

    await client.refresh(force=True)
    calls_after_first = price_route.call_count
    assert calls_after_first == 1

    # Zweiter Refresh innerhalb 1800 s -> kein weiterer HTTP-Call.
    clock.current = _NOW + timedelta(seconds=600)
    await client.refresh(force=False)
    assert price_route.call_count == calls_after_first

    # force=True erzwingt neuen Abruf.
    await client.refresh(force=True)
    assert price_route.call_count == calls_after_first + 1

    # Nach Ablauf des Intervalls -> erneuter Abruf ohne force.
    clock.current = _NOW + timedelta(seconds=600 + 1801)
    await client.refresh(force=False)
    assert price_route.call_count == calls_after_first + 2


# --- tariff_source == "mqtt_only": kein HTTP, kein Forecast -----------------------


@respx.mock
async def test_mqtt_only_source_skips_http() -> None:
    route = respx.get(PRICE_URL).mock(return_value=httpx.Response(200, json={}))
    client = MarketDataClient(_make_config(tariff_source="mqtt_only"), clock=FakeClock(_NOW))
    assert await client.refresh(force=True) is None
    assert route.call_count == 0


# --- HA-Sensor als Quelle -----------------------------------------------------------


class _FakeHaClient:
    def __init__(self, state: str) -> None:
        self._state = state

    async def get_states(self) -> list[Any]:
        class _S:
            entity_id = "sensor.tibber_price"
            state = self._state

        return [_S()]


async def test_ha_sensor_source_constant_all_slots() -> None:
    config = _make_config(
        tariff_source="ha_sensor", ha_tariff_entity_id="sensor.tibber_price"
    )
    client = MarketDataClient(config, clock=FakeClock(_NOW), ha_client=_FakeHaClient("25.5"))
    forecast = await client.refresh(force=True)
    assert forecast is not None
    assert forecast.source == "ha_sensor"
    assert len(forecast.slots) == 24
    assert all(slot.gross_ct_kwh == pytest.approx(25.5) for slot in forecast.slots)


# --- LiveState.apply_market_forecast & EMHASS-load_cost_forecast-Übergabe -----------


@respx.mock
async def test_apply_market_forecast_updates_live_state_inputs() -> None:
    from hems.io.mqtt_client import LiveState

    start = _floor_15(_NOW)
    respx.get(PRICE_URL).mock(
        return_value=httpx.Response(200, json=_price_payload(start, 30, 100.0))
    )
    respx.get(REN_URL).mock(return_value=httpx.Response(200, json=_ren_payload(start, 30, 50.0)))
    config = _make_config()
    client = MarketDataClient(config, clock=FakeClock(_NOW))
    forecast = await client.refresh(force=True)
    assert forecast is not None

    state = LiveState(config)
    state.set_inputs(make_inputs())
    state.apply_market_forecast(forecast)
    assert state.inputs is not None
    assert state.tariff_ct_kwh == pytest.approx(33.32)
    assert state.co2_intensity_g_kwh == pytest.approx(325.0)


@respx.mock
async def test_emhass_receives_24_slot_load_cost_forecast() -> None:
    start = _floor_15(_NOW)
    respx.get(PRICE_URL).mock(
        return_value=httpx.Response(200, json=_price_payload(start, 30, 100.0))
    )
    respx.get(REN_URL).mock(return_value=httpx.Response(200, json=_ren_payload(start, 30, 50.0)))
    config = _make_config()
    client = MarketDataClient(config, clock=FakeClock(_NOW))
    forecast = await client.refresh(force=True)
    assert forecast is not None

    inputs = make_inputs()
    params = build_runtimeparams(inputs, config, market_forecast=forecast)
    assert len(params["load_cost_forecast"]) == 24
    # Slot 0: gross/100 = 0.3332; Modus C (Default) + CO2 325/1000*0.05 = 0.01625.
    assert params["load_cost_forecast"][0] == pytest.approx(0.3332 + 0.01625)

    # Mode B: keine CO2-Gewichtung, nur gross/100.
    config_b = _make_config(operating_mode="B_MARKET_ORIENTED")
    params_b = build_runtimeparams(inputs, config_b, market_forecast=forecast)
    assert params_b["load_cost_forecast"][0] == pytest.approx(0.3332)


async def test_emhass_optimize_sends_market_forecast() -> None:
    config = _make_config()
    inputs = make_inputs()

    class _RecordingClient(EmhassClient):
        captured: dict[str, Any] | None = None

        async def optimize(self, inputs, aggregated_load_w=None, market_forecast=None):  # type: ignore[override]
            type(self).captured = build_runtimeparams(
                inputs, self._config, aggregated_load_w, market_forecast
            )

            class _R:
                status = "ok"
                solve_ms = 1
                setpoints = None
                runtimeparams_hash = "deadbeef"
                wallbox_schedule_w = None
                heatpump_schedule_w = None
                battery_schedule_w = None

            return _R()

    # Forecast mit bekanntem Slot-0-Wert (0.3332 EUR/kWh, kein CO2 in Mode A).
    from hems.io.market_client import MarketForecast, MarketSlot

    forecast = MarketForecast(
        generated_at_utc=_NOW.isoformat(),
        source="energy_charts",
        slots=[
            MarketSlot(
                start_utc=_NOW.isoformat(),
                spot_eur_mwh=100.0,
                gross_ct_kwh=33.32,
                renewable_share_pct=50.0,
                co2_intensity_g_kwh=325.0,
            )
        ],
        load_cost_forecast_eur_kwh=[0.3332] * 24,
        co2_forecast_g_kwh=[325.0] * 24,
    )
    await _RecordingClient(config).optimize(inputs, market_forecast=forecast)
    assert _RecordingClient.captured is not None
    assert len(_RecordingClient.captured["load_cost_forecast"]) == 24
    assert _RecordingClient.captured["load_cost_forecast"][0] == pytest.approx(
        0.3332 + 0.01625
    )


# --- JSON-Roundtrip der Datenmodelle (extra=forbid/frozen) --------------------------


def test_market_forecast_model_roundtrip() -> None:
    from hems.io.market_client import MarketForecast, MarketSlot

    slot = MarketSlot(
        start_utc="2026-11-24T10:00:00+00:00",
        spot_eur_mwh=100.0,
        gross_ct_kwh=33.32,
        renewable_share_pct=50.0,
        co2_intensity_g_kwh=325.0,
    )
    forecast = MarketForecast(
        generated_at_utc="2026-11-24T10:07:00+00:00",
        source="energy_charts",
        slots=[slot],
        load_cost_forecast_eur_kwh=[0.3332],
        co2_forecast_g_kwh=[325.0],
    )
    dumped = json.loads(forecast.model_dump_json())
    assert MarketForecast.model_validate(dumped) == forecast