# 06 – Phase 5: Home Assistant Core API, Universal Consumer & WashData Registry

## Ziel dieser Phase

Erweiterung des HEMS-Orchestrators um:

1. Eine direkte Anbindung an die **Home Assistant Core API** (via Supervisor Proxy, `httpx.AsyncClient`).
2. Eine persistente, über das UI verwaltbare **Consumer & Device Registry**, die sowohl regel-/steuerbare Geräte (`is_controllable=True`, z. B. Wallbox, Batterie, Wärmepumpe, Boiler) als auch rein überwachte Haushaltsverbraucher (`is_controllable=False`, z. B. Waschmaschine, Trockner, Geschirrspüler, Kühlschrank, Backofen, Home-Office) abbildet.
3. Eine **Auto-Discovery** für Home-Assistant-Integrationen (insbesondere WashData / `ha_washdata` sowie Smart-Plug-Leistungssensoren mit `device_class="power"`), sodass deren Laufzeiten, Restlaufzeiten und Lastprofile direkt in die Gesamthaus-Prognose einfließen.
4. Einen erweiterten, registry-getriebenen **Aktor-Dispatcher** mit striktem Shadow-Mode auch für HA-Service-Calls.

**Gate-Vorbedingung:** Alle bestehenden **87 Tests** aus den Phasen 1–4 bleiben unverändert grün (verifizierter Stand: 87 `test_`-Funktionen in `tests/`). Es werden keine bestehenden Tests abgeschwächt, übersprungen oder Assertions entfernt.

**Pfad-Hinweis:** Alle Add-on-Dateien liegen im Unterordner `hems_orchestrator/` (z. B. `hems_orchestrator/config.yaml`, `hems_orchestrator/src/hems/...`, `hems_orchestrator/tests/...`). Alle Gate-Kommandos sind dort auszuführen.

**Abhängigkeit / Referenz:** Die WashData-Detailspec liegt inzwischen als `docs/07_UNIVERSAL_CONSUMER_FORECAST.md` vor (insbesondere Abschnitte 2.1–2.3 zur Attributsemantik von `projected_energy_kwh`, `reference_profile` und `power_profile`). Die in Abschnitt 3.3 dieser Datei festgelegten Erkennungsregeln bleiben die verbindliche Mindestmenge; die Präzisierung in `docs/07` darf die hier definierten Gateway-Verträge nicht brechen (siehe Abschnitt 7).

---

## 1. Manifest- & Dependency-Erweiterung

### 1.1 `hems_orchestrator/config.yaml`

Ergänze folgende Felder im Add-on-Manifest; alle bestehenden Felder bleiben unverändert:

```yaml
ingress: true
ingress_port: 8099
panel_icon: "mdi:flash-auto"
panel_title: "HEMS Orchestrator"
homeassistant_api: true
```

> **Status:** `homeassistant_api: true` ist im aktuellen Manifest bereits Zeile 14 vorhanden — nur bestätigen, nicht duplizieren. `ingress`, `ingress_port`, `panel_icon`, `panel_title` sind neu.
>
> `ingress_port: 8099` korrespondiert mit dem neuen `AppConfig.ingress_port` (Abschnitt 1.3) und dem Uvicorn-Server aus Abschnitt 4.

### 1.2 `hems_orchestrator/pyproject.toml`

Ergänze unter `dependencies` (bestehende Einträge bleiben unverändert):

```toml
"fastapi>=0.115,<1.0",
"uvicorn[standard]>=0.32,<1.0",
```

Damit bleiben die harten Abhängigkeits-Regeln aus `docs/00` §5 gewahrt: keine schweren Frameworks (kein Django, Celery, Pandas, NumPy, SciPy). FastAPI/Uvicorn dienen ausschließlich dem lokalen Ingress-UI-Server im Container, nicht dem Regelkern.

### 1.3 `hems_orchestrator/src/hems/config.py`

Ergänze in `AppConfig` (`model_config = ConfigDict(extra="forbid")` bleibt) folgende Felder mit gültigen Defaults, damit bestehende Tests nicht brechen:

