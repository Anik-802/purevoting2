"""Trustee tool of one organization (D-30, contracts/ballot-elgamal.md). Needs no database and no staff login.

    python -m app.scripts.trustee keygen --org org_observer --secret org_observer.secret.json
        makes the organization's key: the secret file stays with the organization (never commit or send it); the
        public record printed on stdout goes to the deployment (app.scripts.register_trustee)
    GATEWAY_AUDITOR_KEY=... python -m app.scripts.trustee decrypt --core https://vote.example \\
            --gateway http://ledger-gateway:8100 --election elc_... --secret org_observer.secret.json
        after closing: reads the ballots from Core and the ledger's events from the Gateway (the organization's
        read-only auditorKey). Core must hand over the bytes of every ledger-accepted ballot; the tool decides itself
        which are valid, recomputes the sum and only then sends this organization's decryption share of that sum. It
        refuses a second, different sum for the same election (record kept next to the secret file), so Core can
        neither leave ballots out to isolate one nor obtain the difference of two sums.
"""
import argparse
import base64
import hashlib
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

from app import elgamal


def _http(method: str, url: str, body: dict | None = None, headers: dict | None = None) -> dict:
    data = None if body is None else json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as err:
        raise SystemExit(f"{method} {url}: {err.code} {err.read().decode('utf-8', 'replace')}") from None


def keygen(org: str, path: str) -> int:
    if os.path.exists(path):
        raise SystemExit(f"{path} exists; refusing to overwrite a key")
    x, record = elgamal.trustee_keygen(org)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump({"org_id": org, "secret": elgamal.enc_scalar(x).hex(), "record": record}, f)
    print(json.dumps(record))
    return 0


def ledger_view(gateway: str, key: str, election: str, final_k: int) -> dict:
    """From the Gateway's stored (= ACCEPTED) events: {"ballots": {(nullifier_hash, ballot_hash)} with checkpoint <=
    final_k, "public_key_hash": of ELECTION_CREATED or None}."""
    ballots, pkh, cursor = set(), None, None
    while True:
        q = {"election_id": election, "type": ["BALLOT_RECEIVED", "ELECTION_CREATED"], "checkpoint_max": final_k,
             "limit": 500}
        if cursor:
            q["cursor"] = cursor
        page = _http("GET", gateway.rstrip("/") + "/v1/events?" + urllib.parse.urlencode(q, doseq=True),
                     headers={"X-Service-Key": key} if key else None)
        for ev in page["items"]:
            if ev.get("type") == "BALLOT_RECEIVED" and ev.get("checkpoint", final_k + 1) <= final_k:
                ballots.add((ev["payload"]["nullifier_hash"], ev["payload"]["ballot_hash"]))
            elif ev.get("type") == "ELECTION_CREATED":
                pkh = ev["payload"]["public_key_hash"]
        cursor = page.get("next_cursor")
        if not cursor:
            return {"ballots": ballots, "public_key_hash": pkh}


def check(inp: dict, mine: dict, election: str, ledger: dict | None, allow_unavailable: int = 0) -> list:
    """The sum recomputed here from every listed ballot; SystemExit on anything that does not add up. ledger None skips
    the comparison with the Gateway (only for tests and a stack without one)."""
    if inp["election_id"] != election:
        raise SystemExit("Core answered for another election")
    if inp["ballot_scheme"] != "ELGAMAL_ED25519_V1":
        raise SystemExit("not an ElGamal election")
    records = inp["trustee_keys"]
    if mine not in records:
        raise SystemExit("this organization's key is not a trustee key of the election")
    if not all(elgamal.verify_trustee_key(r) for r in records):
        raise SystemExit("a trustee key of the election does not verify")
    Y = elgamal.joint_key(records)
    if Y.hex() != inp["public_key"]:
        raise SystemExit("the election key is not the sum of the trustee keys")
    listed = {(b["nullifier_hash"], b["ballot_hash"]) for b in inp["ballots"]}
    if len(listed) != len(inp["ballots"]) or len({n for n, _ in listed}) != len(listed):
        raise SystemExit("Core lists a ballot or a credential twice")
    if ledger is not None:
        if ledger["public_key_hash"] != hashlib.sha256(Y).hexdigest():
            raise SystemExit("the election key is not the one anchored in ELECTION_CREATED")
        # exactly the ledger's ballots, so Core can neither add ballots it made itself nor leave some out
        if listed != ledger["ballots"]:
            raise SystemExit("Core's ballot list is not the ledger's accepted BALLOT_RECEIVED set")
    n, cts, unavailable = len(inp["candidate_ids"]), [], 0
    for b in inp["ballots"]:
        if b["sealed_ballot"] is None:
            unavailable += 1                 # Core has no matching row: its bytes cannot be checked here
            continue
        raw = base64.b64decode(b["sealed_ballot"], validate=True)
        if hashlib.sha256(raw).hexdigest() != b["ballot_hash"]:
            raise SystemExit("a listed ballot does not match its hash")
        ct = elgamal.verify_ballot(raw, Y, election, b["nullifier_hash"], n)
        if ct is not None:
            cts.append(ct)
    if unavailable > allow_unavailable:
        # every ballot left out narrows the sum; leaving out all but one would decrypt that one
        raise SystemExit(f"Core cannot show {unavailable} ledger ballot(s); refusing (see --allow-unavailable)")
    if len(cts) != inp["valid_ballots"]:
        raise SystemExit(f"Core counts {inp['valid_ballots']} valid ballots, this check {len(cts)}")
    agg = elgamal.aggregate(cts, n)
    if [{"a": a.hex(), "b": b.hex()} for a, b in agg] != inp["aggregate"]:
        raise SystemExit("Core's aggregate is not the sum of the listed ballots")
    return agg


