"""Phase-4-Gate: Quasi-HIL E2E (docs/04 §3) + Unit-Tests für Clock/Plant/KPI (C2)."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import aiosqlite
import httpx
import pytest
import respx

from hems.config import AppConfig
from hems.hil.kpi_calculator import KpiReport, cli_main, compute_kpis
from hems.hil.replay_driver import (
    CSV_COLUMNS,
    FaultInjectingEmhassClient,
    FaultScenario,
    PlantInitialState,
    PlantSimulator,
    ReplayConfig,
    ReplayDriver,
    SimulatedClock,
    load_scenario,
)
from hems.logging.sqlite_store import SqliteAuditStore
from hems.models import ControlOutputs, HeatpumpSGReadyMode
from test_audit_logging import make_decision, make_inputs

EMHASS_URL = "http://a0d7b954-emhass:5000/action/naive-mpc-optim"
START = datetime(2026, 6, 21, 6, 0, 0, tzinfo=UTC)


class _RecordingMqttClient:
    """MQTT-Stub: zeichnet alle Publikationen für die Shadow-Mode-Assertion auf."""

    def __init__(self) -> None:
        self.published: list[tuple[str, str]] = []

    async def publish(self, topic: str, payload: str) -> None:
        self.published.append((topic, payload))


def _write_scenario(path: Path) -> None:
    """24 Schritte à 15 min: hohe PV, 14a-Fenster mit niedriger PV, Timeout-Fenster."""
    lines = [",".join(CSV_COLUMNS)]
    for i in range(24):
        ts = (START + timedelta(minutes=15 * i)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        if 10 <= i <= 13:
            pv = 1000.0  # Dimmungs-Fenster: niedrige PV -> echter 14a-Abwurf
        elif 8 <= i <= 15:
            pv = 9000.0
        elif i < 8:
            pv = 500.0 + 200.0 * i
        else:
            pv = 1500.0
        departure = 480.0 - 15.0 * i
        lines.append(
            f"{ts},{pv},500.0,30.0,400.0,true,{departure},false,4200.0"
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _faults() -> list[FaultScenario]:
    return [
        FaultScenario(kind="INJECT_14A_DIMMING", start_step=10, end_step=13, limit_w=4200.0),
        FaultScenario(kind="INJECT_SOLVER_TIMEOUT", start_step=16, end_step=18),
        FaultScenario(
            kind="INJECT_NEGATIVE_TARIFF", start_step=20, end_step=22, tariff_ct_kwh=-5.0
        ),
    ]


async def _read_decisions(db_path: Path) -> list[dict]:
    async with aiosqlite.connect(db_path) as conn:
        conn.row_factory = aiosqlite.Row
        cursor = await conn.execute("SELECT * FROM decisions ORDER BY timestamp_utc ASC")
        return [dict(row) for row in await cursor.fetchall()]


# --- Unit: SimulatedClock (B4/K5) ------------------------------------------------------


async def test_simulated_clock_sleep_and_advance() -> None:
    clock = SimulatedClock(START)
    woke: list[str] = []

    async def sleeper(name: str, seconds: float) -> None:
        await clock.sleep(seconds)
        woke.append(name)

    task_short = asyncio.create_task(sleeper("short", 10.0))
    task_long = asyncio.create_task(sleeper("long", 1000.0))
    await asyncio.sleep(0)
    assert woke == []

    await clock.advance_to(START + timedelta(seconds=15))
    assert woke == ["short"]
    assert clock.now_utc() == START + timedelta(seconds=15)

    await clock.advance_by(1000.0)
    assert woke == ["short", "long"]
    await task_short
    await task_long


# --- Unit: PlantInitialState & PlantSimulator (B3) --------------------------------------


def test_plant_initial_state_defaults() -> None:
    state = PlantInitialState()
    assert state.soc_battery_pct == 50.0
    assert state.soc_ev_pct == 30.0
    assert state.t_buffer_c == 48.0
    with pytest.raises(ValueError):
        PlantInitialState.model_validate({"unknown": 1})


def test_plant_simulator_physics() -> None:
    config = AppConfig()
    plant = PlantSimulator(config)
    outputs = ControlOutputs(
        cmd_wallbox_w=11000.0,
        cmd_battery_w=2000.0,
        cmd_heatpump_sg_ready=HeatpumpSGReadyMode.FORCED,
        cmd_heatpump_w_est=2500.0,
    )
    p_grid = plant.step(outputs, p_pv_w=9000.0, p_house_base_w=500.0, ev_connected=True, dt_s=900.0)
    # Batterie: 2000 * 0.95 * 0.25 h = 475 Wh -> +4.75 % auf 10 kWh.
    assert plant.soc_battery_pct == pytest.approx(54.75)
    # EV: 11000 * 0.92 * 0.25 h = 2530 Wh -> +4.2167 % auf 60 kWh.
    assert plant.soc_ev_pct == pytest.approx(30.0 + 2530.0 / 60000.0 * 100.0)
    # Puffer: -0.0005 * 900 + 2500 * 3.5 * 0.25 / 1500.
    assert plant.t_buffer_c == pytest.approx(48.0 - 0.45 + 2500.0 * 3.5 * 0.25 / 1500.0)
    assert p_grid == pytest.approx(500.0 + 11000.0 + 2500.0 + 2000.0 - 9000.0)

    # Entladung: -2000 / 0.95 * 0.25 h -> SoC sinkt stärker als nominal.
    plant2 = PlantSimulator(config)
    outputs_dis = outputs.model_copy(update={"cmd_battery_w": -2000.0})
    plant2.step(outputs_dis, 0.0, 500.0, False, 900.0)
    assert plant2.soc_battery_pct == pytest.approx(50.0 - (2000.0 / 0.95 * 0.25) / 10000.0 * 100.0)


def test_plant_simulator_reproducible() -> None:
    config = AppConfig()
    outputs = ControlOutputs(
        cmd_wallbox_w=4200.0,
        cmd_battery_w=1000.0,
        cmd_heatpump_sg_ready=HeatpumpSGReadyMode.NORMAL,
        cmd_heatpump_w_est=0.0,
    )
    runs = []
    for _ in range(2):
        plant = PlantSimulator(config)
        for _ in range(10):
            plant.step(outputs, 3000.0, 500.0, True, 900.0)
        runs.append((plant.soc_battery_pct, plant.soc_ev_pct, plant.t_buffer_c, plant.p_grid_w))
    assert runs[0] == runs[1]


# --- Unit: FaultScenario & CSV -----------------------------------------------------------


def test_fault_scenario_windows() -> None:
    fault = FaultScenario(kind="INJECT_14A_DIMMING", start_step=10, end_step=13)
    assert not fault.is_active(9)
    assert fault.is_active(10)
    assert fault.is_active(13)
    assert not fault.is_active(14)
    assert fault.limit_w == 4200.0


def test_load_scenario_schema_validation(tmp_path: Path) -> None:
    bad = tmp_path / "bad.csv"
    bad.write_text("timestamp_utc,p_pv_w\n2026-06-21T06:00:00.000Z,1000\n", encoding="utf-8")
    with pytest.raises(ValueError):
        load_scenario(bad)

    good = tmp_path / "good.csv"
    _write_scenario(good)
    rows = load_scenario(good)
    assert len(rows) == 24
    assert rows[0].timestamp_utc == START
    assert rows[0].ev_departure_in_min == 480.0


async def test_fault_injecting_emhass_client() -> None:
    client = FaultInjectingEmhassClient(AppConfig())
    client.set_faults(
        [FaultScenario(kind="INJECT_SOLVER_TIMEOUT", start_step=2, end_step=3)], 2
    )
    result = await client.optimize(make_inputs())
    assert result.status == "timeout"
    assert result.setpoints is None


# --- Unit: KPI-Rechner (C2) ---------------------------------------------------------------


async def _seed_kpi_db(tmp_path: Path) -> Path:
    store = SqliteAuditStore(tmp_path)
    await store.initialize()
    await store.insert_decision(
        make_decision(
            inputs=make_inputs(p_grid_before_w=1000.0),
            decision_id="2026-06-21T10:00:00Z_0001",
            timestamp_utc="2026-06-21T10:00:00.000Z",
        )
    )
    await store.insert_decision(
        make_decision(
            inputs=make_inputs(p_grid_before_w=3000.0),
            decision_id="2026-06-21T10:15:00Z_0002",
            timestamp_utc="2026-06-21T10:15:00.000Z",
        )
    )
    await store.insert_decision(
        make_decision(
            inputs=make_inputs(p_grid_before_w=-2000.0),
            decision_id="2026-06-21T11:15:00Z_0003",
            timestamp_utc="2026-06-21T11:15:00.000Z",
        )
    )
    await store.close()
    return store.db_path


async def test_kpi_piecewise_constant_with_gap_cap(tmp_path: Path) -> None:
    db_path = await _seed_kpi_db(tmp_path)
    report = await compute_kpis(
        db_path, "2026-06-21T00:00:00.000Z", "2026-06-21T23:59:59.999Z"
    )
    assert isinstance(report, KpiReport)
    assert report.total_decisions == 3
    # Δt = [900, 900 (Cap statt 3600), 900]; Import = [1000, 3000, 0].
    assert report.total_grid_import_kwh == pytest.approx(4000.0 * 900.0 / 3_600_000.0)
    assert report.mean_grid_import_w == pytest.approx(4000.0 * 900.0 / 2700.0)
    assert report.peak_grid_import_w == 3000.0
    assert report.peak_to_average_ratio == pytest.approx(2.25)
    assert report.total_energy_cost_eur == pytest.approx(1.0 * 0.30)
    assert report.total_co2_kg == pytest.approx(1.0 * 0.40)


def test_kpi_empty_range_and_cli_export(tmp_path: Path) -> None:
    db_path = asyncio.run(_seed_kpi_db(tmp_path))
    empty = asyncio.run(
        compute_kpis(db_path, "2030-01-01T00:00:00.000Z", "2030-12-31T00:00:00.000Z")
    )
    assert empty.total_decisions == 0
    assert empty.peak_to_average_ratio == 0.0

    # CLI-Export (synchon, wie in docs/04 spezifiziert).
    out = tmp_path / "report.json"
    cli_main(["--db", str(db_path), "--out", str(out)])
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["total_decisions"] == 3
    assert payload["total_grid_import_kwh"] == pytest.approx(1.0)


# --- E2E: Pflicht-Assertions 1–5 (docs/04 §3, inkl. K7) ------------------------------------


@respx.mock
async def test_quasi_hil_e2e_full_scenario(tmp_path: Path) -> None:
    respx.post(EMHASS_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "P_batt": [-2000.0] * 24,
                "P_deferrable0": [11000.0] * 24,
                "P_deferrable1": [0.0] * 24,
            },
        )
    )
    csv_path = tmp_path / "scenario.csv"
    _write_scenario(csv_path)
    config = AppConfig(data_dir=tmp_path)
    assert config.dry_run is True

    driver = ReplayDriver(config, ReplayConfig(), faults=_faults())
    mqtt = _RecordingMqttClient()
    driver.attach_mqtt(mqtt)
    await driver.run(csv_path)

    # Assertion 1: Alle Entscheidungen lückenlos in JSONL UND SQLite.
    jsonl_lines = (tmp_path / "logs" / "decision_trace.jsonl").read_text().splitlines()
    jsonl_decisions = [
        json.loads(line) for line in jsonl_lines if json.loads(line)["event_type"] == "decision"
    ]
    db_rows = await _read_decisions(tmp_path / "db" / "hems_audit.db")
    assert len(jsonl_decisions) == 24
    assert len(db_rows) == 24
    assert {r["decision_id"] for r in jsonl_decisions} == {r["decision_id"] for r in db_rows}
    # Jeder geloggten Entscheidung ist genau eine Verifikation zugeordnet (K4).
    jsonl_verifications = [
        json.loads(line) for line in jsonl_lines if json.loads(line)["event_type"] == "verification"
    ]
    assert len(jsonl_verifications) == 24
    assert all(row["verified_at_utc"] is not None for row in db_rows)

    # Assertion 2 (K7): Netto-netzbezogene steuerbare Leistung <= 4200 * 1.02,
    # compliance_ok == True während des §14a-Eingriffs (Schritte 10–13).
    events_14a = [row for row in db_rows if row["dso_14a_active"] == 1]
    assert len(events_14a) == 4
    for row in events_14a:
        assert row["controllable_load_after_w"] <= 4200.0 * 1.02
        assert row["compliance_ok"] == 1
        assert row["winning_priority"] == 1

    # Assertion 3: Solver-Timeout-Fenster (Schritte 16–18) -> Fallback aktiv.
    # Hinweis: In Schritt 17 greift zusätzlich der P2-Komfort-Guard (prio 2),
    # der gemäß Kaskade über Prio 4 steht; die reinen Fallback-Schritte sind
    # 16 und 18 mit winning_priority == 4 und trigger solver_timeout_fallback.
    timeout_rows = db_rows[16:19]
    assert [row["winning_priority"] for row in timeout_rows] == [4, 2, 4]
    fallback_rows = [row for row in timeout_rows if row["winning_priority"] == 4]
    assert len(fallback_rows) == 2
    assert all(row["trigger_reason"] == "solver_timeout_fallback" for row in fallback_rows)
    assert all(
        "P4_fallback_active" in json.loads(row["raw_json"])["derivation"]["cascade_evaluated_rules"]
        for row in fallback_rows
    )

    # Assertion 4: KPI-Report plausibel.
    report = await compute_kpis(
        tmp_path / "db" / "hems_audit.db",
        "2026-06-21T00:00:00.000Z",
        "2026-06-21T23:59:59.999Z",
        mode="C_GRID_SUPPORTIVE",
    )
    assert report.dso_14a_compliance_rate_pct == 100.0
    assert report.solver_fallback_count >= 1
    assert report.peak_to_average_ratio >= 1.0
    assert report.total_decisions == 24

    # Assertion 5: dry_run=True -> keine einzige Nachricht auf hems/cmd/...
    assert mqtt.published, "Shadow-Mode muss publizieren"
    actuator_topics = [
        topic for topic, _ in mqtt.published if "/cmd/" in topic
    ]
    assert actuator_topics
    assert all(topic.startswith("hems/shadow/cmd/") for topic in actuator_topics)
    assert not any(topic.startswith("hems/cmd/") for topic, _ in mqtt.published)


@respx.mock
async def test_quasi_hil_e2e_deterministic_replay(tmp_path: Path) -> None:
    """B3: Zwei identische Replay-Läufe liefern bitidentische Sollwerte."""
    respx.post(EMHASS_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "P_batt": [-2000.0] * 24,
                "P_deferrable0": [11000.0] * 24,
                "P_deferrable1": [0.0] * 24,
            },
        )
    )
    csv_path = tmp_path / "scenario.csv"
    _write_scenario(csv_path)
    outputs_per_run: list[list[tuple]] = []
    for run in range(2):
        run_dir = tmp_path / f"run{run}"
        run_dir.mkdir()
        config = AppConfig(data_dir=run_dir)
        driver = ReplayDriver(config, faults=_faults())
        await driver.run(csv_path)
        rows = await _read_decisions(run_dir / "db" / "hems_audit.db")
        outputs_per_run.append(
            [
                (
                    row["cmd_wallbox_w"],
                    row["cmd_battery_w"],
                    row["cmd_heatpump_sg_ready"],
                    row["cmd_heatpump_w_est"],
                    row["soc_battery_pct"],
                    row["soc_ev_pct"],
                )
                for row in rows
            ]
        )
    assert outputs_per_run[0] == outputs_per_run[1]
