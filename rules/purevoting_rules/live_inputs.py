"""Read a single election's cumulative audit inputs from the v1.1 HTTP APIs.

Clients are supplied by the caller, with independent base URLs, X-Service-Key
and finite timeouts. No value from an HTTP response is used as a URL or put in
exception text. No missing ledger page is interpreted as an empty collection.
"""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from urllib.parse import quote

import httpx
import yaml
from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry
from referencing.jsonschema import DRAFT202012

from .engine import TABLES
from .hashing import H, canonical, snapshot_hash
from .paths import reference_root

VIEWS = ("election_config", *TABLES)
_BASE = "https://purevoting2.local/contracts/"
_PATHS = {
    "elections": ("core-api.openapi.yaml", "/internal/v1/elections"),
    "checkpoints": ("core-api.openapi.yaml", "/internal/v1/checkpoints"),
    "view": ("core-api.openapi.yaml", "/internal/v1/views/{view}"),
    "events": ("ledger-gateway.openapi.yaml", "/v1/events"),
    "rejections": ("ledger-gateway.openapi.yaml", "/v1/rejections"),
    "anchors": ("ledger-gateway.openapi.yaml", "/v1/anchors"),
    "leaves": ("ledger-gateway.openapi.yaml", "/v1/anchors/{anchor_id}/leaves"),
    "event": ("ledger-gateway.openapi.yaml", "/v1/events/{event_id}"),
    "proof": ("ledger-gateway.openapi.yaml", "/v1/events/{event_id}/proof"),
    "rejection": ("ledger-gateway.openapi.yaml", "/v1/rejections/{rejection_id}"),
}


class InputError(Exception):
    """Safe for logs: no endpoint, subject hash, row data, key, or voter ID."""
    def __init__(self, source: str, kind: str):
        self.source = source
        self.kind = kind
        super().__init__(f"{source} input {kind}")


@lru_cache(maxsize=4)
def _validators(root: Path):
    folder = root / "contracts"
    files = {filename for filename, _ in _PATHS.values()}
    docs = {name: yaml.safe_load((folder / name).read_text(encoding="utf-8")) for name in files}
    resources = [(_BASE + name, DRAFT202012.create_resource(doc)) for name, doc in docs.items()]
    for path in folder.glob("*.schema.json"):
        doc = json.loads(path.read_text(encoding="utf-8"))
        resources.append((doc["$id"], DRAFT202012.create_resource(doc)))
    # No network retrieval: references may resolve ONLY under the local contract tree.
    registry = Registry().with_resources(resources)
    out = {}
    for key, (name, path) in _PATHS.items():
        esc = path.replace("~", "~0").replace("/", "~1")
        ref = _BASE + name + "#/paths/" + esc + "/get/responses/200/content/application~1json/schema"
        out[key] = Draft202012Validator({"$ref": ref}, registry=registry, format_checker=FormatChecker())
    return out


def _unique_keys(pairs):
    obj = dict(pairs)
    if len(obj) != len(pairs):
        raise ValueError("duplicate JSON key")
    return obj


