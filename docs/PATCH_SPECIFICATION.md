# Patch-Spezifikation: Verbindliche Verbesserungen für docs/00–04 & AGENTS.md

**Anweisung für den Coding-Agent:** Pflege ausschließlich die unten definierten Änderungen exakt in die jeweils genannten Zieldateien ein. Bestehende Abschnitte, die hier nicht genannt sind, bleiben unverändert.

---

## 1. Zieldatei: `docs/01_ADDON_SCAFFOLD_AND_CONFIG.md`

### Fix C3: Home Assistant Manifest (config.yaml)

- Ändere `startup: services` zu `startup: application` (der Orchestrator konsumiert MQTT/HA und muss nach den Core-Services starten).
- Ergänze `services: ["mqtt:need"]`, damit der Home Assistant Supervisor automatisch die Zugangsdaten für `core-mosquitto` bereitstellt, falls `mqtt_username` / `mqtt_password` leer sind.
- Ergänze in `AppConfig` (`src/hems/config.py`) und in `options`/`schema` von `config.yaml`:
  - `audit_heartbeat_s: int = Field(default=60, ge=5, le=3600)` (Heartbeat-Intervall für Log-Deduplizierung im 1-Hz-Loop).

### Fix C4: Dockerfile-Tippfehler & mypy-Kommentar

- Korrigiere im Dockerfile den Befehl `pip install --no-cache-dir ..` zu:
  ```dockerfile
  RUN pip install --no-cache-dir .
  ```
- Entferne jegliche Erwähnung von `mypy` aus den Kommentaren zu `pyproject.toml` (verbindliche Gate-Tools sind ausschließlich `ruff` und `pytest`).

---

## 2. Zieldatei: `docs/02_DATA_MODELS_AND_AUDIT_LOGGING.md`

### Fix C1: Klarstellung 2-Jahres-Nachweis (§ 14a EnWG) vs. JSONL-Rotation

Ergänze in Abschnitt 2 & 3 folgende verbindliche Architekturregel:

- SQLite (`hems_audit.db`) ist die verbindliche 2-Jahres-Nachweisquelle nach § 14a EnWG / BNetzA BK6-22-300 (keine automatische Löschung von Zeilen innerhalb von 24 Monaten).
- JSONL (`decision_trace.jsonl`) dient als rotierendes, komprimiertes Roh-Ereignisprotokoll (max. `jsonl_backup_count × jsonl_max_bytes`), um die Größe von Home Assistant Backups zu begrenzen.

### Fix B4: Entkopplung von Realzeit und virtueller HIL-Zeit im VerificationTracker

Ergänze in Abschnitt 4 (`src/hems/logging/verification.py`):

- Der `VerificationTracker` akzeptiert einen optionalen Zeitskalierungs- bzw. Taktgeber-Parameter:
  - `clock_sleep: Callable[[float], Awaitable[None]] = asyncio.sleep`
  - `now_utc: Callable[[], datetime] = lambda: datetime.now(timezone.utc)`
- Im Realbetrieb wird `asyncio.sleep(config.verification_delay_s)` genutzt.
- Im Quasi-HIL-Zeitrafferbetrieb wird eine deterministische `SimulatedClock` (oder `immediate_verify_hook` nach dem nächsten `PlantSimulator.step()`) injiziert, sodass Verifikationen ohne reale Wartezeit (0 s Wall-Clock-Time) sofort gegen den simulierten Folgezustand ausgewertet werden.

---

## 3. Zieldatei: `docs/03_CONTROL_CORE_AND_EMHASS.md`

### Fix B2: Exaktes EMHASS-Response-Parsing & Vorzeichen-Konvention

Ergänze in Abschnitt 2 (`src/hems/io/emhass_client.py`) folgendes verbindliches Feld-Mapping für den ersten Zeitschritt ($t = 0$) der EMHASS-Antwort:

- `P_batt` → `battery_w`: In EMHASS bedeutet `P_batt > 0` Entladung und `P_batt < 0` Ladung. Beim Parsen muss das Vorzeichen invertiert werden:
  $$\text{battery\_w} = -\text{float}(\text{row}[\text{"P\_batt"}])$$
- `P_deferrable0` → `wallbox_w`: Index 0 der steuerbaren Lasten ist verbindlich die Wallbox (`wallbox_w = max(0.0, float(row["P_deferrable0"]))`).
- `P_deferrable1` → `heatpump_w`: Index 1 der steuerbaren Lasten ist verbindlich die Wärmepumpe (`heatpump_w = max(0.0, float(row["P_deferrable1"]))`).
- Fehlende Pflichtspalten (`P_batt`, `P_deferrable0`, `P_deferrable1`) oder leere Antworten liefern deterministisch `EmhassResult(status="error", ...)`.

### Fix B1: Verbindliches Mapping `heatpump_w` ↔ `HeatpumpSGReadyMode`

Ergänze vor Abschnitt 4 eine reine Hilfsfunktion:

```python
map_heatpump_power_to_sg_ready(
    heatpump_w: float,
    config: AppConfig,
    force_blocked: bool = False,
    force_boost: bool = False,
) -> tuple[HeatpumpSGReadyMode, float]
```

| Bedingung | `cmd_heatpump_sg_ready` | `cmd_heatpump_w_est` (Watt) | Semantischer Hinweis |
|-----------|-------------------------|-----------------------------|----------------------|
| `force_blocked == True` | `BLOCKED` (Zustand 1) | `0.0` | EVU-Sperre / harter P1-Lastabwurf |
| `force_boost == True` | `FORCED` (Zustand 4) | `config.heatpump_nominal_power_w` | Harter Komfort-Eingriff (P2) |
| `heatpump_w <= 0.0` | `NORMAL` (Zustand 2) | `0.0` | Regulärer Betrieb ohne Anhebung (kein Missbrauch von Zustand 1!) |
| `0.0 < heatpump_w < 0.8 * config.heatpump_nominal_power_w` | `RECOMMENDED` (Zustand 3) | `0.5 * config.heatpump_nominal_power_w` | Verstärkter Betrieb / PV-Anhebung |
| `heatpump_w >= 0.8 * config.heatpump_nominal_power_w` | `FORCED` (Zustand 4) | `config.heatpump_nominal_power_w` | Maximale Leistungsanforderung |

**Korrektur im P2-Temperaturband (Schritt 1):** Bei `inputs.t_buffer_c > config.heatpump_temp_max_c` wird `sg_ready = NORMAL` (und `heatpump_w = 0.0`) gesetzt – nicht `BLOCKED`, da SG-Ready Zustand 1 semantisch der EVU-/P1-Sperre vorbehalten ist.

### Fix A2: Globale Absicherung der Wallbox-Mindestleistung (1380 W)

- Im P2-EV-Komfort-Guard (Schritt 1): Wenn `0.0 < p_required_w < config.wallbox_min_power_w`, runde auf `config.wallbox_min_power_w` (1380 W) auf, bevor `min(..., config.wallbox_max_power_w)` angewendet wird (um den Komfort-Ziel-SoC nicht zu unterschreiten).
- Globaler Post-Processing-Clamp (nach Schritt 2, vor Rückgabe von `ControlOutputs`):
  ```python
  if 0.0 < wallbox_w < config.wallbox_min_power_w:
      wallbox_w = 0.0
      cascade_evaluated_rules.append("POST_wallbox_below_min_clamped_to_0")
  ```
  Damit ist garantiert, dass kein Pfad (weder P1, P2, P3 noch P4) jemals einen ungültigen Wert im Intervall $(0, 1380)\text{ W}$ ausgibt.

### Fix A1 & C5: § 14a EnWG / § 9 EEG Kollisionsregel & Modellannahme

