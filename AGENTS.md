# AGENTS.md – HEMS Grid-Supportive Orchestrator

Dieses Dokument ist die verbindliche Betriebsanleitung für jeden Coding-Agenten (Cursor, Antigravity, OpenCode), der in diesem Repository arbeitet.

## 1. Projekt-Kontext & Single Source of Truth

**Ziel:** Entwicklung eines Home Assistant Add-ons (Python 3.12, asyncio) zur netzdienlichen HEMS-Steuerung mit zustandsloser EMHASS-Kopplung, deterministischer § 14a EnWG / § 9 EEG Prioritätskaskade und revisionssicherem Dual-Logging (JSONL + SQLite).

**Verbindliche Spezifikationen:**

- `docs/00_MASTER_EXECUTION_PLAN.md` (Architektur, Dateibaum, globale Regeln)
- `docs/01_ADDON_SCAFFOLD_AND_CONFIG.md` (Phase 1: Manifest, Docker, Config)
- `docs/02_DATA_MODELS_AND_AUDIT_LOGGING.md` (Phase 2: Pydantic Models, JSONL, SQLite, § 14a-Verifikation)
- `docs/03_CONTROL_CORE_AND_EMHASS.md` (Phase 3: MQTT, EMHASS Client, Modi A/B/C, 4-Stufen-Kaskade)
- `docs/04_QUASI_HIL_AND_VERIFICATION.md` (Phase 4: Quasi-HIL Replay, KPI-Rechner, E2E-Tests)

**Regel:** Bei jeder Unklarheit gilt exakt der Wortlaut der jeweiligen Phasendatei. Triff keine stillschweigenden Architekturentscheidungen, die dort nicht spezifiziert sind.

## 2. Verbindlicher Arbeitsablauf (Phase-Gate-Loop)

Arbeite exakt eine Phase nach der anderen ab. Beginne niemals mit Phase $N+1$, bevor das Gate von Phase $N$ vollständig bestanden ist:

1. **Lesen:** Lies `docs/00_MASTER_EXECUTION_PLAN.md` und die aktuelle Phasendatei vollständig, bevor du eine einzige Zeile Code schreibst.
2. **Implementieren:** Erstelle ausschließlich die in der aktuellen Phase geforderten Dateien am exakt vorgegebenen Pfad.
3. **Testen & Linting:** Führe am Ende jeder Phase zwingend beide Kommandos aus:
   ```bash
   ruff check src tests
   pytest -v
   ```

   Die Gate-Kommandos sind im Add-on-Unterordner `hems_orchestrator/` auszuführen (dort liegen `pyproject.toml`, `src/` und `tests/`). Im GitHub-Repository liegt das Add-on ebenfalls in diesem Unterordner (neben `repository.yaml` für den HA Add-on Store).
4. **Abschlussbericht:** Melde dem Nutzer nach bestandenem Gate kurz:
   - Welche Dateien erstellt/geändert wurden.
   - Das exakte Testergebnis (passed, 0 warnings, ruff clean).

   Warte auf Freigabe für die nächste Phase (sofern nicht explizit Multi-Phase beauftragt wurde).

## 3. Unverhandelbare Technische Leitplanken (Hard Constraints)

### 3.1 Python & Architektur

- Python ≥ 3.12, strikte Type Hints (`from __future__ import annotations`).
- Alle externen Payloads, Konfigurationen und Trace-Objekte nutzen Pydantic v2 mit `ConfigDict(extra="forbid")`.
- **Kein Event-Loop-Blocking:** Alle HTTP-Calls (`httpx.AsyncClient`), MQTT-Calls (`aiomqtt`), SQLite-Zugriffe (`aiosqlite`) und File-Rotationen (`asyncio.to_thread`) müssen nicht-blockierend implementiert sein.
- **Keine unerwünschten Abhängigkeiten:** Installiere keine schweren Pakete (kein pandas, numpy, scipy, django, celery). Nutze ausschließlich die in `docs/00_MASTER_EXECUTION_PLAN.md` definierten Pakete und die Python-Standardbibliothek.

### 3.2 Einheiten- und Vorzeichenkonvention

- **Leistung:** immer Watt (W) als float.
- **Netz (`p_grid_*`):** Bezug > 0, Einspeisung < 0.
- **Batterie (`*_battery_w`):** Ladung > 0, Entladung < 0.
- **EMHASS-Vorzeichen (`P_batt`):** In EMHASS bedeutet `P_batt > 0` Entladung und `P_batt < 0` Ladung. Beim Parsen gilt zwingend die Vorzeicheninvertierung `battery_w = -p_batt_emhass`.
- **PV & Verbraucher:** immer ≥ 0.
- **Energie:** Wattstunden (Wh).
- **SoC:** 0.0 bis 100.0 (Prozent).
- **Zeitstempel:** immer UTC im ISO-8601-Format (`YYYY-MM-DDTHH:MM:SS.sssZ`).

