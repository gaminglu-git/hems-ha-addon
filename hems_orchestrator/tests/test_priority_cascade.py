"""Phase-3-Gate: 4-Stufen-Prioritätskaskade inkl. Pflicht-Tests A1/A2/K1/K6 (docs/03 §5)."""

from __future__ import annotations

import asyncio
from pathlib import Path

from hems.config import AppConfig
from hems.control.cascade import evaluate_cascade, reclamp_battery_after_eeg
from hems.control.modes import compute_baseline_setpoints, map_heatpump_power_to_sg_ready
from hems.control.scheduler import ControlScheduler, should_log_decision
from hems.io.emhass_client import EmhassClient, EmhassResult
from hems.io.mqtt_client import LiveState
from hems.logging.jsonl_logger import JsonlLogger
from hems.logging.sqlite_store import SqliteAuditStore
from hems.logging.verification import VerificationTracker
from hems.models import HeatpumpSGReadyMode, ProposedSetpoints, TriggerReason
from test_audit_logging import make_inputs


def make_config(**overrides: object) -> AppConfig:
    return AppConfig.model_validate(overrides)


def ok_result(wallbox_w: float, battery_w: float, heatpump_w: float) -> EmhassResult:
    return EmhassResult(
        status="ok",
        solve_ms=100,
        setpoints=ProposedSetpoints(
            wallbox_w=wallbox_w, battery_w=battery_w, heatpump_w=heatpump_w
        ),
        runtimeparams_hash="abcd1234",
    )


def steuve_grid(
    wallbox_w: float, heatpump_w: float, battery_w: float, pv: float, house: float
) -> float:
    return max(0.0, wallbox_w + heatpump_w + max(0.0, battery_w) - max(0.0, pv - house))


# --- Pflicht-Test: § 14a EnWG ---------------------------------------------------------


def test_14a_dimming_wallbox_to_4200() -> None:
    config = make_config(operating_mode="A_BASELINE")
    inputs = make_inputs(dso_14a_active=True, dso_14a_limit_w=4200.0)
    outputs, derivation, trigger = evaluate_cascade(inputs, config, None, None)
    assert outputs.cmd_wallbox_w == 4200.0
    assert derivation.winning_priority == 1
    assert trigger is TriggerReason.DSO_14A_DIMMING_EVENT
    assert any(r.startswith("P1_14a_limit_violated_") for r in derivation.cascade_evaluated_rules)


# --- Pflicht-Test: Wallbox-Mindeststrom (Rest 1000 W < 1380 W -> 0) -------------------


def test_wallbox_minimum_power_cutoff() -> None:
    # Hausanschluss 3500 W, WP 2500 W (FORCED) -> nur 1000 W Rest für die Wallbox.
    config = make_config(operating_mode="A_BASELINE", house_fuse_limit_w=3500.0)
    inputs = make_inputs(
        p_pv_w=0.0,
        p_house_base_w=0.0,
        soc_battery_pct=5.0,
        t_buffer_c=40.0,
        ev_departure_in_min=None,
    )
    outputs, derivation, _ = evaluate_cascade(inputs, config, None, None)
    assert outputs.cmd_heatpump_w_est == 2500.0
    assert outputs.cmd_wallbox_w == 0.0
    assert derivation.winning_priority == 1


# --- Pflicht-Test: Prio 1 schlägt Prio 2 ----------------------------------------------


def test_prio1_overrides_prio2_ev_guard() -> None:
    config = make_config(operating_mode="A_BASELINE")
    inputs = make_inputs(
        dso_14a_active=True,
        dso_14a_limit_w=4200.0,
        soc_ev_pct=20.0,
        ev_departure_in_min=120.0,
    )
    outputs, derivation, trigger = evaluate_cascade(inputs, config, None, None)
    assert "P2_ev_target_soc_guard" in derivation.cascade_evaluated_rules
    assert outputs.cmd_wallbox_w <= 4200.0
    assert derivation.winning_priority == 1
    assert trigger is TriggerReason.DSO_14A_DIMMING_EVENT


