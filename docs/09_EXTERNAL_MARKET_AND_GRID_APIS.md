# 09 – Phase 8: Bewertung & Integration externer Strompreis- und Netz-APIs

## Ziel dieser Phase

Schließung der Lücke in der prädiktiven Optimierung der Modi B und C durch externe Day-Ahead-Strompreis- und Erneuerbaren-Prognosen. Ergänzend zur MQTT-Eingabe wird ein zentraler, asynchroner und cachender `MarketDataClient` eingeführt, der dem `EmhassClient` einen zeitabhängigen 24-Slot-Kostenfahrplan und eine ebenfalls zeitabhängige CO₂-Intensität liefert.

**Gate-Vorbedingung:** Phase 7 (`docs/08`) ist implementiert und grün. Alle **127 Tests** aus den Phasen 1–7 bleiben unverändert grün. Keine Tests abschwächen, skippen oder Assertions entfernen. Keine echten Netzwerkaufrufe in Tests (immer `respx`-Mocks).

**Pfad-Hinweis:** Alle Add-on-Dateien liegen in `hems_orchestrator/`. Gate-Kommandos werden dort ausgeführt.

---

## 1. Gesamtfazit

**Ja, uneingeschränkt sinnvoll** – die Integration schließt eine wesentliche Lücke in der prädiktiven Optimierung (Modi B und C).

Aktuell erhält `EmhassClient.build_runtimeparams(...)` ohne externen Prognosevektor lediglich den aktuellen Momentanpreis (`inputs.tariff_ct_kwh`) als konstanten Wert über alle 24 Zeitschritte (6 Stunden). Bei einer flachen Preiskurve hat der MILP-Solver in Modus B (`B_MARKET_ORIENTED`, § 41a EnWG) keinen Anreiz, das Laden von Elektrofahrzeug oder Batteriespeicher in günstige Nacht- oder Mittagsstunden zu verschieben.

Mit einer Day-Ahead-Preis- und Erneuerbaren-Prognose gewinnt das Add-on dreifach:

1. **Echte Lastverschiebung in Modus B (§ 41a EnWG):** EMHASS erkennt Preisspitzen (z. B. 18:00–21:00 Uhr) sowie Niedrigpreis-/Negativpreisfenster (z. B. mittags bei hoher PV-/Windeinspeisung) und plant Ladevorgänge vorausschauend.
2. **Realistisches netz- und CO₂-dienliches Signal in Modus C:** Über den prognostizierten EE-Anteil lässt sich die zeitabhängige CO₂-Intensität dynamisch ableiten statt eines statischen Tageswerts.
3. **Wissenschaftliche Belastbarkeit:** Etablierte deutsche Primärdatenquellen (Fraunhofer ISE / EPEX Spot) machen Experimente im Realbetrieb und Shadow-Mode reproduzier- und zitierbar.

---

## 2. Bewertung der einzelnen APIs

| API / Anbieter | Eignung | Bewertung & Rolle im Projekt |
|---|---|---|
| **Energy-Charts (Fraunhofer ISE)** | Sehr hoch (Primärquelle) | **Empfehlung Nr. 1.** Kein API-Key, hohe Verfügbarkeit, wissenschaftlich erstklassig. Liefert beide Datensätze aus einer Hand: Day-Ahead-Börsenpreise (`/price?bzn=DE-LU`) und EE-Anteil-Prognose (`/ren_share_forecast?country=de`). |
| **aWATTar** (`api.awattar.de`) | Hoch (Fallback) | **Empfehlung Nr. 2.** Ebenfalls ohne API-Key, kompaktes JSON (`/v1/marketdata`). Automatischer Fallback bei kurzzeitiger Nicht-Erreichbarkeit von Energy-Charts. |
| **Tibber / Octopus Energy** | Hoch (im Realbetrieb) | **Empfehlung Nr. 3** (über HA-Sensor). Bei bestehendem Kundenkonto in HA liefert deren Sensor den exakten Brutto-Endkundenpreis inkl. regionaler Netzentgelte (ggf. zeitvariable Netzentgelte nach § 14a Modul 3). |
| **SMARD (Bundesnetzagentur)** | Mittel (nur Offline) | Gut für historische Referenzdatensätze (Kapitel 4/5 der Arbeit). Für den Live-Abruf wegen zweistufiger Zeitstempel-Pagination unnötig komplex. |
| **ENTSO-E Transparency** | Gering (Live-Add-on) | Registrierung/API-Key nötig, XML. Für DE-LU identisch zu Energy-Charts. |
| **Elecz** | Gering | Kleiner Drittanbieter-Wrapper; für eine wissenschaftliche Arbeit sind Primärquellen zu bevorzugen. |

