"""Deterministische 4-Stufen-Prioritätskaskade (docs/03 §4) – sicherheitskritisch.

Reine, seiteneffektfreie Funktion. Auswertungsreihenfolge (keine Abweichung erlaubt):
Schritt 0 (P3/P4) -> Schritt 1 (P2) -> Schritt 1.5 (SG-Ready-Vorab-Quantisierung, K1)
-> Schritt 2 (P1: §14a, §9 EEG mit K6-Re-Clamp, Hausanschluss) -> Schritt 2.5
(globaler Wallbox-Post-Clamp, A2). P1 überschreibt immer P2/P3/P4.
"""

from __future__ import annotations

from hems.config import AppConfig, OperatingMode
from hems.control.modes import compute_baseline_setpoints, map_heatpump_power_to_sg_ready
from hems.io.emhass_client import EmhassResult
from hems.models import (
    ControlOutputs,
    DecisionDerivation,
    HeatpumpSGReadyMode,
    ProposedSetpoints,
    SystemInputs,
    TriggerReason,
)


def reclamp_battery_after_eeg(
    battery_w: float,
    wallbox_w: float,
    heatpump_w: float,
    inputs: SystemInputs,
    limit_14a: float,
) -> tuple[float, bool]:
    """§14a-Re-Clamp nach §9-EEG-Ladungserhöhung (Fix A1 + Guard K6).

    Darf nur positive Batterieladung zurücknehmen (battery_w > 0.0), niemals eine
    bestehende Entladung auf 0.0 clampen. Rückgabe: (battery_w, reclamped).
    """
    if inputs.dso_14a_active and battery_w > 0.0:
        p_steuve_grid = max(
            0.0,
            wallbox_w + heatpump_w + battery_w - max(0.0, inputs.p_pv_w - inputs.p_house_base_w),
        )
        if p_steuve_grid > limit_14a:
            excess = p_steuve_grid - limit_14a
            return max(0.0, battery_w - excess), True
    return battery_w, False


