"""Phase-2-Gate: Datenmodelle, JSONL-Rotation, SQLite-Audit, Wirkungsnachweis (docs/02)."""

from __future__ import annotations

import gzip
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from hems.config import AppConfig, OperatingMode
from hems.logging.jsonl_logger import JsonlLogger
from hems.logging.sqlite_store import SqliteAuditStore
from hems.logging.verification import RealClock, VerificationTracker, format_utc_ms
from hems.models import (
    ControlOutputs,
    DecisionDerivation,
    DecisionTraceRecord,
    HeatpumpSGReadyMode,
    ProposedSetpoints,
    SystemInputs,
    TriggerReason,
    VerificationResult,
    VerificationTraceRecord,
)


def make_inputs(**overrides: Any) -> SystemInputs:
    base: dict[str, Any] = {
        "p_grid_before_w": 3000.0,
        "p_pv_w": 0.0,
        "p_house_base_w": 500.0,
        "soc_battery_pct": 50.0,
        "soc_ev_pct": 20.0,
        "ev_connected": True,
        "ev_departure_in_min": 120.0,
        "t_buffer_c": 48.0,
        "tariff_ct_kwh": 30.0,
        "co2_intensity_g_kwh": 400.0,
        "dso_14a_active": False,
        "dso_14a_limit_w": 4200.0,
        "eeg_feedin_limit_pct": 60.0,
    }
    base.update(overrides)
    return SystemInputs.model_validate(base)


def make_decision(
    inputs: SystemInputs | None = None,
    trigger: TriggerReason = TriggerReason.PERIODIC_1HZ,
    decision_id: str = "2026-11-23T10:00:00Z_0001",
    timestamp_utc: str = "2026-11-23T10:00:00.000Z",
) -> DecisionTraceRecord:
    return DecisionTraceRecord(
        decision_id=decision_id,
        timestamp_utc=timestamp_utc,
        mode=OperatingMode.C_GRID_SUPPORTIVE,
        dry_run=True,
        trigger=trigger,
        inputs=inputs if inputs is not None else make_inputs(),
        derivation=DecisionDerivation(
            emhass_status="disabled",
            emhass_solve_ms=None,
            runtimeparams_hash=None,
            emhass_proposed_setpoints_w=ProposedSetpoints(
                wallbox_w=0.0, battery_w=0.0, heatpump_w=0.0
            ),
            cascade_evaluated_rules=["P1_house_fuse_ok", "P1_14a_limit_ok", "P1_eeg_feedin_ok"],
            winning_priority=3,
            override_reason=None,
        ),
        outputs=ControlOutputs(
            cmd_wallbox_w=0.0,
            cmd_battery_w=0.0,
            cmd_heatpump_sg_ready=HeatpumpSGReadyMode.NORMAL,
            cmd_heatpump_w_est=0.0,
        ),
    )


def make_verification(compliance_ok: bool = True) -> VerificationResult:
    return VerificationResult(
        check_after_s=10.0,
        verified_at_utc="2026-11-23T10:00:10.000Z",
        p_grid_after_w=3500.0,
        controllable_load_after_w=3500.0,
        compliance_ok=compliance_ok,
        e2e_latency_ms=42,
    )


@pytest.fixture
async def store(tmp_path: Path) -> AsyncIterator[SqliteAuditStore]:
    audit_store = SqliteAuditStore(tmp_path)
    await audit_store.initialize()
    yield audit_store
    await audit_store.close()


# --- Akzeptanzkriterium 1: verlustfreie Serialisierung, extra="forbid" ---------------


def test_decision_record_roundtrip_lossless() -> None:
    record = make_decision()
    restored = DecisionTraceRecord.model_validate_json(record.model_dump_json())
    assert restored == record


def test_verification_record_roundtrip_lossless() -> None:
    record = VerificationTraceRecord(
        decision_id="2026-11-23T10:00:00Z_0001",
        verification=make_verification(),
    )
    restored = VerificationTraceRecord.model_validate_json(record.model_dump_json())
    assert restored == record