---

## 3. Verbindliche Praxisregeln

### 3.1 Großhandelspreis → Endkundenpreis (§ 41a EnWG)

Energy-Charts und aWATTar liefern den Netto-Großhandelspreis in **EUR/MWh** (Division durch 10 ergibt ct/kWh). Netzentgelte, Umlagen, Steuern und MwSt werden aufgeschlagen:

$$
p_{\text{brutto}}[\text{ct/kWh}] = \left(\frac{p_{\text{spot}}[\text{EUR/MWh}]}{10} + c_{\text{fix}}[\text{ct/kWh}]\right)\cdot\left(1 + \frac{\text{MwSt}[\%]}{100}\right)
$$

- Typischer Richtwert Deutschland: `c_fix ≈ 16,0 … 20,0 ct/kWh`, Default `18.0`; `MwSt = 19 %`.
- Beispiel: `-100 EUR/MWh = -10 ct/kWh` → `(-10 + 18) · 1,19 = 9,52 ct/kWh` (kein unrealistischer Negativpreis).
- Für extreme Werte ist deterministisch auf `>= 0.0 ct/kWh` zu clampen (siehe Abschnitt 8, offener Punkt 1).

### 3.2 Day-Ahead-Verfügbarkeit ab ca. 13:00–14:00 Uhr

- EPEX-Day-Ahead-Auktion täglich 12:00 Uhr; Preise für den Folgetag ab ca. 13:00–14:00 Uhr verfügbar.
- **Vor 13:30 Uhr:** Daten reichen bis heute 23:45 Uhr. Reicht der 6-Stunden-Horizont von EMHASS über Mitternacht hinaus und fehlen morgige Preise, wird der **letzte bekannte Slot fortgeschrieben**.
- **Caching:** Day-Ahead-Preise ändern sich nur einmal täglich. Abruf alle **1800 s** oder bei Cache-Miss. **Kein HTTP-Request pro 1-Hz-Zyklus oder pro MPC-Lauf ohne Cache.**

### 3.3 15-Minuten-Auflösung

- Energy-Charts liefert 15-Minuten-Slots (aWATTar nur Stundenwerte).
- Mapping auf die 24 Zeitschritte von EMHASS (`time_step: 15`) erfolgt zeitlich über den **UTC-Startzeitstempel** jedes Slots.
- Bei Stundenwerten gilt derselbe Wert für alle vier 15-Minuten-Viertelstunden.

---

## 4. Konfiguration & Manifest

### 4.1 `AppConfig` (`src/hems/config.py`)

```python
tariff_source: Literal["mqtt_only", "energy_charts", "awattar", "ha_sensor"] = "energy_charts"
tariff_grid_fees_ct_kwh: float = Field(default=18.0, ge=0.0, le=60.0)
tariff_vat_pct: float = Field(default=19.0, ge=0.0, le=30.0)
ha_tariff_entity_id: str | None = None
market_refresh_interval_s: float = Field(default=1800.0, ge=300.0, le=86400.0)
```

Default `tariff_source="energy_charts"` ist **rückwärtskompatibel**: Ohne Netzwerk (z. B. in Tests außerhalb von HA) greift der Fallback auf den MQTT-Momentanpreis, sodass alle 127 Bestandstests unverändert grün bleiben.

### 4.2 `config.yaml`

- `options`: neue Felder mit obigen Defaults, `schema`:
  - `tariff_source: list(mqtt_only|energy_charts|awattar|ha_sensor)`
  - `tariff_grid_fees_ct_kwh: float(0.0,60.0)`
  - `tariff_vat_pct: float(0.0,30.0)`
  - `ha_tariff_entity_id: str?`
  - `market_refresh_interval_s: float(300.0,86400.0)`
- Add-on-Version von `"0.1.1"` auf **`"0.1.2"`** erhöhen (HA Store erkennt das Update und baut das Image neu).

---

## 5. `MarketDataClient` (`src/hems/io/market_client.py`)

### 5.1 Datenmodelle (`ConfigDict(extra="forbid", frozen=True)`)

