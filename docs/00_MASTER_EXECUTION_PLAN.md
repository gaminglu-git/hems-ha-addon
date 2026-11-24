# 00 – Master Execution Plan & Global Agent Rules

## 1. Projektziel & Systemarchitektur

Entwicklung eines produktionsreifen, wissenschaftlich evaluierbaren Home Assistant Add-ons (in HA ≥ 2026.2 „App" genannt) in Python 3.12 zur netzdienlichen Steuerung eines Home Energy Management Systems (HEMS-Orchestrator).

**Architektur-Paradigma („Add-on-First & Stateless EMHASS"):**

- **Isolierter Docker-Container:** Der HEMS-Orchestrator läuft als eigenständiges Home Assistant Add-on (Python asyncio) getrennt vom Home Assistant Core Event-Loop.
- **Zustandslose EMHASS-Kopplung:** Das bestehende EMHASS-Add-on wird ausschließlich als zustandsloser Optimierungsrechner über HTTP-REST (POST `/action/naive-mpc-optim`) angesprochen. Alle Messwerte, Prognosen und Limits werden bei jedem Aufruf explizit via `runtimeparams` übergeben (Single Source of Truth, kein Konfigurations-Drift).
- **Home Assistant als Zustands- und Aktor-Drehscheibe:** Sensordaten und Aktor-Steuerbefehle laufen strikt über MQTT (primär) bzw. Home Assistant Supervisor REST API (sekundär).
- **Revisionssicheres Dual-Logging (unabhängig vom HA-Recorder):** Alle Steuerungsentscheidungen inklusive vollständiger Ableitungskette werden in `/data/logs/decision_trace.jsonl` (append-only) und `/data/db/hems_audit.db` (SQLite im WAL-Modus) gespeichert (§ 14a EnWG 2-Jahres-Nachweispflicht).

### Regulatorischer Rahmen (verbindliche Auslegung)

- **§ 14a EnWG i.V.m. BNetzA-Festlegung BK6-22-300:** Die netzorientierte Steuerung begrenzt den *netzwirksamen Leistungsbezug* der steuerbaren Verbrauchseinrichtungen (SteuVE: Wallbox, Wärmepumpe, Stromspeicher in Laderichtung). Die Hausgrundlast ist nicht betroffen. Batterie**entladung** wird durch § 14a nicht begrenzt. Bei EMS-Steuerung (Ziffer 4.4.b) erhält das HEMS einen Gesamtsollwert und darf diesen nach eigener Maßgabe auf die SteuVE verteilen (Ziffer 4.5.2 Satz 6) – das ist die Rechtsgrundlage für die Kostenoptimierung innerhalb des § 14a-Envelopes (EMHASS optimiert, die Kaskade begrenzt).
- **SteuVE-Einstufung vs. Dimmungsbilanz (Fix K3):** Ein Batteriespeicher mit > 4,2 kW netzbezogener Ladeleistung zählt regulatorisch als steuerbare Verbrauchseinrichtung (SteuVE) – auch bei softwareseitiger Begrenzung auf PV-Überschussladung (keine Ausnahme für „nur PV-Laden", BK6-22-300). In der Dimmungsbilanz eines EMS (Saldierungsprivileg nach BNetzA BK6-22-300) zählt für die Einhaltung des § 14a-Limits jedoch ausschließlich der resultierende Netzbezug der SteuVE (`P_steuve,grid`); direkter PV-Überschuss darf saldiert werden (vgl. Modellannahme C5 in docs/03).
- **Risikohinweis (Fix K3):** Bei Messkonzepten ohne EMS-Saldierung oder abweichender VNB-Direktsteuerung einzelner Aktoren kann real eine Brutto-Begrenzung pro Gerät greifen; das MVP modelliert explizit die EMS-Netzdienlichkeitssteuerung.
- **§ 9 EEG (Solarspitzengesetz, seit 25.02.2025):** Die 60-%-Einspeisebegrenzung gilt am Netzverknüpfungspunkt für Anlagen mit Einspeisevergütung/Mieterstrom ohne iMSys + Steuerbox; Eigenverbrauch und Speicherladung zählen nicht zur Grenze. Sie begrenzt die Einspeiserichtung, § 14a die Bezugsrichtung – **beide Grenzen gelten simultan**, es gibt keinen rechtlichen Prioritätskonflikt.
- **PV-Abregelung:** Verbleibender PV-Überschuss, der weder selbst verbraucht noch innerhalb der Limits gespeichert werden kann, wird durch den Wechselrichter abgeregelt. PV-Curtailment ist kein Aktor des HEMS-Orchestrators (bewusste Scope-Grenze).
- **Modell-Vereinfachungen (dokumentiert):** Das § 14a-Signal wird als absoluter Wattwert (`limit_w`) modelliert; prozentuale Dimmungssignale und der Gleichzeitigkeitsfaktor der EMS-Mindestleistung (Ziffer 4.5.2) sind nicht abgebildet. Die fixe Untergrenze von 4200 W entspricht der Direktansteuerungs-Mindestleistung (Ziffer 4.5.1) und ist konservativ.

## 2. Verbindliche Regeln für den Coding-Agent (Zero-Interpretation Rules)

Der Agent MUSS folgende Regeln in jeder Phase strikt einhalten:

1. **Kein Blockieren des Event-Loops:**
   Die 1-Hz-Prioritätskaskade darf niemals durch I/O, HTTP-Aufrufe an EMHASS, SQLite-Writes oder verzögerte Wirkungsprüfungen (`check_after_s`) blockiert werden. EMHASS-Aufrufe und Wirkungsprüfungen laufen als separate asyncio.Tasks mit expliziten Timeouts.

2. **Strikte Einheiten-Konvention (SI-nah, keine impliziten Umrechnungen):**
   - Elektrische Leistung: Watt (W) als float (Netzbezug > 0, Netzeinspeisung < 0; Batterieladung > 0, Batterieentladung < 0; PV-Erzeugung ≥ 0; Verbraucherlasten ≥ 0).
   - Elektrische Energie: Wattstunden (Wh) als float.
   - Ladezustand (SoC): Prozent (0.0 bis 100.0) als float.
   - Temperatur: Grad Celsius (°C) als float.
   - Strompreis: Euro-Cent pro Kilowattstunde (ct/kWh) als float.
   - CO₂-Intensität: Gramm CO₂ pro Kilowattstunde (g/kWh) als float.
   - Zeitstempel: Immer UTC im ISO-8601-Format mit Millisekunden (`YYYY-MM-DDTHH:MM:SS.sssZ`).

3. **Typisierung & Validierung:**
   - Python ≥ 3.12, strikte Type Hints (`from __future__ import annotations`).
   - Alle externen Payloads, Konfigurationen und internen Zustände werden über Pydantic v2 (`BaseModel`, `ConfigDict(extra="forbid")`) validiert.

4. **Dateisystem-Trennung (HA-Backup-Schutz):**
   - Persistente Daten (Audit-DB, rotierte JSONL-Logs): ausschließlich unter `/data/...`.
   - Flüchtige Daten, Caches, temporäre Dateien: ausschließlich unter `/tmp/...`.

5. **Fail-Safe Default (`dry_run = True`):**
   Im Standardzustand (`dry_run: true`) werden niemals reale Aktor-Befehle auf Hardware-MQTT-Topics publiziert. Stattdessen wird ausschließlich auf Shadow-Topics (`hems/shadow/cmd/...`) publiziert und vollständig geloggt.

6. **Kein Silent Swallowing von Fehlern:**
   Jede Exception in Kommunikations-, Solver- oder Parser-Modulen muss strukturiert geloggt werden und im Regelkern deterministisch in den definierten Fallback (Prio 4) überführen.

## 3. Verbindliche Verzeichnisstruktur

Der Agent erstellt exakt diese Projektstruktur und weicht ohne explizite Anweisung nicht davon ab:

```
hems_orchestrator/
├── config.yaml                      # Home Assistant Add-on Manifest
├── build.yaml                       # Multi-Arch Docker Build Config
├── Dockerfile                       # Python 3.12-slim Container Definition
├── run.sh                           # Container Entrypoint (chmod +x)
├── pyproject.toml                   # Abhängigkeiten, pytest, ruff Config
├── README.md                        # Kurzdoku für HA Add-on Store
├── src/
│   └── hems/
│       ├── __init__.py
│       ├── main.py                  # Asyncio Orchestrator Entrypoint & Lifecycle
│       ├── config.py                # Pydantic Settings Loader (/data/options.json + ENV)
│       ├── models.py                # Alle kanonischen Pydantic v2 Datenmodelle
│       ├── logging/
│       │   ├── __init__.py
│       │   ├── jsonl_logger.py      # Append-only Decision-Trace JSONL mit Rotation
│       │   ├── sqlite_store.py      # Async SQLite Audit-DB (WAL-Modus, Schema)
│       │   └── verification.py      # Asynchroner §14a Wirkungsnachweis-Tracker
│       ├── io/
│       │   ├── __init__.py
│       │   ├── mqtt_client.py       # Async MQTT Ingestion & Actuator Publisher
│       │   └── emhass_client.py     # Stateless EMHASS REST API Client + Hashing
│       ├── control/
│       │   ├── __init__.py
│       │   ├── modes.py             # Betriebsmodi A (Baseline), B (Markt), C (Netz)
│       │   ├── cascade.py           # Deterministische 4-Stufen-Prioritätskaskade (1 Hz)
│       │   └── scheduler.py         # 15-min MPC Loop & 1-Hz Real-Time Loop
│       └── hil/
│           ├── __init__.py
│           ├── replay_driver.py     # Quasi-HIL CSV-Replay (VDI 4655 / BDEW) & Faults
│           └── kpi_calculator.py    # Automatisierte KPI-Auswertung aus SQLite
└── tests/
    ├── conftest.py
    ├── test_config_and_models.py
    ├── test_audit_logging.py
    ├── test_emhass_client.py
    ├── test_priority_cascade.py
    └── test_quasi_hil_e2e.py
```

## 4. Verbindliche Ausführungsreihenfolge (Phasen-Gate-Prinzip)

Der Agent arbeitet die Pläne strikt sequenziell ab. Eine Phase gilt erst als abgeschlossen, wenn alle zugehörige Tests (`pytest`) sowie `ruff check` ohne Fehler durchlaufen.

| Phase | Spezifikationsdatei | Fokus | Gate-Bedingung |
|-------|---------------------|-------|----------------|
| Phase 1 | `docs/01_ADDON_SCAFFOLD_AND_CONFIG.md` | Add-on Manifest, Docker, pyproject.toml, config.py | `pytest tests/test_config_and_models.py` grün |
| Phase 2 | `docs/02_DATA_MODELS_AND_AUDIT_LOGGING.md` | Pydantic Models, JSONL-Logger, SQLite Audit-DB, Verification-Tracker | `pytest tests/test_audit_logging.py` grün |
| Phase 3 | `docs/03_CONTROL_CORE_AND_EMHASS.md` | MQTT I/O, Stateless EMHASS Client, Modi A/B/C, 4-Stufen-Kaskade | `pytest tests/test_emhass_client.py tests/test_priority_cascade.py` grün |
| Phase 4 | `docs/04_QUASI_HIL_AND_VERIFICATION.md` | Quasi-HIL Replay, Fault Injection, KPI-Rechner, E2E-Tests | Komplette Testsuite `pytest -v` 100 % grün |
| Phase 5 | `docs/06_HOME_ASSISTANT_API_AND_DEVICE_REGISTRY.md` | HA Core API (Supervisor Proxy), Universal Consumer & Device Registry, WashData Auto-Discovery, Ingress-UI | `ruff check src tests` clean + `pytest -v` grün (87 Bestandstests + neue Registry-/Client-Tests) |
| Phase 6 | `docs/07_UNIVERSAL_CONSUMER_FORECAST.md` | WashData-Auswertung, zirkuläre Mustererkennung, historische Lastprofile, 96-Slot-Einzelverbraucher-Prognose, EMHASS-Rückkopplung | `ruff check src tests` clean + `pytest -v` grün (87 + Phase-5 + neue Prognose-Tests) |
| Phase 7 | `docs/08_INGRESS_WEB_UI_AND_REST_API.md` | HA-Ingress-Web-UI (4 Tabs), FastAPI-REST-API, Live-Regelung, Muster-/Prognoseanzeige, Geräteverwaltung | `ruff check src tests` clean + `pytest -v` grün (87 + Phase-5/-6 + neue Web-Tests), 0 Warnings |
| Phase 8 | `docs/09_EXTERNAL_MARKET_AND_GRID_APIS.md` | Externe Strompreis-/EE-Prognosen (Energy-Charts, aWATTar, HA-Sensor), Brutto-Preis-/CO₂-Vektoren, gecachter MarketDataClient, EMHASS-Kopplung | `ruff check src tests` clean + `pytest -v` grün (127 Bestandstests + neue Markt-Tests), 0 Warnings |

## 5. Abhängigkeiten (pyproject.toml)

Verbindliche Core-Pakete (keine schweren Frameworks wie Django, Celery oder Pandas im Runtime-Container):

- `pydantic>=2.9,<3.0`
- `pydantic-settings>=2.5,<3.0`
- `httpx>=0.27,<1.0` (Async HTTP für EMHASS & Supervisor API)
- `aiomqtt>=2.3,<3.0` (Async MQTT Client)
- `aiosqlite>=0.20,<1.0` (Async SQLite für Audit-DB)
- `fastapi>=0.115,<1.0`, `uvicorn[standard]>=0.32,<1.0` (Phase 5: lokaler Ingress-UI-Server auf Port 8099; kein Ersatz für den asyncio-Regelkern)

Dev/Test:

- `pytest>=8.3`
- `pytest-asyncio>=0.24`
- `respx>=0.21` (HTTP Mocking)
- `ruff>=0.7`