class LiveInputs:
    """Finite HTTP reads. A view that cannot be trusted is left unavailable.

    An unavailable single Core view is represented by None so that rules which
    depend on it can return VIEW_UNAVAILABLE. Discovery failures, ambiguous
    commit identity, missing Gateway reads and inconsistent pagination abort
    the run rather than allowing any clean ledger result.
    """
    def __init__(self, core: httpx.Client, gateway: httpx.Client, *,
                 page_size: int = 250, max_pages: int = 1000, max_page_bytes: int = 16 * 1024 * 1024,
                 max_anchor_records: int = 4096, max_anchor_members: int = 20000):
        if (type(page_size) is not int or not 1 <= page_size <= 1000 or
                type(max_pages) is not int or max_pages < 1 or
                type(max_page_bytes) is not int or max_page_bytes < 1 or
                type(max_anchor_records) is not int or max_anchor_records < 1 or
                type(max_anchor_members) is not int or max_anchor_members < 1):
            raise ValueError("invalid pagination bounds")
        self.core, self.gateway = core, gateway
        self.page_size, self.max_pages, self.max_page_bytes = page_size, max_pages, max_page_bytes
        self.max_anchor_records, self.max_anchor_members = max_anchor_records, max_anchor_members
        self.validators = _validators(reference_root())

    def _read(self, source, kind, path, params):
        client = self.core if source == "core" else self.gateway
        try:
            # Never follow a redirect carrying X-Service-Key to another host.
            with client.stream("GET", path, params=params, follow_redirects=False) as resp:
                if resp.status_code != 200:
                    raise InputError(source, "unavailable")
                raw = bytearray()
                for chunk in resp.iter_bytes():
                    if len(raw) + len(chunk) > self.max_page_bytes:
                        raise InputError(source, "invalid")
                    raw.extend(chunk)
            value = json.loads(raw, object_pairs_hook=_unique_keys)
            # The schemas consider 1.0 an integer, while signed audit bodies
            # forbid floats and unsafe integers. Reject ambiguities here.
            canonical(value)
            if not self.validators[kind].is_valid(value):
                raise InputError(source, "invalid")
            return value
        except httpx.HTTPError:
            raise InputError(source, "unavailable") from None
        except (UnicodeError, ValueError, RecursionError):
            raise InputError(source, "invalid") from None

    def elections(self) -> list[dict]:
        rows = self._read("core", "elections", _PATHS["elections"][1], {})["items"]
        if len({r["election_id"] for r in rows}) != len(rows):
            raise InputError("core", "invalid")
        return rows

    def checkpoints(self, election_id: str) -> list[dict]:
        data = self._read("core", "checkpoints", _PATHS["checkpoints"][1], {"election_id": election_id})
        rows = data["items"]
        ks = [r["checkpoint"] for r in rows]
        if (ks != sorted(set(ks)) or len({r["event_id"] for r in rows}) != len(rows)
                or any(k >= data["open_checkpoint"] for k in ks)):
            raise InputError("core", "invalid")
        return rows

    def _pages(self, source, kind, path, params, field):
        seen, cursor = set(), None
        for _ in range(self.max_pages):
            query = {**params, "limit": self.page_size}
            if cursor is not None:
                query["cursor"] = cursor
            page = self._read(source, kind, path, query)
            batch, cursor = page[field], page["next_cursor"]
            if len(batch) > self.page_size or (cursor is not None and (not batch or not cursor or cursor in seen)):
                raise InputError(source, "invalid")
            yield page
            if cursor is None:
                return
            seen.add(cursor)
        raise InputError(source, "invalid")

    def _view(self, election_id, k, name):
        rows, marker, previous = [], None, None
        for page in self._pages("core", "view", "/internal/v1/views/" + name,
                                {"election_id": election_id, "checkpoint": k}, "rows"):
            if (page["election_id"], page["checkpoint"], page["view"]) != (election_id, k, name):
                raise InputError("core", "invalid")
            visible = (page["total_rows"], page["snapshot_hash"])
            if marker is not None and visible != marker:
                raise InputError("core", "invalid")
            marker = visible
            for row in page["rows"]:
                key = row["row_key"]
                if row["view"] != name or (previous is not None and key <= previous):
                    raise InputError("core", "invalid")
                previous = key
                rows.append(row)
            if len(rows) > marker[0]:
                raise InputError("core", "invalid")
        if len(rows) != marker[0]:
            raise InputError("core", "invalid")
        if name == "election_config":
            if len(rows) != 1 or marker[1] is not None:
                raise InputError("core", "invalid")
        elif snapshot_hash(rows) != marker[1]:
            # A coherent returned image that differs from the committed image
            # is passed to R5 for a finding. A page hash that does not even
            # describe the rows returned by Core cannot certify a whole view.
            raise InputError("core", "invalid")
        return rows

    def _ledger(self, election_id, k, kind):
        rows, ids, ledger_last_seq, last_order = [], set(), None, None
        for page in self._pages("gateway", kind, _PATHS[kind][1],
                                {"election_id": election_id, "checkpoint_max": k}, "items"):
            if kind == "events":
                seq = page["last_core_seq"]
                if ledger_last_seq is not None and ledger_last_seq != seq:
                    raise InputError("gateway", "invalid")
                ledger_last_seq = seq
            for row in page["items"]:
                key = row["event_id"] if kind == "events" else row["rejection_id"]
                if key in ids or row["election_id"] != election_id or row["checkpoint"] > k:
                    raise InputError("gateway", "invalid")
                if kind == "events" and row.get("source") == "core" and row.get("seq", 0) > ledger_last_seq:
                    raise InputError("gateway", "invalid")
                order = (row["occurred_at"], key) if kind == "events" else (row["checkpoint"], key)
                if last_order is not None and order <= last_order:
                    raise InputError("gateway", "invalid")
                last_order = order
                ids.add(key)
                rows.append(row)
        return rows, ledger_last_seq

    def snapshot(self, election_id: str, checkpoint: int, checkpoints: list[dict]) -> dict:
        """Commit index must be the result of checkpoints(election_id) for this poll."""
        selected = {c["checkpoint"]: c for c in checkpoints if c["checkpoint"] <= checkpoint}
        current = selected.get(checkpoint)
        if current is None or current["ledger_status"] != "ACCEPTED":
            raise InputError("core", "pending")
        events, last_seq = self._ledger(election_id, checkpoint, "events")
        rejections, _ = self._ledger(election_id, checkpoint, "rejections")
        # Outcomes are terminal and idempotent by event_id. RETRY_LATER is not
        # a rejection record; a corrected submission after rejection has a new
        # ID. Recounting the same event on either side would corrupt R1-R3.
        accepted_ids = {row["event_id"] for row in events}
        rejected_ids = {row["event_id"] for row in rejections}
        if accepted_ids & rejected_ids or len(rejected_ids) != len(rejections):
            raise InputError("gateway", "invalid")
        # A rejected system event consumes its seq but the rejection object
        # does not expose that value, so require uniqueness, not contiguity.
        seqs = [e["seq"] for e in events if e["source"] == "core" and "seq" in e]
        if len(set(seqs)) != len(seqs):
            raise InputError("gateway", "invalid")
        commits = {e["checkpoint"]: e for e in events if e["type"] == "CHECKPOINT_COMMITTED"}
        rejected = {(r["checkpoint"], r["event_id"]) for r in rejections if r["type"] == "CHECKPOINT_COMMITTED"}
        if len(commits) != sum(e["type"] == "CHECKPOINT_COMMITTED" for e in events):
            raise InputError("gateway", "invalid")
        if any(c["ledger_status"] == "PENDING" for c in selected.values()):
            # Once final, try again; do not skip a potentially historical commit.
            raise InputError("gateway", "pending")
        accepted = {k: c for k, c in selected.items() if c["ledger_status"] == "ACCEPTED"}
        if set(accepted) != set(commits) or any(
            commits[k]["event_id"] != c["event_id"] or commits[k]["payload"]["checkpoint"] != k
            or commits[k]["payload"]["reason"] != c["reason"] for k, c in accepted.items()
        ) or rejected != {(k, c["event_id"]) for k, c in selected.items() if c["ledger_status"] == "REJECTED"}:
            raise InputError("gateway", "invalid")
        history = {}
        for k in sorted(accepted):
            views = {}
            for name in VIEWS:
                try:
                    views[name] = self._view(election_id, k, name)
                except InputError:
                    # A missing view is distinct from an empty but verified page.
                    views[name] = None
            history[str(k)] = {"views": views, "checkpoint_event": commits[k]}
        return {"election_id": election_id, "checkpoint": checkpoint, "checkpoint_event_id": current["event_id"],
                "ledger_last_seq": last_seq, "events": events, "rejections": rejections,
                "views": history[str(checkpoint)]["views"], "history": history, "node": {"available": False}}

    def anchor_evidence(self, election_id: str, checkpoint: int, events: list[dict],
                        rejections: list[dict], *, chain: dict | None = None) -> dict:
        """Read complete trees, including leaves beyond k in a cross-checkpoint batch.

        Read all confirmed anchors, including ones absent from the supplied event
        list: otherwise an event omitted from both lists could evade the audit.
        Extra events are kept only in this verification envelope: they must never
        leak into the rule's cumulative <= k election history. Gateway roots and
        proofs remain untrusted until compared with the independently pinned node.
        A failed index page aborts collection. A temporarily unavailable individual
        tree/proof remains absent so R5 can report BATCH_NOT_ANCHORED after the
        worker's bounded wait. Invalid/scope-conflicting evidence aborts the run.
        """
        anchors, seen, last_block = [], set(), -1
        for page in self._pages("gateway", "anchors", "/v1/anchors",
                                {"election_id": election_id}, "items"):
            for anchor in page["items"]:
                aid = anchor["anchor_id"]
                if (aid in seen or anchor["election_id"] != election_id
                        or anchor["block_number"] < last_block):
                    raise InputError("gateway", "invalid")
                seen.add(aid)
                last_block = anchor["block_number"]
                anchors.append(anchor)
                if len(anchors) > self.max_anchor_records:
                    raise InputError("gateway", "invalid")
        by_event = {e["event_id"]: e for e in events}
        by_rejection = {r["rejection_id"]: r for r in rejections}
        leaves, proofs = {}, {}
        relevant = {e.get("anchor", {}).get("anchor_id") for e in events}
        relevant.update(r.get("anchor_id") for r in rejections)
        confirmed = chain.get("confirmed_block") if isinstance(chain, dict) else None
        node_anchors = chain.get("anchors") if isinstance(chain, dict) else None
        members = 0
        for anchor in anchors:
            aid = anchor["anchor_id"]
            if (type(confirmed) is int and isinstance(node_anchors, dict) and aid not in relevant
                    and H(aid) not in node_anchors and anchor["block_number"] > confirmed):
                continue  # This anchor is beyond the independent fixed-block read.
            try:
                tree = self._read("gateway", "leaves", "/v1/anchors/" + quote(aid, safe="") + "/leaves", {})
            except InputError as exc:
                if exc.kind == "invalid":
                    raise
                continue
            if tree["anchor_id"] != aid:
                raise InputError("gateway", "invalid")
            members += len(tree["leaves"]) + len(tree["rejection_ids"])
            if members > self.max_anchor_members:
                raise InputError("gateway", "invalid")
            leaves[aid] = tree
            for leaf in tree["leaves"]:
                identity = leaf["event_id"]
                if identity not in by_event:
                    try:
                        event = self._read("gateway", "event", "/v1/events/" + quote(identity, safe=""), {})
                    except InputError as exc:
                        if exc.kind == "invalid":
                            raise
                        continue
                    if event["event_id"] != identity or event["election_id"] != election_id:
                        raise InputError("gateway", "invalid")
                    # A supposedly complete cumulative listing omitted this item.
                    if event["checkpoint"] <= checkpoint:
                        raise InputError("gateway", "invalid")
                    by_event[identity] = event
            for identity in tree["rejection_ids"]:
                if identity not in by_rejection:
                    try:
                        rejection = self._read("gateway", "rejection", "/v1/rejections/" + quote(identity, safe=""), {})
                    except InputError as exc:
                        if exc.kind == "invalid":
                            raise
                        continue
                    if rejection["rejection_id"] != identity or rejection["election_id"] != election_id:
                        raise InputError("gateway", "invalid")
                    if rejection["checkpoint"] <= checkpoint:
                        raise InputError("gateway", "invalid")
                    by_rejection[identity] = rejection
        for event in events:
            if event.get("anchor", {}).get("status") != "ANCHORED":
                continue
            identity = event["event_id"]
            try:
                proof = self._read("gateway", "proof", "/v1/events/" + quote(identity, safe="") + "/proof", {})
            except InputError as exc:
                if exc.kind == "invalid":
                    raise
                continue
            if proof["event_id"] != identity:
                raise InputError("gateway", "invalid")
            proofs[identity] = proof
        return {"complete": True, "anchors": anchors, "leaves": leaves,
                "events": by_event, "rejections": by_rejection, "proofs": proofs}