# --- Pflicht-Test: Prio-4-Fallback -----------------------------------------------------


def test_prio4_fallback_on_solver_timeout() -> None:
    config = make_config()
    timeout = EmhassResult(
        status="timeout", solve_ms=15000, setpoints=None, runtimeparams_hash="abcd1234"
    )
    inputs = make_inputs(ev_connected=False)
    outputs, derivation, trigger = evaluate_cascade(inputs, config, timeout, 5.0)
    assert derivation.winning_priority == 4
    assert trigger is TriggerReason.SOLVER_TIMEOUT_FALLBACK
    assert "P4_fallback_active" in derivation.cascade_evaluated_rules
    assert derivation.emhass_status == "timeout"
    # Sicherer Drosselmodus: Wallbox <= dso_14a_min_power_w.
    assert outputs.cmd_wallbox_w <= config.dso_14a_min_power_w


def test_stale_setpoints_trigger_fallback() -> None:
    config = make_config()
    result = ok_result(4200.0, 0.0, 0.0)
    inputs = make_inputs(ev_connected=False)
    _, derivation, _ = evaluate_cascade(inputs, config, result, config.mpc_interval_s + 61.0)
    assert derivation.winning_priority == 4
    assert derivation.emhass_status == "stale"


def test_mode_a_disables_emhass() -> None:
    config = make_config(operating_mode="A_BASELINE")
    inputs = make_inputs(ev_connected=False)
    _, derivation, trigger = evaluate_cascade(inputs, config, ok_result(0, 0, 0), 1.0)
    assert derivation.emhass_status == "disabled"
    assert derivation.winning_priority == 3
    assert trigger is TriggerReason.PERIODIC_1HZ


# --- Pflicht-Test: § 9 EEG --------------------------------------------------------------


def test_eeg_feedin_limit_increases_battery_charging() -> None:
    config = make_config()
    inputs = make_inputs(p_pv_w=12000.0, p_house_base_w=500.0, ev_connected=False)
    result = ok_result(0.0, 0.0, 0.0)
    outputs, derivation, trigger = evaluate_cascade(inputs, config, result, 10.0)
    # Einspeisung -11500 W < -6000 W -> Batterie nimmt 5000 W auf (max_charge).
    assert outputs.cmd_battery_w == 5000.0
    assert derivation.winning_priority == 1
    assert trigger is TriggerReason.EEG_FEEDIN_LIMIT_EVENT
    assert "P1_eeg_feedin_limit_active" in derivation.cascade_evaluated_rules


# --- Pflicht-Tests A1: §14a + §9 EEG Kollision & Re-Clamp -------------------------------


def test_14a_eeg_collision_steuve_grid_within_limit() -> None:
    """A1 Pflicht: 14a aktiv + PV-Einspeisung über Limit -> P_steuve,grid <= 4200 W."""
    config = make_config()
    inputs = make_inputs(
        dso_14a_active=True,
        dso_14a_limit_w=4200.0,
        p_pv_w=15000.0,
        p_house_base_w=500.0,
        ev_connected=False,
    )
    result = ok_result(4200.0, 0.0, 0.0)
    outputs, derivation, _ = evaluate_cascade(inputs, config, result, 10.0)
    # Batterie nimmt den Überschuss ÜBER dem 60-%-Limit auf:
    # p_grid_est = 500 + 4200 - 15000 = -10300; excess = 10300 - 6000 = 4300 W.
    assert outputs.cmd_battery_w == 4300.0
    p_steuve = steuve_grid(
        outputs.cmd_wallbox_w,
        outputs.cmd_heatpump_w_est,
        outputs.cmd_battery_w,
        inputs.p_pv_w,
        inputs.p_house_base_w,
    )
    assert p_steuve <= 4200.0
    assert derivation.winning_priority == 1


