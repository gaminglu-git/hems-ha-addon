"""Thesis-Benchmark-Runner: Modi A/B/C auf deutschen Referenzprofilen.

Baut deterministic auf dem Quasi-HIL-Replay (docs/04) und dem KPI-Rechner auf.
Erzeugt VDI-4655-aehnliche, reproduzierbare synthetische Last-/PV-Profile
(H25 Haushalt, P25 Praxis/Gewerbe, S25 Schule), faehrt jeden Modus auf jedem
Profil und exportiert die Vergleichs-KPIs als CSV, JSON und Markdown.

WICHTIG (Modellannahme): Es wird kein echtes EMHASS-Add-on benoetigt. Stattdessen
liefert ``BenchmarkEmhassClient`` deterministische, modus-abhaengige Sollwerte,
die die Optimierungsziele (A: keine, B: Kosten, C: Netzdienlichkeit) abbilden.
Damit sind die Ergebnisse reproduzierbar und frei von Solver-Non-Determinismus.
"""

from __future__ import annotations

import asyncio
import csv
import math
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import Enum
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from hems.config import AppConfig, OperatingMode
from hems.hil.kpi_calculator import KpiReport, compute_kpis
from hems.hil.replay_driver import (
    CSV_COLUMNS,
    FaultInjectingEmhassClient,
    PlantInitialState,
    ReplayDriver,
)
from hems.io.emhass_client import EmhassResult
from hems.models import ProposedSetpoints, SystemInputs


class ReferenceProfile(str, Enum):
    """Synthetische, VDI-4655-aehnliche Referenzlastgaenge (normierte Tagesverlaeufe)."""

    H25 = "H25"  # Haushalt: Doppelspitze morgens/abends
    P25 = "P25"  # Praxis/Gewerbe: Tagsueber last
    S25 = "S25"  # Schule: Tagsueber, Mittagsspitze


class BenchmarkEmhassClient(FaultInjectingEmhassClient):
    """Deterministischer Offline-Optimierer (kein HTTP, keinSolver-Device).

    - B_MARKET_ORIENTED: PV-Ueberschuss in die Batterie, Wallbox bei niedrigem
      Tarif mit voller Leistung; Batterie entlaedt bei hohem Tarif.
    - C_GRID_SUPPORTIVE: zusaetzlich Netzbezug begrenzt (8000 W) und
      CO2-/EEG-Einspeiselimit beruecksichtigt; Wallbox nur bei PV-Ueberschuss
      oder niedrigem CO2/Preis.
    """

    def __init__(self, config: AppConfig) -> None:
        super().__init__(config)

    async def optimize(self, inputs: SystemInputs) -> EmhassResult:
        if self.timeout_fault_active():
            return EmhassResult(
                status="timeout",
                solve_ms=15000,
                setpoints=None,
                runtimeparams_hash=None,
            )

        config = self._config
        pv_surplus = inputs.p_pv_w - inputs.p_house_base_w
        battery_cap = config.battery_max_charge_w
        battery_dis = config.battery_max_discharge_w
        low_tariff = inputs.tariff_ct_kwh < 25.0
        low_co2 = inputs.co2_intensity_g_kwh < 350.0

        # Batterie: PV-Ueberschuss laden, sonst bei guenstigem Tarif laden,
        # bei hohem Tarif entladen (Kosten-/Netzoptimal).
        if pv_surplus > 0.0 and inputs.soc_battery_pct < 99.0:
            battery_w = min(pv_surplus, battery_cap)
        elif low_tariff and inputs.soc_battery_pct < 95.0:
            battery_w = min(config.house_fuse_limit_w * 0.2, battery_cap)
        elif inputs.tariff_ct_kwh >= 35.0 and inputs.soc_battery_pct > 15.0:
            battery_w = -min(config.house_fuse_limit_w * 0.2, battery_dis)
        else:
            battery_w = 0.0

        # Wallbox: Volle Leistung, wenn Ziel-SoC noch nicht erreicht.
        wallbox_w = (
            config.wallbox_max_power_w
            if inputs.ev_connected and inputs.soc_ev_pct < config.ev_target_soc_pct
            else 0.0
        )
        heatpump_w = (
            config.heatpump_nominal_power_w
            if inputs.t_buffer_c < config.heatpump_temp_min_c
            else 0.0
        )

        if config.operating_mode is OperatingMode.C_GRID_SUPPORTIVE:
            # Netzdienlich: Netzbezug weich auf 8000 W deckeln, indem die
            # Wallbox gedrosselt wird (P1-Kaskade uebernimmt den harten Schnitt).
            grid_cap = min(config.house_fuse_limit_w, 8000.0)
            p_grid_est = inputs.p_house_base_w + wallbox_w + heatpump_w + battery_w - inputs.p_pv_w
            if p_grid_est > grid_cap:
                wallbox_w = max(0.0, wallbox_w - (p_grid_est - grid_cap))
            # CO2-/EEG-Sicht: Batterieladung bei hoher CO2-Intensitaet zuruecknehmen.
            if not low_co2 and battery_w > 0.0:
                battery_w *= 0.5

        # Deterministischer Hash aus den Sollwerten (kein Solver-Device noetig).
        hash_input = f"{wallbox_w:.1f}:{battery_w:.1f}:{heatpump_w:.1f}"
        import hashlib

        params_hash = hashlib.sha256(hash_input.encode("utf-8")).hexdigest()[:8]
        return EmhassResult(
            status="ok",
            solve_ms=1,
            setpoints=ProposedSetpoints(
                wallbox_w=max(0.0, wallbox_w), battery_w=battery_w, heatpump_w=heatpump_w
            ),
            runtimeparams_hash=params_hash,
        )