```python
class MarketSlot(BaseModel):
    start_utc: str                                  # ISO-8601 UTC, 15-Minuten-Raster
    spot_eur_mwh: float
    gross_ct_kwh: float
    renewable_share_pct: float | None = None
    co2_intensity_g_kwh: float | None = None


class MarketForecast(BaseModel):
    generated_at_utc: str
    source: Literal["energy_charts", "awattar", "ha_sensor", "cache"]
    slots: list[MarketSlot]                          # 24 Einträge (6 h)
    load_cost_forecast_eur_kwh: list[float]          # 24 Werte für EMHASS
    co2_forecast_g_kwh: list[float]                  # 24 Werte für EMHASS
```

### 5.2 Abruf- und Fallback-Kette

1. `tariff_source == "ha_sensor"`: `HaCoreClient.get_states()` → Entität `config.ha_tariff_entity_id`, numerischer State (ct/kWh); gilt konstant für alle 24 Slots. CO₂ aus MQTT falls vorhanden, sonst Fallback-Konstante.
2. `tariff_source == "energy_charts"`: asynchron `GET https://api.energy-charts.info/price?bzn=DE-LU` **und** `GET https://api.energy-charts.info/ren_share_forecast?country=de`.
3. Bei Fehler/timeout: automatischer Fallback `GET https://api.awattar.de/v1/marketdata` (Stundenwerte → je 4 Viertelstunden); EE-Anteil entfällt → CO₂ aus letztem Cache bzw. MQTT-Wert.
4. Bei erneutem Fehler: letzter gecachter Vektor (`source="cache"`) verwenden; falls noch nie vorhanden, leerer/`None`-Vektor → `EmhassClient` nutzt unverändert den MQTT-Wert.

- `httpx.AsyncClient(timeout=10.0)`, `asyncio.Lock` gegen parallele Refreshs, `asyncio.to_thread` für CPU-lange Aufbereitung.
- Cache im Speicher + `last_success_monotonic_ns`; Refresh nur bei Ablauf von `config.market_refresh_interval_s` oder explizitem `force=True`.
- Alle Fehler werden strukturiert geloggt; kein `except Exception: pass`.

### 5.3 Aufbereitung (deterministisch)

- `gross_ct_kwh = max(0.0, (spot_eur_mwh / 10.0 + config.tariff_grid_fees_ct_kwh) * (1 + config.tariff_vat_pct / 100.0))`
- `co2_g_kwh = max(40.0, 650.0 * (1 - renewable_share_pct / 100.0))`
- `load_cost_forecast_eur_kwh[i] = gross_ct_kwh[i] / 100.0`; im Modus C zusätzlich `+ co2_g_kwh[i] / 1000.0 * 0.05` (identischer CO₂-Kostenfaktor wie heute, nur zeitabhängig).
- Sortierung der Slots nach UTC-Startzeitpunkt aufsteigend; Duplikate determination via letztem Wert.

---

## 6. Kopplung an LiveState, EmhassClient & Scheduler

### 6.1 LiveState

`LiveState` besitzt keine eigenen `tariff_ct_kwh`/`co2_intensity_g_kwh`-Felder – diese liegen in `state.inputs` (`SystemInputs`, frozen). Verbindlich:

- Neue Methode `LiveState.apply_market_forecast(forecast: MarketForecast) -> None`: setzt `self.inputs = self.inputs.model_copy(update={"tariff_ct_kwh": ..., "co2_intensity_g_kwh": ...})` mit dem **ersten Slot** des Vektors (aktueller Zeitschritt).
- Optional ergänzende Read-only-Properties `tariff_ct_kwh` und `co2_intensity_g_kwh`, die aus `inputs` lesen (keine Datenverdopplung).
- Reihenfolge: Marktdaten zuerst in `LiveState`, danach `SystemInputs`-Snapshot an Kaskade und EMHASS.

### 6.2 EmhassClient (rückwärtskompatibel)

`build_runtimeparams(inputs, config, aggregated_load_w=None, market_forecast=None)`:

- Neuer optionaler Parameter `market_forecast: MarketForecast | None = None`.
- Bei `None` bleibt das erzeugte Dict **bitgleich** (alle 127 Bestandstests, insbesondere Hash-/Parameter-Assertions, bleiben grün).
- Bei gesetztem Forecast: `load_cost_forecast` und (Modus C) die CO₂-Gewichtung aus dem 24-Slot-Vektor; `prod_price_forecast` bleibt unverändert.
- `EmhassClient.optimize(inputs, aggregated_load_w=None, market_forecast=None)` reicht den Forecast durch.