def test_unknown_fields_rejected() -> None:
    payload = json.loads(make_decision().model_dump_json())
    payload["unknown_field"] = 1
    with pytest.raises(ValidationError):
        DecisionTraceRecord.model_validate(payload)
    verification_payload = {
        "decision_id": "2026-11-23T10:00:00Z_0001",
        "verification": json.loads(make_verification().model_dump_json()),
        "extra": True,
    }
    with pytest.raises(ValidationError):
        VerificationTraceRecord.model_validate(verification_payload)


def test_models_are_frozen() -> None:
    record = make_decision()
    with pytest.raises(ValidationError):
        record.dry_run = False


def test_14a_limit_below_4200_rejected_when_active() -> None:
    with pytest.raises(ValidationError):
        make_inputs(dso_14a_active=True, dso_14a_limit_w=3000.0)


def test_invalid_decision_id_format_rejected() -> None:
    with pytest.raises(ValidationError):
        make_decision(decision_id="not-a-valid-id")


# --- Akzeptanzkriterium 2: JSONL-Rotation, gzip-Validität, Backup-Pruning ------------


async def test_jsonl_append_and_rotation(tmp_path: Path) -> None:
    logger = JsonlLogger(tmp_path, jsonl_max_bytes=4096, jsonl_backup_count=10)
    records = [
        make_decision(decision_id=f"2026-11-23T10:00:00Z_{seq:04d}") for seq in range(1, 7)
    ]
    for record in records:
        await logger.append(record)

    archives = sorted((tmp_path / "logs").glob("decision_trace.*.jsonl.gz"))
    assert len(archives) >= 1, "Rotation muss bei Überschreiten von jsonl_max_bytes erfolgen"

    # Jedes Archiv ist valides gzip mit validen JSONL-Zeilen.
    recovered: list[str] = []
    for archive in archives:
        with gzip.open(archive, "rt", encoding="utf-8") as handle:
            lines = handle.read().splitlines()
        assert lines
        for line in lines:
            recovered.append(json.loads(line)["decision_id"])

    # Aktuelle Datei existiert und enthält ebenfalls valide Zeilen.
    current_lines = logger.log_path.read_text(encoding="utf-8").splitlines()
    assert current_lines
    for line in current_lines:
        recovered.append(json.loads(line)["decision_id"])

    # Keine Zeile verloren: alle Records wiederherstellbar (Append-only + Archive).
    assert sorted(recovered) == sorted(r.decision_id for r in records)