def _profile_load(profile: ReferenceProfile, hour: float) -> float:
    """Normierter Grundlastgang (W) eines 10-kW-Referenzhausanschlusses."""
    h = hour % 24.0
    if profile is ReferenceProfile.H25:
        # Haushalt: Morgen- (7-9) und Abendspitze (17-21), Nachtminimum.
        base = 350.0
        morning = 1400.0 * math.exp(-((h - 8.0) / 1.4) ** 2)
        evening = 2200.0 * math.exp(-((h - 19.0) / 2.2) ** 2)
        return base + morning + evening
    if profile is ReferenceProfile.P25:
        # Praxis/Gewerbe: 8-18 UhrAktivitaet, Mittagspause.
        if 8.0 <= h <= 18.0:
            dip = 900.0 if 12.0 <= h <= 13.0 else 0.0
            return 1200.0 + 2600.0 * math.exp(-((h - 11.0) / 3.5) ** 2) - dip
        return 400.0
    # S25 Schule: 8-16 Uhr, markante Pausen- bzw. Mittagsspitze.
    if 8.0 <= h <= 16.0:
        return 900.0 + 1800.0 * math.exp(-((h - 12.5) / 2.5) ** 2)
    return 300.0


def _profile_pv(hour: float, pv_peak_w: float) -> float:
    """VDI-4655-aehnliches PV-Erzeugungsprofil (Glockenkurve, 6-20 Uhr)."""
    if not (6.0 <= hour <= 20.0):
        return 0.0
    return max(0.0, pv_peak_w * math.sin(math.pi * (hour - 6.0) / 14.0) ** 1.3)


def _profile_tariff(hour: float) -> float:
    """Synthetischer dynamischer Tarif (ct/kWh): nachts guenstig, abends teuer."""
    h = hour % 24.0
    if 0.0 <= h < 6.0:
        return 18.0
    if 17.0 <= h < 21.0:
        return 42.0
    return 28.0


def _profile_co2(hour: float) -> float:
    """Synthetische CO2-Intensitaet (g/kWh)."""
    h = hour % 24.0
    if 17.0 <= h < 21.0:
        return 520.0
    if 10.0 <= h < 16.0:
        return 240.0
    return 380.0


