# Patch-Spezifikation: Verbindliche Klärungen für Phasen 5–7 (docs/06–docs/08)

Anweisung für den Coding-Agent: Übernimm alle untenstehenden Festlegungen verbindlich bei der Implementierung der Phasen 5, 6 und 7. Es sind keine weiteren Rückfragen nötig.

## 1. EMHASS-Auflösung & 24-Slot-Fahrplan-Extraktion (docs/03 & docs/07)

- Expliziter 15-Minuten-Zeitschritt in `EmhassClient.build_runtimeparams`: Ergänze `"time_step": 15` in `runtimeparams` (zusammen mit `"prediction_horizon": 24` ergibt das exakt 24 × 15 min = 6 Stunden).
- `load_power_forecast` erhält die ersten 24 Werte (`aggregated_uncontrollable_w[:24]`) aus dem `SystemForecastReport` (falls vorhanden und nicht komplett `0.0`), sonst weiterhin die konstante Liste `[inputs.p_house_base_w] * 24`.
- 24-Slot-Vektoren in `EmhassResult` (`src/hems/io/emhass_client.py`): Erweitere `EmhassResult` um optionale 24-elementige Fahrplan-Listen (Default `None` für Rückwärtskompatibilität):
  - `wallbox_schedule_w: list[float] | None = None` (aus `P_deferrable0[0:24]`, geklemmt auf ≥ 0.0)
  - `heatpump_schedule_w: list[float] | None = None` (aus `P_deferrable1[0:24]`, geklemmt auf ≥ 0.0)
  - `battery_schedule_w: list[float] | None = None` (aus `[-float(v) for v in P_batt[0:24]]` – Vorzeicheninvertierung wie bei t=0!)
- `ApplianceForecaster` übernimmt in Stufe 2 (`MPC_SCHEDULE`) genau diese 24 Werte für die ersten 24 Slots (0..23) und setzt für die restlichen 72 Slots (24..95) `STATIC_BASELINE` (`expected_power_w = device.min_power_w`, `confidence = 0.25`).

## 2. Mode- & Dry-Run-Synchronisation (docs/03 & docs/08)

- Synchrones Update bei Laufzeit-Umschaltung (MQTT und REST-API): Sowohl `LiveState` als auch die aktive `AppConfig`-Referenz in `EmhassClient` und `ControlScheduler` müssen aktualisiert werden (z. B. über `scheduler.set_operating_mode(mode)` und `scheduler.set_dry_run(dry_run)`).
- Read-Only-Property auf `ControlScheduler`: `@property def last_decision(self) -> DecisionTraceRecord | None: return self._last_logged` für `GET /api/dashboard`.

## 3. Forecast-Aktualisierungsfrequenz & Caching (docs/07 & docs/08)

- Zentrale Manager-Klasse `ForecastManager` (`src/hems/forecast/appliance_forecaster.py`), die den aktuellen `SystemForecastReport` im Speicher hält.
- Neuberechnung (inkl. `HaCoreClient.get_states()` für Live-WashData-Status) erfolgt deterministisch:
  - beim Add-on-Start,
  - unmittelbar nach jeder Änderung in der `DeviceRegistry` (`upsert_device`, `delete_device`, `set_enabled`, `sync_washdata_devices`),
  - unmittelbar nach jedem `PatternLearner`-Lauf (Periodisch alle `config.pattern_retrain_interval_s`),
  - alle 60 s im Hintergrund-Loop (hält laufende WashData-Restlaufzeiten aktuell),
  - unmittelbar vor jedem 15-min-MPC-Lauf (`mpc_interval_s`).
- `GET /api/forecasts` liefert den gecachten Report sofort ohne Wartezeit aus (oder berechnet ihn initial, falls noch `None`).

## 4. FastAPI App-Factory, Routing & Ingress-Schutz (docs/08)

- App-Factory (`src/hems/web/app.py`): `create_app(config, scheduler, device_registry, ha_client, sqlite_store, forecast_manager) -> FastAPI`.
- `uvicorn.Server` wird ausschließlich in `src/hems/main.py` gestartet, nicht in `create_app(...)`.
- Serverseitig: Standard-Routen mit führendem Slash (`@app.get("/")`, `@app.get("/api/dashboard")`, ...). Clientseitig ausschließlich relative Pfade ohne führenden Slash (`fetch("api/dashboard")`).
- Ingress-Zugriffsschutz: `AppConfig.ingress_only: bool = Field(default=True)`. Middleware erlaubt nur Clients mit `client.host in {"172.30.32.2", "127.0.0.1", "::1", "testclient"}`; alle anderen erhalten HTTP 403.