def test_14a_reclamp_after_eeg_marker_and_limit() -> None:
    """A1 Pflicht: Re-Clamp-Logik (K6-Codeblock) hält P_steuve,grid <= limit_14a.

    Hinweis: Unter dem EMS-Saldierungsmodell kann der EEG-Schritt die 14a-Bilanz
    mathematisch nicht verletzen (p_grid_est <= -max_feedin => P_steuve,grid = 0);
    der Re-Clamp ist ein defensiver Guard. Seine Logik wird hier direkt am
    spezifizierten Codepfad geprüft (Marker inkl.), die Kaskaden-Invariante oben.
    """
    inputs = make_inputs(dso_14a_active=True, dso_14a_limit_w=4200.0, p_pv_w=0.0)
    # Szenario nach hypothetischer EEG-Erhöhung: SteuVE-Summe 6000 W > 4200 W.
    battery_w, reclamped = reclamp_battery_after_eeg(3000.0, 3000.0, 0.0, inputs, 4200.0)
    assert reclamped is True
    assert battery_w == 1200.0
    assert steuve_grid(3000.0, 0.0, battery_w, 0.0, 500.0) <= 4200.0

    # Integration: Kaskade mit 14a + EEG hält die Invariante strikt ein.
    config = make_config()
    inputs_pv = make_inputs(
        dso_14a_active=True,
        dso_14a_limit_w=4200.0,
        p_pv_w=15000.0,
        p_house_base_w=500.0,
        ev_connected=False,
    )
    outputs, _, _ = evaluate_cascade(inputs_pv, config, ok_result(4200.0, 0.0, 0.0), 10.0)
    assert (
        steuve_grid(
            outputs.cmd_wallbox_w,
            outputs.cmd_heatpump_w_est,
            outputs.cmd_battery_w,
            inputs_pv.p_pv_w,
            inputs_pv.p_house_base_w,
        )
        <= 4200.0
    )


def test_k6_guard_never_clamps_existing_discharge() -> None:
    """K6: Eine bestehende Batterie-Entladung darf nie auf 0.0 geclampt werden."""
    inputs = make_inputs(
        dso_14a_active=True,
        dso_14a_limit_w=4200.0,
        p_pv_w=400.0,
        p_house_base_w=500.0,
        soc_battery_pct=50.0,
        ev_connected=False,
    )
    config = make_config(battery_max_discharge_w=7000.0)
    # Entladung -7000 W -> Einspeisung -6900 W < -6000 W -> EEG reduziert Entladung.
    outputs, derivation, trigger = evaluate_cascade(
        inputs, config, ok_result(0.0, -7000.0, 0.0), 10.0
    )
    assert trigger is TriggerReason.EEG_FEEDIN_LIMIT_EVENT
    assert outputs.cmd_battery_w == -6100.0
    assert outputs.cmd_battery_w < 0.0
    assert "P1_14a_reclamp_after_eeg" not in derivation.cascade_evaluated_rules
    # Direkter Guard-Test: negative Ladung bleibt unangetastet.
    battery_w, reclamped = reclamp_battery_after_eeg(-2000.0, 11000.0, 2500.0, inputs, 4200.0)
    assert battery_w == -2000.0
    assert reclamped is False


# --- Pflicht-Tests A2: Wallbox-Aufrundung & Post-Clamp ----------------------------------


def test_p2_guard_rounds_up_to_min_power() -> None:
    """A2 Pflicht: p_required_w ~ 500 W -> Aufrundung auf 1380 W (14a inaktiv)."""
    config = make_config()
    inputs = make_inputs(
        soc_ev_pct=79.16666666666667,
        ev_departure_in_min=60.0,
        ev_connected=True,
    )
    outputs, derivation, trigger = evaluate_cascade(
        inputs, config, ok_result(0.0, 0.0, 0.0), 10.0
    )
    assert outputs.cmd_wallbox_w == 1380.0
    assert derivation.winning_priority == 2
    assert trigger is TriggerReason.COMFORT_GUARD_OVERRIDE
    assert "P2_ev_target_soc_guard" in derivation.cascade_evaluated_rules


