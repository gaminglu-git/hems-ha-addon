"""Asyncio Orchestrator Entrypoint & Lifecycle (docs/00 Dateibaum).

Verdrahtet Konfiguration, Audit-Logging, MQTT-Ingestion, EMHASS-MPC und die
1-Hz-Prioritätskaskade. Fail-Safe: dry_run=True ist der Standard (Shadow-Mode).
"""

from __future__ import annotations

import asyncio
import logging

import aiomqtt

from hems.config import load_config
from hems.control.scheduler import ControlScheduler
from hems.io.emhass_client import EmhassClient
from hems.io.mqtt_client import LiveState, resolve_mqtt_credentials, run_ingestion
from hems.logging.jsonl_logger import JsonlLogger
from hems.logging.sqlite_store import SqliteAuditStore
from hems.logging.verification import VerificationTracker

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
    scheduler = ControlScheduler(
        config, state, jsonl_logger, sqlite_store, tracker, emhass_client
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
            await asyncio.gather(
                run_ingestion(client, state, config),
                scheduler.run(),
            )
    finally:
        await sqlite_store.close()


def main() -> None:
    try:
        asyncio.run(async_main())
    except KeyboardInterrupt:
        logger.info("HEMS orchestrator stopped by user")


if __name__ == "__main__":
    main()
