# 08 – Phase 7: Home Assistant Ingress Web-UI & REST API

## Ziel dieser Phase

Bereitstellung einer modernen, schnellen, selbsterklärenden Weboberfläche direkt in der Home Assistant Seitenleiste (Home Assistant Ingress) über FastAPI (`src/hems/web/`). Nutzer können dort:

- **Einzelverbraucher-Prognosen** (24 h, 15-Minuten-Auflösung) für jeden eingebundenen Verbraucher im Haus (inkl. laufender WashData-Programme, Smart-Plug-Verbraucher, erkannter Lichter und steuerbarer Lasten) sowie die Gesamthaus-Lastprognose visuell einsehen.
- **Alle erkannten Schaltmuster** (Lichter, Schalter, WashData-Routinen) einsehen und das Training manuell auslösen.
- **Alle Verbraucher & Integrationen verwalten**: steuerbare Geräte (`is_controllable=True`) wie überwachte Verbraucher (`is_controllable=False`) hinzufügen, bearbeiten, per Ein-Klick aus WashData importieren, aktivieren/deaktivieren und löschen.
- **Betriebsmodus (A/B/C) und Shadow-Mode (`dry_run`)** steuern sowie die letzte Kaskadenentscheidung live verfolgen.

**Gate-Vorbedingung:** Die Phasen 5 (`docs/06`) und 6 (`docs/07`) sind implementiert und grün. Stand dieser Planung: `src/hems/forecast/` und `src/hems/web/` existieren noch nicht — Phase 7 darf gemäß `docs/00` §4 erst **nach** bestandenem Phase-5- und Phase-6-Gate begonnen werden. Alle 87 Bestandstests sowie die neuen Tests aus Phase 5 und 6 bleiben unverändert grün.

**Pfad-Hinweis:** Alle Add-on-Dateien liegen in `hems_orchestrator/`. Gate-Kommandos werden dort ausgeführt.

---

## 1. Ingress-Architektur & Sicherheitsregeln (Zero-Interpretation)

### 1.1 Einbettung in den Asyncio-Lifecycle (`src/hems/main.py`)

- Der FastAPI-/Uvicorn-Server läuft als **nicht-blockierender `asyncio.Task` im selben Event-Loop** wie MQTT-Ingestion, 1-Hz-Kaskade und MPC-Loop.
- Bindung auf `0.0.0.0:{config.ingress_port}` (Default `8099`); kein zweiter Server, kein alternativer Port (Server-Disziplin: nur ein Server gleichzeitig).
- Konstruktion mit `uvicorn.Server(uvicorn.Config(app, host="0.0.0.0", port=config.ingress_port, log_level=...))`; Start via `asyncio.create_task(server.serve())`.
- Beim Shutdown: `server.should_exit = True` und `await task` (mit Timeout), sodass der Container sauber beendet wird. Die bestehende `finally: await sqlite_store.close()` bleibt erhalten und wird um den Web-Shutdown ergänzt.
- Der Webserver darf die 1-Hz-Kaskade nicht blockieren: alle Endpunkte sind `async def`; CPU-lange Arbeiten (Muster-Clustering) laufen in `asyncio.to_thread(...)`, HA-Historie- und Registry-I/O sind bereits asynchron.

### 1.2 Ingress-Pfad-Kompatibilität (KRITISCH)

Home Assistant Ingress leitet Anfragen über `/api/hassio_ingress/<token>/...` an Port 8099 weiter.

- Im **Frontend** (`index.html` / Vanilla JS) dürfen **NIEMALS** absolute API-Pfade mit führendem Slash verwendet werden. Alle `fetch(...)`-Aufrufe MÜSSEN strikt **relative Pfade ohne führenden Slash** nutzen:

  ```js
  // RICHTIG – funktioniert direkt und hinter Ingress identisch
  fetch("api/dashboard")
  fetch("api/devices")
  fetch("api/patterns/retrain", { method: "POST" })

  // FALSCH – bricht hinter Ingress (verweist auf den HA-Core, nicht das Add-on)
  fetch("/api/dashboard")
  fetch('/api/devices')
  ```

