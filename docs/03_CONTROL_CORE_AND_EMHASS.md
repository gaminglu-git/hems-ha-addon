# 03 – Phase 3: Ingestion Layer, Stateless EMHASS Client & 4-Stufen-Prioritätskaskade

## Ziel dieser Phase

Implementierung der kompletten Regelungslogik: MQTT-Datenanbindung, zustandsloser EMHASS-REST-Client mit Parameter-Hashing, die drei umschaltbaren Betriebsmodi (A/B/C) inklusive Shadow-Mode (`dry_run`) sowie die deterministische 1-Hz-Prioritätskaskade.

## 1. MQTT-Schnittstelle (src/hems/io/mqtt_client.py)

**Verbindliche MQTT-Topics (Präfix = `config.mqtt_topic_prefix`, Standard: `hems`)**

### Subscribed Topics (Eingänge)

| Topic | Payload-Typ | Beschreibung |
|-------|-------------|--------------|
| `{prefix}/sensors/state` | JSON (`SystemInputs`) | Vollständiger Live-Zustand (Primär-Ingestion für Real-Betrieb & Quasi-HIL) |
| `{prefix}/control/mode` | String (`A_BASELINE \| B_MARKET_ORIENTED \| C_GRID_SUPPORTIVE`) | Laufzeit-Umschaltung des Betriebsmodus |
| `{prefix}/control/dry_run` | String (`true \| false`) | Laufzeit-Umschaltung des Shadow-Modes |
| `{prefix}/dso/14a_signal` | JSON (`{"active": bool, "limit_w": float}`) | Asynchrones Steuerbox-Signal (§ 14a EnWG), überschreibt sofort den Zustand |

### Published Topics (Ausgänge)

**Wenn `dry_run == False` (Realbetrieb):**

- `{prefix}/cmd/wallbox_w` -> Float-String (z. B. `"4200.0"`)
- `{prefix}/cmd/battery_w` -> Float-String (z. B. `"-400.0"`)
- `{prefix}/cmd/heatpump_sg_ready` -> Enum-String (`"blocked"`, `"normal"`, `"recommended"`, `"forced"`)

**Wenn `dry_run == True` (Shadow-Mode):**

- NIEMALS auf `{prefix}/cmd/...` publizieren!
- Stattdessen publizieren auf `{prefix}/shadow/cmd/wallbox_w`, `{prefix}/shadow/cmd/battery_w`, `{prefix}/shadow/cmd/heatpump_sg_ready`.

**Immer (Diagnose / HA-Transparenz):**

- `{prefix}/status/last_decision` -> Kompaktes JSON des letzten `DecisionTraceRecord`.

### MQTT-Credential-Resolution (Realbetrieb)

Sind `config.mqtt_username`/`config.mqtt_password` leer und läuft der Prozess im Supervisor (Umgebungsvariable `SUPERVISOR_TOKEN` gesetzt), löst der Client die Zugangsdaten einmalig beim Start über `GET http://supervisor/services/mqtt` (Header `Authorization: Bearer {SUPERVISOR_TOKEN}`) auf und verwendet Host, Port, Benutzername und Passwort aus der Antwort für die Verbindung. Schlägt die Auflösung fehl (Timeout, HTTP-Fehler, ungültiges JSON), wird der Fehler strukturiert geloggt und mit den konfigurierten Werten verbunden. In Tests wird die Supervisor-API ausschließlich via `respx` gemockt.

## 2. Stateless EMHASS HTTP-Client (src/hems/io/emhass_client.py)

**Anforderungen**

