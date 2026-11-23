"""Gemeinsame Test-Fixtures (Phase 1: Konfiguration)."""

from __future__ import annotations

from typing import Any

import pytest


@pytest.fixture
def valid_options() -> dict[str, Any]:
    """Valides Options-Dict analog ``config.yaml -> options`` (docs/01)."""
    return {
        "operating_mode": "C_GRID_SUPPORTIVE",
        "dry_run": True,
        "log_level": "INFO",
        "mqtt_host": "core-mosquitto",
        "mqtt_port": 1883,
        "mqtt_username": "",
        "mqtt_password": "",
        "mqtt_topic_prefix": "hems",
        "emhass_url": "http://a0d7b954-emhass:5000",
        "emhass_timeout_s": 15.0,
        "mpc_interval_s": 900,
        "control_loop_interval_s": 1.0,
        "verification_delay_s": 10.0,
        "house_fuse_limit_w": 22000.0,
        "pv_peak_w": 10000.0,
        "eeg_feedin_limit_pct": 60.0,
        "dso_14a_min_power_w": 4200.0,
        "battery_capacity_wh": 10000.0,
        "battery_max_charge_w": 5000.0,
        "battery_max_discharge_w": 5000.0,
        "battery_cycle_cost_ct_kwh": 5.0,
        "wallbox_max_power_w": 11000.0,
        "wallbox_min_power_w": 1380.0,
        "heatpump_nominal_power_w": 2500.0,
        "heatpump_temp_min_c": 45.0,
        "heatpump_temp_max_c": 58.0,
        "ev_target_soc_pct": 80.0,
        "ev_capacity_wh": 60000.0,
        "jsonl_max_bytes": 10485760,
        "jsonl_backup_count": 10,
        "audit_heartbeat_s": 60,
    }
