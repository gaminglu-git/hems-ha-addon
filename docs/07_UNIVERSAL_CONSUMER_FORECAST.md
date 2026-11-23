# 07 – Phase 6: Universelle Einzelverbraucher-Prognose, WashData-Auswertung & Mustererkennung

## Ziel dieser Phase

Implementierung einer durchgängigen, transparenten Prognose- und Mustererkennungs-Pipeline (`src/hems/forecast/`), die für jeden einzelnen eingebundenen Verbraucher im gesamten Haus eine präzise 24-Stunden-Lastprognose (96 × 15-Minuten-Intervalle) erstellt:

- **WashData-Integration** (`src/hems/forecast/washdata_adapter.py`): Auswertung laufender Waschmaschinen-, Trockner- und Geschirrspüler-Zyklen (`time_remaining`, `progress`, `program`, `projected_energy_kwh`, `reference_profile` / `power_profile`) in Echtzeit, kombiniert mit gelernten Startgewohnheiten.
- **Mustererkennung für Lichter & Schaltgeräte** (`src/hems/forecast/pattern_learner.py`): Wiederkehrende Einschaltzeiten und -dauern, getrennt nach Werktag/Wochenende, über zirkuläre 1D-Clusterbildung.
- **Historische Lastprofil-Prognose** für beliebige Smart-Plug-/Leistungssensoren: 15-Minuten-Wochentags-/Wochenend-Lastprofil aus der HA-Historie.
- **Gesamthaus-Aggregation für EMHASS**: `aggregated_uncontrollable_w` als realistische Haushalts-Grundlastprognose, damit Batterie und Wallbox anstehende Haushaltslasten (z. B. laufender Waschgang) vorausschauend einplanen.

**Gate-Vorbedingung:** Phase 5 (`docs/06`) ist implementiert und grün; die 87 Bestandstests aus Phasen 1–4 sowie die neuen Phase-5-Tests bleiben unverändert grün. Keine Tests abschwächen, skippen oder Assertions entfernen.

**Pfad-Hinweis:** Alle Add-on-Dateien liegen in `hems_orchestrator/`. Gate-Kommandos werden dort ausgeführt.

**Beziehung zu `docs/06`:** Diese Datei ist die in `docs/06` Abschnitt 7 referenzierte WashData-Detailspec (`docs/07`). Die dort festgelegten Erkennungs- und Gateway-Regeln (`sensor.<slug>_state` + `sensor.<slug>_time_remaining`, `washdata_prefix`, `source_integration=WASHDATA`) bleiben verbindlich; diese Phase präzisiert die Attributsemantik und ergänzt die Prognose.

---

## 1. Konfiguration & SQLite-Schema-Erweiterung

### 1.1 `AppConfig.timezone` (notwendige Ergänzung)

Die Mustererkennung rechnet in **Lokalzeit**; die aktuelle `AppConfig` (`src/hems/config.py`) besitzt kein Zeitzonenfeld. Ergänze:

```python
timezone: str = Field(default="Europe/Berlin", min_length=1)
```

- Verwendet wird `ZoneInfo(config.timezone)` (Standardbibliothek, keine neue Dependency).
- Im `model_validator(mode="after")` einmalig prüfen, dass `ZoneInfo(config.timezone)` konstruierbar ist; bei ungültiger Zone mit klarer Fehlermeldung abbrechen (Fail-Fast, kein Silent Fallback).
- `config.yaml`: `timezone: "Europe/Berlin"` unter `options` und `timezone: str` unter `schema` ergänzen. Bestehende Konfigurationen ohne Feld nutzen den Default — die 87 Bestandstests bleiben grün.

### 1.2 `SqliteAuditStore.initialize()` — idempotente Schema-Erweiterung

Ergänze folgenden SQL-Block in `_SCHEMA` (bestehende `decisions`-Tabelle und Indizes bleiben **unverändert**):

