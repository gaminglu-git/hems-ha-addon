# 02 – Phase 2: Kanonische Datenmodelle & Revisionssicheres Audit-Logging

## Ziel dieser Phase

Implementierung aller unveränderlichen Datenverträge (`src/hems/models.py`) sowie der zweigliedrigen, vom Home Assistant Recorder unabhängigen Speicherarchitektur (Append-Only Decision-Trace JSONL + Async SQLite Audit-DB) inklusive des asynchronen § 14a EnWG Wirkungsnachweises.

## 1. Kanonische Datenmodelle (src/hems/models.py)

Alle Modelle verwenden `model_config = ConfigDict(extra="forbid", frozen=True)` (Ausnahme: interne mutierbare Zustandscontainer).

### 1.1 TriggerReason(str, Enum)

- `MPC_CYCLE = "mpc_cycle"`
- `PERIODIC_1HZ = "periodic_1hz"`
- `DSO_14A_DIMMING_EVENT = "dso_14a_dimming_event"`
- `EEG_FEEDIN_LIMIT_EVENT = "eeg_feedin_limit_event"`
- `HOUSE_FUSE_PROTECTION = "house_fuse_protection"`
- `COMFORT_GUARD_OVERRIDE = "comfort_guard_override"`
- `SOLVER_TIMEOUT_FALLBACK = "solver_timeout_fallback"`
- `SETPOINT_CHANGE = "setpoint_change"`

### 1.2 HeatpumpSGReadyMode(str, Enum)

- `BLOCKED = "blocked"` (SG-Ready Zustand 1)
- `NORMAL = "normal"` (SG-Ready Zustand 2)
- `RECOMMENDED = "recommended"` (SG-Ready Zustand 3)
- `FORCED = "forced"` (SG-Ready Zustand 4)

### 1.3 SystemInputs(BaseModel) (Eingangsdaten-Snapshot)

- `p_grid_before_w: float` (Netzanschlusspunkt, Bezug > 0, Einspeisung < 0)
- `p_pv_w: float` (≥ 0)
- `p_house_base_w: float` (≥ 0, nicht steuerbare Grundlast)
- `soc_battery_pct: float` (0.0 bis 100.0)
- `soc_ev_pct: float` (0.0 bis 100.0)
- `ev_connected: bool`
- `ev_departure_in_min: float | None = None`
- `t_buffer_c: float` (Wärmepumpen-Pufferspeicher-Temperatur)
- `tariff_ct_kwh: float`
- `co2_intensity_g_kwh: float` (≥ 0)
- `dso_14a_active: bool`
- `dso_14a_limit_w: float` (≥ 4200.0 wenn aktiv)
- `eeg_feedin_limit_pct: float` (0.0 bis 100.0)
- `pv_forecast_next_1h_w: float = 0.0`

### 1.4 ProposedSetpoints(BaseModel)

- `wallbox_w: float` (≥ 0)
- `battery_w: float` (Ladung > 0, Entladung < 0)
- `heatpump_w: float` (≥ 0)

### 1.5 DecisionDerivation(BaseModel)

- `emhass_status: Literal["ok", "timeout", "error", "stale", "disabled"]`
- `emhass_solve_ms: int | None`
- `runtimeparams_hash: str | None` (erste 8 Zeichen des SHA-256 Hex-Digests der kanonischen runtimeparams)
- `emhass_proposed_setpoints_w: ProposedSetpoints | None`
- `cascade_evaluated_rules: list[str]` (geordnete Liste aller geprüften Regeln, z. B. `["P1_house_fuse_ok", "P1_14a_limit_violated_11000W_gt_4200W"]`)
- `winning_priority: Literal[1, 2, 3, 4]`
- `override_reason: str | None`

### 1.6 ControlOutputs(BaseModel)

- `cmd_wallbox_w: float` (≥ 0)
- `cmd_battery_w: float`
- `cmd_heatpump_sg_ready: HeatpumpSGReadyMode`
- `cmd_heatpump_w_est: float` (≥ 0, erwartete elektrische Leistung)

### 1.7 VerificationResult(BaseModel)

- `check_after_s: float`
- `verified_at_utc: str`
- `p_grid_after_w: float`
- `controllable_load_after_w: float`
- `compliance_ok: bool`
- `e2e_latency_ms: int`

### 1.8 DecisionTraceRecord(BaseModel)

- `event_type: Literal["decision"] = "decision"`
- `decision_id: str` (Format: `{YYYY-MM-DDTHH:MM:SSZ}_{seq:04d}`, global eindeutig pro Prozesslauf)
- `timestamp_utc: str`
- `mode: OperatingMode`
- `dry_run: bool`
- `trigger: TriggerReason`
- `inputs: SystemInputs`
- `derivation: DecisionDerivation`
- `outputs: ControlOutputs`

### 1.9 VerificationTraceRecord(BaseModel)