### 6.3 Scheduler & Markt-Loop

- `ControlScheduler.__init__` erhält optional `market_client: MarketDataClient | None = None`.
- Neue `asyncio.Task` im selben Event-Loop (in `main.py`): alle `market_refresh_interval_s` `await market_client.refresh(force=False)`; beim Start einmal `force=True`.
- `mpc_cycle()` holt den gecachten Forecast (`market_client.get_forecast()`, **kein** HTTP im MPC-Pfad) und übergibt ihn an `optimize(...)`.
- `POST /api/market/refresh` (optional, UI-Button) erzwingt einen Refresh und gibt `{"status":"ok","source":...}` zurück.

---

## 7. Verzeichnisstruktur & Tests

**Neue Dateien:**

```text
hems_orchestrator/src/hems/io/market_client.py
hems_orchestrator/tests/test_market_client.py
```

**Geänderte Dateien:** `config.py`, `config.yaml`, `io/mqtt_client.py`, `io/emhass_client.py`, `control/scheduler.py`, `main.py`, optional `web/app.py` + `web/static/index.html` (Marktstatus/Preischart in Tab 1).

**Akzeptanzkriterien (Phase-8-Gate):**

| # | Kriterium | Erwartung |
|---|---|---|
| D1 | Energy-Charts-Preise | `respx`-Mock liefert `price` + `ren_share_forecast`; 24 Slots mit korrektem Brutto ct/kWh nach Formel |
| D2 | EE → CO₂ | `r_EE=100 %` → `40 g/kWh` (Untergrenze), `r_EE=0 %` → `650 g/kWh` |
| D3 | Negative Preise | `-100 EUR/MWh` → `9,52 ct/kWh` (c_fix=18, MwSt=19) |
| D4 | Fallback | Energy-Charts 500 → aWATTar wird aufgerufen; Stundenwerte → 4 gleiche 15-Min-Slots |
| D5 | Offline | Beide Quellen down → letzter gecachter Vektor (`source="cache"`), sonst `None` → MQTT-Wert; kein Crash |
| D6 | Caching | 2 Refreshes innerhalb von 1800 s → nur **ein** HTTP-Aufruf-Paar; `force=True` erzwingt neuen |
| D7 | Rückwärtskompatibilität | `build_runtimeparams` ohne Forecast bitgleich; alle 127 Bestandstests grün |
| D8 | Gate | `ruff check src tests` und `pytest -v` Exit-Code 0, 0 Warnings |

---

## 8. Offene Punkte / Entscheidungsbedarf

1. **Negativpreis-Clamp:** Die Vorgabe „keine unrealistischen negativen Endkundenpreise" wird hier als `max(0.0, …)` interpretiert. Das entfernt zwar das negative Signal für den Solver, verhindert aber negative Endkundenkosten. Alternativ: negatives Signal für die Optimierung behalten und nur in der Abrechnung clampen. Klärung vor Implementierung.

2. **Zeitbasis des 15-Minuten-Mappings:** Verbindlich ist der UTC-Startzeitstempel (Abschnitt 3.3). Offen ist, ob der erste Slot „jetzt abgerundet auf 15 min" oder der nächste volle 15-Minuten-Slot ist. Empfehlung: `now.floor("15min")`, damit der laufende Slot berücksichtigt wird.

3. **Antwortschema `ren_share_forecast`:** Die Energy-Charts-Antwort kann EE-Werte direkt oder unter einem `forecast`-Unterarray liefern. Der Parser ist defensiv für beide Formen auszulegen; ungültige Werte werden strukturiert geloggt und als `None` behandelt.

4. **HA-Tarif-Sensor Einheit:** Für `ha_sensor` ist zu klären, ob der Sensor `ct/kWh` (Tibber/Octopus Brutto) oder `EUR/MWh` liefert. Empfehlung: `ct/kWh` als Default, optionaler Hinweis im UI.

5. **Cache-Persistenz:** Der Cache ist zunächst nur im Speicher (verloren bei Neustart). Falls der letzte gecachte Vektor einen Add-on-Neustart überstehen soll, ist eine Datei unter `/data/...` nötig – nicht spezifiziert, daher zurückgestellt.

6. **Package-/Add-on-Version:** Wie beim letzten Fix nur `config.yaml` (`0.1.2`) hochzuziehen oder zusätzlich die Python-Paketversion in `pyproject.toml` – bitte mit dem Nutzer abstimmen.