- **Dokumentierte Modellannahme (C5):** `dso_14a_limit_w` modelliert die aus § 14a EnWG / BNetzA BK6-22-300 resultierende zulässige Netzwirkleistung aller steuerbaren Verbrauchseinrichtungen (steuVE) nach EMS-Saldierungsprivileg in absoluten Watt ($\ge 4200\text{ W}$). Eventuelle prozentuale Steuerbox-Signale werden im vorgelagerten Ingestion-Layer in absolute Watt umgerechnet.
- **Re-Clamping-Regel (A1 – Kollision § 14a vs. § 9 EEG):** Bei der § 14a-Prüfung zählt nach EMS-Saldierungsprivileg ausschließlich der Netzbezug der steuVE:
  $$P_{\text{steuVE,grid}} = \max\!\bigl(0.0,\; \text{wallbox\_w} + \text{heatpump\_w} + \max(0.0, \text{battery\_w}) - \max(0.0, P_{\text{pv}} - P_{\text{house\_base}})\bigr)$$
  Wenn § 9 EEG im Anschluss die Batterieladung `battery_w` erhöht, um überschüssige PV-Einspeisung zu vermeiden, muss direkt danach (vor dem Hausanschlussschutz) ein verbindlicher § 14a Re-Check auf $P_{\text{steuVE,grid}} \le \text{limit}_{14a}$ erfolgen. Ist $P_{\text{steuVE,grid}} > \text{limit}_{14a}$, wird die zusätzliche EEG-Batterieladung exakt so weit zurückgenommen, dass § 14a zwingend eingehalten bleibt (`"P1_14a_reclamp_after_eeg"`).

### Fix C1: Log-Deduplizierung & Heartbeat im 1-Hz-Loop (Schritt 3)

Ersetze die bisherige Logging-Regel in Schritt 3 durch: Ein `DecisionTraceRecord` wird im 1-Hz-Loop geschrieben, wenn mindestens eine Bedingung zutrifft:

- Neuer 15-min-MPC-Lauf (`MPC_CYCLE`), ODER
- Zustands-/Sollwertänderung gegenüber dem letzten geloggten Zyklus:
  - `winning_priority` hat sich geändert, ODER
  - `trigger` hat sich geändert, ODER
  - `abs(cmd_wallbox_w - prev.cmd_wallbox_w) > 10.0`, ODER
  - `abs(cmd_battery_w - prev.cmd_battery_w) > 10.0`, ODER
  - `cmd_heatpump_sg_ready != prev.cmd_heatpump_sg_ready`, ODER
- Heartbeat abgelaufen: Seit dem letzten Log-Eintrag sind $\ge \text{config.audit\_heartbeat\_s}$ Sekunden (Standard: 60 s) vergangen.

**Ergebnis:** Verhindert 1-Hz-Log-Spam bei dauerhaft aktivem P1/P2/P4-Zustand und schont SD-Karten/HA-Backups, ohne Zustandswechsel zu verlieren.

### Ergänzende Pflicht-Unit-Tests (`tests/test_priority_cascade.py`)

Füge zwei neue Pflicht-Tests zum Phase-3-Gate hinzu:

- **Test § 14a + § 9 EEG Kollision (A1):** `dso_14a_active=True` (4200 W) + hohe PV-Einspeisung über 60-%-Limit → Batterie darf PV-Überschuss aufnehmen, aber $P_{\text{steuVE,grid}}$ bleibt strikt $\le 4200\text{ W}$.
- **Test P2 Wallbox-Aufrundung & Post-Clamp (A2):** P2 fordert `p_required_w = 500 W` → wird auf `1380.0 W` aufgerundet; falls P1 anschließend auf < 1380 W drosselt → deterministisch `0.0 W`.

---

## 4. Zieldatei: `docs/04_QUASI_HIL_AND_VERIFICATION.md`

### Fix B3: Deterministische Anfangszustände im PlantSimulator

Ergänze in Abschnitt 1.2 ein Pydantic-Modell `PlantInitialState(BaseModel)`:

```python
class PlantInitialState(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    soc_battery_pct: float = Field(default=50.0, ge=0.0, le=100.0)
    soc_ev_pct: float = Field(default=30.0, ge=0.0, le=100.0)
    t_buffer_c: float = Field(default=48.0, ge=10.0, le=85.0)
```

