"""Publish only durable, locally validated findings using the authenticated service clients."""
from __future__ import annotations

import re
from dataclasses import dataclass

import httpx

from .hashing import canonical, finding_record_digest
from .signing import EIP191Signer
from .storage import FindingStore

_SYSTEM_EVENT = re.compile(r"evt_[0-9A-HJKMNP-TV-Z]{26}")


@dataclass
class Delivery:
    stored: int = 0
    retry: int = 0
    rejected: int = 0


def deliver(store: FindingStore, signer: EIP191Signer, gateway: httpx.Client, core: httpx.Client) -> Delivery:
    """One attempt per pending body per poll. Persist each stage before advancing.

    Unknown outcomes (including a lost HTTP reply) replay the same id and hash.
    The Gateway verifies the signed record; Core enforces registration visibility.
    Remote errors are never logged verbatim: they may contain voter identifiers.
    """
    tally = Delivery()
    deferred = set()
    for record in store.pending():
        identity, body = record["finding_id"], record["body"]
        # Keep the order of findings for one election and rule. Otherwise an
        # unavailable older INCONCLUSIVE could be registered *after* its new
        # same-checkpoint PASS, defeating the contract's supersession order.
        stream = (body["election_id"], body["rule_id"])
        if stream in deferred:
            tally.retry += 1
            continue
        deferred.add(stream)
        if record["state"] == "QUEUED":
            h = record["finding_hash"]
            digest = finding_record_digest(identity, body["election_id"], body["rule_id"], body["result"],
                                           body["checkpoint"], h)
            request = {name: body[name] for name in ("election_id", "finding_id", "rule_id", "result")}
            request.update(finding_checkpoint=body["checkpoint"], finding_hash=h, signature=signer.sign_digest(digest))
            try:
                response = gateway.post("/v1/finding-records", content=canonical(request),
                                        headers={"Content-Type": "application/json"})
            except httpx.RequestError:
                tally.retry += 1
                continue
            if response.status_code == 201:
                try:
                    ack = response.json()
                except ValueError:
                    ack = None
                eid = ack.get("event_id") if isinstance(ack, dict) else None
                if not isinstance(eid, str) or not _SYSTEM_EVENT.fullmatch(eid):
                    tally.rejected += 1
                    continue
            elif response.status_code != 200:
                if response.status_code in (408, 429) or response.status_code >= 500:
                    tally.retry += 1
                else:
                    tally.rejected += 1
                continue
            store.mark_registered(identity)
        try:
            response = core.post("/internal/v1/findings", content=record["body_bytes"],
                                 headers={"Content-Type": "application/json"})
        except httpx.RequestError:
            tally.retry += 1
            continue
        if response.status_code in (200, 201):
            store.mark_stored(identity)
            tally.stored += 1
            deferred.discard(stream)
        elif response.status_code in (408, 429) or response.status_code >= 500:
            tally.retry += 1
        else:
            tally.rejected += 1
    return tally