```sql
CREATE TABLE IF NOT EXISTS learned_patterns (
    pattern_id TEXT PRIMARY KEY,
    entity_id TEXT NOT NULL,
    friendly_name TEXT NOT NULL,
    domain TEXT NOT NULL,
    day_type TEXT NOT NULL,              -- 'weekday' | 'weekend'
    typical_on_minute INTEGER NOT NULL,  -- 0..1439 (Minuten seit 00:00 Lokalzeit)
    typical_off_minute INTEGER,          -- 0..1439 oder NULL
    avg_duration_min REAL NOT NULL,
    std_dev_min REAL NOT NULL,
    confidence REAL NOT NULL,            -- 0.0..1.0
    sample_count INTEGER NOT NULL,
    estimated_power_w REAL NOT NULL,
    updated_at_utc TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_patterns_entity ON learned_patterns(entity_id, day_type);
```

> **Abgrenzung zur 2-Jahres-Nachweispflicht:** Die „niemals löschen"-Regel aus `docs/02` / `docs/00` gilt ausschließlich für die Tabelle `decisions`. `learned_patterns` ist ein austauschbares, abgeleitetes Modell und wird von `replace_patterns(...)` bewusst vollständig ersetzt.

### 1.3 Neue Store-Methoden

```python
async def replace_patterns(self, patterns: list[LearnedPattern]) -> None
async def list_patterns(self) -> list[LearnedPattern]
```

- `replace_patterns`: atomarer Austausch in **einer Transaktion**: `BEGIN IMMEDIATE;` → `DELETE FROM learned_patterns;` → `executemany(INSERT ...)` → `COMMIT`. Bei Fehler `ROLLBACK`, strukturiertes `logger.error(...)` und Re-Raise (kein `except Exception: pass`).
- `list_patterns`: `SELECT * FROM learned_patterns ORDER BY entity_id, day_type, typical_on_minute`, Rückgabe als `LearnedPattern`-Objekte (`day_type` aus String in `DayType` konvertieren).
- Importrichtung: `sqlite_store.py` importiert `LearnedPattern` aus `hems.forecast.pattern_learner`. `pattern_learner.py` darf **nicht** den Store importieren (zyklusfrei halten; der Store bleibt reiner Persistenzlayer).
- Alle SQLite-Zugriffe laufen über die bestehende `aiosqlite`-Connection (`docs/00`: kein Event-Loop-Blocking).

---

## 2. WashData-Integrations-Adapter (`src/hems/forecast/washdata_adapter.py`)

Wertet die Home-Assistant-Custom-Integration **WashData (`ha_washdata`)** aus. Eingang sind `HaEntityState`-Objekte aus `HaCoreClient.get_states()` (Phase 5, inkl. `attributes`).

### 2.1 Erkennungsregeln (`discover_washdata_appliances`)

Ein WashData-Gerät mit Slug `<slug>` gilt als erkannt, wenn in `states: list[HaEntityState]` vorhanden ist:

1. `sensor.{slug}_state` mit einem Zustand aus
   `{"off", "idle", "starting", "running", "paused", "user_paused", "ending", "finished", "anti_wrinkle", "interrupted", "force_stopped", "clean", "delay_wait"}`, **UND**
2. mindestens eine der zugehörigen Entitäten `sensor.{slug}_time_remaining` **oder** `binary_sensor.{slug}_running`.

Der Slug ist der Entity-ID-Anteil zwischen dem Punkt und `_state`.Die Erkennung ist deterministisch und überschreibt keine Registry-Einträge (Phase 5, `sync_washdata_devices`).

### 2.2 Extrahierter Live-Zustand

```python
class WashDataLiveStatus(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    slug: str
    friendly_name: str
    state: str
    is_running: bool
    program: str | None = None
    time_remaining_min: float = Field(default=0.0, ge=0.0)
    progress_pct: float = Field(default=0.0, ge=0.0, le=100.0)
    projected_energy_kwh: float | None = None
    last_cycle_energy_kwh: float | None = None
    last_cycle_duration_min: float | None = None
    current_power_w: float = Field(default=0.0, ge=0.0)
    reference_profile_w: list[float] | None = None
```

**Verbindliches Feld-Mapping (präzisiert die Attributsemantik aus `docs/06` §3.3):**