- Auch `<base href="/">`, absolute CSS-`url(/...)`-Referenzen und `window.location = "/..."` sind untersagt.
- Serverseitig werden die FastAPI-Routen ganz normal mit führendem Slash deklariert (`@app.get("/api/dashboard")`). Das Verbot gilt **ausschließlich für das Frontend** — diese Trennung ist im Code-Kommentar explizit zu dokumentieren, damit sie nicht „korrigiert" wird.
- Erfolgskontrolle: dieselbe UI muss unter `http://localhost:8099/` **und** unter `http://<ha>:8123/api/hassio_ingress/<token>/` vollständig funktionieren (Endpunkte, Styles, SVG, Downloads).
- **Entscheidung (bestätigt):** Die Authentifizierung erfolgt ausschließlich durch den HA-Ingress-Token; die FastAPI-App implementiert **keine** eigene Auth und vertraut dem Proxy. Bindung an `0.0.0.0` ist für Ingress erforderlich. Der lokale Debug-Zugriff auf `localhost:8099` bleibt bewusst offen; es wird kein zusätzliches Passwort-/Token-Gate ergänzt.

### 1.3 Keine externen CDN-Abhängigkeiten

- HTML, CSS und JavaScript werden vollständig lokal ausgeliefert. Kein Laden von Google Fonts, Chart.js-CDNs, externen Icons oder ähnlichen Ressourcen aus dem Internet.
- Diagramme werden als saubere, responsive **Inline-SVGs in Vanilla JS** generiert (kein Framework, keine Chart-Bibliothek).
- Farben, Abstände und Radius über CSS-Variablen im dunklen/hellen Home-Assistant-Stil; Responsive Grid ohne externe Stylesheets.

---

## 2. Verbindliche REST-API-Spezifikation (`src/hems/web/app.py`)

Alle JSON-Endpunkte liegen unter dem Präfix `api/` (relativ zum Root; serverseitig `/api/...`).

### 2.1 App-Factory & Dependency Injection

```python
def create_app(
    config: AppConfig,
    state: LiveState,
    registry: DeviceRegistry,
    ha_client: HaCoreClient,
    store: SqliteAuditStore,
    forecaster: ApplianceForecaster,
    scheduler: ControlScheduler,
) -> FastAPI: ...
```

- Die Factory ist der einzige öffentliche Einstieg; `main.py` erzeugt die App mit den realen Objekten, Tests erzeugen sie mit `respx`-gemocktem `HaCoreClient` und `tmp_path`-Registry. **Uvicorn wird ausschließlich in `main.py` konstruiert**, nicht in `create_app` — sonst ist die App nicht mit `fastapi.testclient.TestClient` testbar.
- Request-Bodies sind Pydantic-v2-Modelle mit `ConfigDict(extra="forbid")` (z. B. `ModeRequest`, `DryRunRequest`, `EnabledRequest`).
- Fehler sind strukturiert: 400 bei ID-Mismatch/ungültigem Body, 404 bei unbekanntem `device_id`, 500 nur bei echten unerwarteten Fehlern (jeweils mit `detail`-Feld). Keine Exceptions stumm verschlucken.

### 2.2 Endpunkte

| Methode | Pfad (serverseitig) | Request-Body | Response |
|---|---|---|---|
| `GET` | `/` | – | `index.html` (`text/html; charset=utf-8`) |
| `GET` | `/api/dashboard` | – | Modus, `dry_run`, LiveState, letzte `DecisionTraceRecord`, Anzahl aktiver Geräte, erkannte WashData-Geräte & gelernte Muster |
| `POST` | `/api/control/mode` | `{"mode": OperatingMode}` | `{"status":"ok","mode":...}` |
| `POST` | `/api/control/dry_run` | `{"dry_run": bool}` | `{"status":"ok","dry_run":...}` |
| `GET` | `/api/devices` | – | `list[ControllableDevice]` (steuerbar + überwacht) |
| `POST` | `/api/devices` | `ControllableDevice` | neues Gerät, HTTP **201** |
| `PUT` | `/api/devices/{device_id}` | `ControllableDevice` | aktualisiertes Gerät, HTTP **200**; **400** bei ID-Mismatch |
| `PATCH` | `/api/devices/{device_id}/enabled` | `{"enabled": bool}` | Gerät, **404** falls unbekannt |
| `DELETE` | `/api/devices/{device_id}` | – | HTTP **200**, **404** falls unbekannt |
| `GET` | `/api/ha/entities` | optional `?domain=light,switch,sensor,number,select` | `list[HaEntityState]` (kommagetrennte Domains als Set parsen; ohne Parameter alle) |
| `GET` | `/api/integrations/washdata` | – | `list[WashDataLiveStatus]` aller erkannten WashData-Geräte |
| `POST` | `/api/integrations/washdata/sync` | – | `{"status":"ok","imported_count": int}` |
| `GET` | `/api/patterns` | – | `list[LearnedPattern]` aus `SqliteAuditStore` |
| `POST` | `/api/patterns/retrain` | – | `{"status":"ok","patterns_found": int}` |
| `GET` | `/api/forecasts` | – | `SystemForecastReport` (96 Slots je Verbraucher + `aggregated_uncontrollable_w` + `total_house_predicted_w`) |

