"""FastAPI App-Factory: Ingress-UI und REST-API (docs/08, Patch-Spec §4).

WICHTIG: Serverseitig werden alle Routen mit führendem Slash deklariert.
Das Verbot absoluter Pfade gilt ausschließlich für das Frontend (index.html),
damit relative fetches sowohl lokal als auch hinter
``/api/hassio_ingress/<token>/`` funktionieren. Bitte nicht "korrigieren".
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, ConfigDict

from hems.config import AppConfig, OperatingMode
from hems.control.device_registry import ControllableDevice, DeviceRegistry
from hems.control.scheduler import ControlScheduler
from hems.forecast.appliance_forecaster import ForecastManager, SystemForecastReport
from hems.forecast.pattern_learner import LearnedPattern
from hems.forecast.washdata_adapter import discover_washdata_appliances
from hems.io.ha_core_client import HaCoreClient
from hems.logging.sqlite_store import SqliteAuditStore

logger = logging.getLogger(__name__)

_STATIC_DIR = Path(__file__).resolve().parent / "static"
_ALLOWED_INGRESS_HOSTS = frozenset({"172.30.32.2", "127.0.0.1", "::1", "testclient"})


class ModeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: OperatingMode


class DryRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    dry_run: bool


class EnabledRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool


def create_app(
    config: AppConfig,
    scheduler: ControlScheduler,
    device_registry: DeviceRegistry,
    ha_client: HaCoreClient,
    sqlite_store: SqliteAuditStore,
    forecast_manager: ForecastManager,
) -> FastAPI:
    """Erzeugt die testbare FastAPI-App (Uvicorn startet ausschließlich in main.py)."""
    app = FastAPI(title="HEMS Orchestrator", docs_url=None, redoc_url=None, openapi_url=None)

    @app.middleware("http")
    async def ingress_only_guard(request: Request, call_next: Any) -> Any:
        if config.ingress_only:
            host = request.client.host if request.client is not None else ""
            if host not in _ALLOWED_INGRESS_HOSTS:
                logger.warning("Rejecting web request from host %s", host)
                return JSONResponse(status_code=403, content={"detail": "Forbidden"})
        return await call_next(request)

    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(_STATIC_DIR / "index.html", media_type="text/html")

    @app.get("/api/dashboard")
    async def dashboard() -> dict[str, Any]:
        state = scheduler.live_state
        devices = await device_registry.list_devices()
        states = await ha_client.get_states()
        patterns = await sqlite_store.list_patterns()
        washdata = discover_washdata_appliances(states)
        last = scheduler.last_decision
        return {
            "operating_mode": state.operating_mode.value,
            "dry_run": state.dry_run,
            "live_state": state.inputs.model_dump(mode="json") if state.inputs else None,
            "last_decision": last.model_dump(mode="json") if last is not None else None,
            "active_devices": sum(1 for device in devices if device.enabled),
            "washdata_devices": [status.model_dump(mode="json") for status in washdata],
            "learned_pattern_count": len(patterns),
        }

    @app.post("/api/control/mode")
    async def set_mode(request: ModeRequest) -> dict[str, str]:
        scheduler.set_operating_mode(request.mode)
        logger.info("Operating mode switched to %s via API", request.mode.value)
        return {"status": "ok", "mode": request.mode.value}

    @app.post("/api/control/dry_run")
    async def set_dry_run(request: DryRunRequest) -> dict[str, Any]:
        scheduler.set_dry_run(request.dry_run)
        if request.dry_run:
            logger.info("Shadow-Mode (dry_run=True) activated via API")
        else:
            logger.warning("Shadow-Mode DEACTIVATED via API - real actuators may be commanded")
        return {"status": "ok", "dry_run": request.dry_run}

    @app.get("/api/devices")
    async def list_devices() -> list[ControllableDevice]:
        return await device_registry.list_devices()

    @app.post("/api/devices", status_code=201)
    async def create_device(device: ControllableDevice) -> ControllableDevice:
        return await device_registry.upsert_device(device)

    @app.put("/api/devices/{device_id}")
    async def update_device(device_id: str, device: ControllableDevice) -> ControllableDevice:
        if device.device_id != device_id:
            return JSONResponse(  # type: ignore[return-value]
                status_code=400,
                content={"detail": "device_id in body does not match path"},
            )
        return await device_registry.upsert_device(device)

    @app.patch("/api/devices/{device_id}/enabled")
    async def set_device_enabled(device_id: str, request: EnabledRequest) -> ControllableDevice:
        try:
            return await device_registry.set_enabled(device_id, request.enabled)
        except KeyError:
            return JSONResponse(  # type: ignore[return-value]
                status_code=404, content={"detail": "device not found"}
            )

    @app.delete("/api/devices/{device_id}")
    async def delete_device(device_id: str) -> dict[str, str]:
        if not await device_registry.delete_device(device_id):
            return JSONResponse(  # type: ignore[return-value]
                status_code=404, content={"detail": "device not found"}
            )
        return {"status": "ok"}

    @app.get("/api/ha/entities")
    async def ha_entities(domain: str | None = None) -> list[dict[str, Any]]:
        states = await ha_client.get_states()
        if domain:
            domains = {item.strip() for item in domain.split(",") if item.strip()}
            states = [state for state in states if state.domain in domains]
        return [state.model_dump(mode="json") for state in states]

    @app.get("/api/integrations/washdata")
    async def washdata_devices() -> list[dict[str, Any]]:
        statuses = discover_washdata_appliances(await ha_client.get_states())
        return [status.model_dump(mode="json") for status in statuses]

    @app.post("/api/integrations/washdata/sync")
    async def sync_washdata() -> dict[str, Any]:
        imported = await device_registry.sync_washdata_devices(await ha_client.get_states())
        return {"status": "ok", "imported_count": len(imported)}

    @app.get("/api/patterns")
    async def list_patterns() -> list[LearnedPattern]:
        return await sqlite_store.list_patterns()

    @app.post("/api/patterns/retrain")
    async def retrain_patterns() -> dict[str, Any]:
        count = await forecast_manager.retrain_patterns()
        return {"status": "ok", "patterns_found": count}

    @app.get("/api/forecasts")
    async def forecasts() -> SystemForecastReport:
        report = forecast_manager.get_report()
        if report is None:
            report = await forecast_manager.refresh()
        return report

    return app
