"""Client for the Ledger Gateway (contracts/ledger-gateway.openapi.yaml), called with coreKey.

Until week 3 the other side is a mock (prism, or tools/fake_gateway.py for a stateful run); the client does not care.
"""
import httpx

from app.config import settings
from app.errors import Problem

TIMEOUT = 5.0   # README "Timeouts / retries": 5 s per inter-service call


class GatewayUnavailable(Exception):
    """Transient: network error or 5xx. Retried with backoff."""


class GatewayRefused(Exception):
    """4xx on a write: the request itself is wrong (schema, key). Raised so the caller can alert."""


class GatewayClient:
    def __init__(self, base_url: str | None = None, http: httpx.Client | None = None):
        self.http = http or httpx.Client(base_url=base_url or settings.gateway_url, timeout=TIMEOUT)

    def _headers(self) -> dict:
        return {"X-Service-Key": settings.core_key}

    def _get(self, path: str, **params):
        try:
            r = self.http.get(path, params=params, headers=self._headers())
        except httpx.HTTPError as e:
            raise GatewayUnavailable(str(e)) from None
        if r.status_code >= 500:
            raise GatewayUnavailable(f"HTTP {r.status_code}")
        return r

    def submit_events(self, events: list[dict]) -> list[dict]:
        """POST /v1/events → one SubmitResult per event."""
        try:
            r = self.http.post("/v1/events", json={"events": events}, headers=self._headers())
        except httpx.HTTPError as e:
            raise GatewayUnavailable(str(e)) from None
        if 400 <= r.status_code < 500:
            raise GatewayRefused(f"HTTP {r.status_code}")
        if r.status_code != 200:
            raise GatewayUnavailable(f"HTTP {r.status_code}")
        return r.json()["results"]

    def chain(self) -> dict:
        """GET /v1/chain (convenience only: compared with the pinned configuration, never used instead of it)."""
        r = self._get("/v1/chain")
        if r.status_code != 200:
            raise GatewayUnavailable(f"HTTP {r.status_code}")
        try:
            body = r.json()
        except ValueError:
            raise GatewayUnavailable("GET /v1/chain: not JSON") from None
        if not isinstance(body, dict):
            raise GatewayUnavailable("GET /v1/chain: not an object")
        return body

    def organizations(self) -> list[dict]:
        r = self._get("/v1/organizations")
        if r.status_code != 200:
            raise Problem(503, "LEDGER_UNAVAILABLE", "organization registry not readable")
        return r.json()["items"]

    def finding_record(self, election_id: str, finding_id: str) -> dict | None:
        """The FINDING_RECORDED event of finding_id, or None if the Gateway has not indexed it (yet)."""
        cursor = None
        while True:
            params = {"election_id": election_id, "limit": 1000}
            if cursor:
                params["cursor"] = cursor
            r = self._get("/v1/finding-records", **params)
            if r.status_code != 200:
                raise GatewayUnavailable(f"HTTP {r.status_code}")
            page = r.json()
            for item in page["items"]:
                if item["finding_id"] == finding_id:
                    return item
            cursor = page.get("next_cursor")
            if not cursor:
                return None

    def event(self, event_id: str, timeout: float = TIMEOUT) -> dict | None:
        try:
            r = self.http.get(f"/v1/events/{event_id}", headers=self._headers(), timeout=timeout)
        except httpx.HTTPError as e:
            raise GatewayUnavailable(str(e)) from None
        return r.json() if r.status_code == 200 else None


_client: GatewayClient | None = None


def get_gateway() -> GatewayClient:
    global _client
    if _client is None:
        _client = GatewayClient()
    return _client


def set_gateway(client: GatewayClient | None) -> None:
    global _client
    _client = client
