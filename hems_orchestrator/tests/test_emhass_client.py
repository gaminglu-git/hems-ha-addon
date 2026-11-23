"""Phase-3-Gate: EMHASS-Client (respx), MQTT-Credentials & Shadow-Mode (docs/03)."""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from hems.config import AppConfig, OperatingMode
from hems.io.emhass_client import (
    EmhassClient,
    build_runtimeparams,
    canonical_runtimeparams_hash,
)
from hems.io.mqtt_client import (
    ActuatorPublisher,
    LiveState,
    actuator_topics,
    handle_message,
    resolve_mqtt_credentials,
    subscribed_topics,
)
from hems.models import ControlOutputs, HeatpumpSGReadyMode
from test_audit_logging import make_inputs

EMHASS_URL = "http://emhass-test:5000/action/naive-mpc-optim"


def make_config(**overrides: object) -> AppConfig:
    return AppConfig.model_validate({"emhass_url": "http://emhass-test:5000", **overrides})


class _FakeMqttClient:
    """Minimaler MQTT-Client-Stub: zeichnet alle Publikationen auf."""

    def __init__(self) -> None:
        self.published: list[tuple[str, str]] = []

    async def publish(self, topic: str, payload: str) -> None:
        self.published.append((topic, payload))


# --- runtimeparams & Hashing --------------------------------------------------------


def test_runtimeparams_complete_and_hash_deterministic() -> None:
    config = make_config()
    inputs = make_inputs(tariff_ct_kwh=30.0, co2_intensity_g_kwh=400.0)
    params = build_runtimeparams(inputs, config)

    assert params["prediction_horizon"] == 24
    assert params["soc_init"] == 0.5
    assert params["soc_final"] == 0.20
    assert len(params["load_cost_forecast"]) == 24
    assert len(params["prod_price_forecast"]) == 24
    assert len(params["pv_power_forecast"]) == 24
    assert len(params["load_power_forecast"]) == 24
    assert params["number_of_deferrable_loads"] == 2
    assert params["nominal_power_of_deferrable_loads"] == [11000.0, 2500.0]
    assert params["alpha"] == 0.5
    assert params["beta"] == 0.5
    assert params["weight_battery_discharge"] == 0.05
    assert params["costfun"] == "profit"
    assert len(params["def_total_hours"]) == 2
    # Modus C: CO₂-Gewichtung im effektiven Kostenterm (0.30 + 0.4 * 0.05).
    assert params["load_cost_forecast"][0] == pytest.approx(0.32)
    assert params["prod_price_forecast"][0] == 0.08
    assert params["maximum_power_from_grid"] == 8000.0
    assert params["maximum_power_to_grid"] == 6000.0

    hash1 = canonical_runtimeparams_hash(params)
    hash2 = canonical_runtimeparams_hash(build_runtimeparams(inputs, config))
    assert hash1 == hash2
    assert len(hash1) == 8
    assert hash1 != canonical_runtimeparams_hash({**params, "soc_final": 0.3})


def test_runtimeparams_mode_b_uses_house_fuse() -> None:
    config = make_config(operating_mode="B_MARKET_ORIENTED")
    params = build_runtimeparams(make_inputs(), config)
    assert params["maximum_power_from_grid"] == 22000.0
    assert "maximum_power_to_grid" not in params
    assert params["load_cost_forecast"][0] == pytest.approx(0.30)


def test_runtimeparams_mode_c_14a_limit_when_active() -> None:
    config = make_config()
    inputs = make_inputs(dso_14a_active=True, dso_14a_limit_w=5000.0)
    params = build_runtimeparams(inputs, config)
    assert params["maximum_power_from_grid"] == 5000.0


def test_runtimeparams_negative_tariff_zero_feedin_price() -> None:
    config = make_config()
    params = build_runtimeparams(make_inputs(tariff_ct_kwh=-5.0), config)
    assert params["prod_price_forecast"][0] == 0.0


# --- HTTP-Verhalten (respx) ----------------------------------------------------------


