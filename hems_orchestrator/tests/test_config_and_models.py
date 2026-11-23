"""Phase-1-Gate: Konfigurations-Loader & AppConfig (docs/01, Akzeptanzkriterien 1–5)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from hems.config import AppConfig, OperatingMode, load_config


def _write_options(tmp_path: Path, options: dict[str, Any]) -> Path:
    options_path = tmp_path / "options.json"
    options_path.write_text(json.dumps(options), encoding="utf-8")
    return options_path


def test_load_config_valid_json_file(tmp_path: Path, valid_options: dict[str, Any]) -> None:
    """Akzeptanzkriterium 1: valides JSON-File wird fehlerfrei geladen."""
    options_path = _write_options(tmp_path, valid_options)
    config = load_config(options_path)
    assert config.operating_mode is OperatingMode.C_GRID_SUPPORTIVE
    assert config.mqtt_host == "core-mosquitto"
    assert config.mpc_interval_s == 900
    assert config.wallbox_min_power_w == 1380.0
    assert config.audit_heartbeat_s == 60


def test_dry_run_default_is_true() -> None:
    """Akzeptanzkriterium 2: dry_run ist zwingend standardmäßig True (Fail-Safe)."""
    assert AppConfig().dry_run is True
    assert AppConfig.model_validate({}).dry_run is True


def test_invalid_dso_14a_min_power_raises(valid_options: dict[str, Any]) -> None:
    """Akzeptanzkriterium 3a: dso_14a_min_power_w < 4200 W wird abgelehnt."""
    with pytest.raises(ValidationError):
        AppConfig.model_validate({**valid_options, "dso_14a_min_power_w": 3000.0})


def test_invalid_heatpump_temperature_band_raises(valid_options: dict[str, Any]) -> None:
    """Akzeptanzkriterium 3b: min-Temperatur >= max-Temperatur wird abgelehnt."""
    with pytest.raises(ValidationError):
        AppConfig.model_validate(
            {**valid_options, "heatpump_temp_min_c": 60.0, "heatpump_temp_max_c": 50.0}
        )


def test_env_data_dir_override(
    tmp_path: Path,
    valid_options: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Akzeptanzkriterium 4: HEMS_DATA_DIR überschreibt den geladenen Wert."""
    options_path = _write_options(tmp_path, valid_options)
    override_dir = tmp_path / "test_data"
    monkeypatch.setenv("HEMS_DATA_DIR", str(override_dir))
    config = load_config(options_path)
    assert config.data_dir == override_dir


def test_env_dry_run_override(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """ENV-Override HEMS_DRY_RUN=false wird als Bool geparst."""
    monkeypatch.setenv("HEMS_DRY_RUN", "false")
    config = load_config(tmp_path / "nonexistent_options.json")
    assert config.dry_run is False


def test_missing_options_file_uses_defaults(tmp_path: Path) -> None:
    """Ohne options.json (lokale Entwicklung/Tests) gelten die Defaults."""
    config = load_config(tmp_path / "nonexistent_options.json")
    assert config == AppConfig()


def test_options_path_from_env(
    tmp_path: Path,
    valid_options: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """HEMS_OPTIONS_PATH bestimmt den Standardpfad der Options-Datei."""
    options_path = _write_options(tmp_path, valid_options)
    monkeypatch.setenv("HEMS_OPTIONS_PATH", str(options_path))
    config = load_config()
    assert config.operating_mode is OperatingMode.C_GRID_SUPPORTIVE


def test_extra_fields_are_forbidden(valid_options: dict[str, Any]) -> None:
    """Unbekannte Felder werden strikt abgelehnt (extra='forbid')."""
    with pytest.raises(ValidationError):
        AppConfig.model_validate({**valid_options, "unknown_field": 1})


def test_wallbox_min_above_max_raises(valid_options: dict[str, Any]) -> None:
    """Cross-Field-Validator: wallbox_min_power_w <= wallbox_max_power_w."""
    with pytest.raises(ValidationError):
        AppConfig.model_validate(
            {**valid_options, "wallbox_min_power_w": 4200.0, "wallbox_max_power_w": 3000.0}
        )


def test_audit_heartbeat_bounds(valid_options: dict[str, Any]) -> None:
    """audit_heartbeat_s: Default 60, Grenzen ge=5 / le=3600 (Fix C3)."""
    assert AppConfig().audit_heartbeat_s == 60
    with pytest.raises(ValidationError):
        AppConfig.model_validate({**valid_options, "audit_heartbeat_s": 4})
    with pytest.raises(ValidationError):
        AppConfig.model_validate({**valid_options, "audit_heartbeat_s": 3601})