- Endpunkt: `POST {config.emhass_url}/action/naive-mpc-optim`
- **Kein Konfigurations-Drift (`runtimeparams`):** Bei jedem Aufruf wird ein vollständiges `runtimeparams`-Dict erzeugt, das mindestens enthält:
  - `prediction_horizon: 24` (6 Stunden bei 15-min-Schritten)
  - `soc_init: inputs.soc_battery_pct / 100.0`
  - `soc_final: 0.20`
  - `load_cost_forecast`: Liste von 24 Preisen in EUR/kWh (`inputs.tariff_ct_kwh / 100.0` oder Prognosevektor)
  - `prod_price_forecast`: Einspeisevergütung in EUR/kWh (0.08 bzw. 0.0 bei negativen Preisen)
  - `pv_power_forecast`: Liste von 24 PV-Leistungswerten in W
  - `load_power_forecast`: Liste von 24 Grundlastwerten in W
  - `def_total_hours`: Liste der benötigten Laufstunden für steuerbare Lasten (Wallbox, WP)
  - `number_of_deferrable_loads: 2` und `nominal_power_of_deferrable_loads: [config.wallbox_max_power_w, config.heatpump_nominal_power_w]` (deferrable Last 0 = Wallbox, deferrable Last 1 = Wärmepumpe; vollständige Übergabe verhindert Konfigurations-Drift)
  - `alpha: 0.5`, `beta: 0.5` (Mischgewichtung Prognose vs. Ist-Wert)
  - `weight_battery_discharge: config.battery_cycle_cost_ct_kwh / 100.0`
- **Modus-abhängige Parameter:**
  - `B_MARKET_ORIENTED`: Maximale Netzbezugsleistung = `config.house_fuse_limit_w`, Optimierungsziel = Kostenminimierung (`costfun: "profit"`).
  - `C_GRID_SUPPORTIVE`: Harte Netzdienlichkeits-Restriktion (`maximum_power_from_grid: min(config.house_fuse_limit_w, 8000.0)` bzw. `dso_14a_limit_w` wenn aktiv; Einspeiselimit `maximum_power_to_grid: config.pv_peak_w * config.eeg_feedin_limit_pct / 100.0`; zusätzliche Gewichtung von CO₂-Intensität über effektiven Kostenterm).
- **Kanonisches Parameter-Hashing:**
  ```python
  canonical_json = json.dumps(runtimeparams, sort_keys=True, separators=(",", ":"))
  runtimeparams_hash = hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()[:8]
  ```
- **Timeout & Fehlerbehandlung:**
  - Nutze `httpx.AsyncClient(timeout=config.emhass_timeout_s)`.
  - Messe `emhass_solve_ms` mittels `time.perf_counter_ns()`.
  - Bei `httpx.TimeoutException`: Rückgabe `EmhassResult(status="timeout", solve_ms=..., setpoints=None, runtimeparams_hash=...)`.
  - Bei HTTP-Status ≠ 200 oder ungültigem JSON: Rückgabe `EmhassResult(status="error", ...)`.
  - Bei Erfolg (HTTP 200, valides JSON): Extrahiere den ersten Zeitschritt ($t = 0$, Index 0) mit exakt diesem verbindlichen Feld-Mapping (Fix B2):
    - `P_batt` → `battery_w`: In EMHASS bedeutet `P_batt > 0` **Entladung** und `P_batt < 0` **Ladung**. Beim Parsen muss das Vorzeichen invertiert werden: `battery_w = -float(response["P_batt"][0])` (entspricht `battery_w = -float(row["P_batt"])` für die erste Zeile).
    - `P_deferrable0` → `wallbox_w`: Index 0 der steuerbaren Lasten ist verbindlich die Wallbox: `wallbox_w = max(0.0, float(response["P_deferrable0"][0]))`.
    - `P_deferrable1` → `heatpump_w`: Index 1 der steuerbaren Lasten ist verbindlich die Wärmepumpe: `heatpump_w = max(0.0, float(response["P_deferrable1"][0]))`.
    - Fehlende Pflichtspalten (`P_batt`, `P_deferrable0`, `P_deferrable1`), ein Feld, das keine Liste ist, eine leere Liste oder eine leere Antwort liefern deterministisch `EmhassResult(status="error", ...)`.

