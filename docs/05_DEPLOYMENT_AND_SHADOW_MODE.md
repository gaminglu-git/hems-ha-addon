# 05 – Deployment & Shadow-Mode-Test (Simulation-to-Reality Gap)

Kompakte Checkliste für den ersten Live-Test des Add-ons gegen die **reale** Home-Assistant-Umgebung. Ziel: Den *Simulation-to-Reality Gap* der Bachelorarbeit schließen und echte MILP-Kosten für Modus B (und C) messen.

**Grundregel:** Der erste Live-Test läuft ausschließlich im **Shadow-Mode (`dry_run: true`)**. Es wird nie auf reale Aktor-Topics (`hems/cmd/...`) publiziert, sondern nur auf `hems/shadow/cmd/...`. Alle Schritte sind ohne Hardware-Wirkung wiederholbar.

---

## 1. Voraussetzungen prüfen

- [ ] Home Assistant **OS** mit Supervisor (nicht Core/Container – Add-ons brauchen Supervisor).
- [ ] **Mosquitto**-Add-on installiert und gestartet (`core-mosquitto:1883` erreichbar).
- [ ] **EMHASS**-Add-on installiert und gestartet (REST-Endpunkt `POST /action/naive-mpc-optim`).
- [ ] Zugriff auf das lokale Add-on-Verzeichnis (Samba-Share `addons`, darin `local/`).
- [ ] Python 3.12 + Docker für den lokalen Build (nur für Schritt 2 nötig; HA baut sonst selbst).

---

## 2. Container lokal bauen (optional, aber empfohlen)

```bash
cd hems_orchestrator
docker build -t hems_orchestrator:0.1.0 .
docker run --rm hems_orchestrator:0.1.0 python3 -c "import hems.main; print('import ok')"
```

Erwartung: `import ok`. Der Container verwendet `python:3.12-slim-bookworm` (siehe `build.yaml`), installiert via `pip install --no-cache-dir .` und erwartet `/data/logs`, `/data/db`, `/tmp/hems_cache` (vom `run.sh` angelegt).

> **Hinweis:** Der Container startet ohne `/data/options.json` mit den Defaults aus `config.py` (`dry_run=True`, `mqtt_host=core-mosquitto`). Ohne erreichbares MQTT/EMHASS läuft er in den Prio-4-Fallback – das ist erwartungskonform, kein Fehler.

---

## 3. Als lokales Add-on installieren

- [ ] Add-on-Ordner (`hems_orchestrator/` aus diesem Repo, Inhalt: `config.yaml`, `build.yaml`, `Dockerfile`, `run.sh`, `pyproject.toml`, `README.md`, `src/`) nach `addons/local/hems_orchestrator/` kopieren.
- [ ] In HA: **Einstellungen → Add-ons → Add-on Store → ⋮ → Lokale Add-ons aktualisieren**.
- [ ] „HEMS Grid-Supportive Orchestrator" auswählen → **Installieren** (HA baut das Image aus `build.yaml`; das dauert beim ersten Mal einige Minuten).
- [ ] **Konfiguration prüfen – Minimum:**

| Option | Wert | Warum |
|---|---|---|
| `dry_run` | `true` | **Pflicht** für den ersten Test |
| `operating_mode` | `C_GRID_SUPPORTIVE` | Netzdienlichkeit zuerst validieren |
| `mqtt_host` | `core-mosquitto` | Supervisor-DNS des Mosquitto-Add-ons |
| `mqtt_username` / `mqtt_password` | `""` (leer) | Supervisor-API löst Credentials automatisch auf |
| `emhass_url` | `http://a0d7b954-emhass:5000` | **Prüfen!** Die Add-on-IP kann abweichen → in HA unter EMHASS → „URL" nachsehen |
| `verification_delay_s` | `10.0` | Wirkungsnachweis gegen den Folgezustand |

- [ ] Add-on **starten**, Log öffnen. Erwartete Meldung: `HEMS orchestrator starting (mode=..., dry_run=True)`.

> **Supervisor-Credentials:** Sind `mqtt_username`/`mqtt_password` leer und `SUPERVISOR_TOKEN` gesetzt, holt der Clienthost/Port/User/Passwort über `GET http://supervisor/services/mqtt`. `config.yaml` deklariert `services: [mqtt:need]` und `hassio_api: true` dafür. Schlägt die Auflösung fehl, wird der Fehler geloggt und mit den konfigurierten Werten verbunden.

---

## 4. Live-Zustand einspeisen (MQTT)

Der Regelkern wartet auf `{prefix}/sensors/state` (`prefix` = `hems`). Beispiel-Payload (alle Felder gemäß `SystemInputs`, Einheiten: W / Wh / % / °C / ct/kWh / g/kWh):

```bash
mosquitto_pub -h core-mosquitto -u <mqtt-user> -P <mqtt-pass> \
  -t 'hems/sensors/state' -m '{
    "p_grid_before_w": 1200.0,
    "p_pv_w": 6000.0,
    "p_house_base_w": 500.0,
    "soc_battery_pct": 55.0,
    "soc_ev_pct": 42.0,
    "ev_connected": true,
    "ev_departure_in_min": 240.0,
    "t_buffer_c": 47.0,
    "tariff_ct_kwh": 28.0,
    "co2_intensity_g_kwh": 380.0,
    "dso_14a_active": false,
    "dso_14a_limit_w": 4200.0,
    "eeg_feedin_limit_pct": 60.0
  }'
```

Optional (Laufzeit-Umschaltung und § 14a-Signal):

