"""Automatisierter KPI-Rechner aus der SQLite-Audit-DB (docs/04 §2, Fix C2).

Zeitgewichtete Integrale als Piecewise-Constant (Left-Riemann / Zero-Order-Hold)
mit max_step_s-Cap (Default 900 s) gegen Verzerrung bei Systempausen.
CLI: python -m hems.hil.kpi_calculator --db /data/db/hems_audit.db --out report.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
from datetime import datetime
from pathlib import Path

import aiosqlite
from pydantic import BaseModel, ConfigDict

_DEFAULT_MAX_STEP_S = 900.0


class KpiReport(BaseModel):
    """KPI-Report eines Auswertungszeitraums (docs/04 §2)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    mode: str
    total_decisions: int
    peak_grid_import_w: float
    mean_grid_import_w: float
    peak_to_average_ratio: float
    total_grid_import_kwh: float
    total_energy_cost_eur: float
    total_co2_kg: float
    dso_14a_event_count: int
    dso_14a_compliance_rate_pct: float
    comfort_violations_count: int
    p95_e2e_latency_ms: float
    solver_fallback_count: int


def _parse_ts(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _percentile_95(values: list[int]) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    rank = max(1, math.ceil(0.95 * len(ordered)))
    return float(ordered[rank - 1])


async def compute_kpis(
    db_path: Path,
    start_utc: str,
    end_utc: str,
    mode: str | None = None,
    max_step_s: float = _DEFAULT_MAX_STEP_S,
    heatpump_temp_min_c: float = 45.0,
) -> KpiReport:
    """Berechnet den KPI-Report für [start_utc, end_utc], optional nach mode."""
    query = (
        "SELECT timestamp_utc, mode, p_grid_before_w, tariff_ct_kwh, co2_intensity_g_kwh, "
        "t_buffer_c, dso_14a_active, compliance_ok, e2e_latency_ms, winning_priority "
        "FROM decisions WHERE timestamp_utc >= ? AND timestamp_utc <= ?"
    )
    params: list[object] = [start_utc, end_utc]
    if mode is not None:
        query += " AND mode = ?"
        params.append(mode)
    query += " ORDER BY timestamp_utc ASC"

    async with aiosqlite.connect(db_path) as conn:
        conn.row_factory = aiosqlite.Row
        cursor = await conn.execute(query, params)
        rows = [dict(row) for row in await cursor.fetchall()]

    n = len(rows)
    if n == 0:
        return KpiReport(
            mode=mode if mode is not None else "ALL",
            total_decisions=0,
            peak_grid_import_w=0.0,
            mean_grid_import_w=0.0,
            peak_to_average_ratio=0.0,
            total_grid_import_kwh=0.0,
            total_energy_cost_eur=0.0,
            total_co2_kg=0.0,
            dso_14a_event_count=0,
            dso_14a_compliance_rate_pct=0.0,
            comfort_violations_count=0,
            p95_e2e_latency_ms=0.0,
            solver_fallback_count=0,
        )

    # Fix C2: Δt als Vorwärtsdifferenz mit max_step_s-Cap; letztes Δt = vorletztes.
    timestamps = [_parse_ts(row["timestamp_utc"]) for row in rows]
    deltas: list[float] = []
    for i in range(n - 1):
        deltas.append(min((timestamps[i + 1] - timestamps[i]).total_seconds(), max_step_s))
    deltas.append(deltas[-1] if n >= 2 else 0.0)

    imports = [max(0.0, float(row["p_grid_before_w"])) for row in rows]
    total_dt = sum(deltas)
    weighted_import = sum(p * dt for p, dt in zip(imports, deltas, strict=True))
    total_kwh = sum(
        p * dt / 3_600_000.0 for p, dt in zip(imports, deltas, strict=True)
    )
    total_cost = sum(
        p * dt / 3_600_000.0 * (float(row["tariff_ct_kwh"]) / 100.0)
        for p, dt, row in zip(imports, deltas, rows, strict=True)
    )
    total_co2 = sum(
        p * dt / 3_600_000.0 * (float(row["co2_intensity_g_kwh"]) / 1000.0)
        for p, dt, row in zip(imports, deltas, rows, strict=True)
    )
    peak = max(imports)
    mean = weighted_import / total_dt if total_dt > 0.0 else 0.0

    events_14a = [row for row in rows if row["dso_14a_active"] == 1]
    compliant = sum(1 for row in events_14a if row["compliance_ok"] == 1)
    compliance_rate = (compliant / len(events_14a) * 100.0) if events_14a else 0.0

    latencies = [int(row["e2e_latency_ms"]) for row in rows if row["e2e_latency_ms"] is not None]
    modes = {str(row["mode"]) for row in rows}
    report_mode = mode if mode is not None else (next(iter(modes)) if len(modes) == 1 else "MIXED")

    return KpiReport(
        mode=report_mode,
        total_decisions=n,
        peak_grid_import_w=peak,
        mean_grid_import_w=mean,
        peak_to_average_ratio=peak / mean if mean > 0.0 else 0.0,
        total_grid_import_kwh=total_kwh,
        total_energy_cost_eur=total_cost,
        total_co2_kg=total_co2,
        dso_14a_event_count=len(events_14a),
        dso_14a_compliance_rate_pct=compliance_rate,
        comfort_violations_count=sum(
            1 for row in rows if float(row["t_buffer_c"]) < heatpump_temp_min_c
        ),
        p95_e2e_latency_ms=_percentile_95(latencies),
        solver_fallback_count=sum(1 for row in rows if row["winning_priority"] == 4),
    )


def cli_main(argv: list[str] | None = None) -> None:
    """CLI-Export des KPI-Reports als formatiertes JSON."""
    parser = argparse.ArgumentParser(description="HEMS KPI calculator (docs/04 §2)")
    parser.add_argument("--db", required=True, type=Path, help="Pfad zur hems_audit.db")
    parser.add_argument("--out", required=True, type=Path, help="Zieldatei für den JSON-Report")
    parser.add_argument("--start", default="0000-01-01T00:00:00.000Z")
    parser.add_argument("--end", default="9999-12-31T23:59:59.999Z")
    parser.add_argument("--mode", default=None)
    parser.add_argument("--max-step-s", type=float, default=_DEFAULT_MAX_STEP_S)
    parser.add_argument("--temp-min", type=float, default=45.0)
    args = parser.parse_args(argv)

    report = asyncio.run(
        compute_kpis(
            args.db,
            args.start,
            args.end,
            mode=args.mode,
            max_step_s=args.max_step_s,
            heatpump_temp_min_c=args.temp_min,
        )
    )
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report.model_dump(), indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    cli_main()