```python
ha_api_url: str = Field(default="http://supervisor/core/api")
ha_token: str = Field(default="")
ingress_port: int = Field(default=8099, ge=1024, le=65535)
pattern_lookback_days: int = Field(default=14, ge=3, le=60)
pattern_retrain_interval_s: float = Field(default=21600.0, ge=300.0, le=86400.0)
auto_discover_washdata: bool = Field(default=True)
```

| Feld | Default | Zweck |
|---|---|---|
| `ha_api_url` | `http://supervisor/core/api` | Supervisor-Proxy zum HA-Core-REST-API |
| `ha_token` | `""` | Optional; zur Laufzeit `os.environ.get("SUPERVISOR_TOKEN", "")`, falls leer |
| `ingress_port` | `8099` | Port des FastAPI-Ingress-Servers |
| `pattern_lookback_days` | `14` | Fenster für HA-Historien-basierte Lastprofile (≥ 3 Tage) |
| `pattern_retrain_interval_s` | `21600.0` | 6 h — Intervall für Profil-Retraining |
| `auto_discover_washdata` | `True` | Automatischer Import erkannter WashData-Geräte |

> Die neuen Felder müssen zusätzlich in `config.yaml` unter `options` und `schema` aufgenommen werden (`ha_api_url: url`, `ha_token: str?`, `ingress_port: int(1024,65535)`, `pattern_lookback_days: int(3,60)`, `pattern_retrain_interval_s: float(300.0,86400.0)`, `auto_discover_washdata: bool`), sonst lehnt HA das Options-Schema ab (`extra="forbid"`-Semantik des Add-on-Schemas).
>
> `ha_token` ist bewusst **nicht** in `schema` als Pflichtfeld geführt: das Token kommt aus `SUPERVISOR_TOKEN`, nicht aus der UI-Konfiguration.

---

## 2. Home Assistant Core API Client (`src/hems/io/ha_core_client.py`)

Implementiere die asynchrone Klasse `HaCoreClient`. Alle Regeln aus `docs/00` §2 (kein Event-Loop-Blocking, strukturiertes Logging, keine Silent Exceptions) gelten unverändert.

### 2.1 Authentifizierung & Basis-Konfiguration

- `httpx.AsyncClient(base_url=config.ha_api_url, timeout=10.0)`.
- Token-Auflösung: `token = config.ha_token or os.environ.get("SUPERVISOR_TOKEN", "")`.
- Header: `Authorization: Bearer {token}` und `Content-Type: application/json`.
- **Kein Token** (z. B. außerhalb von HA ohne Mock): Client loggt **einmalig** eine Warnung und liefert sichere leere Rückgabewerte statt unkontrollierter Abstürze:
  - `get_states()` → `[]`
  - `get_history(...)` → `{}`
  - `call_service(...)` → `False`
- Ein `bool`-Flag auf der Instanz merkt sich die bereits ausgegebene Warnung (kein Log-Spam im 1-Hz-Betrieb).
- HTTP-/JSON-Fehler werden strukturiert mit `logger.error(...)` geloggt; der Regelkern führt deterministisch in den Prio-4-Fallback (kein `except Exception: pass`).

### 2.2 Verbindliche Datenmodelle

```python
class HaEntityState(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    entity_id: str
    state: str
    last_changed: str
    friendly_name: str
    device_class: str | None = None
    unit_of_measurement: str | None = None
    domain: str
    attributes: dict[str, Any] = Field(default_factory=dict)


class HaHistoryPoint(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    state: str
    last_changed_utc: str
```

**Mapping-Regeln für `/states` (wichtig wegen `extra="forbid"`):**

- Roh-Antwort enthält u. a. `entity_id`, `state`, `attributes`, `last_changed`, `last_updated`, `context`. Nur die oben definierten Felder dürfen an das Modell übergeben werden; `last_updated` und `context` sind **vor** der Validierung zu verwerfen.
- `domain = entity_id.split(".", 1)[0]` (bei fehlendem Punkt deterministisch `""` oder `entity_id` — feste Regel im Test dokumentieren).
- `friendly_name = attributes.get("friendly_name", entity_id)`.
- `device_class`, `unit_of_measurement` aus `attributes`.
- `attributes` vollständig übernehmen — benötigt u. a. für WashData-Attribute `projected_energy_kwh`, `reference_profile`, `power_profile`.

