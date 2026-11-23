"""Asynchroner §14a-Wirkungsnachweis-Tracker (docs/02 §4, inkl. K2/K4/K5).

- K2: saldierungskonsistente Aggregat-Formel für controllable_load_after_w.
- K4: schedule_verification nur für tatsächlich persistierte Entscheidungen.
- K5: einheitliches Clock-Protocol (RealClock in Produktion, SimulatedClock im HIL).
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Protocol

from hems.config import AppConfig
from hems.logging.jsonl_logger import JsonlLogger
from hems.logging.sqlite_store import SqliteAuditStore
from hems.models import (
    DecisionTraceRecord,
    SystemInputs,
    TriggerReason,
    VerificationResult,
    VerificationTraceRecord,
)


class Clock(Protocol):
    """Einheitliche Zeitquelle (Fix K5) – einzige Uhr des VerificationTrackers."""

    def now_utc(self) -> datetime: ...

    async def sleep(self, seconds: float) -> None: ...


class RealClock:
    """Produktions-Uhr: echte UTC-Zeit und asyncio.sleep."""

    def now_utc(self) -> datetime:
        return datetime.now(UTC)

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds)


def format_utc_ms(value: datetime) -> str:
    """UTC ISO-8601 mit Millisekunden: YYYY-MM-DDTHH:MM:SS.sssZ (docs/00)."""
    return value.strftime("%Y-%m-%dT%H:%M:%S.") + f"{value.microsecond // 1000:03d}Z"


class VerificationTracker:
    """Plant verzögerte Wirkungsnachweise als nicht-blockierende asyncio.Tasks."""

    def __init__(
        self,
        config: AppConfig,
        jsonl_logger: JsonlLogger,
        sqlite_store: SqliteAuditStore,
        clock: Clock | None = None,
        delay_s: float | None = None,
    ) -> None:
        self._config = config
        self._jsonl = jsonl_logger
        self._sqlite = sqlite_store
        self._clock: Clock = clock if clock is not None else RealClock()
        # delay_s-Override nur für Tests/HIL; Produktion nutzt config.verification_delay_s.
        self._delay_s = delay_s if delay_s is not None else config.verification_delay_s

    def schedule_verification(
        self,
        decision: DecisionTraceRecord,
        decision_monotonic_ns: int,
        dispatch_monotonic_ns: int,
        state_provider: Callable[[], SystemInputs],
    ) -> asyncio.Task[None]:
        """Plant den Wirkungsnachweis nach verification_delay_s.

        Task-Hygiene (Fix K4): Darf ausschließlich für Entscheidungen aufgerufen
        werden, für die tatsächlich ein DecisionTraceRecord persistiert wurde
        (JSONL + SQLite) – garantiert 1:1-Fremdschlüssel-Integrität.
        """
        return asyncio.create_task(
            self._verify(decision, decision_monotonic_ns, dispatch_monotonic_ns, state_provider)
        )

    async def _verify(
        self,
        decision: DecisionTraceRecord,
        decision_monotonic_ns: int,
        dispatch_monotonic_ns: int,
        state_provider: Callable[[], SystemInputs],
    ) -> None:
        await self._clock.sleep(self._delay_s)
        current = state_provider()

        # K2: saldierungskonsistente Aggregat-Formel (EMS-Saldierungsmodell).
        controllable_load_after_w = max(
            0.0,
            min(
                current.p_grid_before_w,
                current.p_grid_before_w + current.p_pv_w - current.p_house_base_w,
            ),
        )

        if decision.inputs.dso_14a_active:
            compliance_ok = (
                controllable_load_after_w <= decision.inputs.dso_14a_limit_w * 1.02
            )
        elif decision.trigger is TriggerReason.HOUSE_FUSE_PROTECTION:
            compliance_ok = current.p_grid_before_w <= self._config.house_fuse_limit_w * 1.02
        elif decision.trigger is TriggerReason.EEG_FEEDIN_LIMIT_EVENT:
            max_feedin_w = self._config.pv_peak_w * (decision.inputs.eeg_feedin_limit_pct / 100.0)
            compliance_ok = current.p_grid_before_w >= -max_feedin_w * 1.02
        else:
            compliance_ok = True

        verification = VerificationResult(
            check_after_s=self._delay_s,
            verified_at_utc=format_utc_ms(self._clock.now_utc()),
            p_grid_after_w=current.p_grid_before_w,
            controllable_load_after_w=controllable_load_after_w,
            compliance_ok=compliance_ok,
            e2e_latency_ms=(dispatch_monotonic_ns - decision_monotonic_ns) // 1_000_000,
        )
        record = VerificationTraceRecord(
            decision_id=decision.decision_id,
            verification=verification,
        )
        await self._jsonl.append(record)
        await self._sqlite.update_verification(decision.decision_id, verification)