| Feld | Quelle | Regel |
|---|---|---|
| `state` | `sensor.{slug}_state.state` | unverändert |
| `is_running` | State + Binary-Sensor | `True`, wenn `state ∈ {"starting","running","ending"}` **oder** `binary_sensor.{slug}_running.state == "on"` |
| `program` | `sensor.{slug}_program.state` | `None` bei `"unknown"`, `"detecting"`, `"unavailable"` |
| `time_remaining_min` | `sensor.{slug}_time_remaining.state` | `float(...)`, ungültig → `0.0`; `"unknown"` → `0.0` |
| `progress_pct` | `sensor.{slug}_progress.state` | `float(...)`, auf `[0, 100]` clampen; `"unknown"` → `0.0` |
| `projected_energy_kwh` | `sensor.{slug}_progress.attributes["projected_energy_kwh"]` | `float(...)`, sonst `None` |
| `last_cycle_energy_kwh` | `sensor.{slug}_last_cycle_energy.state` | `float(...)`, sonst `None` |
| `last_cycle_duration_min` | `sensor.{slug}_last_cycle_duration.state` | `float(...)`, sonst `None` |
| `current_power_w` | `sensor.{slug}_power.state` (falls vorhanden) | `max(0.0, float(...))`, sonst `0.0` |
| `reference_profile_w` | `sensor.{slug}_program.attributes["reference_profile"]` **oder** Attribut `"power_profile"` (Program- oder Progress-Sensor) | Liste von 15-Minuten-W-Werten, Reihenfolge unverändert übernehmen; ungültige Werte → `None` |

> Alle Float-Parsings sind mit `try/except (TypeError, ValueError)` abgesichert und loggen bei ungültigen Werten strukturiert; ein fehlendes Attribut führt nie zu einem Crash des Forecasters.

### 2.3 Deterministische 96-Slot-Prognose (`project_washdata_forecast`)

**Signatur (verbindlich):**
```python
def project_washdata_forecast(
    status: WashDataLiveStatus,
    habitual_patterns: list[LearnedPattern],
    start_utc: datetime,
    tz: ZoneInfo,
) -> list[ForecastSlot]   # exakt 96 Einträge
```

Alle 96 Slots werden mit `0.0 W` initialisiert (`source=STATIC_BASELINE`, `confidence=0.0`) und nur die belegten Slots überschrieben. `timestamp_utc` ist `format_utc_ms(start_utc + timedelta(minutes=15*k))` (`docs/02`-Konvention).

**Aktiver Zyklus** (`status.is_running == True` und `status.time_remaining_min > 0`):

- `n_rem_slots = min(96, max(1, math.ceil(status.time_remaining_min / 15.0)))`

**Fall 1 — exakte WashData-Leistungskurve vorhanden** (`status.reference_profile_w` nicht leer):

- `idx_start = min(len(prof) - 1, int(round((status.progress_pct / 100.0) * len(prof))))`
- Übernehme `prof[idx_start : idx_start + n_rem_slots` in Slots `0 .. k-1`, mit `confidence = 0.95`, `source = ForecastSource.WASHDATA_ACTIVE_CYCLE`.
- Wenn das Profil kürzer ist als `n_rem_slots`, werden nur die verfügbaren Werte übernommen (Rest bleibt `0.0`).

**Fall 2 — Restenergie / Durchschnittsleistung bekannt:**

- `total_kwh = status.projected_energy_kwh or status.last_cycle_energy_kwh or 1.2`
- `rem_wh = max(50.0, total_kwh * 1000.0 * max(0.05, 1.0 - status.progress_pct / 100.0))`
- `avg_w = clamp(rem_wh / (status.time_remaining_min / 60.0), 50.0, 3680.0)`
- Slots `0 .. n_rem_slots - 1`: `expected_power_w = avg_w`, `confidence = 0.90`, `source = WASHDATA_ACTIVE_CYCLE`.

**Zukünftige gewohnheitsmäßige Zyklen:**