### 2.3 Methoden

**`async def get_states(self) -> list[HaEntityState]`**

- `GET /states`.
- Parst jede Entität gemäß Abschnitt 2.2 inklusive `attributes`.

**`async def get_history(self, entity_ids: list[str], start_utc: datetime, end_utc: datetime | None = None) -> dict[str, list[HaHistoryPoint]]`**

- `GET /history/period/{start_utc.isoformat()}` (Pfad-Fragment URL-kodieren, z. B. `+00:00` → `%2B00%3A00`; die Zeichenkette selbst bleibt verbindlich `isoformat()`).
- Query-Parameter:
  - `filter_entity_id`: kommagetrennte Liste der `entity_ids`
  - `minimal_response`: `"true"`
  - `no_attributes`: `"true"`
  - optional `end_time`: `end_utc.isoformat()`
- HA antwortet mit einer Liste von Listen (eine pro Entity); Rückgabe ist ein flaches Dict `{entity_id: [HaHistoryPoint(...), ...]}`, **aufsteigend sortiert nach `last_changed_utc`**. Leere Antwort → `{}`.

**`async def call_service(self, domain: str, service: str, service_data: dict[str, Any]) -> bool`**

- `POST /services/{domain}/{service}` mit JSON-Body `service_data`.
- `True` bei HTTP 200/201, sonst `False` mit strukturiertem Error-Log (Statuscode, Domain, Service).

---

## 3. Universal Consumer & Device Registry (`src/hems/control/device_registry.py`)

Ermöglicht Hinzufügen, Bearbeiten, Aktivieren/Deaktivieren und Löschen aller Verbraucher zur Laufzeit über das UI — sowohl steuerbare Geräte als auch rein prognostizierte/überwachte Verbraucher.

### 3.1 Datenmodelle (alle `ConfigDict(extra="forbid")`)

```python
class DeviceCategory(str, Enum):
    WALLBOX = "wallbox"
    BATTERY = "battery"
    HEATPUMP = "heatpump"
    DEFERRABLE_LOAD = "deferrable_load"  # Steuerbare Last (Boiler, Poolpumpe, Klimagerät)
    APPLIANCE_CYCLE = "appliance_cycle"  # Zyklisches Haushaltsgerät (WashData)
    MONITORED_LOAD = "monitored_load"    # Sonstiger Verbraucher (Smart Plug, Küche, IT)


class ControlModeType(str, Enum):
    NONE = "none"                        # Nur Überwachung & Prognose (is_controllable=False)
    CONTINUOUS_WATT = "continuous_watt"  # Stufenlose Leistungsregelung (z. B. 0..11000 W)
    BINARY_SWITCH = "binary_switch"      # Ein/Aus (0 W oder nominal_power_w)
    SG_READY = "sg_ready"                # 4-stufiger Wärmepumpen-Modus


class ControlTransport(str, Enum):
    NONE = "none"
    MQTT = "mqtt"
    HA_SERVICE = "ha_service"


class IntegrationSource(str, Enum):
    MANUAL = "manual"
    WASHDATA = "washdata"
    HA_POWER_SENSOR = "ha_power_sensor"
    HA_PATTERN = "ha_pattern"
```

```python
class HaServiceBinding(BaseModel):
    model_config = ConfigDict(extra="forbid")
    domain: str                       # "number" | "switch" | "select" | "ha_washdata"
    entity_id: str                    # "number.wallbox_max_power" | "switch.boiler"
    service_on_or_set: str            # "set_value" | "turn_on" | "resume_cycle"
    service_off: str | None = None    # "turn_off" | "pause_cycle"
    value_field: str = "value"        # Parametername im Service-Call


class ControllableDevice(BaseModel):
    model_config = ConfigDict(extra="forbid")
    device_id: str = Field(min_length=1, max_length=64, pattern=r"^[a-z0-9_-]+$")
    name: str = Field(min_length=1, max_length=100)
    category: DeviceCategory
    is_controllable: bool = True
    control_mode: ControlModeType = ControlModeType.CONTINUOUS_WATT
    enabled: bool = True
    is_steuve_14a: bool = True
    priority_rank: int = Field(default=50, ge=1, le=100)
    min_power_w: float = Field(default=0.0, ge=0.0)
    max_power_w: float = Field(gt=0.0)
    nominal_power_w: float = Field(gt=0.0)
    power_sensor_entity_id: str | None = None
    state_entity_id: str | None = None
    soc_sensor_entity_id: str | None = None
    washdata_prefix: str | None = None
    source_integration: IntegrationSource = IntegrationSource.MANUAL
    transport: ControlTransport = ControlTransport.NONE
    mqtt_command_topic: str | None = None
    ha_binding: HaServiceBinding | None = None
```

