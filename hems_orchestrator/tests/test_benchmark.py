"""Tests für den Thesis-Benchmark-Runner (Modus A/B/C auf Referenzprofilen)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from hems.config import AppConfig, OperatingMode
from hems.hil.benchmark import (
    BenchmarkEmhassClient,
    ProfileSpec,
    ReferenceProfile,
    export_report,
    generate_profile_csv,
    run_benchmark,
    soc_adjusted_cost,
)
from hems.hil.replay_driver import load_scenario
from test_audit_logging import make_inputs


def test_reference_profiles_have_distinct_load_shapes() -> None:
    from hems.hil.benchmark import _profile_load

    h_noon = _profile_load(ReferenceProfile.H25, 12.0)
    h_evening = _profile_load(ReferenceProfile.H25, 19.0)
    p_noon = _profile_load(ReferenceProfile.P25, 12.0)
    p_night = _profile_load(ReferenceProfile.P25, 2.0)
    s_noon = _profile_load(ReferenceProfile.S25, 12.0)
    s_night = _profile_load(ReferenceProfile.S25, 2.0)

    assert h_evening > h_noon  # Haushalt: Abendspitze
    assert p_noon > p_night  # Praxis: Tagsueber aktiv
    assert s_noon > s_night  # Schule: Tagsueber aktiv
    assert abs(p_night - s_night) >= 0.0


def test_generate_profile_csv_schema_and_determinism(tmp_path: Path) -> None:
    spec = ProfileSpec(
        profile=ReferenceProfile.H25, steps=24, step_minutes=15, pv_peak_w=10000.0
    )
    path_a = generate_profile_csv(spec, tmp_path / "a" / "H25.csv")
    path_b = generate_profile_csv(spec, tmp_path / "b" / "H25.csv")
    assert path_a.read_bytes() == path_b.read_bytes()
    rows = load_scenario(path_a)
    assert len(rows) == 24
    assert rows[0].timestamp_utc == datetime(2026, 6, 21, 0, 0, 0, tzinfo=UTC)
    assert rows[0].dso_14a_active is False
    assert rows[10].dso_14a_active is True  # Fault-Fenster Schritt 10-13
    assert rows[14].dso_14a_active is False


def test_benchmark_emhass_modes_differ() -> None:
    assert AppConfig().operating_mode is OperatingMode.C_GRID_SUPPORTIVE
    config_b = AppConfig(operating_mode="B_MARKET_ORIENTED")
    config_c = AppConfig(operating_mode="C_GRID_SUPPORTIVE")
    inputs = make_inputs(
        p_pv_w=2000.0,
        p_house_base_w=500.0,
        tariff_ct_kwh=30.0,
        co2_intensity_g_kwh=500.0,
    )
    result_b = __import__("asyncio").run(BenchmarkEmhassClient(config_b).optimize(inputs))
    result_c = __import__("asyncio").run(BenchmarkEmhassClient(config_c).optimize(inputs))
    assert result_b.status == "ok" and result_b.setpoints is not None
    assert result_c.status == "ok" and result_c.setpoints is not None
    # Modus C drosselt die Wallbox (Netzbegrenzung) und die Batterieladung (hohe CO2).
    assert result_c.setpoints.wallbox_w <= result_b.setpoints.wallbox_w
    assert result_c.setpoints.battery_w <= result_b.setpoints.battery_w


def test_soc_adjusted_cost_formula() -> None:
    """Randwert-Korrektur: ΔSoC-Gutschrift mit Roundtrip-Wirkungsgrad η² = 0,9025."""
    # 10 % mehr SoC auf 10 kWh = 1 kWh gespeichert; Mitteltarif 0,25 EUR/kWh.
    adjusted = soc_adjusted_cost(10.0, 50.0, 60.0, 10000.0, 0.25)
    assert adjusted == pytest.approx(10.0 - 1.0 * 0.25 * 0.9025)
    # Negativer ΔSoC: keine Gutschrift, keine zusaetzliche Strafe.
    assert soc_adjusted_cost(10.0, 60.0, 50.0, 10000.0, 0.25) == 10.0
    # Gleicher SoC: unveraendert.
    assert soc_adjusted_cost(10.0, 50.0, 50.0, 10000.0, 0.25) == 10.0


@pytest.mark.asyncio
async def test_benchmark_full_matrix_with_exports(tmp_path: Path) -> None:
    spec = ProfileSpec(
        profile=ReferenceProfile.H25, steps=24, step_minutes=15, pv_peak_w=10000.0
    )
    report = await run_benchmark(
        spec,
        tmp_path / "results",
        profiles=[ReferenceProfile.H25, ReferenceProfile.P25],
        modes=[
            OperatingMode.A_BASELINE,
            OperatingMode.B_MARKET_ORIENTED,
            OperatingMode.C_GRID_SUPPORTIVE,
        ],
    )
    assert len(report.runs) == 6
    assert {(r.profile, r.mode) for r in report.runs} == {
        (p, m)
        for p in (ReferenceProfile.H25, ReferenceProfile.P25)
        for m in OperatingMode
    }
    for run in report.runs:
        assert run.kpi.total_decisions == 24
        assert 0.0 <= run.final_soc_battery_pct <= 100.0
        assert 0.0 <= run.final_soc_ev_pct <= 100.0
        assert run.mean_tariff_eur_kwh > 0.0
        assert run.net_cost_soc_adjusted_eur <= run.kpi.total_energy_cost_eur
        assert run.net_cost_soc_adjusted_eur > 0.0

    # Profil-Unterschiede muessen in den generierten Szenarien sichtbar sein.
    # (Der fruehere Bug war: trotz uebergebenem Profil wurde immer H25 erzeugt.)
    h25_csv = (tmp_path / "results" / "H25.csv").read_text().splitlines()[1]
    p25_csv = (tmp_path / "results" / "P25.csv").read_text().splitlines()[1]
    assert h25_csv != p25_csv
    assert float(h25_csv.split(",")[2]) != float(p25_csv.split(",")[2])

    comparison = report.comparison()
    assert len(comparison) == 6
    assert all("cost_delta_vs_A_pct" in row for row in comparison)
    assert all("cost_soc_adjusted_delta_vs_A_pct" in row for row in comparison)

    paths = export_report(report, tmp_path / "results")
    assert all(path.exists() and path.stat().st_size > 0 for path in paths.values())
    payload = json.loads(paths["json"].read_text(encoding="utf-8"))
    assert len(payload["runs"]) == 6
    csv_text = paths["csv"].read_text(encoding="utf-8")
    assert "C_GRID_SUPPORTIVE" in csv_text
    assert "net_cost_soc_adjusted_eur" in csv_text
    markdown = paths["markdown"].read_text(encoding="utf-8")
    assert "| Profil | Modus |" in markdown
    assert "Kosten SoC-adj." in markdown
    assert "Delta gegenueber Modus A" in markdown


@pytest.mark.asyncio
async def test_benchmark_cli_via_subprocess(tmp_path: Path) -> None:
    """Der CLI-Einstieg muss ohne externe Dienste laufen."""
    import subprocess
    import sys

    out_dir = tmp_path / "cli_results"
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "hems.hil.benchmark",
            "--out-dir",
            str(out_dir),
            "--steps",
            "8",
            "--profiles",
            "H25",
            "--modes",
            "A_BASELINE",
            "C_GRID_SUPPORTIVE",
        ],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        check=True,
        timeout=120,
        env={"PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")},
    )
    assert "Benchmark abgeschlossen" in result.stdout
    assert (out_dir / "benchmark_report.json").exists()
    assert (out_dir / "benchmark_comparison.md").exists()
