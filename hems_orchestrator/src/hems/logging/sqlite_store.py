"""Async SQLite Audit-DB im WAL-Modus (docs/02 §3).

Verbindliche 2-Jahres-Nachweisquelle nach § 14a EnWG / BNetzA BK6-22-300:
Zeilen werden niemals automatisch gelöscht oder geprunt. Verifikationsfelder
werden nachträglich per UPDATE gesetzt (JSONL bleibt append-only).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import aiosqlite

from hems.config import AppConfig
from hems.models import DecisionTraceRecord, VerificationResult

_SCHEMA = """
CREATE TABLE IF NOT EXISTS decisions (
    decision_id TEXT PRIMARY KEY,
    timestamp_utc TEXT NOT NULL,
    mode TEXT NOT NULL,
    dry_run INTEGER NOT NULL,
    trigger_reason TEXT NOT NULL,
    winning_priority INTEGER NOT NULL,
    override_reason TEXT,
    emhass_status TEXT NOT NULL,
    emhass_solve_ms INTEGER,
    runtimeparams_hash TEXT,
    p_grid_before_w REAL NOT NULL,
    p_pv_w REAL NOT NULL,
    p_house_base_w REAL NOT NULL,
    soc_battery_pct REAL NOT NULL,
    soc_ev_pct REAL NOT NULL,
    t_buffer_c REAL NOT NULL,
    tariff_ct_kwh REAL NOT NULL,
    co2_intensity_g_kwh REAL NOT NULL,
    dso_14a_active INTEGER NOT NULL,
    dso_14a_limit_w REAL NOT NULL,
    eeg_feedin_limit_pct REAL NOT NULL,
    cmd_wallbox_w REAL NOT NULL,
    cmd_battery_w REAL NOT NULL,
    cmd_heatpump_sg_ready TEXT NOT NULL,
    cmd_heatpump_w_est REAL NOT NULL,
    -- Verifikationsfelder (initial NULL, später per UPDATE gesetzt)
    verified_at_utc TEXT,
    check_after_s REAL,
    p_grid_after_w REAL,
    controllable_load_after_w REAL,
    compliance_ok INTEGER,
    e2e_latency_ms INTEGER,
    -- Vollständiges JSON für lückenlose Rekonstruktion
    raw_json TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_decisions_timestamp ON decisions(timestamp_utc);
CREATE INDEX IF NOT EXISTS idx_decisions_14a ON decisions(dso_14a_active, timestamp_utc);
CREATE INDEX IF NOT EXISTS idx_decisions_mode ON decisions(mode, timestamp_utc);
"""

_INSERT_SQL = """
INSERT INTO decisions (
    decision_id, timestamp_utc, mode, dry_run, trigger_reason,
    winning_priority, override_reason, emhass_status, emhass_solve_ms, runtimeparams_hash,
    p_grid_before_w, p_pv_w, p_house_base_w, soc_battery_pct, soc_ev_pct,
    t_buffer_c, tariff_ct_kwh, co2_intensity_g_kwh, dso_14a_active, dso_14a_limit_w,
    eeg_feedin_limit_pct, cmd_wallbox_w, cmd_battery_w, cmd_heatpump_sg_ready,
    cmd_heatpump_w_est, raw_json
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""

_UPDATE_VERIFICATION_SQL = """
UPDATE decisions
SET verified_at_utc = ?,
    check_after_s = ?,
    p_grid_after_w = ?,
    controllable_load_after_w = ?,
    compliance_ok = ?,
    e2e_latency_ms = ?
WHERE decision_id = ?
"""


class SqliteAuditStore:
    """Async Audit-Store mit WAL-Pragma, Insert, Verifikations-Update und 14a-Query."""

    def __init__(self, data_dir: Path) -> None:
        self._db_path = data_dir / "db" / "hems_audit.db"
        self._conn: aiosqlite.Connection | None = None

    @classmethod
    def from_config(cls, config: AppConfig) -> SqliteAuditStore:
        return cls(config.data_dir)

    @property
    def db_path(self) -> Path:
        return self._db_path

    async def initialize(self) -> None:
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(self._db_path)
        await self._conn.execute("PRAGMA journal_mode=WAL;")
        await self._conn.execute("PRAGMA synchronous=NORMAL;")
        await self._conn.execute("PRAGMA foreign_keys=ON;")
        self._conn.row_factory = aiosqlite.Row
        await self._conn.executescript(_SCHEMA)
        await self._conn.commit()

    def _require_conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("SqliteAuditStore is not initialized; call initialize() first")
        return self._conn

    async def insert_decision(self, record: DecisionTraceRecord) -> None:
        conn = self._require_conn()
        inputs = record.inputs
        derivation = record.derivation
        outputs = record.outputs
        await conn.execute(
            _INSERT_SQL,
            (
                record.decision_id,
                record.timestamp_utc,
                record.mode.value,
                int(record.dry_run),
                record.trigger.value,
                derivation.winning_priority,
                derivation.override_reason,
                derivation.emhass_status,
                derivation.emhass_solve_ms,
                derivation.runtimeparams_hash,
                inputs.p_grid_before_w,
                inputs.p_pv_w,
                inputs.p_house_base_w,
                inputs.soc_battery_pct,
                inputs.soc_ev_pct,
                inputs.t_buffer_c,
                inputs.tariff_ct_kwh,
                inputs.co2_intensity_g_kwh,
                int(inputs.dso_14a_active),
                inputs.dso_14a_limit_w,
                inputs.eeg_feedin_limit_pct,
                outputs.cmd_wallbox_w,
                outputs.cmd_battery_w,
                outputs.cmd_heatpump_sg_ready.value,
                outputs.cmd_heatpump_w_est,
                record.model_dump_json(),
            ),
        )
        await conn.commit()

    async def update_verification(self, decision_id: str, verification: VerificationResult) -> None:
        conn = self._require_conn()
        await conn.execute(
            _UPDATE_VERIFICATION_SQL,
            (
                verification.verified_at_utc,
                verification.check_after_s,
                verification.p_grid_after_w,
                verification.controllable_load_after_w,
                int(verification.compliance_ok),
                verification.e2e_latency_ms,
                decision_id,
            ),
        )
        await conn.commit()

    async def query_14a_events(self, since_utc: str) -> list[dict[str, Any]]:
        """Liefert alle § 14a-Eingriffe ab since_utc für den 2-Jahres-Nachweis."""
        conn = self._require_conn()
        cursor = await conn.execute(
            "SELECT * FROM decisions WHERE dso_14a_active = 1 AND timestamp_utc >= ? "
            "ORDER BY timestamp_utc ASC",
            (since_utc,),
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None
