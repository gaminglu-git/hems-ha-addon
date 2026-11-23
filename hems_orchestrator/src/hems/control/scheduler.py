"""15-min-MPC-Loop & 1-Hz-Real-Time-Loop (docs/03 §4 Schritt 3, Fix C1/K4).

Log-Deduplizierung mit Heartbeat (audit_heartbeat_s); Verifikations-Scheduling
ausschließlich für tatsächlich geloggte Entscheidungen (K4). Alle I/O-Operationen
laufen nicht-blockierend; Solver-Fehler führen deterministisch zu Prio 4.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime

import aiomqtt

from hems.config import AppConfig
from hems.control.cascade import evaluate_cascade
from hems.io.emhass_client import EmhassClient, EmhassResult
from hems.io.mqtt_client import ActuatorPublisher, LiveState
from hems.logging.jsonl_logger import JsonlLogger
from hems.logging.sqlite_store import SqliteAuditStore
from hems.logging.verification import Clock, RealClock, VerificationTracker, format_utc_ms
from hems.models import (
    ControlOutputs,
    DecisionDerivation,
    DecisionTraceRecord,
    SystemInputs,
    TriggerReason,
)

logger = logging.getLogger(__name__)


def should_log_decision(
    previous: DecisionTraceRecord | None,
    outputs: ControlOutputs,
    derivation: DecisionDerivation,
    trigger: TriggerReason,
    is_mpc_cycle: bool,
    seconds_since_last_log: float | None,
    heartbeat_s: int,
    previous_trigger: TriggerReason | None = None,
) -> bool:
    """Log-Deduplizierung & Heartbeat (docs/03 Schritt 3, Fix C1).

    previous_trigger erlaubt den Vergleich gegen den rohen Kaskaden-Trigger des
    letzten geloggten Zyklus (Heartbeat-Logs speichern PERIODIC_1HZ, dürfen aber
    keinen künstlichen Trigger-Wechsel erzeugen).
    """
    if previous is None or is_mpc_cycle:
        return True
    if derivation.winning_priority != previous.derivation.winning_priority:
        return True
    reference_trigger = previous_trigger if previous_trigger is not None else previous.trigger
    if trigger is not reference_trigger:
        return True
    if abs(outputs.cmd_wallbox_w - previous.outputs.cmd_wallbox_w) > 10.0:
        return True
    if abs(outputs.cmd_battery_w - previous.outputs.cmd_battery_w) > 10.0:
        return True
    if outputs.cmd_heatpump_sg_ready is not previous.outputs.cmd_heatpump_sg_ready:
        return True
    return seconds_since_last_log is not None and seconds_since_last_log >= heartbeat_s


class ControlScheduler:
    """Orchestriert MPC- und Regel-Loop inkl. Persistenz und Aktor-Publikation."""

    def __init__(
        self,
        config: AppConfig,
        state: LiveState,
        jsonl_logger: JsonlLogger,
        sqlite_store: SqliteAuditStore,
        verification_tracker: VerificationTracker,
        emhass_client: EmhassClient,
        clock: Clock | None = None,
    ) -> None:
        self._config = config
        self._state = state
        self._jsonl = jsonl_logger
        self._sqlite = sqlite_store
        self._tracker = verification_tracker
        self._emhass = emhass_client
        self._clock: Clock = clock if clock is not None else RealClock()
        self._publisher = ActuatorPublisher(config.mqtt_topic_prefix)
        self._mqtt: aiomqtt.Client | None = None
        self._emhass_result: EmhassResult | None = None
        self._emhass_time_utc: datetime | None = None
        self._last_logged: DecisionTraceRecord | None = None
        self._last_raw_trigger: TriggerReason | None = None
        self._last_log_monotonic_ns: int | None = None
        self._decision_seq = 0
        self._mpc_pending = False
        self.last_outputs: ControlOutputs | None = None

    def attach_mqtt(self, client: aiomqtt.Client) -> None:
        self._mqtt = client

    async def mpc_cycle(self) -> None:
        """Ein 15-min-MPC-Lauf; Fehler werden geloggt und führen zu Prio 4."""
        inputs = self._state.inputs
        if inputs is None:
            return
        try:
            self._emhass_result = await self._emhass.optimize(inputs)
        except Exception as exc:  # noqa: BLE001 - jede Exception -> Prio-4-Fallback
            logger.error("EMHASS MPC cycle failed, falling back to Prio 4: %s", exc)
            self._emhass_result = None
        self._emhass_time_utc = self._clock.now_utc()
        self._mpc_pending = True

    async def control_cycle(
        self, now_monotonic_ns: int | None = None
    ) -> DecisionTraceRecord | None:
        """Ein 1-Hz-Regelzyklus; gibt den geloggten Record zurück (sonst None)."""
        inputs = self._state.inputs
        if inputs is None:
            return None
        now_ns = now_monotonic_ns if now_monotonic_ns is not None else time.monotonic_ns()
        cycle_start_real_ns = time.monotonic_ns()
        effective_config = self._config.model_copy(
            update={"operating_mode": self._state.operating_mode, "dry_run": self._state.dry_run}
        )
        # Setpoint-Alter in der Zeitquelle der Uhr (RealClock: Echtzeit, HIL: virtuell).
        setpoint_age_s = (
            (self._clock.now_utc() - self._emhass_time_utc).total_seconds()
            if self._emhass_time_utc is not None
            else None
        )
        outputs, derivation, trigger = evaluate_cascade(
            inputs, effective_config, self._emhass_result, setpoint_age_s
        )
        self.last_outputs = outputs

        is_mpc_cycle = self._mpc_pending
        self._mpc_pending = False
        seconds_since_last_log = (
            (now_ns - self._last_log_monotonic_ns) / 1e9
            if self._last_log_monotonic_ns is not None
            else None
        )
        if not should_log_decision(
            self._last_logged,
            outputs,
            derivation,
            trigger,
            is_mpc_cycle,
            seconds_since_last_log,
            effective_config.audit_heartbeat_s,
            previous_trigger=self._last_raw_trigger,
        ):
            await self._publish_outputs(outputs, effective_config.dry_run)
            return None

        # Fix K4: Heartbeat-only-Logs (keine Zustands-, Regel- oder Sollwertänderung)
        # erhalten deterministisch PERIODIC_1HZ; reine Sollwertänderungen ohne
        # Ereignis-Trigger werden SETPOINT_CHANGE.
        raw_trigger = trigger
        change_detected = (
            self._last_logged is None
            or derivation.winning_priority != self._last_logged.derivation.winning_priority
            or trigger is not self._last_raw_trigger
            or outputs != self._last_logged.outputs
        )
        if not is_mpc_cycle and self._last_logged is not None and not change_detected:
            trigger = TriggerReason.PERIODIC_1HZ
        elif (
            not is_mpc_cycle
            and trigger is TriggerReason.PERIODIC_1HZ
            and self._last_logged is not None
            and outputs != self._last_logged.outputs
        ):
            trigger = TriggerReason.SETPOINT_CHANGE

        record = self._build_record(inputs, effective_config, trigger, derivation, outputs)
        decision_monotonic_ns = now_ns
        await self._jsonl.append(record)
        await self._sqlite.insert_decision(record)
        await self._publish_outputs(outputs, effective_config.dry_run)
        if self._mqtt is not None:
            await self._publisher.publish_status(self._mqtt, record)
        # Latenz konsistent zur Zyklus-Zeitbasis (virtuell im HIL, real in Produktion).
        dispatch_monotonic_ns = now_ns + (time.monotonic_ns() - cycle_start_real_ns)

        # Fix K4: Verifikation NUR für tatsächlich geloggte Entscheidungen.
        self._tracker.schedule_verification(
            record,
            decision_monotonic_ns,
            dispatch_monotonic_ns,
            state_provider=self._current_inputs,
        )
        self._last_logged = record
        self._last_raw_trigger = raw_trigger
        self._last_log_monotonic_ns = now_ns
        return record

    def _current_inputs(self) -> SystemInputs:
        inputs = self._state.inputs
        if inputs is None:
            raise RuntimeError("LiveState has no inputs snapshot")
        return inputs

    def _build_record(
        self,
        inputs: SystemInputs,
        config: AppConfig,
        trigger: TriggerReason,
        derivation: DecisionDerivation,
        outputs: ControlOutputs,
    ) -> DecisionTraceRecord:
        self._decision_seq += 1
        now: datetime = self._clock.now_utc()
        decision_id = now.strftime("%Y-%m-%dT%H:%M:%SZ") + f"_{self._decision_seq:04d}"
        return DecisionTraceRecord(
            decision_id=decision_id,
            timestamp_utc=format_utc_ms(now),
            mode=config.operating_mode,
            dry_run=config.dry_run,
            trigger=trigger,
            inputs=inputs,
            derivation=derivation,
            outputs=outputs,
        )

    async def _publish_outputs(self, outputs: ControlOutputs, dry_run: bool) -> None:
        if self._mqtt is not None:
            await self._publisher.publish_outputs(self._mqtt, outputs, dry_run)

    async def run(self) -> None:
        """Startet MPC-Loop (15 min) und Real-Time-Loop (1 Hz) als asyncio.Tasks."""
        await asyncio.gather(self._run_mpc_loop(), self._run_control_loop())

    async def _run_mpc_loop(self) -> None:
        while True:
            await self.mpc_cycle()
            await self._clock.sleep(self._config.mpc_interval_s)

    async def _run_control_loop(self) -> None:
        while True:
            try:
                await self.control_cycle()
            except Exception as exc:  # noqa: BLE001 - Fehler loggen, Loop bleibt deterministisch
                logger.error("Control cycle failed: %s", exc)
            await self._clock.sleep(self._config.control_loop_interval_s)