### 3.2 Validierungsregeln (`@model_validator(mode="after")`)

**`is_controllable == False`:**

- `is_steuve_14a` wird **deterministisch auf `False`** gesetzt (nicht steuerbare Verbraucher können nicht gedimmt werden). Ein explizit übergebenes `True` wird korrigiert, nicht abgelehnt.
- `transport` darf `ControlTransport.NONE` sein (und ist der Normalfall).

**`is_controllable == True`:**

- `transport` darf **nicht** `ControlTransport.NONE` sein.
- `transport == MQTT` → `mqtt_command_topic` darf nicht leer sein.
- `transport == HA_SERVICE` → `ha_binding` darf nicht `None` sein.
- `min_power_w <= max_power_w` und `nominal_power_w <= max_power_w`.

> Diese Regeln müssen auch für die Default-Seeds (Abschnitt 3.3) greifen: die drei Standardgeräte werden mit `transport=MQTT` und je einem `mqtt_command_topic` angelegt, sonst ist das Seeding nicht modellgültig.

### 3.3 Persistenz, Default-Seeding & WashData-Auto-Sync

**Speicherort:** `{config.data_dir}/config/devices.json` → im Standard `/data/config/devices.json` (HA-Backup-Schutz: persistent unter `/data/...`).

**Atomares Schreiben (entschieden):** Temp-Datei im **selben Zielverzeichnis** (`/data/config/.devices.json.tmp`) + `os.replace`. `/data` und `/tmp` können im HA-Container unterschiedliche Mounts sein; `os.replace` über Mount-Grenzen wäre nicht atomar. Schreiben und Verzeichnisanlage laufen nicht-blockierend via `asyncio.to_thread`. Diese begründete Ausnahme von der `/tmp`-Regel ist in `AGENTS.md` §3.3 vermerkt.

**Initiales Seeding:** Existiert `devices.json` beim Start nicht, legt `DeviceRegistry` automatisch die drei steuerbaren Standardgeräte aus `AppConfig` an:

| `device_id` | `name` | `category` | `control_mode` | `transport` | Leistungsfelder |
|---|---|---|---|---|---|
| `default_wallbox` | Wallbox | `WALLBOX` | `CONTINUOUS_WATT` | `MQTT` | `min=wallbox_min_power_w`, `max=nominal=wallbox_max_power_w` |
| `default_battery` | Batteriespeicher | `BATTERY` | `CONTINUOUS_WATT` | `MQTT` | `min=0`, `max=max(battery_max_charge_w, battery_max_discharge_w)`, `nominal=battery_max_charge_w` |
| `default_heatpump` | Wärmepumpe | `HEATPUMP` | `SG_READY` | `MQTT` | `min=0`, `max=nominal=heatpump_nominal_power_w` |

Alle drei: `is_controllable=True`, `enabled=True`, `is_steuve_14a=True`, `source_integration=MANUAL`.

**WashData-Auto-Import:**
`sync_washdata_devices(states: list[HaEntityState]) -> list[ControllableDevice]`