@respx.mock
async def test_optimize_success_sign_inversion_and_mapping() -> None:
    respx.post(EMHASS_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "P_batt": [-3000.0, -2000.0],
                "P_deferrable0": [4200.0, 4000.0],
                "P_deferrable1": [200.0, 100.0],
            },
        )
    )
    result = await EmhassClient(make_config()).optimize(make_inputs())
    assert result.status == "ok"
    assert result.setpoints is not None
    # Fix B2: EMHASS P_batt=-3000 (Ladung) -> HEMS battery_w=+3000 (Ladung).
    assert result.setpoints.battery_w == 3000.0
    assert result.setpoints.wallbox_w == 4200.0
    assert result.setpoints.heatpump_w == 200.0
    assert result.solve_ms is not None and result.solve_ms >= 0
    assert result.runtimeparams_hash is not None


@respx.mock
async def test_optimize_sends_full_runtimeparams() -> None:
    route = respx.post(EMHASS_URL).mock(
        return_value=httpx.Response(
            200,
            json={"P_batt": [0.0], "P_deferrable0": [0.0], "P_deferrable1": [0.0]},
        )
    )
    config = make_config()
    inputs = make_inputs()
    result = await EmhassClient(config).optimize(inputs)
    sent = json.loads(route.calls.last.request.content)
    assert sent == build_runtimeparams(inputs, config)
    assert result.runtimeparams_hash == canonical_runtimeparams_hash(sent)


@respx.mock
async def test_optimize_discharge_sign_inversion() -> None:
    respx.post(EMHASS_URL).mock(
        return_value=httpx.Response(
            200,
            json={"P_batt": [1500.0], "P_deferrable0": [0.0], "P_deferrable1": [0.0]},
        )
    )
    result = await EmhassClient(make_config()).optimize(make_inputs())
    assert result.setpoints is not None
    # EMHASS P_batt=+1500 (Entladung) -> HEMS battery_w=-1500 (Entladung).
    assert result.setpoints.battery_w == -1500.0


@respx.mock
async def test_optimize_timeout_handling() -> None:
    respx.post(EMHASS_URL).mock(side_effect=httpx.ConnectTimeout("simulated timeout"))
    result = await EmhassClient(make_config()).optimize(make_inputs())
    assert result.status == "timeout"
    assert result.setpoints is None
    assert result.runtimeparams_hash is not None


@respx.mock
async def test_optimize_http_500_handling() -> None:
    respx.post(EMHASS_URL).mock(return_value=httpx.Response(500, text="solver exploded"))
    result = await EmhassClient(make_config()).optimize(make_inputs())
    assert result.status == "error"
    assert result.setpoints is None


@respx.mock
async def test_optimize_invalid_json_handling() -> None:
    respx.post(EMHASS_URL).mock(return_value=httpx.Response(200, text="not json"))
    result = await EmhassClient(make_config()).optimize(make_inputs())
    assert result.status == "error"


@respx.mock
async def test_optimize_missing_columns_handling() -> None:
    respx.post(EMHASS_URL).mock(
        return_value=httpx.Response(200, json={"P_batt": [0.0], "P_deferrable0": [0.0]})
    )
    result = await EmhassClient(make_config()).optimize(make_inputs())
    assert result.status == "error"


@respx.mock
async def test_optimize_empty_list_handling() -> None:
    respx.post(EMHASS_URL).mock(
        return_value=httpx.Response(
            200, json={"P_batt": [], "P_deferrable0": [0.0], "P_deferrable1": [0.0]}
        )
    )
    result = await EmhassClient(make_config()).optimize(make_inputs())
    assert result.status == "error"


# --- MQTT-Credential-Resolution (Supervisor-API, respx) ------------------------------


