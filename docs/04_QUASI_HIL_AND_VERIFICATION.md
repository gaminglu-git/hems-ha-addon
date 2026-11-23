# 04 – Phase 4: Quasi-HIL Replay-Treiber, KPI-Rechner & End-to-End-Verifikation

## Ziel dieser Phase

Implementierung des wissenschaftlichen Evaluations-Toolings direkt im Repository: ein deterministischer Quasi-HIL-Replay-Treiber für deutsche Referenzprofile (VDI 4655 / BDEW H25, P25, S25) mit gezielter Störfall-Injektion sowie ein automatisierter KPI-Rechner zur Auswertung der SQLite-Audit-Datenbank.

## 1. Quasi-HIL Replay-Treiber (src/hems/hil/replay_driver.py)

### Zweck

Ermöglicht reproduzierbare, zeitrafferfähige Experimente (z. B. 24-Stunden-Szenario in wenigen Sekunden oder Echtzeit) gegen den realen HEMS-Regelkern (inkl. Logging & SQLite-Audit-DB).

### 1.1 Eingabeformat (CSV-Schema)

Eine Szenario-CSV-Datei hat exakt folgende Spalten:

```csv
timestamp_utc,p_pv_w,p_house_base_w,tariff_ct_kwh,co2_intensity_g_kwh,ev_connected,ev_departure_in_min,dso_14a_active,dso_14a_limit_w
```

### 1.1.1 Anfangszustände (verbindliche Defaults)

Da das CSV keine Zustandsgrößen enthält, startet der PlantSimulator mit deterministischen Anfangszuständen aus `PlantInitialState` (Abschnitt 1.2, verbindliche Defaults: `soc_battery_pct=50.0`, `soc_ev_pct=30.0`, `t_buffer_c=48.0`). Überschreibbar über ein Pydantic-Modell `ReplayConfig` (`extra="forbid"`):

- `initial_state: PlantInitialState | None = None` (überschreibt die deterministischen Anfangszustände; `None` → feste Defaults aus `PlantInitialState`)
- `time_scale: float = 1.0` (siehe Zeitmodell, Abschnitt 1.4)

### 1.2 Einfaches physikalisches Anlagen-Emulationsmodell (PlantSimulator)

Da bei einem Replay die Aktoren (`cmd_wallbox_w`, `cmd_battery_w`, `cmd_heatpump_w_est`) die Zustände im nächsten Zeitschritt (Δt in Sekunden) beeinflussen, aktualisiert PlantSimulator deterministisch:

**Deterministische Anfangszustände (Fix B3, verbindlich):**

```python
class PlantInitialState(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    soc_battery_pct: float = Field(default=50.0, ge=0.0, le=100.0)
    soc_ev_pct: float = Field(default=30.0, ge=0.0, le=100.0)
    t_buffer_c: float = Field(default=48.0, ge=10.0, le=85.0)
```

`PlantSimulator.__init__(self, config: AppConfig, initial_state: PlantInitialState | None = None)` nutzt diese festen Standardwerte (falls `initial_state is None`), sodass jeder Replay-Lauf zu 100 % reproduzierbar ist.

**Batterie-SoC:**
- Wirkungsgrad η_chg = 0.95, η_dis = 0.95.
- Wenn `cmd_battery_w >= 0`: ΔE_bat = `cmd_battery_w` · η_chg · Δt/3600.
- Wenn `cmd_battery_w < 0`: ΔE_bat = `cmd_battery_w` / η_dis · Δt/3600.
- `soc_battery_pct` = clamp(`soc_battery_pct` + (delta_e / `config.battery_capacity_wh`) · 100.0, 0.0, 100.0).

**EV-SoC (wenn ev_connected):**
- ΔE_ev = `cmd_wallbox_w` · 0.92 · Δt/3600.
- `soc_ev_pct` = clamp(`soc_ev_pct` + (delta_e_ev / `config.ev_capacity_wh`) · 100.0, 0.0, 100.0).

**Pufferspeicher-Temperatur (t_buffer_c):**
- Einfaches thermisches Modell: Wärmeverlust −0.0005 °C/s, Wärmeeintrag bei `cmd_heatpump_w_est > 0` mit COP = 3.5 auf thermische Kapazität C_th = 1500 Wh/K.

**Resultierende Netzleistung:**
```text
p_grid_w = p_house_base_w + cmd_wallbox_w + cmd_heatpump_w_est + cmd_battery_w - p_pv_w
```

### 1.3 Störfall-Injektion (FaultScenario)