def evaluate_cascade(
    inputs: SystemInputs,
    config: AppConfig,
    emhass_result: EmhassResult | None,
    setpoint_age_s: float | None,
) -> tuple[ControlOutputs, DecisionDerivation, TriggerReason]:
    """Wertung der Prioritätskaskade; liefert finale Sollwerte + Ableitungskette."""
    rules: list[str] = []
    override_reason: str | None = None

    # --- Schritt 0: Initiale Sollwert-Bestimmung (Prio 3 vs. Prio 4) ---------------
    if config.operating_mode is OperatingMode.A_BASELINE:
        setpoints = compute_baseline_setpoints(inputs, config)
        winning_priority = 3
        emhass_status = "disabled"
        trigger = TriggerReason.PERIODIC_1HZ
    else:
        usable = (
            emhass_result is not None
            and emhass_result.status == "ok"
            and emhass_result.setpoints is not None
            and setpoint_age_s is not None
            and setpoint_age_s <= config.mpc_interval_s + 60.0
        )
        if usable:
            assert emhass_result is not None and emhass_result.setpoints is not None
            setpoints = emhass_result.setpoints
            winning_priority = 3
            emhass_status = "ok"
            trigger = TriggerReason.PERIODIC_1HZ
        else:
            baseline = compute_baseline_setpoints(inputs, config)
            setpoints = ProposedSetpoints(
                wallbox_w=min(baseline.wallbox_w, config.dso_14a_min_power_w),
                battery_w=baseline.battery_w,
                heatpump_w=baseline.heatpump_w,
            )
            winning_priority = 4
            trigger = TriggerReason.SOLVER_TIMEOUT_FALLBACK
            rules.append("P4_fallback_active")
            if emhass_result is None or emhass_result.status == "ok":
                emhass_status = "stale"
            else:
                emhass_status = emhass_result.status

    wallbox_w = setpoints.wallbox_w
    battery_w = setpoints.battery_w
    heatpump_w = setpoints.heatpump_w

    # --- Schritt 1: Prio 2 — Harte Nutzer-Komfortgrenzen ---------------------------
    if inputs.t_buffer_c < config.heatpump_temp_min_c:
        heatpump_w = config.heatpump_nominal_power_w
        rules.append("P2_hp_temp_below_min")
        winning_priority = min(winning_priority, 2)
    elif inputs.t_buffer_c > config.heatpump_temp_max_c:
        heatpump_w = 0.0
        rules.append("P2_hp_temp_above_max")

    if (
        inputs.ev_connected
        and inputs.ev_departure_in_min is not None
        and inputs.ev_departure_in_min > 0.0
        and inputs.soc_ev_pct < config.ev_target_soc_pct
    ):
        e_missing_wh = (
            (config.ev_target_soc_pct - inputs.soc_ev_pct) / 100.0
        ) * config.ev_capacity_wh
        p_required_w = e_missing_wh / (inputs.ev_departure_in_min / 60.0)
        # Fix A2: Mindestleistungs-Aufrundung VOR min(..., wallbox_max_power_w).
        if 0.0 < p_required_w < config.wallbox_min_power_w:
            p_required_w = config.wallbox_min_power_w
        if p_required_w > wallbox_w:
            wallbox_w = min(p_required_w, config.wallbox_max_power_w)
            rules.append("P2_ev_target_soc_guard")
            winning_priority = 2
            trigger = TriggerReason.COMFORT_GUARD_OVERRIDE

    # --- Schritt 1.5: SG-Ready-Vorab-Quantisierung (Fix K1) ------------------------
    sg_ready, heatpump_w = map_heatpump_power_to_sg_ready(
        heatpump_w,
        config,
        force_blocked=False,
        force_boost=(inputs.t_buffer_c < config.heatpump_temp_min_c),
    )

    # --- Schritt 2: Prio 1 — Sicherheit & Regulatorik ------------------------------
    pv_surplus_offset_w = max(0.0, inputs.p_pv_w - inputs.p_house_base_w)
    limit_14a = max(inputs.dso_14a_limit_w, config.dso_14a_min_power_w)

    # § 14a EnWG Netzbetreiber-Dimmung (EMS-Saldierungsprivileg, Fix A1/C5).
    if inputs.dso_14a_active:
        p_steuve_grid_w = max(
            0.0, wallbox_w + heatpump_w + max(0.0, battery_w) - pv_surplus_offset_w
        )
        if p_steuve_grid_w > limit_14a:
            rule = f"P1_14a_limit_violated_{int(p_steuve_grid_w)}W_gt_{int(limit_14a)}W"
            rules.append(rule)
            winning_priority = 1
            trigger = TriggerReason.DSO_14A_DIMMING_EVENT
            override_reason = rule
            needed_w = p_steuve_grid_w - limit_14a
            # Abwurf-Reihenfolge: (1) Batterieladung, (2) Wallbox, (3) Wärmepumpe.
            if battery_w > 0.0:
                reduction = min(battery_w, needed_w)
                battery_w -= reduction
                needed_w -= reduction
            if needed_w > 0.0 and wallbox_w > 0.0:
                target = wallbox_w - needed_w
                if target < config.wallbox_min_power_w:
                    target = 0.0
                needed_w -= wallbox_w - target
                wallbox_w = target
            if needed_w > 0.0 and heatpump_w > 0.0:
                heatpump_w = 0.0
                sg_ready = HeatpumpSGReadyMode.BLOCKED
        else:
            rules.append("P1_14a_limit_ok")

    # § 9 EEG Einspeisebegrenzung.
    max_feedin_w = config.pv_peak_w * (inputs.eeg_feedin_limit_pct / 100.0)
    p_grid_est_w = inputs.p_house_base_w + wallbox_w + heatpump_w + battery_w - inputs.p_pv_w
    if p_grid_est_w < -max_feedin_w:
        excess_feedin_w = abs(p_grid_est_w) - max_feedin_w
        if inputs.soc_battery_pct < 99.0:
            battery_w += min(excess_feedin_w, config.battery_max_charge_w - battery_w)
        # Fix A1 + Guard K6: Re-Check/Re-Clamp VOR dem Hausanschlussschutz.
        battery_w, reclamped = reclamp_battery_after_eeg(
            battery_w, wallbox_w, heatpump_w, inputs, limit_14a
        )
        if reclamped:
            rules.append("P1_14a_reclamp_after_eeg")
        rules.append("P1_eeg_feedin_limit_active")
        winning_priority = 1
        trigger = TriggerReason.EEG_FEEDIN_LIMIT_EVENT
    else:
        rules.append("P1_eeg_feedin_ok")

    # Hausanschlussschutz (Hauptsicherung).
    p_grid_est_w = inputs.p_house_base_w + wallbox_w + heatpump_w + battery_w - inputs.p_pv_w
    if p_grid_est_w > config.house_fuse_limit_w:
        overload_w = p_grid_est_w - config.house_fuse_limit_w
        if battery_w > 0.0:
            reduction = min(battery_w, overload_w)
            battery_w -= reduction
            overload_w -= reduction
        if overload_w > 0.0 and inputs.soc_battery_pct > 10.0:
            extra_discharge = min(overload_w, config.battery_max_discharge_w + battery_w)
            battery_w -= extra_discharge
            overload_w -= extra_discharge
        if overload_w > 0.0 and wallbox_w > 0.0:
            target = max(0.0, wallbox_w - overload_w)
            overload_w -= wallbox_w - target
            wallbox_w = target
        if overload_w > 0.0 and heatpump_w > 0.0:
            heatpump_w = 0.0
            sg_ready = HeatpumpSGReadyMode.BLOCKED
        rules.append("P1_house_fuse_violated")
        winning_priority = 1
        trigger = TriggerReason.HOUSE_FUSE_PROTECTION
    else:
        rules.append("P1_house_fuse_ok")

    # --- Schritt 2.5: Globale Aktor-Normalisierung (Fix A2) ------------------------
    if 0.0 < wallbox_w < config.wallbox_min_power_w:
        wallbox_w = 0.0
        rules.append("POST_wallbox_below_min_clamped_to_0")
    battery_w = max(-config.battery_max_discharge_w, min(config.battery_max_charge_w, battery_w))
    heatpump_w = max(0.0, min(config.heatpump_nominal_power_w, heatpump_w))

    # Fix K1: Keine erneute SG-Ready-Quantisierung nach P1.
    outputs = ControlOutputs(
        cmd_wallbox_w=wallbox_w,
        cmd_battery_w=battery_w,
        cmd_heatpump_sg_ready=sg_ready,
        cmd_heatpump_w_est=heatpump_w,
    )
    derivation = DecisionDerivation(
        emhass_status=emhass_status,
        emhass_solve_ms=emhass_result.solve_ms if emhass_result is not None else None,
        runtimeparams_hash=emhass_result.runtimeparams_hash if emhass_result is not None else None,
        emhass_proposed_setpoints_w=(
            emhass_result.setpoints if emhass_result is not None else None
        ),
        cascade_evaluated_rules=rules,
        winning_priority=winning_priority,
        override_reason=override_reason,
    )
    return outputs, derivation, trigger