def test_p2_roundup_then_p1_post_clamp_to_zero() -> None:
    """A2 Pflicht: P2 rundet auf 1380 W auf; P1 drosselt darunter -> 0.0 W + Marker."""
    config = make_config(house_fuse_limit_w=3500.0)
    inputs = make_inputs(
        p_pv_w=0.0,
        p_house_base_w=500.0,
        soc_battery_pct=5.0,
        t_buffer_c=40.0,
        soc_ev_pct=79.16666666666667,
        ev_departure_in_min=60.0,
        ev_connected=True,
    )
    outputs, derivation, trigger = evaluate_cascade(
        inputs, config, ok_result(0.0, 0.0, 0.0), 10.0
    )
    assert "P2_ev_target_soc_guard" in derivation.cascade_evaluated_rules
    assert "POST_wallbox_below_min_clamped_to_0" in derivation.cascade_evaluated_rules
    assert outputs.cmd_wallbox_w == 0.0
    assert derivation.winning_priority == 1
    assert trigger is TriggerReason.HOUSE_FUSE_PROTECTION


# --- Pflicht-Test K1: SG-Ready-Vorab-Quantisierung vor P1 --------------------------------


def test_k1_sg_ready_pre_quantization_before_p1() -> None:
    """K1 Pflicht: 200 W -> 1250 W (RECOMMENDED) vor P1; Wallbox 3500 -> 2950 W."""
    config = make_config()
    inputs = make_inputs(
        dso_14a_active=True,
        dso_14a_limit_w=4200.0,
        p_pv_w=0.0,
        p_house_base_w=500.0,
        ev_connected=False,
    )
    outputs, derivation, trigger = evaluate_cascade(
        inputs, config, ok_result(3500.0, 0.0, 200.0), 10.0
    )
    assert outputs.cmd_heatpump_w_est == 1250.0
    assert outputs.cmd_heatpump_sg_ready is HeatpumpSGReadyMode.RECOMMENDED
    assert outputs.cmd_wallbox_w == 2950.0
    assert outputs.cmd_wallbox_w + outputs.cmd_heatpump_w_est == 4200.0
    assert derivation.winning_priority == 1
    assert trigger is TriggerReason.DSO_14A_DIMMING_EVENT


def test_k1_p1_shed_sets_blocked_directly() -> None:
    """P1-Abwurfstufe 3 setzt BLOCKED/0.0 direkt (keine erneute Quantisierung)."""
    config = make_config()
    inputs = make_inputs(
        dso_14a_active=True,
        dso_14a_limit_w=4200.0,
        p_pv_w=0.0,
        p_house_base_w=0.0,
        t_buffer_c=40.0,
        ev_connected=False,
    )
    # WP FORCED 2500 W + Wallbox 11000 W -> Wallbox-Abwurf auf 1700 W (Stufe 2).
    outputs, derivation, _ = evaluate_cascade(
        inputs, config, ok_result(11000.0, 0.0, 2500.0), 10.0
    )
    assert outputs.cmd_wallbox_w == 1700.0
    assert outputs.cmd_heatpump_w_est == 2500.0
    assert derivation.winning_priority == 1
    # Härterer Fall: WP-Nennleistung 5000 W > Limit 4200 W erzwingt WP-Abwurf (Stufe 3).
    config_low = make_config(heatpump_nominal_power_w=5000.0)
    inputs_low = make_inputs(
        dso_14a_active=True,
        dso_14a_limit_w=4200.0,
        p_pv_w=0.0,
        p_house_base_w=0.0,
        t_buffer_c=40.0,
        ev_connected=False,
    )
    outputs_low, _, _ = evaluate_cascade(
        inputs_low, config_low, ok_result(11000.0, 0.0, 5000.0), 10.0
    )
    assert outputs_low.cmd_wallbox_w == 0.0
    assert outputs_low.cmd_heatpump_w_est == 0.0
    assert outputs_low.cmd_heatpump_sg_ready is HeatpumpSGReadyMode.BLOCKED


# --- SG-Ready-Mapping-Tabelle (Fix B1) ---------------------------------------------------


