"""Read side of the Ledger Gateway: events, rejections, anchors, proofs, organizations, chain and election state.

Chain-derived data is shown only from confirmed blocks (chain-interface.md "Confirmations"): system events appear once
ACCEPTED, anchors once ANCHORED, registry and election state are read at block latest - D.
"""
import json

import gw_abi
import pv_hash
from gw_chain import ChainError
from gw_engine import FINDING_STATUS, GATEWAY_SQL, STATUS_NAMES, now_iso, to_iso, to_unix


class NotFound(Exception):
    pass


def _page(items, limit, cursor):
    start = int(cursor or 0)
    page = items[start:start + limit]
    return page, (str(start + limit) if start + limit < len(items) else None)


class Views:
    def __init__(self, engine, cfg):
        self.e, self.store, self.chain, self.cfg = engine, engine.store, engine.chain, cfg

    # ---------------------------------------------------------------- events
    def _anchor_block(self, anchor_id):
        a = self.store.one("SELECT * FROM anchors WHERE anchor_id=? AND status='ANCHORED'", (anchor_id,)) \
            if anchor_id else None
        if not a:
            return {"status": "PENDING"}
        return {"status": "ANCHORED", "anchor_id": a["anchor_id"], "mode": a["mode"], "merkle_root": a["merkle_root"],
                "tx_hash": a["tx_hash"], "block_number": a["block_number"]}

    def _event_view(self, row, voter):
        ev = json.loads(row["submission"])
        if not voter:
            ev["received_at"] = row["received_at"]
        ev["anchor"] = self._anchor_block(row["anchor_id"])
        return ev

    def _all_events(self, election_id):
        sysrows = self.store.all("SELECT * FROM events WHERE election_id=? AND status='ACCEPTED'", (election_id,))
        vrows = self.store.all("SELECT * FROM voter_events WHERE election_id=? AND status IN ('PENDING','ANCHORED')",
                               (election_id,))
        out = [(r, False) for r in sysrows] + [(r, True) for r in vrows]
        out.sort(key=lambda x: (x[0]["occurred_at"], x[0]["event_id"]))
        return out

    def list_events(self, q):
        el = q["election_id"]
        types = q.get("type") or []
        items = []
        for row, voter in self._all_events(el):
            ev = self._event_view(row, voter)
            if types and ev["type"] not in types:
                continue
            if "checkpoint_max" in q and ev["checkpoint"] > q["checkpoint_max"]:
                continue
            if "from" in q and ev["occurred_at"] < q["from"]:
                continue
            if "to" in q and ev["occurred_at"] >= q["to"]:
                continue
            if "anchor_status" in q and ev["anchor"]["status"] != q["anchor_status"]:
                continue
            items.append(ev)
        page, nxt = _page(items, q.get("limit", 100), q.get("cursor"))
        return {"items": page, "next_cursor": nxt, "last_core_seq": self._last_final_seq(el)}

    def _last_final_seq(self, el):
        n = 0
        for r in self.store.all("SELECT seq, status FROM events WHERE election_id=? AND type NOT IN (" + GATEWAY_SQL + ") ORDER BY seq", (el,)):
            if r["status"] in ("ACCEPTED", "REJECTED") and r["seq"] == n + 1:
                n = r["seq"]
            else:
                break
        return n

    def list_finding_records(self, q):
        """FINDING_RECORDED events of an election, grouped per finding, with the contract's own status."""
        items = []
        for row, voter in self._all_events(q["election_id"]):
            if voter or row["type"] != "FINDING_RECORDED":
                continue
            ev = self._event_view(row, False)
            fid = ev["payload"]["finding_id"]
            rec = self.chain.call("findingRecord", gw_abi.idh(fid), block="confirmed")
            items.append({"finding_id": fid, "recorded": ev, "reviews": [], "contract_status": FINDING_STATUS[rec[5]]})
        page, nxt = _page(items, q.get("limit", 100), q.get("cursor"))
        return {"items": page, "next_cursor": nxt}

    def get_event(self, event_id):
        row = self.store.one("SELECT * FROM events WHERE event_id=? AND status='ACCEPTED'", (event_id,))
        if row:
            return self._event_view(row, False)
        row = self.store.one("SELECT * FROM voter_events WHERE event_id=? AND status IN ('PENDING','ANCHORED')", (event_id,))
        if row:
            return self._event_view(row, True)
        raise NotFound("event " + event_id)

    # ---------------------------------------------------------------- proofs
    @staticmethod
    def merkle_path(leaves, index):
        """Sibling path of leaf `index` for pv_hash.merkle_root (odd last node carried up without a sibling)."""
        path, level, i = [], list(leaves), index
        while len(level) > 1:
            if i % 2 == 0:
                if i + 1 < len(level):
                    path.append({"position": "right", "hash": level[i + 1]})
            else:
                path.append({"position": "left", "hash": level[i - 1]})
            level = [pv_hash.H(level[k] + level[k + 1]) for k in range(0, len(level) - 1, 2)] + \
                    ([level[-1]] if len(level) % 2 else [])
            i //= 2
        return path

    @staticmethod
    def apply_path(leaf, path):
        h = leaf
        for step in path:
            h = pv_hash.H(step["hash"] + h) if step["position"] == "left" else pv_hash.H(h + step["hash"])
        return h

    def _anchored_leaf(self, event_id):
        ev = self.get_event(event_id)
        if ev["anchor"]["status"] != "ANCHORED":
            return None, None, None
        a = self.store.one("SELECT * FROM anchors WHERE anchor_id=?", (ev["anchor"]["anchor_id"],))
        leaves = json.loads(a["leaves"])
        idx = next(i for i, (eid, _) in enumerate(leaves) if eid == event_id)
        return ev, a, (leaves, idx)

    def proof(self, event_id):
        ev, a, li = self._anchored_leaf(event_id)
        if ev is None:
            return None
        leaves, idx = li
        hashes = [h for _, h in leaves]
        return {"event_id": event_id, "leaf_hash": hashes[idx], "merkle_root": a["merkle_root"],
                "path": self.merkle_path(hashes, idx), "anchor_id": a["anchor_id"], "tx_hash": a["tx_hash"],
                "block_number": a["block_number"]}

    def verify(self, event_ids):
        out = []
        for eid in event_ids:
            try:
                ev = self.get_event(eid)
            except NotFound:
                out.append({"event_id": eid, "verified": False, "detail": "unknown event",
                            "checks": {"payload_hash_ok": False, "leaf_hash_ok": False, "merkle_proof_ok": None,
                                       "on_chain_root_ok": None}})
                continue
            sub = {k: v for k, v in ev.items() if k not in ("received_at", "anchor")}
            checks = {"payload_hash_ok": pv_hash.Hc(ev["payload"]) == ev["payload_hash"],
                      "leaf_hash_ok": True, "merkle_proof_ok": None, "on_chain_root_ok": None}
            _, a, li = self._anchored_leaf(eid)
            detail = "not anchored yet"
            if a:
                leaves, idx = li
                hashes = [h for _, h in leaves]
                checks["leaf_hash_ok"] = pv_hash.Hc(sub) == hashes[idx]
                checks["merkle_proof_ok"] = self.apply_path(hashes[idx], self.merkle_path(hashes, idx)) == a["merkle_root"]
                checks["on_chain_root_ok"] = self._chain_root(a) == a["merkle_root"]
                detail = ""
            else:
                checks["leaf_hash_ok"] = True
            ok = all(v is True for v in checks.values())
            r = {"event_id": eid, "verified": ok, "checks": checks}
            if detail:
                r["detail"] = detail
            out.append(r)
        return out

    def _chain_root(self, a):
        """merkle_root stored on chain for this anchor (confirmed block), or None."""
        try:
            el, aid = gw_abi.idh(a["election_id"]), gw_abi.idh(a["anchor_id"])
            n = self.chain.call("anchorCount", el, block="confirmed")
            for i in range(n):
                r = self.chain.call("anchorAt", el, i, block="confirmed")
                if r[0] == aid:
                    return gw_abi.unhex32(r[1])
        except ChainError:
            return None
        return None

    # ---------------------------------------------------------------- anchors
    def _anchor_record(self, a):
        rec = {"anchor_id": a["anchor_id"], "election_id": a["election_id"], "mode": a["mode"],
               "merkle_root": a["merkle_root"], "event_count": a["event_count"], "rejections_root": a["rejections_root"],
               "tx_hash": a["tx_hash"], "block_number": a["block_number"], "anchored_at": a["anchored_at"]}
        if a["mode"] == "batched":
            rec["bucket_range"] = {"min": to_iso(a["bucket_min"]), "max": to_iso(a["bucket_max"])}
        return rec

    def list_anchors(self, q):
        rows = self.store.all("SELECT * FROM anchors WHERE election_id=? AND status='ANCHORED' "
                              "ORDER BY block_number, anchor_id", (q["election_id"],))
        page, nxt = _page([self._anchor_record(a) for a in rows], q.get("limit", 100), q.get("cursor"))
        return {"items": page, "next_cursor": nxt}

    def _anchor_row(self, anchor_id):
        a = self.store.one("SELECT * FROM anchors WHERE anchor_id=? AND status='ANCHORED'", (anchor_id,))
        if not a:
            raise NotFound("anchor " + anchor_id)
        return a

    def get_anchor(self, anchor_id):
        return self._anchor_record(self._anchor_row(anchor_id))

    def anchor_leaves(self, anchor_id):
        a = self._anchor_row(anchor_id)
        return {"anchor_id": anchor_id, "leaves": [{"event_id": e, "leaf_hash": h} for e, h in json.loads(a["leaves"])],
                "rejection_ids": json.loads(a["rejection_ids"]), "rejections_root": a["rejections_root"]}

    # ---------------------------------------------------------------- rejections
    @staticmethod
    def _rejection(r, anchored_ids):
        out = {k: r[k] for k in ("rejection_id", "election_id", "event_id", "type", "checkpoint", "reject_code",
                                 "recorded_at", "payload_hash", "subject_hash")}
        if r["detail"]:
            out["detail"] = r["detail"]
        if r["anchor_id"] in anchored_ids:
            out["anchor_id"] = r["anchor_id"]
        return out

    def _anchored_ids(self, election_id):
        return {a["anchor_id"] for a in self.store.all(
            "SELECT anchor_id FROM anchors WHERE election_id=? AND status='ANCHORED'", (election_id,))}

    def list_rejections(self, q):
        rows = self.store.all("SELECT * FROM rejections WHERE election_id=? ORDER BY checkpoint, rejection_id",
                              (q["election_id"],))
        ids = self._anchored_ids(q["election_id"])
        items = [self._rejection(r, ids) for r in rows
                 if ("checkpoint_max" not in q or r["checkpoint"] <= q["checkpoint_max"])
                 and ("reject_code" not in q or r["reject_code"] == q["reject_code"])]
        page, nxt = _page(items, q.get("limit", 100), q.get("cursor"))
        return {"items": page, "next_cursor": nxt}

    def get_rejection(self, rejection_id):
        r = self.store.one("SELECT * FROM rejections WHERE rejection_id=?", (rejection_id,))
        if not r:
            raise NotFound("rejection " + rejection_id)
        return self._rejection(r, self._anchored_ids(r["election_id"]))

    # ---------------------------------------------------------------- chain, registry, state
    def chain_info(self):
        urls = self.cfg["pinned_urls"] or [self.cfg["rpc_url"]]
        return {"platform": "other", "chain_id": str(self.chain.chain_id), "contract_address": self.chain.contract,
                "interface_version": "1.1.0", "rpc_urls": urls, "confirmation_depth": self.chain.depth}

    def organizations(self):
        cb = self.chain.confirmed_block()
        op = self.chain.call("operatorOrg", block=cb)
        regs = {}
        for lg in self.chain.logs(gw_abi.TOPIC_ORG_REGISTERED, to_block=cb):
            regs[lg["topics"][1]] = (lg["transactionHash"], int(lg["blockNumber"], 16))
        items = []
        for i in range(self.chain.call("orgCount", block=cb)):
            b32 = self.chain.call("orgAt", i, block=cb)
            org_id = self.e.org_by_b32.get(b32)
            if org_id is None:
                continue                                     # an organization this Gateway does not know by name
            signer, by = self.chain.call("orgSigner", b32, block=cb)
            tx, bn = regs.get(b32, ("0x" + "00" * 32, 0))
            items.append({"org_id": org_id, "signer": signer, "registered_by": by,
                          "is_operator": bool(op[1]) and op[0] == b32, "tx_hash": tx, "block_number": bn})
        return {"items": sorted(items, key=lambda o: o["org_id"])}

    def election_state(self, election_id):
        cb = self.chain.confirmed_block()
        el = gw_abi.idh(election_id)
        s = self.chain.call("electionState", el, block=cb)
        if int(s[4], 16) == 0:
            raise NotFound("election " + election_id)
        gov = self.chain.call("electionGovernance", el, block=cb)
        orgs = []
        for b32 in gov[0]:
            org_id = self.e.org_by_b32.get(b32, "org_unknown")
            orgs.append({"org_id": org_id, "signer": self.chain.call("orgSigner", b32, block=cb)[0]})
        out = {"election_id": election_id, "status": STATUS_NAMES[s[0]], "window_start": to_iso(s[1]),
               "window_end": to_iso(s[2]), "time_bucket_seconds": s[3], "config_hash": gw_abi.unhex32(s[4]),
               "checkpoint_interval_seconds": gov[4], "roster_hash": gw_abi.unhex32(s[5]),
               "token_set_root": gw_abi.unhex32(s[6]), "result_hash": gw_abi.unhex32(s[7]),
               "approval_threshold": gov[1], "operator_org": self.e.org_by_b32.get(gov[2], "org_unknown"),
               "organizations": sorted(orgs, key=lambda o: o["org_id"]), "proposal_ttl_hours": gov[3],
               "last_core_seq": s[9], "block_number": cb}
        if s[8] > 0 or self.chain.call("electionStateAtCheckpoint", el, 0, block=cb)[1] != 0:
            out["last_checkpoint"] = s[8]
        return out

    def nodes(self):
        items = []
        for n in self.cfg["nodes"]:
            from gw_chain import ChainClient
            c = ChainClient(n["url"], self.chain.chain_id, self.chain.contract, "", self.chain.depth, timeout=2)
            item = {"node_id": n["node_id"], "org_id": n["org_id"], "rpc_url": n["url"], "reachable": False}
            try:
                item["block_number"] = c.latest()
                item["reachable"] = True
                pc = c.peer_count()
                if pc is not None:
                    item["peer_count"] = pc
            except ChainError:
                pass
            items.append(item)
        return {"items": items}

    def health(self):
        try:
            self.chain.latest()
            reachable = True
        except ChainError:
            reachable = False
        pending = self.store.one("SELECT (SELECT COUNT(*) FROM events WHERE status IN ('QUEUED','SENT')) + "
                                 "(SELECT COUNT(*) FROM voter_events WHERE status='PENDING') AS n")["n"]
        degraded = (not reachable) or bool(self.e.anchor_blocked)
        return {"status": "degraded" if degraded else "ok", "chain_reachable": reachable, "pending_events": pending}