### 2.3 Verbindliche Präzisierungen zur Endpunkt-Logik

- **`POST /api/control/mode` und `/dry_run` (sicherheitskritisch):** Es muss **sowohl** `state.operating_mode` / `state.dry_run` (`LiveState`) **als auch** das gemeinsam genutzte `config.operating_mode` / `config.dry_run` aktualisiert werden. Grund: Der Scheduler arbeitet mit einem `config.model_copy(update=...)` aus dem `LiveState`, `EmhassClient.build_runtimeparams` liest jedoch direkt `config.operating_mode`. Wird nur der `LiveState` geändert, steuert die Kaskade neu, der EMHASS-Lauf aber mit dem alten Modus. Jede Änderung wird zusätzlich strukturiert geloggt; ein Wechsel auf `dry_run=false` erzeugt eine deutliche Warnung im Log und im UI-Banner.
- **`GET /api/dashboard`:** Vor dem ersten MQTT-Zustand ist `state.inputs is None` — das Feld wird dann `null` zurückgegeben, der Endpunkt darf nicht abstürzen. Die „letzte Entscheidung" kommt aus einer neuen **read-only Property** `ControlScheduler.last_decision` (bisher privates `_last_logged`); das ist eine reine Ergänzung ohne Verhaltensänderung.
- **`POST /api/patterns/retrain`:** Ruft `HaCoreClient.get_history(...)` für die analysierten Entitäten, führt das zirkuläre Clustering in `asyncio.to_thread(...)` aus, speichert über `store.replace_patterns(...)` und löst anschließend eine Prognose-Aktualisierung aus. Rückgabe ist die Anzahl gefundener Muster. Die CPU-Arbeit darf niemals im 1-Hz-Loop laufen.
- **`GET /api/forecasts` (Entscheidung bestätigt):** Liefert den im Speicher gehaltenen aktuellen `SystemForecastReport` (mit `generated_at_utc`). Aktualisierung ausschließlich beim Start, bei `POST /api/patterns/retrain` und im 15-min-MPC-Zyklus — nicht bei jedem GET und nicht bei Sensor-Statuswechseln (sonst blockierende HA-Historie-Aufrufe im Request-Pfad). Solange kein Report vorliegt, wird ein leerer Report mit 96 Nullwerten je Aggregatliste zurückgegeben.

---

## 3. UI-Aufbau (`src/hems/web/static/index.html`)

Single-Page-Application im modernen, dunklen/hellen Home-Assistant-Stil (CSS-Variablen, Responsive Grid), gegliedert in 4 Tabs.

### Tab 1: Übersicht & Live-Regelung

- Status-Karten: Netzleistung (`p_grid_before_w`), PV-Erzeugung, Batterie-SoC, § 14a-Status (Aktiv / Normal).
- Schnellsteuerung: Dropdown für den Betriebsmodus (`A_BASELINE`, `B_MARKET_ORIENTED`, `C_GRID_SUPPORTIVE`) und Sicherheits-Toggle für `dry_run` mit deutlichem **Warnbanner**, sobald `dry_run == false` aktiv ist.
- Aktive Haushalts-Integrationen (z. B. WashData): laufende Programme direkt anzeigen, z. B. „Waschmaschine: Baumwolle 40 °C – 62 % (noch 38 min)".
- Letzte Entscheidung (White-Box-Trace): `winning_priority` (Prio 1–4), `trigger`, `override_reason` und aktive Sollwerte aller steuerbaren Geräte.

