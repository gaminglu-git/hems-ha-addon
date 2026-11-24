"""Quasi-HIL CSV-Replay-Treiber (docs/04 §1, Fixes B3/B4/K5).

Deterministischer, zeitrafferfähiger Replay gegen den realen Regelkern inkl.
Logging & SQLite-Audit-DB. Zeitquelle ausschließlich das Clock-Protocol aus
docs/02 (SimulatedClock, kein immediate_verify_hook, kein reales asyncio.sleep
zwischen den Schritten).
"""

from __future__ import annotations

import asyncio
import csv
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from hems.config import AppConfig
from hems.control.scheduler import ControlScheduler
from hems.io.emhass_client import EmhassClient, EmhassResult
from hems.io.market_client import MarketForecast
from hems.io.mqtt_client import LiveState
from hems.logging.jsonl_logger import JsonlLogger
from hems.logging.sqlite_store import SqliteAuditStore
from hems.logging.verification import VerificationTracker
from hems.models import ControlOutputs, SystemInputs

# CSV-Schema (docs/04 §1.1, exakte Spalten).
CSV_COLUMNS = [
    "timestamp_utc",
    "p_pv_w",
    "p_house_base_w",
    "tariff_ct_kwh",
    "co2_intensity_g_kwh",
    "ev_connected",
    "ev_departure_in_min",
    "dso_14a_active",
    "dso_14a_limit_w",
]

# Physikalische Konstanten des Anlagenmodells (docs/04 §1.2).
_ETA_CHG = 0.95
_ETA_DIS = 0.95
_EV_CHARGE_EFFICIENCY = 0.92
_BUFFER_LOSS_C_PER_S = 0.0005
_HEATPUMP_COP = 3.5
_BUFFER_CAPACITY_WH_PER_K = 1500.0


class PlantInitialState(BaseModel):
    """Deterministische Anfangszustände (Fix B3, verbindliche Defaults)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    soc_battery_pct: float = Field(default=50.0, ge=0.0, le=100.0)
    soc_ev_pct: float = Field(default=30.0, ge=0.0, le=100.0)
    t_buffer_c: float = Field(default=48.0, ge=10.0, le=85.0)


class ReplayConfig(BaseModel):
    """Replay-Konfiguration (docs/04 §1.1.1)."""

    model_config = ConfigDict(extra="forbid")

    initial_state: PlantInitialState | None = None
    time_scale: float = 1.0


class FaultScenario(BaseModel):
    """Injizierbarer Störfall über das Schritt-Intervall [start_step, end_step]."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["INJECT_14A_DIMMING", "INJECT_SOLVER_TIMEOUT", "INJECT_NEGATIVE_TARIFF"]
    start_step: int = Field(ge=0)
    end_step: int = Field(ge=0)
    limit_w: float = 4200.0
    tariff_ct_kwh: float = -5.0

    def is_active(self, step: int) -> bool:
        return self.start_step <= step <= self.end_step


def _parse_bool(value: str) -> bool:
    return value.strip().lower() in ("true", "1", "yes")


def _parse_timestamp(value: str) -> datetime:
    text = value.strip().replace("Z", "+00:00")
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


