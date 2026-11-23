"""Kanonische Pydantic-v2-Datenmodelle (docs/02, Phase 2).

Alle Modelle sind unveränderlich (`frozen=True`) und strikt (`extra="forbid"`).
Einheiten- und Vorzeichenkonvention gemäß docs/00: Leistung in W (Netzbezug > 0,
Einspeisung < 0; Batterieladung > 0, Entladung < 0; PV/Verbraucher >= 0),
Energie in Wh, SoC in Prozent (0–100), Zeitstempel UTC ISO-8601 mit Millisekunden.
"""

from __future__ import annotations

from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from hems.config import OperatingMode


class TriggerReason(str, Enum):
    """Auslöser einer Steuerungsentscheidung (docs/02 §1.1)."""

    MPC_CYCLE = "mpc_cycle"
    PERIODIC_1HZ = "periodic_1hz"
    DSO_14A_DIMMING_EVENT = "dso_14a_dimming_event"
    EEG_FEEDIN_LIMIT_EVENT = "eeg_feedin_limit_event"
    HOUSE_FUSE_PROTECTION = "house_fuse_protection"
    COMFORT_GUARD_OVERRIDE = "comfort_guard_override"
    SOLVER_TIMEOUT_FALLBACK = "solver_timeout_fallback"
    SETPOINT_CHANGE = "setpoint_change"


class HeatpumpSGReadyMode(str, Enum):
    """SG-Ready-Zustände der Wärmepumpe (docs/02 §1.2)."""

    BLOCKED = "blocked"
    NORMAL = "normal"
    RECOMMENDED = "recommended"
    FORCED = "forced"


class SystemInputs(BaseModel):
    """Vollständiger Eingangsdaten-Snapshot (docs/02 §1.3)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    p_grid_before_w: float
    p_pv_w: float = Field(ge=0.0)
    p_house_base_w: float = Field(ge=0.0)
    soc_battery_pct: float = Field(ge=0.0, le=100.0)
    soc_ev_pct: float = Field(ge=0.0, le=100.0)
    ev_connected: bool
    ev_departure_in_min: float | None = None
    t_buffer_c: float
    tariff_ct_kwh: float
    co2_intensity_g_kwh: float = Field(ge=0.0)
    dso_14a_active: bool
    dso_14a_limit_w: float
    eeg_feedin_limit_pct: float = Field(ge=0.0, le=100.0)
    pv_forecast_next_1h_w: float = 0.0

    @model_validator(mode="after")
    def _validate_14a_limit(self) -> SystemInputs:
        if self.dso_14a_active and self.dso_14a_limit_w < 4200.0:
            raise ValueError(
                "dso_14a_limit_w must be >= 4200.0 W when dso_14a_active is true "
                "(§ 14a EnWG / BNetzA BK6-22-300)"
            )
        return self


class ProposedSetpoints(BaseModel):
    """Vorgeschlagene Sollwerte (EMHASS oder Baseline), docs/02 §1.4."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    wallbox_w: float = Field(ge=0.0)
    battery_w: float
    heatpump_w: float = Field(ge=0.0)


class DecisionDerivation(BaseModel):
    """Vollständige Ableitungskette einer Entscheidung (docs/02 §1.5)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    emhass_status: Literal["ok", "timeout", "error", "stale", "disabled"]
    emhass_solve_ms: int | None
    runtimeparams_hash: str | None
    emhass_proposed_setpoints_w: ProposedSetpoints | None
    cascade_evaluated_rules: list[str]
    winning_priority: Literal[1, 2, 3, 4]
    override_reason: str | None


class ControlOutputs(BaseModel):
    """Finale Aktor-Sollwerte nach der Prioritätskaskade (docs/02 §1.6)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    cmd_wallbox_w: float = Field(ge=0.0)
    cmd_battery_w: float
    cmd_heatpump_sg_ready: HeatpumpSGReadyMode
    cmd_heatpump_w_est: float = Field(ge=0.0)


class VerificationResult(BaseModel):
    """Ergebnis des verzögerten §14a-Wirkungsnachweises (docs/02 §1.7)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    check_after_s: float
    verified_at_utc: str
    p_grid_after_w: float
    controllable_load_after_w: float
    compliance_ok: bool
    e2e_latency_ms: int


class DecisionTraceRecord(BaseModel):
    """Append-only Entscheidungsdatensatz (docs/02 §1.8)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    event_type: Literal["decision"] = "decision"
    decision_id: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z_\d{4}$")
    timestamp_utc: str
    mode: OperatingMode
    dry_run: bool
    trigger: TriggerReason
    inputs: SystemInputs
    derivation: DecisionDerivation
    outputs: ControlOutputs


class VerificationTraceRecord(BaseModel):
    """Append-only Verifikationsdatensatz, verknüpft per decision_id (docs/02 §1.9)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    event_type: Literal["verification"] = "verification"
    decision_id: str
    verification: VerificationResult