class BenchmarkSettings(BaseModel):
    """Globale Benchmark-Einstellungen (profilunabhaengig)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    steps: int = Field(gt=0)
    step_minutes: int = Field(gt=0)
    pv_peak_w: float = Field(ge=0.0)

    @property
    def duration_h(self) -> float:
        return self.steps * self.step_minutes / 60.0


@dataclass(frozen=True)
class ProfileSpec:
    """Spezifikation eines generierten Szenarios."""

    profile: ReferenceProfile
    steps: int
    step_minutes: int
    pv_peak_w: float

    @property
    def duration_h(self) -> float:
        return self.steps * self.step_minutes / 60.0

    def settings(self) -> BenchmarkSettings:
        return BenchmarkSettings(
            steps=self.steps, step_minutes=self.step_minutes, pv_peak_w=self.pv_peak_w
        )


def generate_profile_csv(spec: ProfileSpec, path: Path, start: datetime | None = None) -> Path:
    """Schreibt ein reproduzierbares Szenario-CSV im docs/04-Schema."""
    if start is None:
        start = datetime(2026, 6, 21, 0, 0, 0, tzinfo=UTC)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(CSV_COLUMNS)
        for step in range(spec.steps):
            moment = start + timedelta(minutes=spec.step_minutes * step)
            hour = moment.hour + moment.minute / 60.0
            dso_active = 10 <= step < 14  # Simulierter §14a-Dimmungsabruf
            writer.writerow(
                [
                    moment.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
                    f"{_profile_pv(hour, spec.pv_peak_w):.3f}",
                    f"{_profile_load(spec.profile, hour):.3f}",
                    f"{_profile_tariff(hour):.3f}",
                    f"{_profile_co2(hour):.3f}",
                    "true",
                    f"{max(0.0, 720.0 - spec.step_minutes * step):.1f}",
                    "true" if dso_active else "false",
                    "4200.0",
                ]
            )
    return path


# Wirkungsgrad-Roundtrip der Batterie (konsistent zu docs/04 η_chg = η_dis = 0.95).
_BATTERY_ROUNDTRIP_EFFICIENCY = 0.95 * 0.95  # 0.9025


def soc_adjusted_cost(
    total_energy_cost_eur: float,
    soc_start_pct: float,
    soc_end_pct: float,
    battery_capacity_wh: float,
    mean_tariff_eur_kwh: float,
) -> float:
    """Kosten abzueglich des Restwerts der am Tagesende gespeicherten Energie.

    Transparenz zum 24-h-Randwert-Effekt: Nur ein positiver SoC-Zuwachs wird
    gutgeschrieben (mit Roundtrip-Wirkungsgrad, da die Energie noch einmal
    umgewandelt werden muss). Ein SoC-Abfall wird nicht zusätzlich bestraft –
    dieser ist bereits im Bezugsstrom enthalten.
    """
    delta_soc = max(0.0, soc_end_pct - soc_start_pct)
    stored_kwh = delta_soc / 100.0 * battery_capacity_wh / 1000.0
    residual_value_eur = stored_kwh * mean_tariff_eur_kwh * _BATTERY_ROUNDTRIP_EFFICIENCY
    return total_energy_cost_eur - residual_value_eur


class BenchmarkRun(BaseModel):
    """Ein Ergebnis: ein Profil in einem Betriebsmodus."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    profile: ReferenceProfile
    mode: OperatingMode
    kpi: KpiReport
    final_soc_battery_pct: float
    final_soc_ev_pct: float
    ev_target_soc_reached: bool
    mean_tariff_eur_kwh: float
    net_cost_soc_adjusted_eur: float


