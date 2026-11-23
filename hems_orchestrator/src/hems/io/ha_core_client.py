"""Asynchroner Home Assistant Core API Client (docs/06, Phase 5).

Läuft über den Supervisor-Proxy (``http://supervisor/core/api``). Ohne Token
liefert der Client einmalig eine Warnung und sichere leere Rückgabewerte statt
einer unkontrollierten Exception (Fail-Safe außerhalb von Home Assistant).
"""

from __future__ import annotations

import logging
import os
from datetime import datetime
from typing import Any
from urllib.parse import quote

import httpx
from pydantic import BaseModel, ConfigDict, Field

from hems.config import AppConfig

logger = logging.getLogger(__name__)


class HaEntityState(BaseModel):
    """Projizierter Zustand einer HA-Entität (nur bekannte Felder, extra=forbid)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    entity_id: str
    state: str
    last_changed: str
    friendly_name: str
    device_class: str | None = None
    unit_of_measurement: str | None = None
    domain: str
    attributes: dict[str, Any] = Field(default_factory=dict)


class HaHistoryPoint(BaseModel):
    """Ein Zustandspunkt aus der HA-Historie (ohne Attribute)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    state: str
    last_changed_utc: str


def _optional_str(value: Any) -> str | None:
    """Normalisiert Attributwerte zu ``str | None`` (Pydantic v2 ist hier strikt)."""
    return None if value is None else str(value)


def _entity_state_from_raw(raw: Any) -> HaEntityState:
    """Projiziert eine rohe ``/states``-Antwort auf das kanonische Modell.

    ``last_updated`` und ``context`` werden bewusst verworfen, sonst lehnt
    ``extra="forbid"`` die Antwort ab.
    """
    if not isinstance(raw, dict):
        raise ValueError("HA state entry is not an object")
    entity_id = str(raw.get("entity_id", ""))
    if not entity_id:
        raise ValueError("HA state entry has no entity_id")
    attributes_raw = raw.get("attributes", {})
    attributes = dict(attributes_raw) if isinstance(attributes_raw, dict) else {}
    domain = entity_id.split(".", 1)[0] if "." in entity_id else ""
    return HaEntityState(
        entity_id=entity_id,
        state=str(raw.get("state", "")),
        last_changed=str(raw.get("last_changed", "")),
        friendly_name=str(attributes.get("friendly_name", entity_id)),
        device_class=_optional_str(attributes.get("device_class")),
        unit_of_measurement=_optional_str(attributes.get("unit_of_measurement")),
        domain=domain,
        attributes=attributes,
    )


class HaCoreClient:
    """Nicht-blockierender Client für HA ``/states``, ``/history`` und ``/services``."""

    def __init__(self, config: AppConfig) -> None:
        self._base_url = config.ha_api_url
        self._token = config.ha_token or os.environ.get("SUPERVISOR_TOKEN", "")
        self._warned_no_token = False

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._token}",
            "Content-Type": "application/json",
        }

    def _missing_token(self) -> bool:
        """Gibt True zurück und warnt genau einmal, wenn kein Token vorliegt."""
        if self._token:
            return False
        if not self._warned_no_token:
            logger.warning(
                "No Home Assistant token configured; HA Core API calls return safe empty results"
            )
            self._warned_no_token = True
        return True

    async def get_states(self) -> list[HaEntityState]:
        """GET /states – liefert alle Entitäten inklusive ``attributes``."""
        if self._missing_token():
            return []
        try:
            async with httpx.AsyncClient(base_url=self._base_url, timeout=10.0) as client:
                response = await client.get("/states", headers=self._headers())
        except httpx.HTTPError as exc:
            logger.error("HA /states request failed: %s", exc)
            return []
        if response.status_code != 200:
            logger.error("HA /states returned HTTP %s", response.status_code)
            return []
        try:
            payload = response.json()
        except ValueError as exc:
            logger.error("HA /states returned invalid JSON: %s", exc)
            return []
        if not isinstance(payload, list):
            logger.error("HA /states returned unexpected payload: %s", type(payload).__name__)
            return []
        states: list[HaEntityState] = []
        for raw in payload:
            try:
                states.append(_entity_state_from_raw(raw))
            except (ValueError, TypeError, KeyError) as exc:
                logger.warning("Skipping invalid HA state entry: %s", exc)
        return states

    async def get_history(
        self,
        entity_ids: list[str],
        start_utc: datetime,
        end_utc: datetime | None = None,
    ) -> dict[str, list[HaHistoryPoint]]:
        """GET /history/period/... – gibt je Entity aufsteigend sortierte Punkte zurück."""
        if self._missing_token() or not entity_ids:
            return {}
        params: dict[str, str] = {
            "filter_entity_id": ",".join(entity_ids),
            "minimal_response": "true",
            "no_attributes": "true",
        }
        if end_utc is not None:
            params["end_time"] = end_utc.isoformat()
        path = f"/history/period/{quote(start_utc.isoformat(), safe='')}"
        try:
            async with httpx.AsyncClient(base_url=self._base_url, timeout=10.0) as client:
                response = await client.get(path, params=params, headers=self._headers())
        except httpx.HTTPError as exc:
            logger.error("HA /history request failed: %s", exc)
            return {}
        if response.status_code != 200:
            logger.error("HA /history returned HTTP %s", response.status_code)
            return {}
        try:
            payload = response.json()
        except ValueError as exc:
            logger.error("HA /history returned invalid JSON: %s", exc)
            return {}
        if not isinstance(payload, list):
            logger.error("HA /history returned unexpected payload: %s", type(payload).__name__)
            return {}

        result: dict[str, list[HaHistoryPoint]] = {entity_id: [] for entity_id in entity_ids}
        for index, group in enumerate(payload):
            if not isinstance(group, list):
                continue
            fallback_id = entity_ids[index] if index < len(entity_ids) else None
            for raw in group:
                if not isinstance(raw, dict):
                    continue
                entity_id = str(raw.get("entity_id", fallback_id or ""))
                if not entity_id:
                    continue
                try:
                    point = HaHistoryPoint(
                        state=str(raw.get("state", "")),
                        last_changed_utc=str(raw.get("last_changed", "")),
                    )
                except (ValueError, TypeError) as exc:
                    logger.warning("Skipping invalid HA history point: %s", exc)
                    continue
                result.setdefault(entity_id, []).append(point)
        for points in result.values():
            points.sort(key=lambda point: point.last_changed_utc)
        return {entity_id: points for entity_id, points in result.items() if points}

    async def call_service(
        self, domain: str, service: str, service_data: dict[str, Any]
    ) -> bool:
        """POST /services/{domain}/{service} – True bei HTTP 200/201."""
        if self._missing_token():
            return False
        try:
            async with httpx.AsyncClient(base_url=self._base_url, timeout=10.0) as client:
                response = await client.post(
                    f"/services/{domain}/{service}",
                    json=service_data,
                    headers=self._headers(),
                )
        except httpx.HTTPError as exc:
            logger.error("HA service call %s.%s failed: %s", domain, service, exc)
            return False
        if response.status_code in (200, 201):
            return True
        logger.error(
            "HA service call %s.%s returned HTTP %s", domain, service, response.status_code
        )
        return False