Unterstütze per Konfiguration injizierbare Störfälle zu definierten Zeitschritten:

- `INJECT_14A_DIMMING(start_step, end_step, limit_w=4200.0)`
- `INJECT_SOLVER_TIMEOUT(start_step, end_step)`
- `INJECT_NEGATIVE_TARIFF(start_step, end_step, tariff_ct_kwh=-5.0)`

### 1.4 Zeitmodell (Zeitraffer & Verifikation, verbindlich)

Replay-Treiber und VerificationTracker nutzen ausschließlich das einheitliche `Clock`-Protocol aus docs/02 (`now_utc() -> datetime`, `async sleep(seconds: float) -> None`):

- **Produktion:** `RealClock` (`datetime.now(timezone.utc)`, `asyncio.sleep`).
- **Quasi-HIL:** `SimulatedClock` (verbindliche Implementierung in Abschnitt 1.5). Zwischen den Schritten erfolgt kein reales `asyncio.sleep(...)`; ein 24-h-Szenario läuft deterministisch in Sekunden Wall-Clock-Time.
- `verification_delay_s` und `mpc_interval_s` werden in virtueller Zeit ausgewertet; die fachlichen Assertions (Compliance, Latenz, KPIs) bleiben identisch zur Echtzeit-Betrachtung.

### 1.5 SimulatedClock im Replay-Treiber (Fix B4/K5, verbindlich)

Einheitliche Implementierung des `Clock`-Protocols aus docs/02 (ersetzt alle früheren `clock_sleep`/`now_utc`-Callables und den `immediate_verify_hook`):

- `SimulatedClock` speichert die aktuelle virtuelle Zeit `current_utc: datetime` sowie eine Liste wartender `(wake_at_utc, asyncio.Event)`-Einträge.
- `sleep(seconds)` registriert `(current_utc + timedelta(seconds=seconds), event)` und wartet auf `await event.wait()`.
- Der `ReplayDriver` übernimmt den Zeitstempel jeder CSV-Zeile und ruft nach jedem `PlantSimulator.step()` `await simulated_clock.advance_to(next_timestamp_utc)` auf:
  1. Setzt `current_utc = next_timestamp_utc`.
  2. Setzt alle Events mit `wake_at_utc <= current_utc`.
  3. Gibt dem Event-Loop via `await asyncio.sleep(0)` Kontrolle, sodass alle fälligen `VerificationTracker`-Tasks deterministisch gegen den neuen PlantSimulator-Zustand abschließen.
- Am Ende des Replays wird `await simulated_clock.advance_by(config.verification_delay_s + 1.0)` aufgerufen, damit auch die Verifikation des letzten Schritts sauber abgeschlossen wird.
- Ein 24-Stunden-Szenario (96 × 15-min-Schritte) läuft dadurch in < 2 Sekunden Wall-Clock-Time durch.

## 2. Automatisierter KPI-Rechner (src/hems/hil/kpi_calculator.py)

### Anforderungen

Liest eine SQLite-Audit-Datenbank (`hems_audit.db`) für einen definierten Zeitraum (`start_utc`, `end_utc`) und optional gefiltert nach `mode` aus und berechnet ein Pydantic-Modell `KpiReport`:

| KPI-Feld | Formel / Berechnung | Einheit |
|----------|---------------------|---------|
| `mode` | Betriebsmodus | String |
| `total_decisions` | Anzahl der geloggten Entscheidungen | Integer |
| `peak_grid_import_w` | max(0, max_t P_grid(t)) | W |
| `mean_grid_import_w` | Zeitgewichtet: Σ max(0, P_grid)·Δt / Σ Δt (siehe Δt-Definition, Fix C2) | W |
| `peak_to_average_ratio` (PAR) | peak_grid_import_w / mean_grid_import_w (0.0 falls Nenner = 0) | Dimensionslos |
| `total_grid_import_kwh` | Zeitgewichtetes Integral Σ max(0, P_grid) · Δt / 3600000 | kWh |
| `total_energy_cost_eur` | Σ Import_kWh · (tariff_ct_kwh / 100) | EUR |
| `total_co2_kg` | Σ Import_kWh · (co2_intensity_g_kwh / 1000) | kg |
| `dso_14a_event_count` | Anzahl Entscheidungen mit dso_14a_active == 1 | Integer |
| `dso_14a_compliance_rate_pct` | Anteil der § 14a-Events mit compliance_ok == 1 in Prozent | % |
| `comfort_violations_count` | Anzahl Zeitschritte mit t_buffer_c < heatpump_temp_min_c | Integer |
| `p95_e2e_latency_ms` | 95. Perzentil aller nicht-NULL e2e_latency_ms | ms |
| `solver_fallback_count` | Anzahl Entscheidungen mit winning_priority == 4 | Integer |