def remember(secret_path: str, election: str, agg: list) -> None:
    """One sum per election: a different one later would let Core subtract two sums."""
    path = secret_path + ".shared.json"
    seen = {}
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            seen = json.load(f)
    digest = hashlib.sha256(b"".join(a + b for a, b in agg)).hexdigest()
    if seen.get(election, digest) != digest:
        raise SystemExit("this organization already decrypted a different aggregate for this election; refusing")
    seen[election] = digest
    with open(path + ".tmp", "w", encoding="utf-8") as f:
        json.dump(seen, f)
    os.replace(path + ".tmp", path)          # never a half-written record


def decrypt(core: str, gateway: str | None, election: str, path: str, allow_unavailable: int = 0) -> int:
    with open(path, encoding="utf-8") as f:
        secret = json.load(f)
    base = core.rstrip("/") + f"/v1/public/elections/{election}"
    inp = _http("GET", base + "/tally-input")
    ledger = None
    if gateway:
        # the organization's read-only Gateway key; the real Gateway answers 401 without it
        ledger = ledger_view(gateway, os.environ.get("GATEWAY_AUDITOR_KEY", ""), election, inp["final_checkpoint"])
    agg = check(inp, secret["record"], election, ledger, allow_unavailable)
    print(f"{len(inp['ballots'])} ballots listed, {inp['valid_ballots']} valid; aggregate checked"
          + ("" if ledger is None else "; list equals the ledger"))
    if not inp["valid_ballots"]:
        print("no valid ballot: nothing to decrypt")
        return 0
    remember(path, election, agg)
    share = elgamal.decryption_share(int.from_bytes(bytes.fromhex(secret["secret"]), "little"), secret["org_id"],
                                     election, agg)
    out = _http("POST", base + "/decryption-shares", share)
    missing = out["missing_shares"]
    print(f"{secret['org_id']}: share accepted; " + ("still missing: " + ", ".join(missing) if missing else "all shares in"))
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    k = sub.add_parser("keygen")
    k.add_argument("--org", required=True)
    k.add_argument("--secret", required=True, help="file to create for the secret (mode 600)")
    d = sub.add_parser("decrypt")
    d.add_argument("--core", required=True, help="Core base URL, e.g. https://desktop-xxx.ts.net:8443")
    d.add_argument("--gateway", help="Ledger Gateway URL; GATEWAY_AUDITOR_KEY holds the read-only key")
    d.add_argument("--no-ledger-check", action="store_true",
                   help="skip the comparison with the Gateway (Core could then make this tool decrypt a chosen ballot)")
    d.add_argument("--allow-unavailable", type=int, default=0, metavar="N",
                   help="accept up to N ledger ballots whose bytes Core cannot show (a damaged row, after a person "
                        "checked why); each one left out narrows the sum")
    d.add_argument("--election", required=True)
    d.add_argument("--secret", required=True)
    a = p.parse_args(argv)
    if a.cmd == "keygen":
        return keygen(a.org, a.secret)
    if not a.gateway and not a.no_ledger_check:
        p.error("--gateway is required (or --no-ledger-check, which gives up the protection against a dishonest Core)")
    return decrypt(a.core, None if a.no_ledger_check else a.gateway, a.election, a.secret, a.allow_unavailable)


if __name__ == "__main__":
    sys.exit(main())