@respx.mock
async def test_credentials_resolved_via_supervisor(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SUPERVISOR_TOKEN", "test-token")
    route = respx.get("http://supervisor/services/mqtt").mock(
        return_value=httpx.Response(
            200,
            json={
                "result": "ok",
                "data": {
                    "host": "core-mosquitto",
                    "port": 1883,
                    "username": "addon_user",
                    "password": "addon_pass",
                },
            },
        )
    )
    credentials = await resolve_mqtt_credentials(AppConfig())
    assert credentials.username == "addon_user"
    assert credentials.password == "addon_pass"
    assert route.calls.last.request.headers["Authorization"] == "Bearer test-token"


@respx.mock
async def test_credentials_supervisor_failure_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SUPERVISOR_TOKEN", "test-token")
    respx.get("http://supervisor/services/mqtt").mock(side_effect=httpx.ConnectError("down"))
    credentials = await resolve_mqtt_credentials(AppConfig())
    assert credentials.host == "core-mosquitto"
    assert credentials.username == ""


async def test_credentials_explicit_config_skips_supervisor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SUPERVISOR_TOKEN", "test-token")
    config = AppConfig(mqtt_username="user", mqtt_password="pass")
    credentials = await resolve_mqtt_credentials(config)
    assert credentials.username == "user"
    assert credentials.password == "pass"


# --- Shadow-Mode & Ingestion ----------------------------------------------------------


async def test_shadow_mode_never_publishes_real_cmd_topics() -> None:
    client = _FakeMqttClient()
    publisher = ActuatorPublisher("hems")
    outputs = ControlOutputs(
        cmd_wallbox_w=4200.0,
        cmd_battery_w=-400.0,
        cmd_heatpump_sg_ready=HeatpumpSGReadyMode.NORMAL,
        cmd_heatpump_w_est=0.0,
    )
    await publisher.publish_outputs(client, outputs, dry_run=True)
    topics = [topic for topic, _ in client.published]
    assert topics == [
        "hems/shadow/cmd/wallbox_w",
        "hems/shadow/cmd/battery_w",
        "hems/shadow/cmd/heatpump_sg_ready",
    ]
    assert not any(topic.startswith("hems/cmd/") for topic in topics)
    payloads = dict(client.published)
    assert payloads["hems/shadow/cmd/wallbox_w"] == "4200.0"
    assert payloads["hems/shadow/cmd/battery_w"] == "-400.0"


async def test_real_mode_publishes_cmd_topics() -> None:
    client = _FakeMqttClient()
    publisher = ActuatorPublisher("hems")
    outputs = ControlOutputs(
        cmd_wallbox_w=0.0,
        cmd_battery_w=0.0,
        cmd_heatpump_sg_ready=HeatpumpSGReadyMode.FORCED,
        cmd_heatpump_w_est=2500.0,
    )
    await publisher.publish_outputs(client, outputs, dry_run=False)
    topics = [topic for topic, _ in client.published]
    assert topics == [
        "hems/cmd/wallbox_w",
        "hems/cmd/battery_w",
        "hems/cmd/heatpump_sg_ready",
    ]


def test_actuator_topics_and_subscriptions() -> None:
    assert actuator_topics("hems", dry_run=True)["wallbox"] == "hems/shadow/cmd/wallbox_w"
    assert actuator_topics("hems", dry_run=False)["battery"] == "hems/cmd/battery_w"
    assert subscribed_topics("hems") == [
        "hems/sensors/state",
        "hems/control/mode",
        "hems/control/dry_run",
        "hems/dso/14a_signal",
    ]


def test_handle_message_state_mode_dry_run_and_dso() -> None:
    config = AppConfig()
    state = LiveState(config)
    inputs = make_inputs()
    handle_message("hems/sensors/state", inputs.model_dump_json().encode(), state, config)
    assert state.inputs == inputs

    handle_message("hems/control/mode", b"A_BASELINE", state, config)
    assert state.operating_mode is OperatingMode.A_BASELINE
    handle_message("hems/control/dry_run", b"false", state, config)
    assert state.dry_run is False

    handle_message(
        "hems/dso/14a_signal", b'{"active": true, "limit_w": 4200.0}', state, config
    )
    assert state.inputs is not None
    assert state.inputs.dso_14a_active is True
    assert state.inputs.dso_14a_limit_w == 4200.0

    # DSO-Signal überschreibt auch nachfolgende State-Snapshots sofort.
    handle_message("hems/sensors/state", inputs.model_dump_json().encode(), state, config)
    assert state.inputs.dso_14a_active is True


def test_handle_message_invalid_payloads_do_not_crash() -> None:
    config = AppConfig()
    state = LiveState(config)
    handle_message("hems/control/mode", b"INVALID_MODE", state, config)
    assert state.operating_mode is config.operating_mode
    handle_message("hems/control/dry_run", b"maybe", state, config)
    assert state.dry_run is True
    handle_message("hems/sensors/state", b"not json", state, config)
    assert state.inputs is None
    handle_message("hems/unknown/topic", b"x", state, config)
