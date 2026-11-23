"""Betriebsmodi: Baseline-Sollwerte (Modus A / Prio-4-Fallback) und SG-Ready-Mapping.

docs/03 §3. Das SG-Ready-Mapping wird verbindlich in Schritt 1.5 der Kaskade
aufgerufen (Vorab-Quantisierung vor Prio 1, Fix K1).
"""

from __future__ import annotations

from hems.config import AppConfig
from hems.models import HeatpumpSGReadyMode, ProposedSetpoints, SystemInputs


def compute_baseline_setpoints(inputs: SystemInputs, config: AppConfig) -> ProposedSetpoints:
    """Klassischer Eigenverbrauch (Modus A) und sicherer Prio-4-Fallback."""
    p_surplus_w = inputs.p_pv_w - inputs.p_house_base_w

    if p_surplus_w > 0.0 and inputs.soc_battery_pct < 99.0:
        battery_w = min(p_surplus_w, config.battery_max_charge_w)
    elif p_surplus_w < 0.0 and inputs.soc_battery_pct > 10.0:
        battery_w = -min(abs(p_surplus_w), config.battery_max_discharge_w)
    else:
        battery_w = 0.0

    if inputs.ev_connected and inputs.soc_ev_pct < config.ev_target_soc_pct:
        wallbox_w = config.wallbox_max_power_w
    else:
        wallbox_w = 0.0

    if inputs.t_buffer_c < config.heatpump_temp_min_c:
        heatpump_w = config.heatpump_nominal_power_w
    else:
        heatpump_w = 0.0

    return ProposedSetpoints(wallbox_w=wallbox_w, battery_w=battery_w, heatpump_w=heatpump_w)


def map_heatpump_power_to_sg_ready(
    heatpump_w: float,
    config: AppConfig,
    force_blocked: bool = False,
    force_boost: bool = False,
) -> tuple[HeatpumpSGReadyMode, float]:
    """Verbindliche Mapping-Tabelle (docs/03 §3, Fix B1) in Auswertungsreihenfolge.

    Rückgabe: (cmd_heatpump_sg_ready, cmd_heatpump_w_est). BLOCKED (Zustand 1)
    ist der EVU-/P1-Sperre vorbehalten; Komfortabschaltungen nutzen NORMAL.
    """
    if force_blocked:
        return HeatpumpSGReadyMode.BLOCKED, 0.0
    if force_boost:
        return HeatpumpSGReadyMode.FORCED, config.heatpump_nominal_power_w
    if heatpump_w <= 0.0:
        return HeatpumpSGReadyMode.NORMAL, 0.0
    if heatpump_w < 0.8 * config.heatpump_nominal_power_w:
        return HeatpumpSGReadyMode.RECOMMENDED, 0.5 * config.heatpump_nominal_power_w
    return HeatpumpSGReadyMode.FORCED, config.heatpump_nominal_power_w