## 3. Betriebsmodi (src/hems/control/modes.py)

Implementiere die Funktion `compute_baseline_setpoints(inputs: SystemInputs, config: AppConfig) -> ProposedSetpoints` für Modus A (A_BASELINE) und als Prio-4-Fallback:

- PV-Überschuss berechnen: `p_surplus_w = inputs.p_pv_w - inputs.p_house_base_w`.
- **Batterie (klassischer Eigenverbrauch):**
  - Wenn `p_surplus_w > 0` und `inputs.soc_battery_pct < 99.0`: `battery_w = min(p_surplus_w, config.battery_max_charge_w)`.
  - Wenn `p_surplus_w < 0` und `inputs.soc_battery_pct > 10.0`: `battery_w = -min(abs(p_surplus_w), config.battery_max_discharge_w)`.
  - Sonst: `battery_w = 0.0`.
- **Wallbox (ungesteuert in Modus A):**
  - Wenn `inputs.ev_connected` und `inputs.soc_ev_pct < config.ev_target_soc_pct`: `wallbox_w = config.wallbox_max_power_w` (sofortiges Laden mit voller Leistung).
  - Sonst: `wallbox_w = 0.0`.
- **Wärmepumpe:**
  - Wenn `inputs.t_buffer_c < config.heatpump_temp_min_c`: `heatpump_w = config.heatpump_nominal_power_w`.
  - Sonst: `heatpump_w = 0.0`.

### SG-Ready-Mapping & erwartete Leistung (verbindlich, Fix B1)

Reine, seiteneffektfreie Hilfsfunktion (in `modes.py` definiert). **Verbindlicher Aufrufzeitpunkt (Fix K1): direkt nach Schritt 1 (Prio 2) und vor Schritt 2 (Prio 1)** – siehe Schritt 1.5 der Kaskade:

```python
map_heatpump_power_to_sg_ready(
    heatpump_w: float,
    config: AppConfig,
    force_blocked: bool = False,
    force_boost: bool = False,
) -> tuple[HeatpumpSGReadyMode, float]
```

Rückgabe: `(cmd_heatpump_sg_ready, cmd_heatpump_w_est)`. Verbindliche Mapping-Tabelle (in dieser Auswertungsreihenfolge):

| Bedingung | `cmd_heatpump_sg_ready` | `cmd_heatpump_w_est` (Watt) | Semantischer Hinweis |
|-----------|-------------------------|-----------------------------|----------------------|
| `force_blocked == True` | `BLOCKED` (Zustand 1) | `0.0` | EVU-Sperre / harter P1-Lastabwurf |
| `force_boost == True` | `FORCED` (Zustand 4) | `config.heatpump_nominal_power_w` | Harter Komfort-Eingriff (P2) |
| `heatpump_w <= 0.0` | `NORMAL` (Zustand 2) | `0.0` | Regulärer Betrieb ohne Anhebung (kein Missbrauch von Zustand 1!) |
| `0.0 < heatpump_w < 0.8 * config.heatpump_nominal_power_w` | `RECOMMENDED` (Zustand 3) | `0.5 * config.heatpump_nominal_power_w` | Verstärkter Betrieb / PV-Anhebung |
| `heatpump_w >= 0.8 * config.heatpump_nominal_power_w` | `FORCED` (Zustand 4) | `config.heatpump_nominal_power_w` | Maximale Leistungsanforderung |

`force_boost` wird beim Vorab-Quantisierungsschritt 1.5 aus dem P2-Temperaturband (`t_buffer_c < heatpump_temp_min_c`) abgeleitet. Der Parameter `force_blocked` bleibt für API-Vollständigkeit reserviert; im Kaskadenablauf setzt Prio 1 `BLOCKED` direkt ohne erneuten Mapping-Aufruf (Fix K1). SG-Ready Zustand 1 (`BLOCKED`) ist semantisch der EVU-/P1-Sperre vorbehalten und darf nicht für Komfortabschaltungen missbraucht werden (siehe Korrektur im P2-Temperaturband, Schritt 1).