```bash
mosquitto_pub -h core-mosquitto -u <mqtt-user> -P <mqtt-pass> -t 'hems/control/mode' -m 'C_GRID_SUPPORTIVE'
mosquitto_pub -h core-mosquitto -u <mqtt-user> -P <mqtt-pass> -t 'hems/control/dry_run' -m 'true'
mosquitto_pub -h core-mosquitto -u <mqtt-user> -P <mqtt-pass> -t 'hems/dso/14a_signal' -m '{"active": true, "limit_w": 4200.0}'
```

> **`dso/14a_signal` überschreibt sofort** den aktuellen und alle folgenden Zustände (`LiveState.apply_dso_signal`). Zum Zurücksetzen `{"active": false, "limit_w": 4200.0}` publizieren.

---

## 5. Erfolgskontrolle (Shadow-Mode)

- [ ] **Keine** Nachricht auf `hems/cmd/#` – das ist die wichtigste Sicherheitsprüfung:

```bash
mosquitto_sub -h core-mosquitto -u <mqtt-user> -P <mqtt-pass> -t 'hems/#' -v
```

Erwartete Ausgänge (ausschließlich Shadow-Topics + Diagnose):

```
hems/shadow/cmd/wallbox_w 4200.0
hems/shadow/cmd/battery_w -400.0
hems/shadow/cmd/heatpump_sg_ready normal
hems/status/last_decision {"event_type":"decision",...}
```

- [ ] Log-Dateien im Add-on-Container vorhanden:

```bash
docker exec -it <container> sh -c 'ls -l /data/logs /data/db; head -1 /data/logs/decision_trace.jsonl'
```

Erwartung: `decision_trace.jsonl` enthält `DecisionTraceRecord`s; `hems_audit.db` liegt im WAL-Modus.

- [ ] Wirkungsnachweise: Nach `verification_delay_s` erscheinen `event_type:"verification"`-Zeilen, und in SQLite sind `verified_at_utc`/`compliance_ok` gefüllt (1:1-Fremdschlüssel je geloggtem Decision).

- [ ] KPI-Export aus der echten DB:

```bash
docker exec <container> python3 -m hems.hil.kpi_calculator \
  --db /data/db/hems_audit.db --out /data/report.json --mode C_GRID_SUPPORTIVE
```

---

## 6. Simulation-to-Reality Gap messen (Kern der Arbeit)

1. Shadow-Lauf mit **Modus A** fahren (MQTT `hems/control/mode` → `A_BASELINE`), KPI-Export als `report_A.json`.
2. Shadow-Lauf mit **Modus B** fahren → `report_B.json` – **hier kommen die echten MILP-Kosten des EMHASS-Add-ons** an, nicht die Offline-Heuristik aus `docs/benchmark`.
3. Shadow-Lauf mit **Modus C** fahren → `report_C.json`.
4. Vergleichen: `total_energy_cost_eur`, `peak_grid_import_w`, `total_co2_kg`, `dso_14a_compliance_rate_pct`.

**Erwarteter Erkenntnisgewinn:** Im synthetischen Benchmark war Modus B teurer als A (Heuristik-Artefakt). Der echte EMHASS-MILP sollte das anders einordnen; weicht er ab, ist das ein dokumentierbarer, begründbarer Gap zwischen Surrogatmodell und Real-Optimierer.

> Der Benchmark-Runner (`python -m hems.hil.benchmark`) bleibt der reproduzierbare Referenzvergleich; der Live-Lauf liefert die Validierung daran. Beide nutzen dieselben KPI-Definitionen (C2-Integral, K2-Saldierung).

---

## 7. Troubleshooting

| Symptom | Ursache / Maßnahme |
|---|---|
| Keine MQTT-Verbindung | `core-mosquitto` falsch? Supervisor-API-Credentials prüfen; `mqtt:need` in `config.yaml` vorhanden? |
| EMHASS `status="error"` im Log | `emhass_url` prüfen (Add-on-IP!), Endpunkt `POST /action/naive-mpc-optim` erreichbar? |
| `status="timeout"` | `emhass_timeout_s` erhöhen (Default 15 s); Solver dauert beim ersten Lauf länger. |
| Prio 4 trotz EMHASS | Setpoints älter als `mpc_interval_s + 60 s` → MPC-Loop prüfen. |
| Keine Verifikation in SQLite | Nur **geloggte** Entscheidungen werden verifiziert (K4); Heartbeat/Dedup beachten (`audit_heartbeat_s`). |
| Nachrichten auf `hems/cmd/#` | **Sofort stoppen!** `dry_run` ist nicht `true` – Shadow-Mode-Regel verletzt. |
| Batterie lädt falsch herum | `P_batt`-Vorzeichen: EMHASS `P_batt > 0` = Entladung → Parser invertiert zwingend (`battery_w = -P_batt`). |

---

## 8. Safety-Netze (Erinnerung)

- `dry_run: true` → ausschließlich `hems/shadow/cmd/...` (AGENTS.md §3.3).
- Wallbox-Zwischenwerte unter 1380 W → deterministisch 0 W (`POST_wallbox_below_min_clamped_to_0`).
- § 14a (Prio 1) überschreibt Komfort (P2), EMHASS (P3) und Fallback (P4) immer.
- Persistenz nur unter `/data/...`, Caches unter `/tmp/...` (HA-Backup-Schutz).

> **Erst nach erfolgreichem Shadow-Test** darf über einen separaten, bewusst dokumentierten Schritt auf `dry_run: false` gewechselt werden – dieser ist nicht Teil dieser Checkliste.