- Nur für Slots `i >= n_rem_slots` auswerten (kein Doppelzählen eines laufenden Zyklus!).
- Lokaler Tagtyp und lokale Minute aus `start_utc + 15 min × i` in `ZoneInfo(config.timezone)` bestimmen.
- Für passende `LearnedPattern` (Historie von `binary_sensor.{slug}_running` bzw. `sensor.{slug}_state`):
  `expected_power_w = pattern.estimated_power_w * pattern.confidence`, `confidence = pattern.confidence`, `source = WASHDATA_HABITUAL`.
- `estimated_power_w` wird aus `(last_cycle_energy_kwh * 1000.0) / (last_cycle_duration_min / 60.0)` berechnet (Fallback `800.0 W`).

**Beispiel (Gate A1):** `time_remaining=45`, `progress=50`, `projected_energy_kwh=1.6` → `n_rem_slots=3`, `rem_wh=800`, `avg_w≈1066.67 W` → genau Slots 0–2 > 0 (confidence 0.90), danach 0 W.

---

## 3. Mustererkennung (`src/hems/forecast/pattern_learner.py`)

### 3.1 Datenmodelle (`ConfigDict(extra="forbid", frozen=True)`)

```python
class DayType(str, Enum):
    WEEKDAY = "weekday"   # Montag (0) bis Freitag (4)
    WEEKEND = "weekend"   # Samstag (5) bis Sonntag (6)


class LearnedPattern(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    pattern_id: str                      # "{entity_id}_{day_type}_{typical_on_minute:04d}"
    entity_id: str
    friendly_name: str
    domain: str
    day_type: DayType
    typical_on_minute: int = Field(ge=0, le=1439)
    typical_off_minute: int | None = Field(default=None, ge=0, le=1439)
    avg_duration_min: float = Field(ge=0.0)
    std_dev_min: float = Field(ge=0.0)
    confidence: float = Field(ge=0.0, le=1.0)
    sample_count: int = Field(ge=1)
    estimated_power_w: float = Field(ge=0.0)
    updated_at_utc: str
```

> **Entscheidung (bestätigt):** Der Algorithmus liefert `typical_on_minute`, `avg_duration_min`, `std_dev_min`. Für `typical_off_minute` gilt verbindlich `(typical_on_minute + round(avg_duration_min)) % 1440`, sofern `avg_duration_min > 0`, sonst `None`.

### 3.2 Verbindlicher Clustering-Algorithmus (Zero-Interpretation)

Gegeben: Historie von `HaHistoryPoint` für eine Entität im Fenster `[start_local, end_local]` in Lokalzeit (`ZoneInfo(config.timezone)`).

**Schritt 1 — Aktive Zustände normalisieren:**
`state.lower() in {"on", "running", "starting", "ending"}` → aktiv (`True`), sonst inaktiv (`False`).
Dadurch funktioniert derselbe Algorithmus für `light.*`, `switch.*`, `binary_sensor.*_running` und `sensor.*_state`.

**Schritt 2 — Ereignis-Extraktion:**

- Alle `False → True`-Übergänge als Einschalt-Events `(on_dt, day_type, minute_of_day)` mit `minute_of_day = on_dt.hour * 60 + on_dt.minute`.
- Dauer `duration_min` bis zum nächsten `False`-Event, begrenzt auf `720.0 min`; Fallback `60.0 min`.
- Flackern ignorieren: `duration_min < 2.0 min` → Event verwerfen.
- `total_days[day_type]` = Anzahl eindeutiger Kalendertage im Beobachtungsfenster (mindestens 1).
- **Entscheidung (Historienfenster-Start):** Ist der erste Historienpunkt bereits aktiv, wird ein synthetischer `False → True`-Event bei `start_local` eingefügt. Ohne diese Regel würde ein am Fensteranfang bereits laufendes Gerät nicht erkannt.

**Schritt 3 — Zirkuläre 1D-Zeitfenster-Clusterbildung (pro `entity_id` und `day_type`):**