## 4. Deterministische 4-Stufen-Prioritätskaskade (src/hems/control/cascade.py)

Dies ist das sicherheitskritische Kernmodul. Implementiere eine reine, seiteneffektfreie Funktion:

```python
evaluate_cascade(
    inputs: SystemInputs,
    config: AppConfig,
    emhass_result: EmhassResult | None,
    setpoint_age_s: float | None,
) -> tuple[ControlOutputs, DecisionDerivation, TriggerReason]
```

**Exakte Auswertungsreihenfolge (keine Abweichung erlaubt!)**

### Schritt 0: Initiale Sollwert-Bestimmung (Prio 3 vs. Prio 4)

- **Wenn `config.operating_mode == OperatingMode.A_BASELINE`:**
  - Basis-Sollwerte = `compute_baseline_setpoints(inputs, config)`
  - `winning_priority = 3` (in Modus A ist Baseline der reguläre Plan), `emhass_status = "disabled"`, `trigger = TriggerReason.PERIODIC_1HZ`.
- **Sonst (Modus B oder C):**
  - Prüfe, ob `emhass_result` vorhanden, `emhass_result.status == "ok"`, `emhass_result.setpoints is not None` UND `setpoint_age_s <= config.mpc_interval_s + 60.0`.
  - **Wenn JA:** Basis-Sollwerte = `emhass_result.setpoints`, `winning_priority = 3`, `trigger = TriggerReason.PERIODIC_1HZ`.
  - **Wenn NEIN (Fallback — Prio 4):** Basis-Sollwerte = `compute_baseline_setpoints(inputs, config)` (jedoch im sicheren Drosselmodus: `wallbox_w = min(wallbox_w, config.dso_14a_min_power_w)`). `winning_priority = 4`, `trigger = TriggerReason.SOLVER_TIMEOUT_FALLBACK`. Füge `"P4_fallback_active"` zu `cascade_evaluated_rules` hinzu.

### Schritt 1: Prio 2 — Harte Nutzer-Komfortgrenzen (überschreibt Prio 3/4, wird aber von Prio 1 begrenzt!)

- **Wärmepumpen-Temperaturband:**
  - Wenn `inputs.t_buffer_c < config.heatpump_temp_min_c`: Setze `heatpump_w = config.heatpump_nominal_power_w`, `sg_ready = FORCED`. Markiere Regel `"P2_hp_temp_below_min"`, setze `winning_priority = min(winning_priority, 2)` (sofern Änderung erfolgte).
  - Wenn `inputs.t_buffer_c > config.heatpump_temp_max_c`: Setze `heatpump_w = 0.0`, `sg_ready = NORMAL`. Markiere Regel `"P2_hp_temp_above_max"`. **Korrektur (Fix B1):** nicht `BLOCKED` – SG-Ready Zustand 1 ist semantisch der EVU-/P1-Sperre vorbehalten.
- **EV-Ziel-SoC-Guard:**
  - Wenn `inputs.ev_connected` und `inputs.ev_departure_in_min is not None` und `inputs.ev_departure_in_min > 0` und `inputs.soc_ev_pct < config.ev_target_soc_pct`:
    - Berechne fehlende Energie: `e_missing_wh = ((config.ev_target_soc_pct - inputs.soc_ev_pct) / 100.0) * config.ev_capacity_wh`.
    - Berechne Mindestladeleistung: `p_required_w = e_missing_wh / (inputs.ev_departure_in_min / 60.0)`.
    - **Mindestleistungs-Aufrundung (Fix A2):** Wenn `0.0 < p_required_w < config.wallbox_min_power_w`, runde auf `config.wallbox_min_power_w` (1380 W) auf, **bevor** `min(..., config.wallbox_max_power_w)` angewendet wird (um den Komfort-Ziel-SoC nicht zu unterschreiten).
    - Wenn `p_required_w > wallbox_w`: Setze `wallbox_w = min(p_required_w, config.wallbox_max_power_w)`. Markiere Regel `"P2_ev_target_soc_guard"`, setze `winning_priority = 2`, `trigger = TriggerReason.COMFORT_GUARD_OVERRIDE`.

