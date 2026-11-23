"""Asyncio Orchestrator Entrypoint & Lifecycle (docs/00 Dateibaum).

Verdrahtet Konfiguration, Audit-Logging, MQTT-Ingestion, EMHASS-MPC und die
1-Hz-Prioritätskaskade. Fail-Safe: dry_run=True ist der Standard (Shadow-Mode).
"""

from __future__ import annotations

import asyncio
import logging

import aiomqtt
import uvicorn

from hems.config import load_config
from hems.control.device_registry import DeviceRegistry
from hems.control.scheduler import ControlScheduler
from hems.forecast.appliance_forecaster import ForecastManager
from hems.io.emhass_client import EmhassClient
from hems.io.ha_core_client import HaCoreClient
from hems.io.mqtt_client import LiveState, resolve_mqtt_credentials, run_ingestion
from hems.logging.jsonl_logger import JsonlLogger
from hems.logging.sqlite_store import SqliteAuditStore
from hems.logging.verification import VerificationTracker
from hems.web.app import create_app

logger = logging.getLogger(__name__)


async def async_main() -> None:
    config = load_config()
    logging.basicConfig(level=config.log_level)
    logger.info(
        "HEMS orchestrator starting (mode=%s, dry_run=%s)",
        config.operating_mode.value,
        config.dry_run,
    )

    state = LiveState(config)
    jsonl_logger = JsonlLogger.from_config(config)
    sqlite_store = SqliteAuditStore.from_config(config)
    await sqlite_store.initialize()
    tracker = VerificationTracker(config, jsonl_logger, sqlite_store)
    emhass_client = EmhassClient(config)
    ha_client = HaCoreClient(config)
    registry = DeviceRegistry(config)
    await registry.initialize()
    imported = await registry.sync_washdata_devices(await ha_client.get_states())
    if imported:
        logger.info("Imported %d WashData device(s) into registry", len(imported))
    forecast_manager = ForecastManager(config, registry, ha_client, sqlite_store)
    registry.set_change_callback(forecast_manager.refresh)
    await forecast_manager.refresh()
    scheduler = ControlScheduler(
        config,
        state,
        jsonl_logger,
        sqlite_store,
        tracker,
        emhass_client,
        device_registry=registry,
        ha_client=ha_client,
        forecast_manager=forecast_manager,
    )
    app = create_app(config, scheduler, registry, ha_client, sqlite_store, forecast_manager)
    web_server = uvicorn.Server(
        uvicorn.Config(
            app,
            host="0.0.0.0",
            port=config.ingress_port,
            log_level=config.log_level.lower(),
            access_log=False,
        )
    )

    credentials = await resolve_mqtt_credentials(config)
    try:
        async with aiomqtt.Client(
            hostname=credentials.host,
            port=credentials.port,
            username=credentials.username or None,
            password=credentials.password or None,
        ) as client:
            scheduler.attach_mqtt(client)
            web_task = asyncio.create_task(web_server.serve())
            try:
                await asyncio.gather(
                    run_ingestion(client, state, config, scheduler),
                    scheduler.run(),
                    forecast_manager.run_background(),
                    forecast_manager.run_retrain_loop(),
                    web_task,
                )
            finally:
                web_server.should_exit = True
                try:
                    await asyncio.wait_for(web_task, timeout=5.0)
                except (TimeoutError, asyncio.CancelledError):
                    web_task.cancel()
    finally:
        await sqlite_store.close()


def main() -> None:
    try:
        asyncio.run(async_main())
    except KeyboardInterrupt:
        logger.info("HEMS orchestrator stopped by user")


if __name__ == "__main__":
    main()