- Zirkulärer Abstand: $\Delta_{\text{circ}}(a,b)=\min(|a-b|,\;1440-|a-b|)$
- Kandidaten-Mittelpunkte $m \in \{0, 5, 10, \dots, 1435\}$ (5-Minuten-Raster).
- Sammle alle Events mit $\Delta_{\text{circ}}(\text{minute\_of\_day}, m) \le 30\text{ min}$, maximal **1 Event pro Kalendertag** (das zeitlich nächste zu $m$).
- Zirkulärer Mittelwert $\mu_{\text{on}} \in [0, 1439]$ (gerundet auf `int`), zirkuläre Standardabweichung $\sigma_{\text{on}}$ in Minuten.
- **Non-Maximum-Suppression:** lokal beste Cluster behalten (höchster `sample_count`, kleinstes $\sigma_{\text{on}}$), sodass zwei Cluster derselben Entität am selben `day_type` mindestens **60 min** Abstand haben.

**Schritt 4 — Akzeptanzkriterien (alle müssen zutreffen):**

- `sample_count >= 3`
- `confidence = round(sample_count / total_days[day_type], 3) >= 0.50`
- `std_dev_min <= 35.0`

**Schritt 5 — Leistungsschätzung `estimated_power_w` (in dieser Reihenfolge):**

1. Gerät in der `DeviceRegistry` mit `nominal_power_w` vorhanden → dessen `nominal_power_w`; bei WashData stattdessen `(last_cycle_energy_kwh * 1000.0) / (last_cycle_duration_min / 60.0)` (falls vorhanden).
2. Sonst: Leistungssensor `sensor.{object_id}_power` in den States mit historischem Median > 0 während aktiver Phasen → dieser Median.
3. Sonst Domain-Fallback: `15.0 W` für `light.*`, `100.0 W` für `switch.*`, `800.0 W` für WashData-Entitäten, `50.0 W` sonst.

**Zeitstempel-Konvertierung (verbindliche Präzisierung):** `HaHistoryPoint.last_changed_utc` ist ein ISO-8601-String (ggf. mit `Z`-Suffix). Vor der Konvertierung nach `ZoneInfo(config.timezone)` `Z` durch `+00:00` ersetzen; ungültige Werte werden strukturiert geloggt und übersprungen. Bei DST-Umschaltung zählt der **lokale Wanduhren-Minutenwert** (`hour*60 + minute`) — destilliert aus dem konvertierten `datetime`.

### 3.3 Analysierte Entitäten

- Alle Entitäten aus `HaCoreClient.get_states()` mit Domain `light` oder `switch` (ohne `hems_*`-Entitäten).
- Alle erkannten WashData-Entitäten (`binary_sensor.{slug}_running` bzw. `sensor.{slug}_state`).
- Alle in der `DeviceRegistry` hinterlegten `state_entity_id`-Entitäten.

Historie wird über `HaCoreClient.get_history([...], start_utc=now - config.pattern_lookback_days, ...)` geladen (`pattern_lookback_days` aus Phase 5, Default 14).

---

## 4. Universelle Einzelverbraucher-Prognose-Engine (`src/hems/forecast/appliance_forecaster.py`)

Berechnet für `start_utc` eine 24-Stunden-Prognose in 96 Schritten à 15 Minuten für **jeden einzelnen** eingebundenen Verbraucher.

### 4.1 Datenmodelle (alle `ConfigDict(extra="forbid", frozen=True)`)

```python
class ForecastSource(str, Enum):
    WASHDATA_ACTIVE_CYCLE = "washdata_active_cycle"
    WASHDATA_HABITUAL = "washdata_habitual"
    MPC_SCHEDULE = "mpc_schedule"
    HABITUAL_PATTERN = "habitual_pattern"
    HISTORICAL_PROFILE = "historical_profile"
    STATIC_BASELINE = "static_baseline"


class ForecastSlot(BaseModel):
    timestamp_utc: str
    expected_power_w: float = Field(ge=0.0)
    confidence: float = Field(ge=0.0, le=1.0)
    source: ForecastSource


class ConsumerForecast(BaseModel):
    consumer_id: str             # device_id oder entity_id
    name: str
    category: str                # wallbox|battery|heatpump|deferrable_load|appliance_cycle|monitored_load|light|switch
    is_controllable: bool
    source_integration: str      # manual|washdata|ha_power_sensor|ha_pattern
    active_status_label: str | None = None  # z. B. "Läuft (Baumwolle 40°C, noch 45 min)"
    total_predicted_kwh_24h: float
    peak_power_w: float
    slots: list[ForecastSlot]    # exakt 96 Einträge


class SystemForecastReport(BaseModel):
    generated_at_utc: str
    horizon_hours: int = 24
    resolution_minutes: int = 15
    consumers: list[ConsumerForecast]
    aggregated_uncontrollable_w: list[float]  # 96 Werte → EMHASS load_power_forecast
    total_house_predicted_w: list[float]      # 96 Werte: steuerbar + nicht-steuerbar
```