async def test_jsonl_backup_pruning(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Mehrere Rotationen: älteste Archive werden zuerst gelöscht (jsonl_backup_count=2)."""
    from datetime import UTC as _UTC
    from datetime import datetime as _datetime

    class _SteppingDatetime:
        """Deterministische Rotations-Stempel (eine Rotation pro simulierter Sekunde)."""

        calls = 0

        @classmethod
        def now(cls, tz: Any) -> _datetime:
            cls.calls += 1
            return _datetime(2026, 11, 23, 10, 0, cls.calls, tzinfo=_UTC)

    monkeypatch.setattr("hems.logging.jsonl_logger.datetime", _SteppingDatetime)
    logger = JsonlLogger(tmp_path, jsonl_max_bytes=4096, jsonl_backup_count=2)
    for seq in range(1, 17):
        await logger.append(make_decision(decision_id=f"2026-11-23T10:00:00Z_{seq:04d}"))

    archives = sorted((tmp_path / "logs").glob("decision_trace.*.jsonl.gz"))
    assert len(archives) >= 2, "Es müssen mehrere Rotationen stattgefunden haben"
    assert len(archives) <= 2, "Backup-Pruning muss jsonl_backup_count einhalten"
    # Ältestes Archiv (Sekunde 01) wurde gelöscht, neuere bleiben erhalten.
    assert not any("100001" in archive.name for archive in archives)


async def test_jsonl_verification_record_append(tmp_path: Path) -> None:
    logger = JsonlLogger.from_config(AppConfig(data_dir=tmp_path))
    await logger.append(make_decision())
    await logger.append(
        VerificationTraceRecord(
            decision_id="2026-11-23T10:00:00Z_0001",
            verification=make_verification(),
        )
    )
    lines = logger.log_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["event_type"] == "decision"
    assert json.loads(lines[1])["event_type"] == "verification"


# --- Akzeptanzkriterium 3: SQLite WAL, Insert, Update, 14a-Query ---------------------


async def test_sqlite_wal_mode_and_schema(store: SqliteAuditStore) -> None:
    conn = store._require_conn()
    cursor = await conn.execute("PRAGMA journal_mode;")
    row = await cursor.fetchone()
    assert row[0] == "wal"
    cursor = await conn.execute("SELECT name FROM sqlite_master WHERE type = 'index'")
    index_names = {row[0] for row in await cursor.fetchall()}
    assert {
        "idx_decisions_timestamp",
        "idx_decisions_14a",
        "idx_decisions_mode",
    } <= index_names


async def test_sqlite_insert_and_verification_update(store: SqliteAuditStore) -> None:
    record = make_decision(inputs=make_inputs(dso_14a_active=True))
    await store.insert_decision(record)

    events = await store.query_14a_events("2026-11-23T00:00:00.000Z")
    assert len(events) == 1
    row = events[0]
    assert row["decision_id"] == record.decision_id
    assert row["dso_14a_active"] == 1
    assert row["cmd_heatpump_sg_ready"] == "normal"
    assert row["verified_at_utc"] is None
    assert row["compliance_ok"] is None
    assert json.loads(row["raw_json"])["decision_id"] == record.decision_id

    verification = make_verification(compliance_ok=True)
    await store.update_verification(record.decision_id, verification)

    events = await store.query_14a_events("2026-11-23T00:00:00.000Z")
    row = events[0]
    assert row["verified_at_utc"] == verification.verified_at_utc
    assert row["check_after_s"] == 10.0
    assert row["p_grid_after_w"] == 3500.0
    assert row["controllable_load_after_w"] == 3500.0
    assert row["compliance_ok"] == 1
    assert row["e2e_latency_ms"] == 42


async def test_sqlite_14a_query_filters(store: SqliteAuditStore) -> None:
    await store.insert_decision(make_decision(inputs=make_inputs(dso_14a_active=False)))
    await store.insert_decision(
        make_decision(
            inputs=make_inputs(dso_14a_active=True),
            decision_id="2026-11-23T11:00:00Z_0002",
            timestamp_utc="2026-11-23T11:00:00.000Z",
        )
    )
    events = await store.query_14a_events("2026-11-23T10:30:00.000Z")
    assert len(events) == 1
    assert events[0]["decision_id"] == "2026-11-23T11:00:00Z_0002"


# --- Akzeptanzkriterium 4: VerificationTracker asynchron, True/False, K2/K5 ----------


def _tracker(tmp_path: Path, store: SqliteAuditStore) -> VerificationTracker:
    config = AppConfig(data_dir=tmp_path)
    logger = JsonlLogger.from_config(config)
    # docs/02 §5.4: verification_delay_s=0.05 im Test (Override; Config-Grenzen bleiben).
    return VerificationTracker(config, logger, store, clock=RealClock(), delay_s=0.05)


async def _run_verification(
    tmp_path: Path,
    store: SqliteAuditStore,
    decision: DecisionTraceRecord,
    after_state: SystemInputs,
) -> VerificationResult:
    tracker = _tracker(tmp_path, store)
    await store.insert_decision(decision)
    task = tracker.schedule_verification(
        decision,
        decision_monotonic_ns=1_000_000_000,
        dispatch_monotonic_ns=1_042_000_000,
        state_provider=lambda: after_state,
    )
    await task

    conn = store._require_conn()
    cursor = await conn.execute(
        "SELECT * FROM decisions WHERE decision_id = ?", (decision.decision_id,)
    )
    row = dict(await cursor.fetchone())
    return VerificationResult(
        check_after_s=row["check_after_s"],
        verified_at_utc=row["verified_at_utc"],
        p_grid_after_w=row["p_grid_after_w"],
        controllable_load_after_w=row["controllable_load_after_w"],
        compliance_ok=bool(row["compliance_ok"]),
        e2e_latency_ms=row["e2e_latency_ms"],
    )


async def test_verification_14a_compliant(tmp_path: Path, store: SqliteAuditStore) -> None:
    decision = make_decision(inputs=make_inputs(dso_14a_active=True, dso_14a_limit_w=4200.0))
    after = make_inputs(p_grid_before_w=4000.0, p_house_base_w=500.0)
    result = await _run_verification(tmp_path, store, decision, after)
    assert result.controllable_load_after_w == 3500.0
    assert result.compliance_ok is True
    assert result.e2e_latency_ms == 42
    assert result.check_after_s == 0.05


async def test_verification_14a_violation(tmp_path: Path, store: SqliteAuditStore) -> None:
    decision = make_decision(inputs=make_inputs(dso_14a_active=True, dso_14a_limit_w=4200.0))
    after = make_inputs(p_grid_before_w=5000.0, p_house_base_w=500.0)
    result = await _run_verification(tmp_path, store, decision, after)
    # controllable_load_after_w = 4500 > 4200 * 1.02 = 4284
    assert result.controllable_load_after_w == 4500.0
    assert result.compliance_ok is False


async def test_verification_k2_netting_with_pv_surplus(
    tmp_path: Path, store: SqliteAuditStore
) -> None:
    """K2: PV-Überschuss wird saldiert – Brutto-SteuVE > Limit, aber Netto compliant."""
    decision = make_decision(inputs=make_inputs(dso_14a_active=True, dso_14a_limit_w=4200.0))
    # p_grid=1000, pv=8000, house=1000 -> Brutto-SteuVE=8000, Netto=1000 <= 4284.
    after = make_inputs(p_grid_before_w=1000.0, p_pv_w=8000.0, p_house_base_w=1000.0)
    result = await _run_verification(tmp_path, store, decision, after)
    assert result.controllable_load_after_w == 1000.0
    assert result.compliance_ok is True


async def test_verification_house_fuse_trigger(tmp_path: Path, store: SqliteAuditStore) -> None:
    decision = make_decision(trigger=TriggerReason.HOUSE_FUSE_PROTECTION)
    after = make_inputs(p_grid_before_w=23000.0)
    result = await _run_verification(tmp_path, store, decision, after)
    assert result.compliance_ok is False  # 23000 > 22000 * 1.02

    decision_ok = make_decision(
        trigger=TriggerReason.HOUSE_FUSE_PROTECTION,
        decision_id="2026-11-23T10:00:01Z_0002",
    )
    result_ok = await _run_verification(
        tmp_path, store, decision_ok, make_inputs(p_grid_before_w=21000.0)
    )
    assert result_ok.compliance_ok is True


async def test_verification_eeg_trigger(tmp_path: Path, store: SqliteAuditStore) -> None:
    decision = make_decision(trigger=TriggerReason.EEG_FEEDIN_LIMIT_EVENT)
    # Limit: -(10000 * 0.60) * 1.02 = -6120 W
    after_bad = make_inputs(p_grid_before_w=-7000.0, p_pv_w=8000.0)
    result = await _run_verification(tmp_path, store, decision, after_bad)
    assert result.compliance_ok is False

    decision_ok = make_decision(
        trigger=TriggerReason.EEG_FEEDIN_LIMIT_EVENT,
        decision_id="2026-11-23T10:00:01Z_0002",
    )
    after_ok = make_inputs(p_grid_before_w=-5000.0, p_pv_w=6000.0)
    result_ok = await _run_verification(tmp_path, store, decision_ok, after_ok)
    assert result_ok.compliance_ok is True


async def test_verification_writes_jsonl_and_sqlite_1to1(
    tmp_path: Path, store: SqliteAuditStore
) -> None:
    decision = make_decision(inputs=make_inputs(dso_14a_active=True))
    tracker = _tracker(tmp_path, store)
    logger = JsonlLogger.from_config(AppConfig(data_dir=tmp_path))
    await logger.append(decision)
    await store.insert_decision(decision)
    task = tracker.schedule_verification(
        decision, 1_000_000_000, 1_010_000_000, lambda: make_inputs(p_grid_before_w=3000.0)
    )
    await task

    lines = logger.log_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    verification_line = json.loads(lines[1])
    assert verification_line["event_type"] == "verification"
    assert verification_line["decision_id"] == decision.decision_id

    events = await store.query_14a_events("2026-11-23T00:00:00.000Z")
    assert events[0]["verified_at_utc"] is not None
    assert events[0]["e2e_latency_ms"] == 10


def test_format_utc_ms() -> None:
    from datetime import UTC, datetime

    value = datetime(2026, 11, 23, 10, 0, 0, 123456, tzinfo=UTC)
    assert format_utc_ms(value) == "2026-11-23T10:00:00.123Z"