### Schritt 1.5: SG-Ready-Vorab-Quantisierung (verbindlich, Fix K1 – sicherheitskritisch)

Direkt nach Schritt 1 (Prio 2) und vor Schritt 2 (Prio 1):

- `sg_ready, heatpump_w = map_heatpump_power_to_sg_ready(heatpump_w, config, force_blocked=False, force_boost=(inputs.t_buffer_c < config.heatpump_temp_min_c))`.
- Ab diesem Punkt rechnen **alle P1-Bilanzen** (§ 14a EnWG, § 9 EEG, Hausanschlussschutz) ausschließlich mit dem bereits quantisierten `heatpump_w` (`0.0`, `0.5 * config.heatpump_nominal_power_w` oder `1.0 * config.heatpump_nominal_power_w`).
- Wenn P1 die Wärmepumpe abwerfen muss (§ 14a-Abwurfstufe 3 oder Hausanschlussschutz-Stufe 4), setzt P1 direkt `sg_ready = HeatpumpSGReadyMode.BLOCKED` und `heatpump_w = 0.0`.
- Nach P1 erfolgt **keine erneute SG-Ready-Quantisierung** – `cmd_heatpump_sg_ready` und `cmd_heatpump_w_est` entsprechen exakt dem von P1 verifizierten Zustand.

### Schritt 2: Prio 1 — Sicherheit & Regulatorik (Höchste Priorität, überschreibt ALLES!)

