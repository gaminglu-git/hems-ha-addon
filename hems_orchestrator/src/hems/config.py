"""Strikt validierter Konfigurations-Loader (docs/01, Phase 1).

Funktioniert identisch im Home Assistant Supervisor (``/data/options.json``) und
lokal in Tests/CI (JSON-Datei bzw. Defaults + ``HEMS_``-Umgebungsvariablen).
"""

from __future__ import annotations

import json
import os
from enum import Enum
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, model_validator


class OperatingMode(str, Enum):
    """Betriebsmodi des HEMS-Orchestrators (docs/01)."""

    A_BASELINE = "A_BASELINE"
    B_MARKET_ORIENTED = "B_MARKET_ORIENTED"
    C_GRID_SUPPORTIVE = "C_GRID_SUPPORTIVE"


class AppConfig(BaseModel):
    """Vollständige Add-on-Konfiguration; spiegelt exakt ``config.yaml -> options``."""

    model_config = ConfigDict(extra="forbid")

    operating_mode: OperatingMode = OperatingMode.C_GRID_SUPPORTIVE
    dry_run: bool = True
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"

    mqtt_host: str = "core-mosquitto"
    mqtt_port: int = Field(default=1883, ge=1, le=65535)
    mqtt_username: str = ""
    mqtt_password: str = ""
    mqtt_topic_prefix: str = "hems"

    emhass_url: str = "http://a0d7b954-emhass:5000"
    emhass_timeout_s: float = Field(default=15.0, ge=1.0, le=60.0)
    mpc_interval_s: int = Field(default=900, ge=60, le=3600)
    control_loop_interval_s: float = Field(default=1.0, ge=0.5, le=5.0)
    verification_delay_s: float = Field(default=10.0, ge=1.0, le=60.0)

    house_fuse_limit_w: float = Field(default=22000.0, ge=3000.0, le=63000.0)
    pv_peak_w: float = Field(default=10000.0, ge=0.0, le=100000.0)
    eeg_feedin_limit_pct: float = Field(default=60.0, ge=0.0, le=100.0)
    dso_14a_min_power_w: float = Field(default=4200.0, ge=4200.0, le=22000.0)

    battery_capacity_wh: float = Field(default=10000.0, ge=0.0, le=100000.0)
    battery_max_charge_w: float = Field(default=5000.0, ge=0.0, le=25000.0)
    battery_max_discharge_w: float = Field(default=5000.0, ge=0.0, le=25000.0)
    battery_cycle_cost_ct_kwh: float = Field(default=5.0, ge=0.0, le=50.0)

    wallbox_max_power_w: float = Field(default=11000.0, ge=0.0, le=22000.0)
    wallbox_min_power_w: float = Field(default=1380.0, ge=0.0, le=4200.0)

    heatpump_nominal_power_w: float = Field(default=2500.0, ge=0.0, le=15000.0)
    heatpump_temp_min_c: float = Field(default=45.0, ge=20.0, le=60.0)
    heatpump_temp_max_c: float = Field(default=58.0, ge=30.0, le=75.0)

    ev_target_soc_pct: float = Field(default=80.0, ge=10.0, le=100.0)
    ev_capacity_wh: float = Field(default=60000.0, ge=10000.0, le=150000.0)

    jsonl_max_bytes: int = Field(default=10485760, ge=1048576, le=104857600)
    jsonl_backup_count: int = Field(default=10, ge=1, le=50)
    audit_heartbeat_s: int = Field(default=60, ge=5, le=3600)

    ha_api_url: str = Field(default="http://supervisor/core/api")
    ha_token: str = Field(default="")
    ingress_port: int = Field(default=8099, ge=1024, le=65535)
    pattern_lookback_days: int = Field(default=14, ge=3, le=60)
    pattern_retrain_interval_s: float = Field(default=21600.0, ge=300.0, le=86400.0)
    auto_discover_washdata: bool = Field(default=True)
    timezone: str = Field(default="Europe/Berlin", min_length=1)
    ingress_only: bool = Field(default=True)

    # Externe Markt-/Netz-APIs (docs/09, Phase 8). Default "energy_charts" ist
    # rückwärtskompatibel: ohne Netz greift der MQTT-Fallback (tariff_ct_kwh).
    tariff_source: Literal["mqtt_only", "energy_charts", "awattar", "ha_sensor"] = "energy_charts"
    tariff_grid_fees_ct_kwh: float = Field(default=18.0, ge=0.0, le=60.0)
    tariff_vat_pct: float = Field(default=19.0, ge=0.0, le=30.0)
    ha_tariff_entity_id: str | None = None
    market_refresh_interval_s: float = Field(default=1800.0, ge=300.0, le=86400.0)

    data_dir: Path = Path("/data")
    tmp_dir: Path = Path("/tmp/hems_cache")

    @model_validator(mode="after")
    def _validate_cross_field_constraints(self) -> AppConfig:
        if self.heatpump_temp_min_c >= self.heatpump_temp_max_c:
            raise ValueError(
                "heatpump_temp_min_c must be < heatpump_temp_max_c "
                f"({self.heatpump_temp_min_c} >= {self.heatpump_temp_max_c})"
            )
        if self.wallbox_min_power_w > self.wallbox_max_power_w:
            raise ValueError(
                "wallbox_min_power_w must be <= wallbox_max_power_w "
                f"({self.wallbox_min_power_w} > {self.wallbox_max_power_w})"
            )
        if self.dso_14a_min_power_w < 4200.0:
            raise ValueError(
                "dso_14a_min_power_w must be >= 4200.0 W "
                "(regulatorische Untergrenze nach § 14a EnWG / BNetzA BK6-22-300)"
            )
        try:
            ZoneInfo(self.timezone)
        except (KeyError, ValueError, TypeError) as exc:
            raise ValueError(f"invalid timezone: {self.timezone!r}") from exc
        return self


_ENV_PREFIX = "HEMS_"
_ENV_OPTIONS_PATH = "HEMS_OPTIONS_PATH"


def load_config(options_path: Path | None = None) -> AppConfig:
    """Lädt die Konfiguration aus options.json (falls vorhanden) plus ENV-Overrides.

    - Standardpfad: ``HEMS_OPTIONS_PATH`` oder ``/data/options.json``.
    - Datei vorhanden: JSON laden und mit ``AppConfig`` validieren.
    - Datei nicht vorhanden (lokale Entwicklung/Tests): Defaults verwenden.
    - Umgebungsvariablen ``HEMS_<FELDNAME>`` überschreiben geladene Werte gezielt
      (z. B. ``HEMS_DRY_RUN=false``, ``HEMS_DATA_DIR=/tmp/test_data``).
    """
    if options_path is None:
        options_path = Path(os.getenv(_ENV_OPTIONS_PATH, "/data/options.json"))

    data: dict[str, object] = {}
    if options_path.exists():
        data = json.loads(options_path.read_text(encoding="utf-8"))

    for key, value in os.environ.items():
        if key.startswith(_ENV_PREFIX) and key != _ENV_OPTIONS_PATH:
            field_name = key[len(_ENV_PREFIX) :].lower()
            if field_name in AppConfig.model_fields:
                data[field_name] = value

    return AppConfig.model_validate(data)
