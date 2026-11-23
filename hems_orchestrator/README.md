# HEMS Grid-Supportive Orchestrator

Home Assistant Add-on (Python 3.12, asyncio) zur netzdienlichen Steuerung eines Home Energy
Management Systems: zustandslose EMHASS-Kopplung, deterministische § 14a EnWG / § 9 EEG
Prioritätskaskade und revisionssicheres Dual-Logging (JSONL + SQLite).

## Sicherheits-Default

`dry_run: true` (Shadow-Mode): Es werden ausschließlich Shadow-Topics (`hems/shadow/cmd/...`)
bedient, niemals reale Aktor-Topics.

## Betriebsmodi

- `A_BASELINE`: klassischer Eigenverbrauch ohne Optimierung.
- `B_MARKET_ORIENTED`: Kostenminimierung via EMHASS.
- `C_GRID_SUPPORTIVE`: Netzdienliche Optimierung mit harten Netzrestriktionen.

## Persistenz

- Audit-DB (2-Jahres-Nachweis § 14a EnWG): `/data/db/hems_audit.db`
- Decision-Trace (rotierend): `/data/logs/decision_trace.jsonl`

## Entwicklung

```bash
pip install -e ".[dev]"
ruff check src tests
pytest -v
```

Verbindliche Spezifikationen: `docs/00_MASTER_EXECUTION_PLAN.md` bis `docs/04_QUASI_HIL_AND_VERIFICATION.md`.