`total_predicted_kwh_24h = Σ(expected_power_w) × 0.25 / 1000`, `peak_power_w = max(expected_power_w)`.

### 4.2 Verbindliche Prognose-Kaskade für ALLE Verbraucher

`ApplianceForecaster.build_forecast(...)` erzeugt für jeden Eintrag in `DeviceRegistry.list_devices()` mit `enabled == True` sowie für jede Entität mit erkanntem `LearnedPattern` (sofern nicht bereits über die Registry abgedeckt) **genau ein** `ConsumerForecast`-Objekt. Pro Verbraucher gilt die erste zutreffende Stufe:

| Stufe | Bedingung | Prognose |
|---|---|---|
| **1** | `washdata_prefix` gesetzt oder `source_integration == WASHDATA` | `project_washdata_forecast(...)` (aktiver Zyklus + gelernte Muster) |
| **2** | `is_controllable == True` | Erste 24 Slots (6 h): EMHASS-Fahrplan aus `EmhassResult.wallbox_schedule_w` / `heatpump_schedule_w` / `battery_schedule_w` (`MPC_SCHEDULE`); Slots 24–95: `STATIC_BASELINE` mit `device.min_power_w`, `confidence=0.25` |
| **3** | `LearnedPattern` vorhanden (Lichter, Schalter, binäre Verbraucher) | Für Slot `k` mit lokalem Tagtyp und Minute `m_k`: liegt `m_k` in `[typical_on_minute, typical_on_minute + avg_duration_min]` (mod 1440) → `expected_power_w = pattern.estimated_power_w * pattern.confidence`, `confidence = pattern.confidence`, `source = HABITUAL_PATTERN` |
| **4** | `power_sensor_entity_id` gesetzt | `HaCoreClient.get_history([...], start_utc=now - pattern_lookback_days)`; 15-Minuten-Slot-Mittelwert (`0..95`) getrennt nach `weekday`/`weekend`, `confidence=0.75`, `source=HISTORICAL_PROFILE`. Bei unzureichender Historie: aktueller Sensorwert bzw. `device.min_power_w` als `STATIC_BASELINE` (`confidence=0.30`) |
| **5** | Neu eingebunden, ohne Historie | `expected_power_w = device.min_power_w` für alle 96 Slots, `confidence=0.20`, `source=STATIC_BASELINE` |

Nicht-steuerbare Verbraucher aus Stufe 3 (gelernte Lichter), die nicht in der Registry liegen, erhalten `is_controllable=False`, `source_integration=ha_pattern` und gehen damit in die Aggregation ein.

**24-Slot-Fahrpläne (verbindlich nach `docs/PATCH_SPEC_PHASE_5_7.md`):** `EmhassResult` wird um drei optionale Felder ergänzt – `wallbox_schedule_w`, `heatpump_schedule_w`, `battery_schedule_w` (jeweils `list[float] | None = None`). Quellen sind `P_deferrable0[0:24]`, `P_deferrable1[0:24]` (jeweils ≥ 0 geklemmt) und `[-float(v) for v in P_batt[0:24]]` (Vorzeicheninvertierung wie bei t=0). Bei kürzeren Antworten wird der letzte Wert aufgefüllt; die Felder bleiben `None`, wenn die Spalte fehlt oder leer ist. Bestands-Mocks mit 1–2 Einträgen bleiben damit grün.