class BenchmarkReport(BaseModel):
    """Vollstaendiger Vergleichsreport (CSV/JSON/Markdown-Export)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    generated_at_utc: str
    settings: BenchmarkSettings
    runs: list[BenchmarkRun] = Field(min_length=1)

    def comparison(self) -> list[dict[str, object]]:
        """Modus-Vergleich je Profil: Kosten-, Peak- und CO2-Delta ggueber A."""
        rows: list[dict[str, object]] = []
        for profile in ReferenceProfile:
            baseline = next(
                (
                    r
                    for r in self.runs
                    if r.profile is profile and r.mode is OperatingMode.A_BASELINE
                ),
                None,
            )
            if baseline is None:
                continue
            for run in (r for r in self.runs if r.profile is profile):
                cost_delta = (
                    (run.kpi.total_energy_cost_eur - baseline.kpi.total_energy_cost_eur)
                    / baseline.kpi.total_energy_cost_eur
                    * 100.0
                    if baseline.kpi.total_energy_cost_eur > 0.0
                    else 0.0
                )
                peak_delta = (
                    (run.kpi.peak_grid_import_w - baseline.kpi.peak_grid_import_w)
                    / baseline.kpi.peak_grid_import_w
                    * 100.0
                    if baseline.kpi.peak_grid_import_w > 0.0
                    else 0.0
                )
                co2_delta = (
                    (run.kpi.total_co2_kg - baseline.kpi.total_co2_kg)
                    / baseline.kpi.total_co2_kg
                    * 100.0
                    if baseline.kpi.total_co2_kg > 0.0
                    else 0.0
                )
                adjusted_delta = (
                    (run.net_cost_soc_adjusted_eur - baseline.net_cost_soc_adjusted_eur)
                    / baseline.net_cost_soc_adjusted_eur
                    * 100.0
                    if baseline.net_cost_soc_adjusted_eur > 0.0
                    else 0.0
                )
                rows.append(
                    {
                        "profile": profile.value,
                        "mode": run.mode.value,
                        "cost_delta_vs_A_pct": round(cost_delta, 2),
                        "cost_soc_adjusted_delta_vs_A_pct": round(adjusted_delta, 2),
                        "peak_delta_vs_A_pct": round(peak_delta, 2),
                        "co2_delta_vs_A_pct": round(co2_delta, 2),
                        "dso_compliance_pct": round(run.kpi.dso_14a_compliance_rate_pct, 2),
                        "ev_target_reached": run.ev_target_soc_reached,
                    }
                )
        return rows


async def run_benchmark(
    spec: ProfileSpec,
    output_dir: Path,
    profiles: list[ReferenceProfile] | None = None,
    modes: list[OperatingMode] | None = None,
) -> BenchmarkReport:
    """Fuehrt alle Profil-x-Modus-Kombinationen aus und liefert den Report."""
    profiles = profiles if profiles is not None else list(ReferenceProfile)
    modes = modes if modes is not None else list(OperatingMode)
    output_dir.mkdir(parents=True, exist_ok=True)
    runs: list[BenchmarkRun] = []

    for profile in profiles:
        # Wichtig: Die Profil-Kennung muss in den Spec uebernommen werden,
        # sonst generiert jeder Schleifendurchlauf dasselbe H25-Szenario.
        profile_spec = ProfileSpec(
            profile=profile,
            steps=spec.steps,
            step_minutes=spec.step_minutes,
            pv_peak_w=spec.pv_peak_w,
        )
        scenario_path = generate_profile_csv(profile_spec, output_dir / f"{profile.value}.csv")
        for mode in modes:
            run_dir = output_dir / f"{profile.value}_{mode.value}"
            run_dir.mkdir(parents=True, exist_ok=True)
            config = AppConfig(
                data_dir=run_dir,
                operating_mode=mode,
                dry_run=True,
                mpc_interval_s=spec.step_minutes * 60,
            )
            emhass = BenchmarkEmhassClient(config)
            driver = ReplayDriver(config, emhass_client=emhass)
            await driver.run(scenario_path)

            kpi = await compute_kpis(
                config.data_dir / "db" / "hems_audit.db",
                "0000-01-01T00:00:00.000Z",
                "9999-12-31T23:59:59.999Z",
                mode=mode.value,
                heatpump_temp_min_c=config.heatpump_temp_min_c,
            )
            import aiosqlite

            db_path = config.data_dir / "db" / "hems_audit.db"
            async with aiosqlite.connect(db_path) as conn:
                cursor = await conn.execute(
                    "SELECT soc_battery_pct, soc_ev_pct FROM decisions "
                    "ORDER BY timestamp_utc DESC LIMIT 1"
                )
                last = await cursor.fetchone()
                # Delta-t-gewichteter Mitteltarif (konsistent zum C2-Integral).
                cursor = await conn.execute(
                    "SELECT timestamp_utc, tariff_ct_kwh FROM decisions ORDER BY timestamp_utc ASC"
                )
                tariff_rows = await cursor.fetchall()
            final_battery = float(last[0]) if last else 0.0
            final_ev = float(last[1]) if last else 0.0
            from hems.hil.kpi_calculator import _parse_ts

            times = [_parse_ts(row[0]) for row in tariff_rows]
            deltas = [
                min((times[i + 1] - times[i]).total_seconds(), 900.0)
                for i in range(len(times) - 1)
            ]
            if deltas:
                deltas.append(deltas[-1])
            total_dt = sum(deltas)
            mean_tariff = (
                sum(float(row[1]) / 100.0 * dt for row, dt in zip(tariff_rows, deltas, strict=True))
                / total_dt
                if total_dt > 0.0
                else 0.0
            )
            adjusted_cost = soc_adjusted_cost(
                kpi.total_energy_cost_eur,
                PlantInitialState().soc_battery_pct,  # Start-SoC (B3-Default = 50 %)
                final_battery,
                config.battery_capacity_wh,
                mean_tariff,
            )

            runs.append(
                BenchmarkRun(
                    profile=profile,
                    mode=mode,
                    kpi=kpi,
                    final_soc_battery_pct=final_battery,
                    final_soc_ev_pct=final_ev,
                    ev_target_soc_reached=final_ev >= config.ev_target_soc_pct - 1.0,
                    mean_tariff_eur_kwh=mean_tariff,
                    net_cost_soc_adjusted_eur=adjusted_cost,
                )
            )

    return BenchmarkReport(
        generated_at_utc=datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
        settings=spec.settings(),
        runs=runs,
    )


def export_report(report: BenchmarkReport, output_dir: Path) -> dict[str, Path]:
    """Exportiert JSON, CSV und Markdown; liefert die Pfade."""
    output_dir.mkdir(parents=True, exist_ok=True)
    paths = {
        "json": output_dir / "benchmark_report.json",
        "csv": output_dir / "benchmark_kpis.csv",
        "markdown": output_dir / "benchmark_comparison.md",
    }
    paths["json"].write_text(
        report.model_dump_json(indent=2) + "\n", encoding="utf-8"
    )

    kpi_fields = list(KpiReport.model_fields)
    with paths["csv"].open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            [
                "profile",
                "mode",
                *kpi_fields,
                "final_soc_battery_pct",
                "final_soc_ev_pct",
                "mean_tariff_eur_kwh",
                "net_cost_soc_adjusted_eur",
            ]
        )
        for run in report.runs:
            writer.writerow(
                [
                    run.profile.value,
                    run.mode.value,
                    *[run.kpi.model_dump()[field] for field in kpi_fields],
                    f"{run.final_soc_battery_pct:.3f}",
                    f"{run.final_soc_ev_pct:.3f}",
                    f"{run.mean_tariff_eur_kwh:.5f}",
                    f"{run.net_cost_soc_adjusted_eur:.4f}",
                ]
            )

    compared_profiles = ", ".join(sorted({run.profile.value for run in report.runs}))
    lines = [
        "# HEMS Modus-Vergleich (Thesis-Benchmark)",
        "",
        f"Generiert: {report.generated_at_utc}",
        f"Profile: {compared_profiles} · "
        f"Horizont: {report.settings.duration_h:.0f} h · "
        f"PV-Peak: {report.settings.pv_peak_w:.0f} W",
        "",
        "## KPIs je Profil und Modus",
        "",
        "| Profil | Modus | Kosten (EUR) | Kosten SoC-adj. (EUR) | Peak (W) | PAR | "
        "Import (kWh) | CO2 (kg) | 14a-Compliance | Fallbacks |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for run in report.runs:
        k = run.kpi
        lines.append(
            f"| {run.profile.value} | {run.mode.value} | {k.total_energy_cost_eur:.3f} | "
            f"{run.net_cost_soc_adjusted_eur:.3f} | "
            f"{k.peak_grid_import_w:.1f} | {k.peak_to_average_ratio:.3f} | "
            f"{k.total_grid_import_kwh:.3f} | {k.total_co2_kg:.3f} | "
            f"{k.dso_14a_compliance_rate_pct:.1f} % | {k.solver_fallback_count} |"
        )
    lines.extend(
        [
            "",
            "## Delta gegenueber Modus A (Baseline)",
            "",
            "| Profil | Modus | Kosten Δ | Kosten SoC-adj. Δ | Peak Δ | CO2 Δ | "
            "EV-Ziel-SoC erreicht |",
            "|---|---|---:|---:|---:|---:|:---:|",
        ]
    )
    for row in report.comparison():
        lines.append(
            f"| {row['profile']} | {row['mode']} | {row['cost_delta_vs_A_pct']:+.2f} % | "
            f"{row['cost_soc_adjusted_delta_vs_A_pct']:+.2f} % | "
            f"{row['peak_delta_vs_A_pct']:+.2f} % | {row['co2_delta_vs_A_pct']:+.2f} % | "
            f"{'ja' if row['ev_target_reached'] else 'nein'} |"
        )
    lines.extend(
        [
            "",
            "> Kosten SoC-adj. = Kosten abzüglich Restwert der am Tagesende gespeicherten",
            "> Batterieenergie (ΔSoC × Kapazität × Mitteltarif × η_rt mit η_rt = 0,95² = 0,9025).",
            "> Macht den 24-h-Randwert-Effekt transparent.",
            "",
            "> Hinweis: synthetische VDI-4655-ähnliche Profile und deterministischer",
            "> Offline-Optimierer. Keine amtlichen Datensätze, kein echtes EMHASS.",
            "",
        ]
    )
    paths["markdown"].write_text("\n".join(lines), encoding="utf-8")
    return paths


def cli_main(argv: list[str] | None = None) -> None:
    """CLI: python -m hems.hil.benchmark --out-dir results [Optionen]."""
    import argparse

    parser = argparse.ArgumentParser(description="HEMS Thesis-Benchmark (A/B/C)")
    parser.add_argument("--out-dir", type=Path, default=Path("benchmark_results"))
    parser.add_argument("--steps", type=int, default=96, help="Anzahl 15-min-Schritte (96 = 24 h)")
    parser.add_argument("--step-minutes", type=int, default=15)
    parser.add_argument("--pv-peak-w", type=float, default=10000.0)
    parser.add_argument(
        "--profiles",
        nargs="+",
        default=[p.value for p in ReferenceProfile],
        choices=[p.value for p in ReferenceProfile],
    )
    parser.add_argument(
        "--modes",
        nargs="+",
        default=[m.value for m in OperatingMode],
        choices=[m.value for m in OperatingMode],
    )
    args = parser.parse_args(argv)

    spec = ProfileSpec(
        profile=ReferenceProfile.H25,
        steps=args.steps,
        step_minutes=args.step_minutes,
        pv_peak_w=args.pv_peak_w,
    )
    report = asyncio.run(
        run_benchmark(
            spec,
            args.out_dir,
            profiles=[ReferenceProfile(value) for value in args.profiles],
            modes=[OperatingMode(value) for value in args.modes],
        )
    )
    paths = export_report(report, args.out_dir)
    print("Benchmark abgeschlossen:")
    for name, path in paths.items():
        print(f"  {name}: {path}")


if __name__ == "__main__":
    cli_main()