def test_sg_ready_mapping_table() -> None:
    config = make_config()
    assert map_heatpump_power_to_sg_ready(0.0, config) == (HeatpumpSGReadyMode.NORMAL, 0.0)
    assert map_heatpump_power_to_sg_ready(200.0, config) == (
        HeatpumpSGReadyMode.RECOMMENDED,
        1250.0,
    )
    assert map_heatpump_power_to_sg_ready(2000.0, config) == (HeatpumpSGReadyMode.FORCED, 2500.0)
    assert map_heatpump_power_to_sg_ready(2500.0, config) == (HeatpumpSGReadyMode.FORCED, 2500.0)
    assert map_heatpump_power_to_sg_ready(2500.0, config, force_blocked=True) == (
        HeatpumpSGReadyMode.BLOCKED,
        0.0,
    )
    assert map_heatpump_power_to_sg_ready(0.0, config, force_boost=True) == (
        HeatpumpSGReadyMode.FORCED,
        2500.0,
    )


def test_p2_temperature_band_sg_ready_semantics() -> None:
    config = make_config(operating_mode="A_BASELINE")
    cold = make_inputs(t_buffer_c=40.0, ev_connected=False)
    outputs_cold, derivation_cold, _ = evaluate_cascade(cold, config, None, None)
    assert outputs_cold.cmd_heatpump_sg_ready is HeatpumpSGReadyMode.FORCED
    assert outputs_cold.cmd_heatpump_w_est == 2500.0
    assert "P2_hp_temp_below_min" in derivation_cold.cascade_evaluated_rules
    assert derivation_cold.winning_priority == 2

    hot = make_inputs(t_buffer_c=60.0, ev_connected=False)
    outputs_hot, derivation_hot, _ = evaluate_cascade(hot, config, None, None)
    # Fix B1: Übertemperatur -> NORMAL (nicht BLOCKED; Zustand 1 ist EVU-/P1-Sperre).
    assert outputs_hot.cmd_heatpump_sg_ready is HeatpumpSGReadyMode.NORMAL
    assert outputs_hot.cmd_heatpump_w_est == 0.0
    assert "P2_hp_temp_above_max" in derivation_hot.cascade_evaluated_rules


# --- Baseline (Modus A) -------------------------------------------------------------------


def test_baseline_setpoints_self_consumption() -> None:
    config = make_config()
    surplus = make_inputs(p_pv_w=4000.0, p_house_base_w=500.0, ev_connected=False)
    sp = compute_baseline_setpoints(surplus, config)
    assert sp.battery_w == 3500.0
    assert sp.wallbox_w == 0.0

    deficit = make_inputs(p_pv_w=0.0, p_house_base_w=800.0, ev_connected=False)
    sp = compute_baseline_setpoints(deficit, config)
    assert sp.battery_w == -800.0

    full = make_inputs(p_pv_w=4000.0, soc_battery_pct=99.5, ev_connected=False)
    assert compute_baseline_setpoints(full, config).battery_w == 0.0
    empty = make_inputs(p_pv_w=0.0, p_house_base_w=800.0, soc_battery_pct=5.0, ev_connected=False)
    assert compute_baseline_setpoints(empty, config).battery_w == 0.0


# --- 14a-Abwurf-Reihenfolge ------------------------------------------------------------------


def test_14a_shed_order_battery_then_wallbox_then_heatpump() -> None:
    config = make_config()
    inputs = make_inputs(
        dso_14a_active=True,
        dso_14a_limit_w=4200.0,
        p_pv_w=0.0,
        p_house_base_w=0.0,
        t_buffer_c=40.0,
        ev_connected=False,
    )
    # Batterie 2000 + Wallbox 5000 + WP 2500 = 9500 > 4200.
    outputs, _, _ = evaluate_cascade(inputs, config, ok_result(5000.0, 2000.0, 2500.0), 10.0)
    assert outputs.cmd_battery_w == 0.0
    assert outputs.cmd_wallbox_w == 1700.0
    assert outputs.cmd_heatpump_w_est == 2500.0
    assert outputs.cmd_wallbox_w + outputs.cmd_heatpump_w_est == 4200.0


