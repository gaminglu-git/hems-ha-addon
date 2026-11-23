# 01 – Phase 1: Home Assistant Add-on Scaffold & Configuration

## Ziel dieser Phase

Erstellung des lauffähigen Home Assistant Add-on-Grundgerüsts (Docker, Manifeste, Python-Paketstruktur) sowie eines strikt validierten Konfigurations-Loaders, der sowohl im Home Assistant Supervisor (`/data/options.json`) als auch lokal in Tests/CI (via Umgebungsvariablen oder JSON-Datei) identisch funktioniert.

## 1. Home Assistant Add-on Manifest (config.yaml)

Erstelle `hems_orchestrator/config.yaml` mit exakt diesen Feldern:

```yaml
name: "HEMS Grid-Supportive Orchestrator"
description: "Netzdienliches HEMS mit EMHASS-Kopplung, §14a EnWG Prioritätskaskade und revisionssicherem Decision-Trace-Logging"
version: "0.1.0"
slug: "hems_orchestrator"
init: false
arch:
  - aarch64
  - amd64
startup: application
boot: auto
services:
  - mqtt:need
hassio_api: true
homeassistant_api: true
map:
  - share:ro
options:
  operating_mode: "C_GRID_SUPPORTIVE"
  dry_run: true
  log_level: "INFO"
  mqtt_host: "core-mosquitto"
  mqtt_port: 1883
  mqtt_username: ""
  mqtt_password: ""
  mqtt_topic_prefix: "hems"
  emhass_url: "http://a0d7b954-emhass:5000"
  emhass_timeout_s: 15.0
  mpc_interval_s: 900
  control_loop_interval_s: 1.0
  verification_delay_s: 10.0
  house_fuse_limit_w: 22000.0
  pv_peak_w: 10000.0
  eeg_feedin_limit_pct: 60.0
  dso_14a_min_power_w: 4200.0
  battery_capacity_wh: 10000.0
  battery_max_charge_w: 5000.0
  battery_max_discharge_w: 5000.0
  battery_cycle_cost_ct_kwh: 5.0
  wallbox_max_power_w: 11000.0
  wallbox_min_power_w: 1380.0
  heatpump_nominal_power_w: 2500.0
  heatpump_temp_min_c: 45.0
  heatpump_temp_max_c: 58.0
  ev_target_soc_pct: 80.0
  ev_capacity_wh: 60000.0
  jsonl_max_bytes: 10485760
  jsonl_backup_count: 10
  audit_heartbeat_s: 60
schema:
  operating_mode: list(A_BASELINE|B_MARKET_ORIENTED|C_GRID_SUPPORTIVE)
  dry_run: bool
  log_level: list(DEBUG|INFO|WARNING|ERROR)
  mqtt_host: str
  mqtt_port: port
  mqtt_username: str?
  mqtt_password: password?
  mqtt_topic_prefix: str
  emhass_url: url
  emhass_timeout_s: float(1.0,60.0)
  mpc_interval_s: int(60,3600)
  control_loop_interval_s: float(0.5,5.0)
  verification_delay_s: float(1.0,60.0)
  house_fuse_limit_w: float(3000.0,63000.0)
  pv_peak_w: float(0.0,100000.0)
  eeg_feedin_limit_pct: float(0.0,100.0)
  dso_14a_min_power_w: float(4200.0,22000.0)
  battery_capacity_wh: float(0.0,100000.0)
  battery_max_charge_w: float(0.0,25000.0)
  battery_max_discharge_w: float(0.0,25000.0)
  battery_cycle_cost_ct_kwh: float(0.0,50.0)
  wallbox_max_power_w: float(0.0,22000.0)
  wallbox_min_power_w: float(0.0,4200.0)
  heatpump_nominal_power_w: float(0.0,15000.0)
  heatpump_temp_min_c: float(20.0,60.0)
  heatpump_temp_max_c: float(30.0,75.0)
  ev_target_soc_pct: float(10.0,100.0)
  ev_capacity_wh: float(10000.0,150000.0)
  jsonl_max_bytes: int(1048576,104857600)
  jsonl_backup_count: int(1,50)
  audit_heartbeat_s: int(5,3600)
```