### 4.3 Rückkopplung an EMHASS

- `aggregated_uncontrollable_w[k] = Σ forecast.expected_power_w[k]` über alle `ConsumerForecast` mit `is_controllable == False` (inkl. WashData, überwachte Smart-Plug-Verbraucher, gelernte Lichter).
- **Verbindlich nach `docs/PATCH_SPEC_PHASE_5_7.md`:** `build_runtimeparams` erhält `"time_step": 15`. Damit ergeben `prediction_horizon=24` und 15-Minuten-Schritte exakt 6 Stunden. `load_power_forecast` bekommt direkt die ersten 24 Werte `aggregated_uncontrollable_w[:24]`, sofern vorhanden und nicht komplett `0.0`; sonst Fallback auf `[inputs.p_house_base_w] * 24`. Eine Stundenmittel-Aggregation findet **nicht** statt.
- **Rückwärtskompatibilität (hart):** `build_runtimeparams(inputs, config)` behält seine Signatur. Die neue Lastprognose wird als optionaler Parameter mit Default `None` ergänzt, z. B. `build_runtimeparams(inputs, config, aggregated_load_w: list[float] | None = None)`. Bei `None` bleibt das erzeugte Dict **bitgleich** (damit `runtimeparams_hash` und alle Hash-/Parameter-Assertions aus `tests/test_emhass_client.py` unverändert grün bleiben). `EmhassClient.optimize(inputs, aggregated_load_w=None)` reicht den Wert durch.
- Der aktuelle Hash wird weiterhin aus dem vollständigen `runtimeparams`-Dict berechnet; eine geänderte Lastprognose ändert damit deterministisch den `runtimeparams_hash` (wünschenswert für die Auditierbarkeit).

### 4.4 ForecastManager & Aktualisierungsfrequenz (Patch-Spec)

`ForecastManager` hält den aktuellen `SystemForecastReport` im Speicher. Neuberechnung (inkl. `HaCoreClient.get_states()` für Live-WashData-Status) erfolgt beim Add-on-Start, nach jeder `DeviceRegistry`-Änderung, nach jedem PatternLearner-Lauf, alle 60 s im Hintergrund sowie unmittelbar vor jedem MPC-Lauf. `GET /api/forecasts` liefert den Cache sofort. Details: `docs/PATCH_SPEC_PHASE_5_7.md` §3.

---

## 5. Akzeptanzkriterien & Tests (Phase-6-Gate)

Erstelle `hems_orchestrator/tests/test_pattern_forecast.py` und verifiziere:

| # | Akzeptanzkriterium | Erwartetes Ergebnis |
|---|---|---|
| B1 | WashData-Live-Zyklus | `sensor.waschmaschine_state="running"`, `time_remaining="45"`, `progress="50"`, `projected_energy_kwh=1.6` → genau die ersten **3** Slots > 0 (`WASHDATA_ACTIVE_CYCLE`, confidence ≥ 0.90), danach 0 W |
| B2 | Exaktes `reference_profile` | `sensor.geschirrspueler_program` liefert ein 8-Slot-Referenzprofil, `progress_pct=25.0` → `idx_start=2`, genau die verbleibenden **6** Slots exakt aus dem Profil übernommen |
| B3 | Licht-Muster Werktag vs. Wochenende | Synthetische Historie: 10 Werktage + 4 Wochenendtage; `light.wohnzimmer` an 8/10 Werktagen um 19:30 für 180 min, am Wochenende nur 1× → **genau ein** `WEEKDAY`-Muster mit `confidence == 0.8`, **kein** `WEEKEND`-Muster |
| B4 | Mitternachts-Wrap & Rauschunterdrückung | Schaltvorgänge 23:50–00:30 werden korrekt als ein zirkuläres Cluster geclustert; zufällige Einzelevents (< 3 Treffer) erzeugen keine False Positives |
| B5 | Universelle Prognose | Gemischtes Haus (Wallbox + Wärmepumpe + WashData-Waschmaschine + Smart-Plug-Kühlschrank mit Historie + gelerntes Wohnzimmerlicht) → für alle **5** Verbraucher je ein valider 96-Slot-`ConsumerForecast`; `aggregated_uncontrollable_w` summiert alle nicht-steuerbaren Verbraucher korrekt |
| B6 | Bestandsschutz | 87 Bestandstests + alle Phase-5-Tests bleiben grün; `build_runtimeparams`-Hash ohne Lastprognose unverändert |

