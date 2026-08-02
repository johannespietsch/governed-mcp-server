"""ServiceNow Table API connector.

Two backends behind one interface:

* `live` — a real instance, addressed through the Table API. Credentials come
  from the environment; a Personal Developer Instance is enough to exercise
  this end to end.
* `mock` — an in-process store that answers the same calls with the same
  shapes. This is the default, so the repository runs, and the tests pass, with
  no instance and no credentials.

The split is not only for convenience. A connector that can be run in-process
is a connector whose failure modes can be tested — a 401 from an expired
credential, a CI that does not resolve, an incident that already exists — and
those paths are exactly the ones that never get exercised against a live
instance because provoking them there is awkward.

Credentials are read from the environment and never logged. Tranche 2 moves
them behind a Key Vault reference; tranche 5 deploys that. What this module
establishes now is that nothing reaches for a credential at import time, so a
missing secret is a startup error with a clear message rather than a stack
trace on the first tool call.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal, Protocol

import httpx2

logger = logging.getLogger("governance.servicenow")

Urgency = Literal["1", "2", "3"]  # ServiceNow: 1 high, 2 medium, 3 low

ENV_INSTANCE = "SERVICENOW_INSTANCE"
ENV_USER = "SERVICENOW_USER"
ENV_PASSWORD = "SERVICENOW_PASSWORD"


@dataclass(frozen=True)
class Incident:
    number: str
    sys_id: str
    short_description: str
    state: str
    urgency: str
    configuration_item: str
    opened_at: str

    @classmethod
    def from_record(cls, record: dict[str, Any]) -> Incident:
        """Project a Table API record onto the fields this server exposes.

        Deliberately narrow. A ServiceNow incident record carries well over a
        hundred columns, many of them free text that could contain anything a
        user typed; forwarding the lot to a language model is how data gets
        somewhere it was never classified for.
        """
        ci = record.get("cmdb_ci")
        if isinstance(ci, dict):
            ci = ci.get("display_value") or ci.get("value")
        return cls(
            number=str(record.get("number", "")),
            sys_id=str(record.get("sys_id", "")),
            short_description=str(record.get("short_description", "")),
            state=str(record.get("state", "")),
            urgency=str(record.get("urgency", "")),
            configuration_item=str(ci or ""),
            opened_at=str(record.get("opened_at", "")),
        )


@dataclass(frozen=True)
class ConfigurationItem:
    name: str
    sys_id: str
    ci_class: str
    operational_status: str


class ServiceNowError(Exception):
    """The instance rejected a call, or could not be reached."""


class ServiceNow(Protocol):
    """What the tools depend on. Both backends satisfy it."""

    async def find_configuration_item(self, name: str) -> ConfigurationItem | None: ...

    async def search_incidents(self, *, configuration_item: str | None = None,
                               active_only: bool = True, limit: int = 10) -> list[Incident]: ...

    async def create_incident(self, *, short_description: str, configuration_item: str,
                              urgency: Urgency = "3", description: str = "") -> Incident: ...


# ---------------------------------------------------------------------------
# Live backend
# ---------------------------------------------------------------------------


@dataclass
class LiveServiceNow:
    """Talks to a real instance over the Table API."""

    instance: str
    user: str
    password: str
    timeout_seconds: float = 15.0

    @classmethod
    def from_environment(cls) -> LiveServiceNow:
        missing = [k for k in (ENV_INSTANCE, ENV_USER, ENV_PASSWORD) if not os.environ.get(k)]
        if missing:
            raise ServiceNowError(
                f"ServiceNow credentials missing: {', '.join(missing)}. "
                "Set them, or run with the default mock backend."
            )
        return cls(
            instance=os.environ[ENV_INSTANCE].strip().removesuffix("/"),
            user=os.environ[ENV_USER],
            password=os.environ[ENV_PASSWORD],
        )

    @property
    def base_url(self) -> str:
        if self.instance.startswith("http"):
            return f"{self.instance}/api/now/table"
        return f"https://{self.instance}.service-now.com/api/now/table"

    async def _request(self, method: str, table: str, **kwargs: Any) -> Any:
        url = f"{self.base_url}/{table}"
        try:
            async with httpx2.AsyncClient(timeout=self.timeout_seconds) as client:
                response = await client.request(
                    method,
                    url,
                    auth=(self.user, self.password),
                    headers={"Accept": "application/json", "Content-Type": "application/json"},
                    **kwargs,
                )
        except httpx2.HTTPError as exc:
            # The message deliberately does not include the response body: on an
            # auth failure ServiceNow echoes request detail that can include the
            # credential it rejected.
            raise ServiceNowError(f"ServiceNow request failed: {type(exc).__name__}") from exc

        if response.status_code == 401:
            raise ServiceNowError("ServiceNow rejected the credentials (401)")
        if response.status_code >= 400:
            raise ServiceNowError(f"ServiceNow returned {response.status_code} for {table}")
        return response.json().get("result", [])

    async def find_configuration_item(self, name: str) -> ConfigurationItem | None:
        records = await self._request(
            "GET", "cmdb_ci",
            params={"sysparm_query": f"name={name}", "sysparm_limit": "1"},
        )
        if not records:
            return None
        record = records[0]
        return ConfigurationItem(
            name=str(record.get("name", "")),
            sys_id=str(record.get("sys_id", "")),
            ci_class=str(record.get("sys_class_name", "")),
            operational_status=str(record.get("operational_status", "")),
        )

    async def search_incidents(self, *, configuration_item: str | None = None,
                               active_only: bool = True, limit: int = 10) -> list[Incident]:
        clauses = []
        if configuration_item:
            clauses.append(f"cmdb_ci.name={configuration_item}")
        if active_only:
            clauses.append("active=true")
        records = await self._request(
            "GET", "incident",
            params={
                "sysparm_query": "^".join(clauses) or "active=true",
                "sysparm_limit": str(limit),
                "sysparm_display_value": "true",
            },
        )
        return [Incident.from_record(r) for r in records]

    async def create_incident(self, *, short_description: str, configuration_item: str,
                              urgency: Urgency = "3", description: str = "") -> Incident:
        record = await self._request(
            "POST", "incident",
            json={
                "short_description": short_description,
                "description": description,
                "cmdb_ci": configuration_item,
                "urgency": urgency,
                "category": "inquiry",
            },
        )
        return Incident.from_record(record if isinstance(record, dict) else {})


# ---------------------------------------------------------------------------
# Mock backend
# ---------------------------------------------------------------------------


@dataclass
class MockServiceNow:
    """An in-process stand-in that answers the same calls with the same shapes."""

    incidents: list[Incident] = field(default_factory=list)
    configuration_items: dict[str, ConfigurationItem] = field(default_factory=dict)
    _counter: int = field(default=0, init=False)

    def __post_init__(self) -> None:
        if not self.configuration_items:
            self.configuration_items = {
                "CI-WMS-01": ConfigurationItem("CI-WMS-01", "a1b2c3d4", "cmdb_ci_appl", "1"),
                "CI-TMS-02": ConfigurationItem("CI-TMS-02", "e5f6a7b8", "cmdb_ci_appl", "1"),
            }
        if not self.incidents:
            self.incidents = [
                Incident(
                    number="INC0010001",
                    sys_id="11111111",
                    short_description="WMS batch job latency above threshold",
                    state="2",
                    urgency="2",
                    configuration_item="CI-WMS-01",
                    opened_at="2026-07-30 08:14:02",
                )
            ]

    async def find_configuration_item(self, name: str) -> ConfigurationItem | None:
        return self.configuration_items.get(name)

    async def search_incidents(self, *, configuration_item: str | None = None,
                               active_only: bool = True, limit: int = 10) -> list[Incident]:
        found = [
            incident for incident in self.incidents
            if (configuration_item is None or incident.configuration_item == configuration_item)
            and (not active_only or incident.state not in ("6", "7"))
        ]
        return found[:limit]

    async def create_incident(self, *, short_description: str, configuration_item: str,
                              urgency: Urgency = "3", description: str = "") -> Incident:
        if configuration_item not in self.configuration_items:
            raise ServiceNowError(f"no configuration item named {configuration_item!r}")
        self._counter += 1
        incident = Incident(
            number=f"INC00200{self._counter:02d}",
            sys_id=f"mock{self._counter:08d}",
            short_description=short_description,
            state="1",
            urgency=urgency,
            configuration_item=configuration_item,
            opened_at=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
        )
        self.incidents.append(incident)
        return incident


def build(backend: Literal["mock", "live"] = "mock") -> ServiceNow:
    """Pick a backend. `mock` is the default so nothing requires credentials."""
    if backend == "live":
        client = LiveServiceNow.from_environment()
        logger.info("ServiceNow connector: live instance %s", client.instance)
        return client
    logger.info("ServiceNow connector: in-process mock (no credentials in use)")
    return MockServiceNow()