**Rechtsrahmen (siehe docs/00, „Regulatorischer Rahmen"):** § 14a EnWG begrenzt den netzwirksamen Bezug der SteuVE (Bezugsrichtung), § 9 EEG die Einspeisung am Netzverknüpfungspunkt (Einspeiserichtung). Beide Grenzen gelten simultan; kein Schritt darf eine bereits durchgesetzte Grenze einer anderen Regel wieder verletzen.

**Dokumentierte Modellannahme (Fix C5, verbindlich):** `dso_14a_limit_w` modelliert die aus § 14a EnWG / BNetzA BK6-22-300 resultierende zulässige Netzwirkleistung aller steuerbaren Verbrauchseinrichtungen (steuVE) nach EMS-Saldierungsprivileg in absoluten Watt ($\ge 4200\text{ W}$). Eventuelle prozentuale Steuerbox-Signale werden im vorgelagerten Ingestion-Layer in absolute Watt umgerechnet.

- **§ 14a EnWG Netzbetreiber-Dimmung:**
  - Wenn `inputs.dso_14a_active` wahr ist:
    - Effektives Limit: `limit_14a = max(inputs.dso_14a_limit_w, config.dso_14a_min_power_w)`.
    - **EMS-Saldierungsprivileg (Fix A1, verbindlich):** Bei der § 14a-Prüfung zählt ausschließlich der Netzbezug der steuVE:
      $$P_{\text{steuVE,grid}} = \max\!\bigl(0.0,\; \text{wallbox\_w} + \text{heatpump\_w} + \max(0.0, \text{battery\_w}) - \max(0.0, P_{\text{pv}} - P_{\text{house\_base}})\bigr)$$
      Implementierung: `pv_surplus_offset_w = max(0.0, inputs.p_pv_w - inputs.p_house_base_w)` und `p_steuve_grid_w = max(0.0, wallbox_w + heatpump_w + max(0.0, battery_w) - pv_surplus_offset_w)`.
    - Wenn `p_steuve_grid_w > limit_14a`: Drossle in fester Abwurf-Reihenfolge bis `p_steuve_grid_w <= limit_14a`:
      1. Zuerst Batterieladung (`battery_w > 0`) bis auf 0.0 reduzieren.
      2. Danach Wallbox (`wallbox_w`) reduzieren (falls unter `config.wallbox_min_power_w` gedrosselt werden muss, auf 0.0 abschalten!).
      3. Zuletzt Wärmepumpe direkt auf `heatpump_w = 0.0` und `sg_ready = HeatpumpSGReadyMode.BLOCKED` setzen (Fix K1: keine erneute Quantisierung).
      - Markiere `"P1_14a_limit_violated_{int(p_steuve_grid_w)}W_gt_{int(limit_14a)}W"`, setze `winning_priority = 1`, `trigger = TriggerReason.DSO_14A_DIMMING_EVENT`, `override_reason = ...`.
    - Sonst: Markiere `"P1_14a_limit_ok"`.
- **§ 9 EEG Einspeisebegrenzung (z. B. 60 %):**
  - `max_feedin_w = config.pv_peak_w * (inputs.eeg_feedin_limit_pct / 100.0)`.
  - Geschätzte Netzleistung nach aktuellen Sollwerten: `p_grid_est_w = inputs.p_house_base_w + wallbox_w + heatpump_w + battery_w - inputs.p_pv_w`.
  - Wenn `p_grid_est_w < -max_feedin_w` (Einspeisung überschreitet Limit):
    - Überschuss über Limit: `excess_feedin_w = abs(p_grid_est_w) - max_feedin_w`.
    - Wenn Batterie ladbar (`inputs.soc_battery_pct < 99.0`): Erhöhe `battery_w` um `min(excess_feedin_w, config.battery_max_charge_w - battery_w)`.
    - **14a-Re-Check / Re-Clamp nach § 9 EEG (verbindlich, Fix A1 + Guard K6):** Wenn § 9 EEG die Batterieladung `battery_w` erhöht, muss direkt danach (vor dem Hausanschlussschutz) ein verbindlicher § 14a Re-Check auf `P_steuve,grid <= limit_14a` erfolgen (EMS-Saldierungsformel von oben). Der Re-Clamp darf **nur ausgeführt werden, wenn `battery_w > 0.0`** – es darf ausschließlich positive Batterieladung zurückgenommen werden, niemals eine bestehende Batterie-Entladung (`battery_w < 0.0`) auf 0.0 geclampt werden:
      ```python
      if inputs.dso_14a_active and battery_w > 0.0:
          p_steuve_grid = max(
              0.0,
              wallbox_w + heatpump_w + battery_w - max(0.0, inputs.p_pv_w - inputs.p_house_base_w),
          )
          if p_steuve_grid > limit_14a:
              excess = p_steuve_grid - limit_14a
              battery_w = max(0.0, battery_w - excess)
              cascade_evaluated_rules.append("P1_14a_reclamp_after_eeg")
      ```
      Da die bereits vor der EEG-Erhöhung gesetzte Ladung § 14a erfüllte, entfernt dieser Clamp ausschließlich den EEG-Zuwachs. Verbleibender PV-Überschuss wird nicht weiter verarbeitet (Abregelung durch den Wechselrichter, bewusste Scope-Grenze).
    - Markiere `"P1_eeg_feedin_limit_active"`, setze `winning_priority = 1`, `trigger = TriggerReason.EEG_FEEDIN_LIMIT_EVENT`.
  - Sonst: Markiere `"P1_eeg_feedin_ok"`.
- **Hausanschlussschutz (Hauptsicherung):**
  - Neu berechnen: `p_grid_est_w = inputs.p_house_base_w + wallbox_w + heatpump_w + battery_w - inputs.p_pv_w`.
  - Wenn `p_grid_est_w > config.house_fuse_limit_w`:
    - Reduziere Lasten um `overload_w = p_grid_est_w - config.house_fuse_limit_w` in der Reihenfolge: (1) Batterieladung auf 0, (2) Batterieentladung erhöhen falls `soc_battery_pct > 10.0`, (3) Wallbox drosseln/abschalten, (4) Wärmepumpe direkt abschalten (`heatpump_w = 0.0`, `sg_ready = BLOCKED`, Fix K1).
    - Markiere `"P1_house_fuse_violated"`, setze `winning_priority = 1`, `trigger = TriggerReason.HOUSE_FUSE_PROTECTION`.
  - Sonst: Markiere `"P1_house_fuse_ok"`.

### Schritt 2.5: Globale Aktor-Normalisierung (nach allen Prioritätsstufen)

Unabhängig von der gewinnenden Priorität gilt für die finalen Sollwerte:

- **Wallbox-Mindestleistung (globaler Post-Processing-Clamp, Fix A2):** Nach Schritt 2, vor Rückgabe von `ControlOutputs`:
  ```python
  if 0.0 < wallbox_w < config.wallbox_min_power_w:
      wallbox_w = 0.0
      cascade_evaluated_rules.append("POST_wallbox_below_min_clamped_to_0")
  ```
  Damit ist garantiert, dass kein Pfad (weder P1, P2, P3 noch P4) jemals einen ungültigen Wert im Intervall $(0, 1380)\text{ W}$ ausgibt.
- **Batterie-Leistungsgrenzen:** `battery_w = clamp(battery_w, -config.battery_max_discharge_w, config.battery_max_charge_w)`.
- **Wärmepumpe:** `heatpump_w = clamp(heatpump_w, 0.0, config.heatpump_nominal_power_w)`.

**Keine erneute SG-Ready-Quantisierung nach P1 (Fix K1):** Die Quantisierung erfolgte bereits in Schritt 1.5. `cmd_heatpump_sg_ready` und `cmd_heatpump_w_est` werden exakt aus dem von P1 verifizierten Zustand übernommen (`cmd_heatpump_w_est = heatpump_w`).

### Schritt 3: Log-Deduplizierung & Heartbeat im 1-Hz-Loop (Fix C1)

Ein `DecisionTraceRecord` wird im 1-Hz-Loop geschrieben, wenn mindestens eine Bedingung zutrifft:

- Neuer 15-min-MPC-Lauf (`MPC_CYCLE`), ODER
- Zustands-/Sollwertänderung gegenüber dem letzten geloggten Zyklus:
  - `winning_priority` hat sich geändert, ODER
  - `trigger` hat sich geändert, ODER
  - `abs(cmd_wallbox_w - prev.cmd_wallbox_w) > 10.0`, ODER
  - `abs(cmd_battery_w - prev.cmd_battery_w) > 10.0`, ODER
  - `cmd_heatpump_sg_ready != prev.cmd_heatpump_sg_ready`, ODER
- Heartbeat abgelaufen: Seit dem letzten Log-Eintrag sind $\ge \text{config.audit\_heartbeat\_s}$ Sekunden (Standard: 60 s) vergangen.

**Ergebnis:** Verhindert 1-Hz-Log-Spam bei dauerhaft aktivem P1/P2/P4-Zustand und schont SD-Karten/HA-Backups, ohne Zustandswechsel zu verlieren.

- **Heartbeat-Trigger (Fix K4):** Wird ein Log-Eintrag ausschließlich durch Ablauf des Heartbeat-Intervalls (`audit_heartbeat_s`) ausgelöst (keine Zustands-, Regel- oder Sollwertänderung), wird der Trigger deterministisch auf `TriggerReason.PERIODIC_1HZ` gesetzt.
- **Verifikations-Kopplung (Fix K4):** `VerificationTracker.schedule_verification(...)` wird nur aufgerufen, wenn der Entscheidungszyklus die Logging-Bedingung erfüllt und der `DecisionTraceRecord` in JSONL/SQLite geschrieben wurde.

## 5. Akzeptanzkriterien & Tests (Phase 3 Gate)

### tests/test_emhass_client.py (mit respx HTTP-Mocking)

- Verifiziert, dass `runtimeparams` vollständig gesendet und `runtimeparams_hash` deterministisch berechnet wird.
- Verifiziert Timeout-Handling (`status == "timeout"`, kein Crash).
- Verifiziert HTTP-500-Handling (`status == "error"`).

### tests/test_priority_cascade.py (Unit-Tests für jede Stufe)

- **Test § 14a EnWG:** Wallbox 11000 W + `dso_14a_active=True` (Limit 4200 W) -> `cmd_wallbox_w == 4200.0`, `winning_priority == 1`, `trigger == DSO_14A_DIMMING_EVENT`.
- **Test Wallbox-Mindeststrom:** Wenn nach Abzug der Wärmepumpe (2500 W) nur 1000 W für die Wallbox bleiben (< `wallbox_min_power_w = 1380` W), muss `cmd_wallbox_w == 0.0` gesetzt werden (kein ungültiger 1000-W-Befehl!).
- **Test Prio 1 schlägt Prio 2:** EV-Komfort-Guard fordert 11000 W, gleichzeitig ist § 14a-Dimmung (4200 W) aktiv -> Ergebnis bleibt strikt ≤ 4200 W (`winning_priority == 1`).
- **Test Prio 4 Fallback:** `emhass_result.status == "timeout"` -> Fallback aktiv, `winning_priority == 4`.
- **Test § 9 EEG:** Einspeisung > 60 % von `pv_peak_w` -> Batterieladung wird erhöht, `winning_priority == 1`.
- **Test 14a-Re-Clamp nach EEG:** § 14a aktiv (Limit 4200 W) UND PV-Einspeisung über Limit -> Batterieladung wird erhöht, aber $P_{\text{steuVE,grid}}$ bleibt strikt ≤ 4200 W; Regel `"P1_14a_reclamp_after_eeg"` ist markiert.
- **Test Wallbox-Minimum im P2-Guard (Fix A2):** EV-Komfort-Guard errechnet `p_required_w = 500 W` (< `wallbox_min_power_w = 1380 W`), § 14a inaktiv -> `cmd_wallbox_w == 1380.0` (P2-Aufrundung auf die Mindestleistung, damit der Komfort-Ziel-SoC nicht unterschritten wird).
- **Test § 14a + § 9 EEG Kollision (Fix A1, Pflicht):** `dso_14a_active=True` (Limit 4200 W) + hohe PV-Einspeisung über dem 60-%-Limit -> Batterie darf PV-Überschuss aufnehmen, aber $P_{\text{steuVE,grid}}$ bleibt strikt $\le 4200\text{ W}$.
- **Test P2 Wallbox-Aufrundung & Post-Clamp (Fix A2, Pflicht):** P2 fordert `p_required_w = 500 W` -> wird auf `1380.0 W` aufgerundet; falls P1 anschließend auf < 1380 W drosselt -> deterministisch `0.0 W` (globaler Post-Processing-Clamp `"POST_wallbox_below_min_clamped_to_0"`).
- **Test SG-Ready-Vorab-Quantisierung (Fix K1, Pflicht):** EMHASS schlägt `heatpump_w = 200.0 W` und `wallbox_w = 3500.0 W` bei aktivem § 14a-Limit (4200 W) ohne PV vor -> Quantisierung erhöht `heatpump_w` vor P1 auf `1250.0 W` (RECOMMENDED), Summe 4750 W > 4200 W -> P1 drosselt Wallbox auf `2950.0 W`, sodass `cmd_wallbox_w + cmd_heatpump_w_est == 4200.0 W` strikt eingehalten wird.

`ruff check src tests` und `pytest tests/test_emhass_client.py tests/test_priority_cascade.py` laufen mit Exit-Code 0.