- Erkennt alle in HA vorhandenen WashData-Geräte. Verbindliche Mindest-Erkennungsregel: ein `sensor.<slug>_state` gilt als WashData-Gerät, wenn ein sibling `sensor.<slug>_time_remaining` (oder `_progress` / `_program`) in `states` vorhanden ist. Der `slug` ist der Entity-ID-Anteil zwischen `sensor.` und `_state`.
- Für jeden noch nicht registrierten Slug wird angelegt, sofern `config.auto_discover_washdata == True`:
  - `device_id = f"washdata_{slug}"`
  - `name = friendly_name` des `_state`-Sensors (Fallback: `slug`)
  - `category = APPLIANCE_CYCLE`
  - `is_controllable = False`, `control_mode = NONE`, `transport = NONE`
  - `washdata_prefix = slug`
  - `source_integration = WASHDATA`
  - `power_sensor_entity_id` = `sensor.<slug>_power` (falls vorhanden, sonst `None`)
  - `min_power_w = 0.0`, `max_power_w = nominal_power_w` = `projected_energy_kwh`-basierter Schätzwert, falls vorhanden; sonst verbindlich **`800.0 W`** (konsistent zum WashData-Domain-Fallback in `docs/07` §3.2 Schritt 5)
- **Bestehende, manuell bearbeitete Einträge werden niemals überschrieben** (nur `device_id`-basierte Neuanlage). Die Rückgabe ist die Liste der neu angelegten Geräte.
- Smart-Plug-Erkennung (`device_class="power"`, `IntegrationSource.HA_POWER_SENSOR`, `category=MONITORED_LOAD`) folgt derselben Nicht-Überschreibungsregel; sie ist Teil der Auto-Discovery, aber nicht im Pflicht-Gate von Abschnitt 5 enthalten.

**CRUD-Methoden (async-safe via `asyncio.Lock`):**

```python
async def list_devices(self) -> list[ControllableDevice]
async def get_device(self, device_id: str) -> ControllableDevice | None
async def upsert_device(self, device: ControllableDevice) -> ControllableDevice
async def delete_device(self, device_id: str) -> bool
async def set_enabled(self, device_id: str, enabled: bool) -> ControllableDevice
```

Jede mutierende Methode hält die `asyncio.Lock`, validiert erneut über das Pydantic-Modell und schreibt atomar. `list_devices()` gibt eine Kopie der Liste zurück (keine Referenz auf internen Zustand).

### 3.4 Kopplung an Regelkaskade & Aktor-Dispatcher

**Rückwärtskompatibilität (hart):** `evaluate_cascade(inputs, config, emhass_result, setpoint_age_s)` aus `src/hems/control/cascade.py` bleibt in **Signatur und Verhalten unverändert**. Alle 87 Bestandstests (insbesondere `tests/test_priority_cascade.py`) laufen ohne Anpassung weiter.

**Erweiterter Dispatcher (`DeviceCommandDispatcher`, neuer Bereich in `device_registry.py` oder eigenes Modul — siehe Abschnitt 7):**

- Berücksichtigt ausschließlich Geräte mit `enabled == True` **und** `is_controllable == True`.
- `is_controllable == False` (WashData-Haushaltsgeräte, überwachte Smart-Plug-Verbraucher) löst **niemals** Schaltbefehle aus; diese fließen ausschließlich über die Einzelverbraucher-Prognose in die erwartete Haushaltslast ein.
- Zusätzliche `DEFERRABLE_LOAD`-Geräte (`is_controllable=True`, `is_steuve_14a=True`) werden gemäß `priority_rank` (**absteigend abgeworfen**, d. h. niedrigster Rang zuerst) in die Leistungsbilanz von § 14a EnWG und Hausanschlussschutz einbezogen. **Entscheidung:** dies geschieht in einer eigenen reinen Nachbearbeitungsstufe `apply_registry_load_shedding(outputs, inputs, config, devices)` zwischen `evaluate_cascade(...)` und dem Dispatcher; `evaluate_cascade` bleibt dadurch exakt unverändert. Der § 14a-Re-Clamp nach § 9 EEG (`P1_14a_reclamp_after_eeg`, `docs/03` Fix A1) bleibt vorrangig anzuwenden.
- **Entscheidung Topic-Schema (Koexistenz):** Die drei Standardgeräte behalten ihre Legacy-MQTT-Topics über `ActuatorPublisher` (`hems[/shadow]/cmd/wallbox_w | battery_w | heatpump_sg_ready`), damit die Bestandstests grün bleiben. Der neue Dispatcher publiziert zusätzlich registry-spezifisch auf `hems[/shadow]/cmd/{device_id}`. Beide Schemata sind dauerhaft koexistent; eine Migration ist nicht Teil dieser Phasen.