`PlantSimulator.__init__(self, config: AppConfig, initial_state: PlantInitialState | None = None)` nutzt diese festen Standardwerte, sodass jeder Replay-Lauf zu 100 % reproduzierbar ist.

### Fix B4: Virtuelle Zeitsteuerung im Replay-Treiber

Ergänze in Abschnitt 1 (`src/hems/hil/replay_driver.py`):

- Der `ReplayDriver` arbeitet mit einer virtuellen Uhr (`SimulatedClock`), die den Zeitstempel aus jeder CSV-Zeile übernimmt.
- Zwischen den Schritten erfolgt kein `asyncio.sleep(...)`.
- Der `VerificationTracker` wird im Replay-Modus mit einem deterministischen Hook betrieben, der die Verifikation jedes § 14a-/P1-Events direkt gegen den vom `PlantSimulator` berechneten Folgezustand ($t + \Delta t$) ausführt. Ein 24-Stunden-Szenario (96 × 15-min-Schritte) läuft dadurch in $< 2\text{ Sekunden}$ Wall-Clock-Time durch.

### Fix C2: Exaktes zeitgewichtetes KPI-Integral für nicht-äquidistante Logs

Präzisiere in Abschnitt 2 (`src/hems/hil/kpi_calculator.py`) die Berechnungsformel für alle zeitbasierten KPIs (`total_grid_import_kwh`, `total_energy_cost_eur`, `total_co2_kg`, `mean_grid_import_w`):

1. Sortiere alle geladenen Datensätze strikt aufsteigend nach `timestamp_utc`: $(t_0, t_1, \dots, t_{N-1})$.
2. Definiere die Intervalldauer $\Delta t_i$ (in Sekunden) mittels Piecewise-Constant (Left-Riemann / Zero-Order-Hold):
   - Für $0 \le i < N - 1$: $\Delta t_i = \min\!\bigl((t_{i+1} - t_i).\text{total\_seconds}(),\; \text{max\_step\_s}\bigr)$ mit Default `max_step_s = 900.0` (15 min, verhindert Verzerrung bei längeren Systempausen).
   - Für den letzten Datensatz $i = N - 1$: $\Delta t_{N-1} = \Delta t_{N-2}$ (falls $N \ge 2$, sonst 0.0).
3. Exakte Formeln:
   - $\text{total\_grid\_import\_kwh} = \sum_{i=0}^{N-1} \max(0.0,\; P_{\text{grid},i}) \cdot \frac{\Delta t_i}{3\,600\,000.0}$
   - $\text{mean\_grid\_import\_w} = \frac{\sum_{i=0}^{N-1} \max(0.0,\; P_{\text{grid},i}) \cdot \Delta t_i}{\sum_{i=0}^{N-1} \Delta t_i}$ (falls $\sum \Delta t_i > 0$, sonst 0.0).
   - Für `total_energy_cost_eur` und `total_co2_kg` wird jeweils der zugehörige Import-Anteil $\max(0.0, P_{\text{grid},i}) \cdot \frac{\Delta t_i}{3\,600\,000.0}$ mit `tariff_ct_kwh[i] / 100.0` bzw. `co2_intensity_g_kwh[i] / 1000.0` multipliziert.

---

## 5. Zieldatei: `AGENTS.md`

Prüfe, dass folgende Invarianten in Abschnitt 3 (`AGENTS.md`) synchron zu den obigen Patches stehen (bereits angelegt, bei Abweichung angleichen):

- `P_batt`-Vorzeicheninvertierung (`battery_w = -p_batt_emhass`).
- Globale Wallbox-Mindeststrom-Regel (P2-Aufrundung + Post-P1-Clamp auf 0.0).
- § 14a EMS-Saldierungsprivileg + Re-Clamp nach § 9 EEG.
- Log-Deduplizierung mit `audit_heartbeat_s = 60`.