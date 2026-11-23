"""Append-only JSONL Decision-Trace mit Rotation & gzip-Kompression (docs/02 §2).

Die JSONL ist ein rollierender Kurzzeit-Puffer (HA-Backup-Schutz), NICHT die
2-Jahres-Nachweisquelle – diese ist ausschließlich die SQLite-Audit-DB.
Schreibzugriffe sind über ein asyncio.Lock serialisiert; File-I/O läuft über
asyncio.to_thread und blockiert den Event-Loop nicht.
"""

from __future__ import annotations

import asyncio
import gzip
import os
import shutil
from datetime import UTC, datetime
from pathlib import Path

from hems.config import AppConfig
from hems.models import DecisionTraceRecord, VerificationTraceRecord

_TRACE_BASENAME = "decision_trace"


class JsonlLogger:
    """Append-only JSONL-Logger mit atomarer Rotation und Backup-Pruning."""

    def __init__(self, data_dir: Path, jsonl_max_bytes: int, jsonl_backup_count: int) -> None:
        self._log_dir = data_dir / "logs"
        self._log_path = self._log_dir / f"{_TRACE_BASENAME}.jsonl"
        self._max_bytes = jsonl_max_bytes
        self._backup_count = jsonl_backup_count
        self._lock = asyncio.Lock()
        self._log_dir.mkdir(parents=True, exist_ok=True)

    @classmethod
    def from_config(cls, config: AppConfig) -> JsonlLogger:
        return cls(config.data_dir, config.jsonl_max_bytes, config.jsonl_backup_count)

    @property
    def log_path(self) -> Path:
        return self._log_path

    async def append(self, record: DecisionTraceRecord | VerificationTraceRecord) -> None:
        """Hängt einen Record als kompakte JSON-Zeile an (sofort geflusht)."""
        line = record.model_dump_json() + "\n"
        async with self._lock:
            await asyncio.to_thread(self._append_sync, line)

    def _append_sync(self, line: str) -> None:
        line_bytes = len(line.encode("utf-8"))
        if self._log_path.exists() and self._log_path.stat().st_size + line_bytes > self._max_bytes:
            self._rotate_sync()
        with self._log_path.open("a", encoding="utf-8") as handle:
            handle.write(line)
            handle.flush()

    def _rotate_sync(self) -> None:
        stamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
        archive = self._log_dir / f"{_TRACE_BASENAME}.{stamp}.jsonl.gz"
        tmp_archive = archive.with_name(archive.name + ".tmp")
        with self._log_path.open("rb") as src, gzip.open(tmp_archive, "wb") as dst:
            shutil.copyfileobj(src, dst)
        os.replace(tmp_archive, archive)
        self._log_path.unlink()
        self._prune_backups_sync()

    def _prune_backups_sync(self) -> None:
        archives = sorted(self._log_dir.glob(f"{_TRACE_BASENAME}.*.jsonl.gz"))
        excess = len(archives) - self._backup_count
        for old_archive in archives[: max(0, excess)]:
            old_archive.unlink()