**Fail-Safe (`dry_run == True`):**

| Transport | `dry_run=True` | `dry_run=False` |
|---|---|---|
| `MQTT` | ausschließlich `hems/shadow/cmd/{device_id}` | `hems/cmd/{device_id}` |
| `HA_SERVICE` | **kein** `ha_core_client.call_service(...)`; geplanter Service-Call wird als Shadow-Aktion strukturiert geloggt (Domain, Service, `service_data`, `device_id`) | `call_service(...)` gemäß `HaServiceBinding` |
| `NONE` | kein Dispatch (nur Prognose) | kein Dispatch (nur Prognose) |

> Wie in `docs/00` §3.3 / `AGENTS.md` §3.3: Bei `dry_run=True` darf niemals auf reale Aktor-Topics (`{prefix}/cmd/...`) publiziert werden.

---

## 4. Ingress-UI (Betrieb, nicht im Pflicht-Gate)

`ingress: true` + `ingress_port: 8099` erfordert einen im Container laufenden FastAPI/Uvicorn-Server, der die Registry-CRUD-Methoden über HTTP verfügbar macht. Verbindlich für diese Phase ist zunächst, dass Manifest, Dependency und Port konsistent sind; eine minimale Endpunktmenge wird vorgeschlagen:

| Methode | Pfad | Aktion |
|---|---|---|
| `GET` | `/api/devices` | `list_devices()` |
| `GET` | `/api/devices/{device_id}` | `get_device(...)` |
| `POST` | `/api/devices` | `upsert_device(...)` |
| `DELETE` | `/api/devices/{device_id}` | `delete_device(...)` |
| `PATCH` | `/api/devices/{device_id}/enabled` | `set_enabled(...)` |

Der Server ist vom Regelkern entkoppelt (eigene `asyncio.Task` in `main.py`) und darf die 1-Hz-Kaskade nicht blockieren. Die genaue UI-Gestaltung und der vollständige HTTP-Vertrag sind nicht Teil des Phase-5-Gates; sie sind jetzt verbindlich in `docs/08_INGRESS_WEB_UI_AND_REST_API.md` (Phase 7) spezifiziert.

---

## 5. Akzeptanzkriterien & Tests (Phase-5-Gate)

Erstelle `hems_orchestrator/tests/test_device_registry.py` und verifiziere:

| # | Akzeptanzkriterium | Erwartetes Ergebnis |
|---|---|---|
| A1 | `HaCoreClient` liest `/states` inklusive `attributes` | `HaEntityState` mit `domain`, `friendly_name`, `device_class`, vollständigem `attributes`-Dict (respx-Mock, `SUPERVISOR_TOKEN`-Header geprüft) |
| A2 | `HaCoreClient` liest `/history/period/...` | Korrekte Query-Parameter (`filter_entity_id`, `minimal_response`, `no_attributes`, optional `end_time`), Dict aufsteigend nach `last_changed_utc` sortiert |
| A3 | `HaCoreClient` ruft `/services/...` korrekt auf | `POST /services/{domain}/{service}` mit JSON-Body; `True` bei 200/201, `False` bei Fehler |
| A4 | Default-Seeding | Beim Erststart ohne `devices.json` existieren danach `default_wallbox`, `default_battery`, `default_heatpump` mit den Defaults aus Abschnitt 3.3 |
| A5 | CRUD & gemischte Geräte | Hinzufügen steuerbarer (`is_controllable=True`, z. B. Boiler/`DEFERRABLE_LOAD` mit `HA_SERVICE`) **und** rein überwachter Verbraucher (`is_controllable=False`, z. B. Trockner/Kühlschrank mit Leistungssensor); Update + Löschen; Validierungsregeln aus 3.2 greifen |
| A6 | WashData-Auto-Sync | Aus `sensor.waschmaschine_state` + `sensor.waschmaschine_time_remaining` wird `washdata_waschmaschine` mit `source_integration=WASHDATA` angelegt; bestehende Einträge bleiben unverändert |
| A7 | § 14a-Einbeziehung | Neu registrierter `DEFERRABLE_LOAD` (`is_controllable=True`, `is_steuve_14a=True`) wird bei aktivem § 14a-Limit (4200 W) korrekt abgeworfen |
| A8 | Kein Schaltbefehl für Monitored Loads | Ein Verbraucher mit `is_controllable=False` löst niemals Schaltbefehle aus (weder MQTT noch HA-Service) |
| A9 | Shadow-Mode HA_SERVICE | Bei `dry_run=True` werden **keine** echten HA-Service-Calls abgesetzt; stattdessen Shadow-Log-Eintrag |
| A10 | Bestandsschutz | Alle 87 Bestandstests aus Phasen 1–4 bleiben grün |