### 3.3 Sicherheit, Regulatorik & Fail-Safe

- **Shadow-Mode Default:** `dry_run` ist standardmäßig `True`. Bei `dry_run=True` darf niemals auf reale Aktor-Topics (`{prefix}/cmd/...`) publiziert werden (nur `{prefix}/shadow/cmd/...`).
- **Prioritätskaskade:** Prio 1 (§ 14a EnWG, § 9 EEG, Hausanschlussschutz) überschreibt immer Prio 2 (Komfort), Prio 3 (EMHASS) und Prio 4 (Fallback).
- **Wallbox-Mindestleistung (global, P2-Aufrundung + Post-Clamp):** Der P2-Komfort-Guard rundet `0.0 < p_required_w < wallbox_min_power_w` auf `wallbox_min_power_w` (Standard 1380.0 W) auf, bevor `min(..., wallbox_max_power_w)` greift. Ein finaler Sollwert im Intervall $(0, 1380)$ W wird über den globalen Post-Processing-Clamp deterministisch auf 0.0 W gesetzt (`"POST_wallbox_below_min_clamped_to_0"`). Kein Pfad (P1, P2, P3, P4) darf einen ungültigen Zwischenwert ausgeben.
- **§ 14a EMS-Saldierungsprivileg + Re-Clamp nach § 9 EEG:** Die § 14a-Prüfung zählt ausschließlich den Netzbezug der steuVE: `P_steuve,grid = max(0.0, wallbox_w + heatpump_w + max(0.0, battery_w) - max(0.0, p_pv_w - p_house_base_w))`. Erhöht § 9 EEG danach die Batterieladung, ist zwingend ein § 14a Re-Check/Re-Clamp vor dem Hausanschlussschutz durchzuführen, der die EEG-Ladung exakt so weit zurücknimmt, dass `P_steuve,grid <= limit_14a` bleibt (`"P1_14a_reclamp_after_eeg"`).
- **SG-Ready-Quantisierung vor P1:** `map_heatpump_power_to_sg_ready(...)` läuft immer nach P2 und vor P1; alle P1-Bilanzen rechnen mit dem quantisierten `heatpump_w` (`0.0`, `0.5·nominal`, `1.0·nominal`). P1-Abwurf setzt `BLOCKED`/`0.0` direkt; keine erneute Quantisierung nach P1.
- **Einheitliche Zeitquelle (Clock-Protocol):** `VerificationTracker` nutzt ausschließlich das `Clock`-Protocol (`RealClock` in Produktion, `SimulatedClock` im Replay) und wird nur für tatsächlich geloggte Entscheidungen geplant.
- **Dateisystem (HA-Backup-Schutz):** Persistente Daten ausschließlich unter `/data/...`, temporäre Dateien/Caches ausschließlich unter `/tmp/...`.
- **Append-Only JSONL:** `decision_trace.jsonl` wird niemals nachträglich editiert; verzögerte § 14a-Wirkungsnachweise werden als separater `VerificationTraceRecord` angehängt und in SQLite per UPDATE verknüpft.
- **Log-Deduplizierung & Heartbeat (`audit_heartbeat_s = 60`):** Im 1-Hz-Loop wird ein `DecisionTraceRecord` nur geschrieben bei neuem MPC-Lauf (`MPC_CYCLE`), Zustands-/Sollwertänderung gegenüber dem letzten geloggten Zyklus (`winning_priority`-/`trigger`-Wechsel, `abs(cmd_wallbox_w - prev) > 10.0`, `abs(cmd_battery_w - prev) > 10.0`, `cmd_heatpump_sg_ready`-Wechsel) oder abgelaufenem Heartbeat (≥ `config.audit_heartbeat_s` Sekunden, Standard 60 s). Verhindert 1-Hz-Log-Spam bei dauerhaft aktivem P1/P2/P4-Zustand.

## 4. Verbotene Aktionen (Do NOT)

- **NICHT** die Verzeichnisstruktur oder Dateinamen aus `docs/00_MASTER_EXECUTION_PLAN.md` eigenmächtig umbenennen.
- **NICHT** Tests abschwächen, überspringen (`pytest.mark.skip`) oder Assertions entfernen, um ein Gate grün zu bekommen.
- **NICHT** Exceptions stumm verschlucken (`except Exception: pass`); jeder Fehler im Solver oder I/O muss geloggt werden und im Regelkern deterministisch zu Prio 4 (Fallback) führen.
- **NICHT** echte externe Netzwerkanfragen in Unit-/Integrationstests ausführen (HTTP-Calls immer über `respx` mocken, Filesystem immer über `tmp_path`).