class ScenarioRow(BaseModel):
    """Eine CSV-Zeile des Szenarios (exaktes Schema, docs/04 §1.1)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    timestamp_utc: datetime
    p_pv_w: float = Field(ge=0.0)
    p_house_base_w: float = Field(ge=0.0)
    tariff_ct_kwh: float
    co2_intensity_g_kwh: float = Field(ge=0.0)
    ev_connected: bool
    ev_departure_in_min: float | None = None
    dso_14a_active: bool
    dso_14a_limit_w: float


def load_scenario(csv_path: Path) -> list[ScenarioRow]:
    with csv_path.open(encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames != CSV_COLUMNS:
            raise ValueError(
                f"CSV schema mismatch: expected {CSV_COLUMNS}, got {reader.fieldnames}"
            )
        rows: list[ScenarioRow] = []
        for raw in reader:
            departure = raw["ev_departure_in_min"].strip()
            rows.append(
                ScenarioRow(
                    timestamp_utc=_parse_timestamp(raw["timestamp_utc"]),
                    p_pv_w=float(raw["p_pv_w"]),
                    p_house_base_w=float(raw["p_house_base_w"]),
                    tariff_ct_kwh=float(raw["tariff_ct_kwh"]),
                    co2_intensity_g_kwh=float(raw["co2_intensity_g_kwh"]),
                    ev_connected=_parse_bool(raw["ev_connected"]),
                    ev_departure_in_min=float(departure) if departure else None,
                    dso_14a_active=_parse_bool(raw["dso_14a_active"]),
                    dso_14a_limit_w=float(raw["dso_14a_limit_w"]),
                )
            )
    return rows


class PlantSimulator:
    """Einfaches physikalisches Anlagen-Emulationsmodell (docs/04 §1.2)."""

    def __init__(self, config: AppConfig, initial_state: PlantInitialState | None = None) -> None:
        self._config = config
        state = initial_state if initial_state is not None else PlantInitialState()
        self.soc_battery_pct = state.soc_battery_pct
        self.soc_ev_pct = state.soc_ev_pct
        self.t_buffer_c = state.t_buffer_c
        self.p_grid_w = 0.0

    def step(
        self,
        outputs: ControlOutputs,
        p_pv_w: float,
        p_house_base_w: float,
        ev_connected: bool,
        dt_s: float,
    ) -> float:
        """Aktualisiert die Zustände deterministisch und liefert p_grid_w."""
        hours = dt_s / 3600.0

        # Batterie-SoC (Ladung > 0, Entladung < 0).
        if outputs.cmd_battery_w >= 0.0:
            delta_e_bat = outputs.cmd_battery_w * _ETA_CHG * hours
        else:
            delta_e_bat = outputs.cmd_battery_w / _ETA_DIS * hours
        if self._config.battery_capacity_wh > 0.0:
            self.soc_battery_pct = max(
                0.0,
                min(
                    100.0,
                    self.soc_battery_pct
                    + (delta_e_bat / self._config.battery_capacity_wh) * 100.0,
                ),
            )

        # EV-SoC (nur wenn angeschlossen).
        if ev_connected and self._config.ev_capacity_wh > 0.0:
            delta_e_ev = outputs.cmd_wallbox_w * _EV_CHARGE_EFFICIENCY * hours
            self.soc_ev_pct = max(
                0.0,
                min(100.0, self.soc_ev_pct + (delta_e_ev / self._config.ev_capacity_wh) * 100.0),
            )

        # Pufferspeicher-Temperatur: Verlust + Wärmeeintrag (COP, C_th).
        thermal_gain_wh = outputs.cmd_heatpump_w_est * _HEATPUMP_COP * hours
        self.t_buffer_c += (
            -_BUFFER_LOSS_C_PER_S * dt_s + thermal_gain_wh / _BUFFER_CAPACITY_WH_PER_K
        )

        # Resultierende Netzleistung (Bezug > 0, Einspeisung < 0).
        self.p_grid_w = (
            p_house_base_w
            + outputs.cmd_wallbox_w
            + outputs.cmd_heatpump_w_est
            + outputs.cmd_battery_w
            - p_pv_w
        )
        return self.p_grid_w


class SimulatedClock:
    """Virtuelle Uhr (Fix B4/K5): Implementierung des Clock-Protocols aus docs/02."""

    def __init__(self, start_utc: datetime) -> None:
        self.current_utc = start_utc
        self._waiters: list[tuple[datetime, asyncio.Event, asyncio.Task | None]] = []

    def now_utc(self) -> datetime:
        return self.current_utc

    async def sleep(self, seconds: float) -> None:
        event = asyncio.Event()
        wake_at = self.current_utc + timedelta(seconds=seconds)
        self._waiters.append((wake_at, event, asyncio.current_task()))
        await event.wait()

    async def advance_to(self, next_utc: datetime) -> None:
        """Setzt die virtuelle Zeit und macht alle fälligen Tasks ausführbar."""
        self.current_utc = next_utc
        fired_tasks: list[asyncio.Task] = []
        for _ in range(4):
            # Waiter, deren Zeitpunkt erreicht ist, wecken und merken.
            remaining: list[tuple[datetime, asyncio.Event, asyncio.Task | None]] = []
            for wake_at, event, task in self._waiters:
                if wake_at <= self.current_utc and not event.is_set():
                    event.set()
                    if task is not None and task is not asyncio.current_task():
                        fired_tasks.append(task)
                elif not event.is_set():
                    remaining.append((wake_at, event, task))
            self._waiters = remaining
            # Event-Loop-Kontrolle: geweckte Tasks laufen weiter und registrieren
            # ggf. neue Waiter (z. B. Folge-Sleeps), die im nächsten Durchlauf
            # gegen dieselbe virtuelle Zeit geprüft werden.
            await asyncio.sleep(0)
        if fired_tasks:
            await asyncio.gather(*dict.fromkeys(fired_tasks))

    async def advance_by(self, seconds: float) -> None:
        await self.advance_to(self.current_utc + timedelta(seconds=seconds))


class FaultInjectingEmhassClient(EmhassClient):
    """EMHASS-Client-Wrapper: injiziert Solver-Timeouts im aktiven Fault-Fenster."""

    def __init__(self, config: AppConfig) -> None:
        super().__init__(config)
        self._faults: list[FaultScenario] = []
        self._current_step = 0

    def set_faults(self, faults: list[FaultScenario], current_step: int) -> None:
        self._faults = faults
        self._current_step = current_step

    def timeout_fault_active(self) -> bool:
        """True, wenn im aktuellen Schritt ein Solver-Timeout injiziert ist."""
        return any(
            fault.kind == "INJECT_SOLVER_TIMEOUT" and fault.is_active(self._current_step)
            for fault in self._faults
        )

    async def optimize(
        self,
        inputs: SystemInputs,
        aggregated_load_w: list[float] | None = None,
        market_forecast: MarketForecast | None = None,
    ) -> EmhassResult:
        if self.timeout_fault_active():
            return EmhassResult(
                status="timeout",
                solve_ms=15000,
                setpoints=None,
                runtimeparams_hash=None,
            )
        return await super().optimize(inputs, aggregated_load_w, market_forecast)


class ReplayDriver:
    """Fährt ein CSV-Szenario deterministisch gegen den realen Regelkern."""

    def __init__(
        self,
        config: AppConfig,
        replay_config: ReplayConfig | None = None,
        faults: list[FaultScenario] | None = None,
        emhass_client: EmhassClient | None = None,
    ) -> None:
        self._config = config
        self._replay_config = replay_config if replay_config is not None else ReplayConfig()
        self._faults = faults if faults is not None else []
        self._plant = PlantSimulator(config, self._replay_config.initial_state)
        self._state = LiveState(config)
        self._jsonl = JsonlLogger.from_config(config)
        self._store = SqliteAuditStore.from_config(config)
        # Default: FaultInjectingEmhassClient. Der Benchmark injiziert einen
        # deterministischen Offline-Optimierer (gleiches Interface inkl. set_faults).
        self._emhass = (
            emhass_client
            if emhass_client is not None
            else FaultInjectingEmhassClient(config)
        )
        self._clock: SimulatedClock | None = None
        self._scheduler: ControlScheduler | None = None
        self._tracker: VerificationTracker | None = None
        self._mqtt: object | None = None

    def attach_mqtt(self, client: object) -> None:
        """Optional: MQTT-Client (oder Test-Stub) für Aktor-Publikationen anbinden."""
        self._mqtt = client
        if self._scheduler is not None:
            self._scheduler.attach_mqtt(client)  # type: ignore[arg-type]

    @property
    def scheduler(self) -> ControlScheduler:
        if self._scheduler is None:
            raise RuntimeError("ReplayDriver not initialized; call run() first")
        return self._scheduler

    @property
    def store(self) -> SqliteAuditStore:
        return self._store

    def _row_inputs(self, row: ScenarioRow, step: int) -> SystemInputs:
        dso_active = row.dso_14a_active
        dso_limit = row.dso_14a_limit_w
        tariff = row.tariff_ct_kwh
        for fault in self._faults:
            if not fault.is_active(step):
                continue
            if fault.kind == "INJECT_14A_DIMMING":
                dso_active = True
                dso_limit = fault.limit_w
            elif fault.kind == "INJECT_NEGATIVE_TARIFF":
                tariff = fault.tariff_ct_kwh
        return SystemInputs(
            p_grid_before_w=self._plant.p_grid_w,
            p_pv_w=row.p_pv_w,
            p_house_base_w=row.p_house_base_w,
            soc_battery_pct=self._plant.soc_battery_pct,
            soc_ev_pct=self._plant.soc_ev_pct,
            ev_connected=row.ev_connected,
            ev_departure_in_min=row.ev_departure_in_min,
            t_buffer_c=self._plant.t_buffer_c,
            tariff_ct_kwh=tariff,
            co2_intensity_g_kwh=row.co2_intensity_g_kwh,
            dso_14a_active=dso_active,
            dso_14a_limit_w=dso_limit,
            eeg_feedin_limit_pct=self._config.eeg_feedin_limit_pct,
        )

    async def run(self, csv_path: Path) -> None:
        rows = load_scenario(csv_path)
        if len(rows) < 2:
            raise ValueError("Scenario needs at least two rows")

        await self._store.initialize()
        self._clock = SimulatedClock(rows[0].timestamp_utc)
        self._tracker = VerificationTracker(self._config, self._jsonl, self._store, self._clock)
        self._scheduler = ControlScheduler(
            self._config,
            self._state,
            self._jsonl,
            self._store,
            self._tracker,
            self._emhass,
            clock=self._clock,
        )
        if self._mqtt is not None:
            self._scheduler.attach_mqtt(self._mqtt)  # type: ignore[arg-type]

        epoch = datetime(1970, 1, 1, tzinfo=UTC)
        try:
            for step, row in enumerate(rows):
                self._emhass.set_faults(self._faults, step)
                inputs = self._row_inputs(row, step)
                self._state.set_inputs(inputs)
                virtual_ns = int((row.timestamp_utc - epoch).total_seconds() * 1e9)

                # MPC-Zyklus in virtueller Zeit (mpc_interval_s).
                elapsed_s = (row.timestamp_utc - rows[0].timestamp_utc).total_seconds()
                if elapsed_s % self._config.mpc_interval_s == 0:
                    await self._scheduler.mpc_cycle()
                await self._scheduler.control_cycle(now_monotonic_ns=virtual_ns)
                # Fix: Dem per create_task gestarteten Verifikations-Task Gelegenheit
                # geben, seinen clock.sleep-Waiter bei der AKTUELLEN virtuellen Zeit zu
                # registrieren, bevor advance_to die Uhr weitersetzt. Sonst entsteht
                # ein Schritt Versatz und die letzte Verifikation geht verloren.
                await asyncio.sleep(0)
                await asyncio.sleep(0)

                outputs = self._scheduler.last_outputs
                if outputs is None:
                    raise RuntimeError("Control cycle produced no outputs")
                if step + 1 < len(rows):
                    dt_s = (rows[step + 1].timestamp_utc - row.timestamp_utc).total_seconds()
                    self._plant.step(
                        outputs, row.p_pv_w, row.p_house_base_w, row.ev_connected, dt_s
                    )
                    # Folgezustand für die Verifikation: aktueller Snapshot trägt
                    # den neuen p_grid_w und die aktualisierten Speicherzustände.
                    follow_up = self._row_inputs(rows[step + 1], step + 1)
                    self._state.set_inputs(follow_up)
                    await self._clock.advance_to(rows[step + 1].timestamp_utc)

            # Letzten Schritt sauber verifizieren (docs/04 §1.5).
            await self._clock.advance_by(self._config.verification_delay_s + 1.0)
        finally:
            await self._store.close()