# --- Log-Deduplizierung & Heartbeat (Fix C1/K4) ----------------------------------------------


def test_should_log_decision_dedup_rules() -> None:
    from test_audit_logging import make_decision

    prev = make_decision()
    outputs = prev.outputs
    derivation = prev.derivation
    # Erster Log immer; MPC-Zyklus immer.
    assert should_log_decision(None, outputs, derivation, prev.trigger, False, None, 60) is True
    assert should_log_decision(prev, outputs, derivation, prev.trigger, True, 1.0, 60) is True
    # Unveränderter Zustand innerhalb des Heartbeats -> kein Log.
    assert should_log_decision(prev, outputs, derivation, prev.trigger, False, 30.0, 60) is False
    # Heartbeat abgelaufen -> Log.
    assert should_log_decision(prev, outputs, derivation, prev.trigger, False, 60.0, 60) is True
    # Sollwertänderung > 10 W -> Log.
    changed = outputs.model_copy(update={"cmd_wallbox_w": outputs.cmd_wallbox_w + 11.0})
    assert should_log_decision(prev, changed, derivation, prev.trigger, False, 1.0, 60) is True
    # Trigger-Wechsel -> Log.
    assert (
        should_log_decision(
            prev, outputs, derivation, TriggerReason.DSO_14A_DIMMING_EVENT, False, 1.0, 60
        )
        is True
    )


async def _make_scheduler(tmp_path: Path, **config_overrides: object) -> ControlScheduler:
    config = make_config(**config_overrides)
    state = LiveState(config)
    state.set_inputs(make_inputs(ev_connected=False))
    jsonl = JsonlLogger.from_config(config)
    store = SqliteAuditStore.from_config(config)
    await store.initialize()
    tracker = VerificationTracker(config, jsonl, store, delay_s=0.01)
    return ControlScheduler(config, state, jsonl, store, tracker, EmhassClient(config))


async def test_scheduler_dedup_heartbeat_and_verification_coupling(tmp_path: Path) -> None:
    scheduler = await _make_scheduler(tmp_path, data_dir=tmp_path)
    base_ns = 1_000_000_000
    first = await scheduler.control_cycle(now_monotonic_ns=base_ns)
    assert first is not None
    # Identischer Folgezyklus -> kein Log (Dedup), keine Verifikation.
    second = await scheduler.control_cycle(now_monotonic_ns=base_ns + 1_000_000_000)
    assert second is None
    # Heartbeat nach 60 s -> Log mit Trigger PERIODIC_1HZ (K4).
    heartbeat = await scheduler.control_cycle(now_monotonic_ns=base_ns + 61_000_000_000)
    assert heartbeat is not None
    assert heartbeat.trigger is TriggerReason.PERIODIC_1HZ
    # Folgezyklus nach Heartbeat: roher Trigger unverändert -> erneut Dedup.
    after_heartbeat = await scheduler.control_cycle(now_monotonic_ns=base_ns + 62_000_000_000)
    assert after_heartbeat is None
    # K4: Nur geloggte Entscheidungen erhalten Verifikations-Tasks (1:1 in SQLite).
    await asyncio.sleep(0.05)  # fällige Verifikations-Tasks abschließen lassen
    store = scheduler._sqlite
    lines = (tmp_path / "logs" / "decision_trace.jsonl").read_text().splitlines()
    decision_lines = [line for line in lines if '"event_type":"decision"' in line]
    assert len(decision_lines) == 2
    conn = store._require_conn()
    cursor = await conn.execute("SELECT COUNT(*) FROM decisions")
    row = await cursor.fetchone()
    assert row[0] == 2
    cursor = await conn.execute("SELECT COUNT(*) FROM decisions WHERE verified_at_utc IS NOT NULL")
    row = await cursor.fetchone()
    assert row[0] == 2
    await store.close()