### Gate-Kommandos (ausgeführt in `hems_orchestrator/`)

```bash
ruff check src tests
pytest -v
```

**Gate-Bedingung:** Exit-Code 0 für beide Kommandos; `pytest -v` meldet `87 + N` bestandene Tests, 0 Failures, 0 Errors, 0 Warnings; ruff clean.

---

## 6. Reihenfolge der Implementierung (empfohlen)

1. Manifest, `pyproject.toml`, `config.py` (Abschnitt 1) — zuerst, damit neue Fields in allen Fixtures verfügbar sind.
2. `HaCoreClient` inkl. Modelle (Abschnitt 2) + Tests A1–A3.
3. `ControllableDevice` / Enums / Validatoren (Abschnitt 3.1–3.2).
4. `DeviceRegistry` mit Persistenz, Seeding, CRUD (Abschnitt 3.3) + Tests A4–A5.
5. `sync_washdata_devices` (Abschnitt 3.3) + Test A6.
6. `DeviceCommandDispatcher` und Einbindung in Scheduler/`main.py` (Abschnitt 3.4) + Tests A7–A9.
7. Ingress-Server (Abschnitt 4) — nach dem Gate, sofern nicht ausdrücklich mitbeauftragt.
8. Abschluss-Gate (Abschnitt 5) und Abschlussbericht an den Nutzer.

---

## 7. Entscheidungsprotokoll (alle Punkte geklärt)

Alle zuvor offenen Punkte wurden mit dem Nutzer abgestimmt und sind hiermit verbindlich entschieden. Die Entscheidungen sind bereits in den obigen Abschnitten eingearbeitet.

| Punkt | Entscheidung | Auswirkung |
|---|---|---|
| `docs/07`-Abhängigkeit | WashData-Attributsemantik in `docs/07` §2.2 spezifiziert; Default `nominal_power_w = 800.0 W` für auto-importierte Geräte | Abschnitt 3.3 aktualisiert; keine weitere Klärung nötig |
| Atomares Schreiben | Temp-Datei im Zielverzeichnis `/data/config/.devices.json.tmp` + `os.replace` | Garantierte Atomarität; begründete Ausnahme von der `/tmp`-Regel in `AGENTS.md` §3.3 vermerkt |
| `DEFERRABLE_LOAD`-Einbindung | Eigene reine Nachbearbeitungsstufe `apply_registry_load_shedding(...)` zwischen Kaskade und Dispatcher | `evaluate_cascade(...)` bleibt exakt unverändert; 87 Bestandstests grün |
| Topic-Schema | Dauerhafte Koexistenz: Legacy-Topics für die 3 Standardgeräte + `hems[/shadow]/cmd/{device_id}` für Registry-Geräte | Keine Migration, kein Test wird angepasst |
| Ingress-Endpunktvertrag | Vollständige REST-API in `docs/08` §2, UI in §3 (Phase 7) | Für Phase 5 testen A1–A10 nur Client, Registry und Dispatcher |
| `AGENTS.md`-Synchronisierung | Neue harte Regeln wurden in `AGENTS.md` §3.3 nachgezogen | Shadow-Logging `HA_SERVICE`, keine Schaltbefehle bei `is_controllable=False`, Registry-Pfad, Ingress-Pfadregeln |