- `event_type: Literal["verification"] = "verification"`
- `decision_id: str` (Fremdschlüssel auf `DecisionTraceRecord.decision_id`)
- `verification: VerificationResult`

**Architektur-Klarstellung (Zero-Ambiguity):** JSONL ist strikt append-only. Daher wird beim Treffen der Entscheidung sofort ein `DecisionTraceRecord` geschrieben (kein Warten auf `check_after_s`). Nach Ablauf von `verification_delay_s` wird ein verknüpfter `VerificationTraceRecord` angehängt UND die korrespondierende Zeile in SQLite aktualisiert.

## 2. Append-Only JSONL Logger (src/hems/logging/jsonl_logger.py)

**Anforderungen**

- Zieldatei: `{config.data_dir}/logs/decision_trace.jsonl`.
- **Rotation & Kompression (HA-Backup-Schutz):**
  - Wenn die Datei `config.jsonl_max_bytes` erreicht, rotiere sie atomar zu `decision_trace.{YYYYMMDD_HHMMSS}.jsonl.gz` (gzip-komprimiert).
  - Behalte maximal `config.jsonl_backup_count` komprimierte Archive; lösche ältere Archive automatisch (älteste zuerst).
- **Aufbewahrungs-Rollen (verbindliche Architekturregel, 2-Jahres-Nachweis § 14a EnWG):**
  - SQLite (`hems_audit.db`) ist die verbindliche 2-Jahres-Nachweisquelle nach § 14a EnWG / BNetzA BK6-22-300 (keine automatische Löschung von Zeilen innerhalb von 24 Monaten); die Audit-DB wird niemals automatisch gelöscht oder geprunt.
  - JSONL (`decision_trace.jsonl`) dient als rotierendes, komprimiertes Roh-Ereignisprotokoll (max. `jsonl_backup_count × jsonl_max_bytes`), um die Größe von Home Assistant Backups zu begrenzen. Die rotierte JSONL ist ein rollierender Kurzzeit-Puffer (Begrenzung des Speicherwachstums auf dem HA-Datenträger), nicht die Nachweisquelle.
- **Format:** Jede Zeile ist kompaktes JSON (`model.model_dump_json() + "\n"`), UTF-8 kodiert, sofort geflusht (`flush()`).
- **Thread-/Async-Sicherheit:** Schreibzugriffe sind über ein `asyncio.Lock` serialisiert; File-I/O erfolgt über `asyncio.to_thread(...)`, um den Event-Loop nicht zu blockieren.

## 3. SQLite Audit-Datenbank (src/hems/logging/sqlite_store.py)

**Anforderungen**

- Datenbankpfad: `{config.data_dir}/db/hems_audit.db`.
- **2-Jahres-Nachweis (verbindlich, Fix C1):** Die SQLite-Audit-DB ist die verbindliche Nachweisquelle nach § 14a EnWG / BNetzA BK6-22-300. Zeilen innerhalb von 24 Monaten werden niemals automatisch gelöscht oder geprunt. Die JSONL-Rotation (Abschnitt 2) betrifft ausschließlich das rotierende Roh-Ereignisprotokoll und darf die Nachweisvollständigkeit der SQLite-DB nicht berühren.
- Verbindungs-Pragmas bei jedem Connect:
  ```sql
  PRAGMA journal_mode=WAL;
  PRAGMA synchronous=NORMAL;
  PRAGMA foreign_keys=ON;
  ```

**Verbindliches Schema (exakte Tabellen- & Spaltennamen):**

```sql
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
```

**Methoden:**

```python
async def initialize(self) -> None
async def insert_decision(self, record: DecisionTraceRecord) -> None
async def update_verification(self, decision_id: str, verification: VerificationResult) -> None
async def query_14a_events(self, since_utc: str) -> list[dict[str, Any]]  # liefert alle § 14a-Eingriffe für den 2-Jahres-Nachweis
async def close(self) -> None
```

## 4. Asynchroner Wirkungsnachweis-Tracker (src/hems/logging/verification.py)

**Anforderungen**

Klasse `VerificationTracker`:

- Methode `schedule_verification(decision: DecisionTraceRecord, decision_monotonic_ns: int, dispatch_monotonic_ns: int, state_provider: Callable[[], SystemInputs]) -> asyncio.Task`.
- **Task-Hygiene (verbindlich, Fix K4):** `schedule_verification(...)` darf ausschließlich für Entscheidungen aufgerufen werden, für die tatsächlich ein `DecisionTraceRecord` persistiert wurde (JSONL + SQLite). Verhindert 1-Hz-Task-Spam und garantiert 1:1-Fremdschlüssel-Integrität in SQLite.
- **Einheitliche Zeitquelle (verbindlich, Fix K5 – Clock-Protocol):** Der `VerificationTracker` nutzt ausschließlich das folgende Clock-Protocol als einzige Zeitquelle (keine separaten `clock_sleep`/`now_utc`-Callables, kein `immediate_verify_hook`):
  ```python
  class Clock(Protocol):
      def now_utc(self) -> datetime: ...
      async def sleep(self, seconds: float) -> None: ...
  ```
  - `RealClock` (Standard in Produktion): `now_utc()` liefert `datetime.now(timezone.utc)`, `sleep(seconds)` ist `asyncio.sleep(seconds)`.
  - `VerificationTracker.__init__(..., clock: Clock | None = None)` nutzt defaultmäßig `RealClock()`.
  - Im Quasi-HIL-Zeitrafferbetrieb wird die `SimulatedClock` aus docs/04 (Abschnitt 1.5) injiziert, sodass Verifikationen ohne reale Wartezeit (0 s Wall-Clock-Time) gegen den simulierten Folgezustand ausgewertet werden.
- Wartet nicht-blockierend `config.verification_delay_s` Sekunden über die injizierte Uhr (`await clock.sleep(...)`; Realbetrieb-Default `RealClock` → `asyncio.sleep`).
- Liest den aktuellen Zustand über `state_provider()`.
- Berechnet `controllable_load_after_w` mit der **saldierungskonsistenten Aggregat-Formel (verbindlich, Fix K2)**:
  ```python
  controllable_load_after_w = max(
      0.0,
      min(
          current.p_grid_before_w,
          current.p_grid_before_w + current.p_pv_w - current.p_house_base_w,
      ),
  )
  ```
  (oder nutzt direkte steuerbare Messwerte, sofern vorhanden). Klarstellung: `current.p_grid_before_w` bezeichnet den Netzleistungswert des aktuellen Nachher-Snapshots (der Feldname folgt der `SystemInputs`-Konvention). Begründung (mathematisch konsistent zur Kaskade, EMS-Saldierungsmodell):
  - Bei PV-Überschuss (`p_pv_w >= p_house_base_w`) greift `current.p_grid_before_w` → netto-netzbezogene SteuVE-Leistung `max(0.0, p_grid_after)`.
  - Ohne PV-Überschuss (`p_pv_w < p_house_base_w`) greift `p_grid_after + p_pv_w - p_house_base_w` → Brutto-SteuVE-Leistung (inkl. entlastender Batterieentladung).
  - Damit gilt für alle Betriebspunkte (inkl. PV-Überschuss und Batterieentladung): **Kaskade compliant ⇒ Verifikation compliant.** Sind direkte Geräte-Messwerte (Wallbox, Wärmepumpe, Batterieladung) verfügbar, sind diese zu bevorzugen.
- **Compliance-Prüfregel (`compliance_ok`):**
  - Wenn `decision.inputs.dso_14a_active` wahr ist: `controllable_load_after_w <= decision.inputs.dso_14a_limit_w * 1.02` (2 % Messtoleranz).
  - Wenn `decision.trigger == TriggerReason.HOUSE_FUSE_PROTECTION`: `current.p_grid_before_w <= config.house_fuse_limit_w * 1.02`.
  - Wenn `decision.trigger == TriggerReason.EEG_FEEDIN_LIMIT_EVENT`: `current.p_grid_before_w >= -(config.pv_peak_w * (decision.inputs.eeg_feedin_limit_pct / 100.0)) * 1.02`.
  - Sonst: `True`.
- Berechnet `e2e_latency_ms = (dispatch_monotonic_ns - decision_monotonic_ns) // 1_000_000` (Zeit vom Entscheidungszeitpunkt bis zur Aktor-Publikation; beide Monotonic-Werte liefert der Dispatcher).
- Schreibt `VerificationTraceRecord` in den JsonlLogger UND ruft `sqlite_store.update_verification(...)` auf.

## 5. Akzeptanzkriterien & Tests (Phase 2 Gate)

Erstelle `tests/test_audit_logging.py` und verifiziere:

1. `DecisionTraceRecord` und `VerificationTraceRecord` serialisieren/deserialisieren verlustfrei; unbekannte Felder werden abgelehnt (`extra="forbid"`).
2. JsonlLogger rotiert korrekt bei Überschreiten von `jsonl_max_bytes`, erzeugt valide `.jsonl.gz`-Dateien und löscht Überschuss-Backups über `jsonl_backup_count`.
3. SqliteAuditStore legt Schema im WAL-Modus an, speichert `DecisionTraceRecord`, aktualisiert Verifikationsfelder korrekt und liefert § 14a-Events über `query_14a_events`.
4. VerificationTracker arbeitet asynchron (mit `verification_delay_s=0.05` im Test), erkennt sowohl `compliance_ok=True` als auch `compliance_ok=False` (z. B. wenn `controllable_load_after_w > 4200 * 1.02`).
5. `ruff check src tests` und `pytest tests/test_audit_logging.py` laufen mit Exit-Code 0.