**Δt-Definition & zeitgewichtete KPI-Integrale (verbindlich, Fix C2):** Für alle zeitbasierten KPIs (`total_grid_import_kwh`, `total_energy_cost_eur`, `total_co2_kg`, `mean_grid_import_w`) gilt Piecewise-Constant (Left-Riemann / Zero-Order-Hold) für nicht-äquidistante Logs:

1. Sortiere alle geladenen Datensätze strikt aufsteigend nach `timestamp_utc`: $(t_0, t_1, \dots, t_{N-1})$.
2. Definiere die Intervalldauer $\Delta t_i$ (in Sekunden):
   - Für $0 \le i < N - 1$: $\Delta t_i = \min\!\bigl((t_{i+1} - t_i).\text{total\_seconds}(),\; \text{max\_step\_s}\bigr)$ mit Default `max_step_s = 900.0` (15 min, verhindert Verzerrung bei längeren Systempausen).
   - Für den letzten Datensatz $i = N - 1$: $\Delta t_{N-1} = \Delta t_{N-2}$ (falls $N \ge 2$, sonst 0.0).
3. Exakte Formeln:
   - $\text{total\_grid\_import\_kwh} = \sum_{i=0}^{N-1} \max(0.0,\; P_{\text{grid},i}) \cdot \frac{\Delta t_i}{3\,600\,000.0}$
   - $\text{mean\_grid\_import\_w} = \frac{\sum_{i=0}^{N-1} \max(0.0,\; P_{\text{grid},i}) \cdot \Delta t_i}{\sum_{i=0}^{N-1} \Delta t_i}$ (falls $\sum \Delta t_i > 0$, sonst 0.0).
   - Für `total_energy_cost_eur` und `total_co2_kg` wird jeweils der zugehörige Import-Anteil $\max(0.0, P_{\text{grid},i}) \cdot \frac{\Delta t_i}{3\,600\,000.0}$ mit `tariff_ct_kwh[i] / 100.0` bzw. `co2_intensity_g_kwh[i] / 1000.0` multipliziert.

Hinweis: `total_energy_cost_eur` berücksichtigt nur Bezugskosten, keine Einspeiseerlöse (bewusste Vereinfachung für den Modi-Vergleich). Bei triggerbasiertem (nicht äquidistantem) Logging approximiert das Left-Riemann-Integral den wahren Verlauf; im E2E-Test mit äquidistanten Schritten ist es exakt.

Zusätzlich bietet `kpi_calculator.py` eine CLI-Funktion (`python -m hems.hil.kpi_calculator --db /data/db/hems_audit.db --out report.json`), die den Report als formatiertes JSON exportiert.

## 3. End-to-End-Verifikation & Finales Gate (tests/test_quasi_hil_e2e.py)

Erstelle einen vollständigen End-to-End-Integrationstest ohne externe Netzabhängigkeiten (EMHASS gemockt via respx, SQLite + JSONL in tmp_path):

**Szenario-Lauf (Modus C, 24 Zeitschritte à 15 min simuliert):**

- Enthält einen injizierten § 14a EnWG-Dimmungsabruf (Limit 4200 W) während hoher Wallbox-Ladeanforderung (11000 W).
- Enthält einen injizierten EMHASS-Solver-Timeout.

**Pflicht-Assertions:**

1. Alle Entscheidungen sind lückenlos sowohl in `decision_trace.jsonl` als auch in `hems_audit.db` vorhanden.
2. Während des § 14a-Eingriffs liegt die resultierende netto-netzbezogene steuerbare Leistung `P_steuve,grid` (`controllable_load_after_w` gemäß Saldierungsformel K2, docs/02) strikt ≤ 4200 W · 1.02 und `compliance_ok == True`.
3. Während des Solver-Timeouts greift deterministisch `winning_priority == 4` ohne Exception.
4. `KpiReport` berechnet plausible Werte (`dso_14a_compliance_rate_pct == 100.0`, `solver_fallback_count >= 1`, `peak_to_average_ratio >= 1.0`).
5. Im `dry_run=True`-Modus wurde keine einzige Nachricht auf Hardware-Topics (`hems/cmd/...`) publiziert.

## Finales Abnahme-Kommando für den Agenten

```bash
ruff check src tests
pytest -v
```

Beide Kommandos müssen mit Exit-Code 0 und 0 Warnings/Errors abschließen.