**Hinweis zu B2 (entschieden):** Der Mock muss `sensor.geschirrspueler_time_remaining ≈ 90` liefern, damit `n_rem_slots = ceil(90/15) = 6` ergibt. Ohne `time_remaining` liefert die Formel `n_rem_slots = 1`. Es werden die Eingangsdaten korrigiert, nicht die Assertion abgeschwächt.

### Gate-Kommandos (aus `hems_orchestrator/`)

```bash
ruff check src tests
pytest -v
```

**Gate-Bedingung:** Exit-Code 0 für beide Kommandos; 0 Failures, 0 Errors, 0 Warnings; alle `ConsumerForecast`-Objekte haben exakt 96 Slots; ruff clean.

---

## 6. Empfohlene Implementierungsreihenfolge

1. `config.py` / `config.yaml`: `timezone` (Abschnitt 1.1) — zuerst, damit alle Module darauf zugreifen können.
2. `sqlite_store.py`: Schema + `replace_patterns` / `list_patterns` (Abschnitt 1.2–1.3) + Store-Tests.
3. `forecast/__init__.py`, `pattern_learner.py`: Modelle + zirkulärer Cluster-Algorithmus (Abschnitt 3) + Tests B3/B4.
4. `washdata_adapter.py`: Erkennung, Status-Extraktion, 96-Slot-Prognose (Abschnitt 2) + Tests B1/B2.
5. `appliance_forecaster.py`: Kaskade Stufen 1–5 + Aggregation (Abschnitt 4.1–4.2) + Test B5.
6. `emhass_client.py`: optionaler `aggregated_load_w`-Parameter ohne Hash-Änderung im Default-Fall (Abschnitt 4.3) + Test B6.
7. Verdrahtung in `scheduler.py` / `main.py`: Prognose im MPC-Zyklus erzeugen, Store aktualisieren, EMHASS übergeben — I/O außerhalb des 1-Hz-Loops.
8. Abschluss-Gate (Abschnitt 5) und Abschlussbericht.

---

## 7. Entscheidungsprotokoll (alle Punkte geklärt)

Alle zuvor offenen Punkte wurden mit dem Nutzer abgestimmt und sind hiermit verbindlich entschieden. Die Entscheidungen sind in den obigen Abschnitten eingearbeitet.

| Punkt | Entscheidung | Auswirkung |
|---|---|---|
| EMHASS-Auflösung | **Überschrieben durch `docs/PATCH_SPEC_PHASE_5_7.md`:** `time_step: 15`: direkt `aggregated_uncontrollable_w[:24]` als `load_power_forecast`, keine Stundenmittel | 24 × 15 min = 6 h; Fallback auf konstante Hauslast bei Nullprognose |
| `MPC_SCHEDULE`-Quelle | **Überschrieben durch Patch-Spec:** drei 24-Slot-Vektoren in `EmhassResult` (`wallbox_/heatpump_/battery_schedule_w`) | Bestands-Mocks mit 1–2 Einträgen bleiben grün; Slots 24–95 `STATIC_BASELINE` (0.25) |
| `typical_off_minute` | `(typical_on_minute + round(avg_duration_min)) % 1440`, sonst `None` | Verbindliche Regel in §3.1 |
| Test B2 | Mock liefert `time_remaining ≈ 90 min` → `n_rem_slots = 6` | Assertion unverändert, nur Eingangsdaten korrekt |
| Historienfenster-Start | Synthetischer `False → True`-Event bei `start_local`, wenn erster Punkt bereits aktiv | Laufende Geräte am Fensteranfang werden erkannt |
| `docs/06` | Alle dortigen Punkte ebenfalls geklärt (siehe `docs/06` §7) | Keine offenen Abhängigkeiten mehr |