**Begründung Manifest-Details:** `startup: application` startet das Add-on erst nach dem HA-Core-Start (es ist Konsument von MQTT/HA, kein früher Systemdienst). `services: [mqtt:need]` deklariert die MQTT-Abhängigkeit gegenüber dem Supervisor. Sind `mqtt_username`/`mqtt_password` leer, werden die Zugangsdaten zur Laufzeit über die Supervisor-API aufgelöst (siehe Phase 3, „MQTT-Credential-Resolution").

## 2. Docker & Build Dateien

### build.yaml

```yaml
build_from:
  aarch64: "ghcr.io/home-assistant/base:latest"
  amd64: "ghcr.io/home-assistant/base:latest"
```

> Begründung: Der HA Supervisor baut Add-ons aus der `ghcr.io/home-assistant/base`-Familie (Alpine, s6-overlay, bashio). Ein reines `python:3.12-slim-bookworm` liefert zwar pip, entspricht aber nicht der HA-Laufzeitumgebung; umgekehrt fehlt der HA-Basis Python/pip → deshalb der explizite `apk`-Schritt im Dockerfile.

### Dockerfile

Anforderungen:

- Base-Image `ARG BUILD_FROM=ghcr.io/home-assistant/base:latest` (Alpine) und `apk add --no-cache python3 py3-pip`.
- Setze `PYTHONDONTWRITEBYTECODE=1`, `PYTHONUNBUFFERED=1`, `TMPDIR=/tmp`.
- Installiere das Paket in `/opt/venv` via `python3 -m venv /opt/venv && /opt/venv/bin/pip install --no-cache-dir .` (vermeidet PEP-668 „externally-managed-environment" auf Alpine); `ENV PATH="/opt/venv/bin:$PATH"`.
- `ENTRYPOINT []`: Die HA-Basis setzt `ENTRYPOINT ["/init"]` (s6-overlay v3); bei `init: false` in `config.yaml` wird dieser überschrieben, damit `CMD ["/run.sh"]` direkt läuft.
- Installiere keine unnötigen Build-Tools im finalen Image.
- Erstelle Verzeichnisse `/data/logs`, `/data/db`, `/tmp/hems_cache`.
- Kopiere `run.sh`, setze `chmod a+x /run.sh`, `CMD ["/run.sh"]`.

### run.sh

```bash
#!/usr/bin/env bash
set -euo pipefail

mkdir -p /data/logs /data/db /tmp/hems_cache
exec python3 -m hems.main
```

## 3. Konfigurations-Loader (src/hems/config.py)

### Anforderungen an AppConfig(BaseModel)

Definiere ein Enum `OperatingMode(str, Enum)`:

- `A_BASELINE = "A_BASELINE"`
- `B_MARKET_ORIENTED = "B_MARKET_ORIENTED"`
- `C_GRID_SUPPORTIVE = "C_GRID_SUPPORTIVE"`

Alle Felder aus `config.yaml -> options` müssen exakt namensgleich in `AppConfig` abgebildet sein, plus folgende Pfad-Felder mit Defaults:

- `data_dir: Path = Path("/data")`
- `tmp_dir: Path = Path("/tmp/hems_cache")`

Zusätzlich ist das Logging-Feld explizit mit Validierungsgrenzen zu definieren (Fix C3):

- `audit_heartbeat_s: int = Field(default=60, ge=5, le=3600)` (Heartbeat-Intervall für Log-Deduplizierung im 1-Hz-Loop).

### Validatoren (`@model_validator(mode="after")`)

- `heatpump_temp_min_c < heatpump_temp_max_c` (sonst `ValueError`).
- `wallbox_min_power_w <= wallbox_max_power_w` (sonst `ValueError`).
- `dso_14a_min_power_w >= 4200.0` (regulatorische Untergrenze nach § 14a EnWG / BNetzA BK6-22-300).

### Lade-Funktion `load_config(options_path: Path | None = None) -> AppConfig`

- Standardpfad: `Path(os.getenv("HEMS_OPTIONS_PATH", "/data/options.json"))`.
- Falls Datei existiert: JSON laden und mit `AppConfig.model_validate(data)` validieren.
- Falls Datei nicht existiert (nur für lokale Entwicklung/Tests): Default-`AppConfig()` erzeugen.
- Umgebungsvariablen mit Präfix `HEMS_` (z. B. `HEMS_DRY_RUN=false`, `HEMS_DATA_DIR=/tmp/test_data`) dürfen geladene Werte gezielt überschreiben.

## 4. Akzeptanzkriterien & Tests (Phase 1 Gate)

Erstelle `tests/test_config_and_models.py` und verifiziere:

1. `load_config()` lädt ein valides JSON-File (analog `config.yaml -> options`) fehlerfrei.
2. Standardwert für `dry_run` ist zwingend `True`.
3. Ungültige Werte (`dso_14a_min_power_w = 3000.0`, `heatpump_temp_min_c = 60.0 > heatpump_temp_max_c = 50.0`) werfen `pydantic.ValidationError`.
4. `HEMS_DATA_DIR` Override über Umgebungsvariable funktioniert.
5. `ruff check src tests` und `pytest tests/test_config_and_models.py` laufen mit Exit-Code 0.