### Tab 2: Einzelverbraucher- & Gesamthaus-Prognosen (24 Stunden)

- Verbraucher-Auswahl als Karten:
  - Gesamthaus-Lastprognose (`total_house_predicted_w`) und nicht-steuerbare Grundlast (`aggregated_uncontrollable_w`).
  - Jeder einzelne eingebundene Verbraucher mit erwarteter 24-h-Energie (kWh), Spitzenleistung (W), Status-Badge (WashData aktiv / Gelerntes Muster / Historisches Profil / MPC-Fahrplan) und Prognosequelle.
- Interaktives 24-h-SVG-Balken-/Liniendiagramm für den ausgewählten Verbraucher oder die Gesamthaus-Aggregation: 15-Minuten-Leistungsverlauf, Uhrzeit-Achse, Hover-Tooltip mit Uhrzeit, Leistung (W), Konfidenz (%) und Quelle.

### Tab 3: Erkannte Schaltmuster

- Tabelle pro erkanntem Licht, Schalter oder WashData-Gerät:
  - Name & Entity-ID (z. B. „Wohnzimmer Deckenlicht (`light.wohnzimmer`)").
  - Tagtyp-Badge (Werktags Mo–Fr vs. Wochenende Sa–So).
  - Typische Einschaltzeit (`HH:MM ± σ min`) und durchschnittliche Dauer (min).
  - Konfidenz-Balken (z. B. 85 % bei 11/13 Tagen) und geschätzte Leistung (W).
- Button „Muster aus HA-Historie aktualisieren" (`POST api/patterns/retrain`) mit Ladeanzeige und Statusmeldung.

### Tab 4: Verbraucher & Geräte verwalten

- Integrations-Banner (WashData): erkannte Geräte aus `GET api/integrations/washdata` mit Button „WashData-Geräte synchronisieren" (`POST api/integrations/washdata/sync`).
- Geräte- & Verbraucherliste: alle konfigurierten Einträge mit Typ-Badge (Steuerbar vs. Überwacht/Prognose), Kategorie-Icon, Leistungsgrenzen, § 14a-Badge, Direkt-Schalter (Aktiviert/Deaktiviert), Bearbeiten- und Löschen-Buttons.
- Formular „Neuen Verbraucher / Neues Gerät hinzufügen":
  - Typ-Umschalter `is_controllable=true/false` (blendet Aktor-Bindung aus, wenn nicht steuerbar).
  - Felder: Name, Geräte-ID, Kategorie (`wallbox`, `battery`, `heatpump`, `deferrable_load`, `appliance_cycle`, `monitored_load`), Regelungsart (`continuous_watt`, `binary_switch`, `sg_ready`, `none`), Mindest-/Maximal-/Nennleistung (W), Prioritätsrang (1–100), Checkbox § 14a steuVE.
  - Home-Assistant-Entity-Picker: Entitäten aus `GET api/ha/entities` in durchsuchbare `<datalist>`-Felder für `power_sensor_entity_id`, `state_entity_id`, optional `washdata_prefix` sowie `ha_binding.entity_id` oder `mqtt_command_topic` bei steuerbaren Geräten.

---

## 4. Verzeichnisstruktur-Erweiterung

```text
hems_orchestrator/
└── src/
    └── hems/
        ├── forecast/
        │   ├── __init__.py
        │   ├── washdata_adapter.py       # Phase 6: WashData-Erkennung & Zyklus-Projektion
        │   ├── pattern_learner.py        # Phase 6: Zirkuläre 1D-Mustererkennung
        │   └── appliance_forecaster.py   # Phase 6: Universelle 96-Slot-Verbraucherprognose
        └── web/
            ├── __init__.py
            ├── app.py                    # Phase 7: FastAPI App & REST API
            └── static/
                └── index.html            # Phase 7: Ingress Web-UI
```

**Keine neuen Dependencies:** `fastapi`/`uvicorn` wurden bereits in Phase 5 (`docs/06` §1.2) ergänzt; `httpx` (für `TestClient`) liegt bereits als Core-Dependency vor. Keine Frontend-Build-Tools, kein npm, kein CDN.

---

## 5. Akzeptanzkriterien & Tests (Phase-7-Gate)

Erstelle `hems_orchestrator/tests/test_web_ui_api.py` und verifiziere:

| # | Akzeptanzkriterium | Erwartetes Ergebnis |
|---|---|---|
| C1 | `GET /` | HTTP 200, HTML-Inhalt; im ausgelieferten HTML/JS kommt **kein** `fetch("/api` und **kein** `fetch('/api` vor (Ingress-Pfadsicherheit) |
| C2 | Geräte-CRUD | `POST api/devices` legt sowohl ein steuerbares Gerät (`is_controllable=True`) als auch einen überwachten Verbraucher (`is_controllable=False`) an; `GET`, `PATCH .../enabled` und `DELETE` funktionieren deterministisch |
| C3 | WashData-Integration | `GET api/integrations/washdata` und `POST api/integrations/washdata/sync` erkennen/importieren gemockte WashData-Entitäten (`imported_count` korrekt, keine Überschreibung) |
| C4 | Muster & Prognosen | `POST api/patterns/retrain` führt die Mustererkennung gegen einen gemockten `HaCoreClient` aus; `GET api/patterns` und `GET api/forecasts` liefern erkannte Lichtmuster, WashData-Prognosen und 96-Slot-Einzelverbraucherprognosen |
| C5 | Laufzeitsteuerung | `POST api/control/mode` und `POST api/control/dry_run` aktualisieren Laufzeitzustand **und** `config` und werden in `GET api/dashboard` reflektiert |
| C6 | Ingress- und CDN-Regeln | Zusätzlich zu C1: kein `<base href="/">`, keine absoluten `url(/...)` und keine externen `http(s)://`-Ressourcen im HTML |
| C7 | Gesamtsuite | `ruff check src tests` und `pytest -v` laufen mit Exit-Code 0, 0 Failures, 0 Errors, **0 Warnings** (87 + Phase-5 + Phase-6 + neue Phase-7-Tests) |

### Gate-Kommandos (aus `hems_orchestrator/`)

```bash
ruff check src tests
pytest -v
```

**Gate-Bedingung:** Exit-Code 0 für beide Kommandos; keine Testabschwächung, keine Skips, keine gelöschten Assertions.

---

## 6. Empfohlene Implementierungsreihenfolge

1. `web/__init__.py`, `web/app.py`: `create_app(...)` mit allen Endpunkten und Pydantic-Request-Modellen (Abschnitt 2) + Tests C1–C5.
2. `ControlScheduler.last_decision`-Property und `main.py`-Verdrahtung inkl. Uvicorn-Task und sauberem Shutdown (Abschnitt 1.1).
3. `web/static/index.html`: Layout, CSS-Variablen, 4 Tabs, relative fetches (Abschnitt 1.2–1.3 und 3) + Ingress-/CDN-Test C6.
4. Prognose-Cache und Retrain-Ablauf (Abschnitt 2.3) gegen Phase-6-Pipeline verdrahten.
5. Abschluss-Gate (Abschnitt 5) und Abschlussbericht.

---

## 7. Entscheidungsprotokoll (alle Punkte geklärt)

| Punkt | Entscheidung | Auswirkung |
|---|---|---|
| Gate-Abhängigkeit | Faktum, keine Entscheidung: `forecast/` und `web/` existieren noch nicht; Phase 7 erst nach bestandenen Phase-5-/Phase-6-Gates | Reihenfolge in `docs/00` §4 bleibt verbindlich |
| Prognose-Refresh | Start + `POST api/patterns/retrain` + 15-min-MPC-Zyklus; kein Event-/Polling-Refresh | Kein HA-I/O im Request-Pfad; Abschnitt 2.3 aktualisiert |
| Zugriffsschutz | Nur HA-Ingress-Token; lokaler Debug-Zugriff bewusst offen, kein eigenes Auth | Abschnitt 1.2 aktualisiert |
| Offene Punkte aus Phase 5/6 | Alle in `docs/06` §7 und `docs/07` §7 geklärt | Keine offenen Abhängigkeiten mehr; `MPC_SCHEDULE` kommt aus den drei 24-Slot-Vektoren in `EmhassResult` (Patch-Spec §1) |
