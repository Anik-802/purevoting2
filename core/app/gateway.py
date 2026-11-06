"""Client for the Ledger Gateway (contracts/ledger-gateway.openapi.yaml), called with coreKey.

Until M1 (docs/schedule.md) the other side is a mock (prism, or tools/fake_gateway.py for a stateful run); the client does not care.
"""
import httpx

from app.config import settings
from app.errors import Problem

TIMEOUT = 5.0   # README "Timeouts / retries": 5 s per inter-service call
MAX_EVENT_PAGES = 1000   # GET /v1/events: 1000 pages of 1000 events; a Gateway that never ends the listing is unavailable


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

    def finding_records(self, election_id: str) -> list[dict]:
        """Every finding record of the election with its contract_status (all pages, at most MAX_EVENT_PAGES); a
        record without finding_id, rule, checkpoint or contract_status makes the listing unusable."""
        items, cursor, cursors = [], None, set()
        while True:
            if len(cursors) >= MAX_EVENT_PAGES or cursor in cursors:
                raise GatewayUnavailable("GET /v1/finding-records: too many pages or a repeated cursor")
            cursors.add(cursor)
            params = {"election_id": election_id, "limit": 1000}
            if cursor:
                params["cursor"] = cursor
            r = self._get("/v1/finding-records", **params)
            if r.status_code != 200:
                raise GatewayUnavailable(f"GET /v1/finding-records: HTTP {r.status_code}")
            try:
                page = r.json()
                for item in page["items"]:
                    p = item["recorded"]["payload"]
                    if not (isinstance(item["finding_id"], str) and isinstance(item["contract_status"], str)
                            and isinstance(p["rule_id"], str) and isinstance(p["finding_checkpoint"], int)):
                        raise TypeError
                items += page["items"]
                cursor = page.get("next_cursor")
            except Exception:   # noqa: BLE001 — anything unparsable is unavailable, never a 500
                raise GatewayUnavailable("GET /v1/finding-records: malformed page") from None
            if not cursor:
                return items

    def finding_record(self, election_id: str, finding_id: str) -> dict | None:
        """The FINDING_RECORDED event of finding_id, or None if the Gateway has not indexed it (yet)."""
        return next((item for item in self.finding_records(election_id) if item["finding_id"] == finding_id), None)

    def events(self, election_id: str) -> list[dict]:
        """GET /v1/events: every stored event of the election (all pages, at most MAX_EVENT_PAGES)."""
        items, cursor, cursors = [], None, set()
        while True:
            if len(cursors) >= MAX_EVENT_PAGES or cursor in cursors:
                raise GatewayUnavailable("GET /v1/events: too many pages or a repeated cursor")
            cursors.add(cursor)
            params = {"election_id": election_id, "limit": 1000}
            if cursor:
                params["cursor"] = cursor
            r = self._get("/v1/events", **params)
            if r.status_code != 200:
                raise GatewayUnavailable(f"GET /v1/events: HTTP {r.status_code}")
            try:
                page = r.json()
                if not isinstance(page["items"], list):
                    raise TypeError
                items += page["items"]
                cursor = page.get("next_cursor")
            except Exception:   # noqa: BLE001 — anything unparsable (incl. RecursionError on deep JSON) is unavailable
                raise GatewayUnavailable("GET /v1/events: malformed page") from None
            if not cursor:
                return items

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
