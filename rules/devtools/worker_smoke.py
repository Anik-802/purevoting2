"""Create one disposable election on the LOCAL development stack; no formal scenario or ground truth.
Uses the Core interpreter (httpx and PyNaCl). Does not read databases or persist any credentials.
"""
import argparse
import base64
import hashlib
import json
import os
import secrets
import time
from datetime import datetime, timezone
from urllib.parse import urlsplit

import httpx
from nacl.public import PublicKey, SealedBox

def wait(read, what, seconds=120):
    until = time.monotonic() + seconds
    while time.monotonic() < until:
        value = read()
        if value:
            return value
        time.sleep(1)
    raise RuntimeError("timed out: " + what)

def stamp(t):
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

def smoke(url):
    if urlsplit(url).hostname not in {"127.0.0.1","localhost","::1"}:
        raise ValueError("Use the isolated local development stack only.")
    with httpx.Client(base_url=url, timeout=10, trust_env=False) as c:
        def req(method, path, code, **kwargs):
            r = c.request(method,path,**kwargs)
            if r.status_code != code:
                raise RuntimeError("unexpected HTTP status " + str(r.status_code))  # omit voter path/body
            return r
        login = {"username":os.environ["SMOKE_USERNAME"], "password":os.environ["SMOKE_PASSWORD"]}
        h = {"Authorization":"Bearer "+req("POST","/v1/auth/login",200,json=login).json()["access_token"]}
        start = (int(time.time()) + 15)//30*30 + 30
        election = {"title":"Rule worker HTTP smoke", "window_start":stamp(start), "window_end":stamp(start+60),
                    "time_bucket_seconds":30,"checkpoint_interval_seconds":30,
                    "candidates":[{"name":"A"},{"name":"B"}],"org_ids":["org_commission","org_neutral","org_observer"],
                    "approval_threshold":2}
        eid = req("POST","/v1/elections",201,json=election,headers=h).json()["election_id"]
        prefix = "/v1/elections/"+eid
        req("PUT",prefix+"/roster",200,json={"voters":[{"external_ref":f"SMK-{i}","name":f"Voter {i}"} for i in range(20)]},headers=h)
        wait(lambda: req("GET",prefix,200,headers=h).json()["creation_check"]=="PASSED","Core creation check")
        req("POST",prefix+"/roster:freeze",202,headers=h)
        csv = req("POST",prefix+"/tokens:issue",200,json={"delivery":"CSV"},headers=h|{"Idempotency-Key":secrets.token_hex(12)}).text
        token = csv.strip().splitlines()[1].rsplit("#t=",1)[1]
        print("Core created the election, froze the roster and issued the voting links.",flush=True)
        def opened():
            info = req("GET","/v1/public/elections/"+eid,200).json()
            return info if info["status"]=="OPEN" else None
        info=wait(opened,"scheduled opening")
        credential=base64.urlsafe_b64encode(secrets.token_bytes(16)).decode().rstrip("=")
        req("POST","/v1/credentials:redeem",200,json={"election_id":eid,"token":token,"nullifier_hash":hashlib.sha256(credential.encode()).hexdigest()})
        sealed=SealedBox(PublicKey(base64.b64decode(info["public_key"]))).encrypt(json.dumps({"candidate_id":info["candidates"][0]["candidate_id"]}).encode())
        receipt=req("POST","/v1/ballots",201,json={"election_id":eid,"credential":credential,"sealed_ballot":base64.b64encode(sealed).decode()}).json()["receipt_hash"]
        k=req("GET",prefix,200,headers=h).json()["open_checkpoint"]
        wait(lambda: req("GET","/v1/public/receipts/"+receipt,200).json()["status"]=="ANCHORED","stand-in anchor")
        print("Core accepted one sealed ballot. Waiting for the worker's findings at its checkpoint.",flush=True)
        def decisions():
            items=req("GET",prefix+"/findings",200,headers=h).json()["items"]
            rows=[f for f in items if f["checkpoint"]==k]
            return rows if len({f["rule_id"] for f in rows})==5 else None
        rows=wait(decisions,"all five stored rule findings",200)
        results={f["rule_id"]:f["result"] for f in rows}
        expected={"R1":"PASS","R2":"PASS","R3":"PASS","R4":"INCONCLUSIVE","R5":"INCONCLUSIVE"}
        ok = len(rows)==5 and results==expected and all("not implemented" in f["summary"].lower() for f in rows if f["rule_id"] in ("R4","R5"))
        print(json.dumps({"smoke":"developer HTTP check","core":"real","gateway":"development stand-in",
              "ballots_accepted":1,"checkpoint":k,"findings_stored":len(rows),"results":results,
              "matches_smoke_expectation":ok,"official_T01_run":False},sort_keys=True),flush=True)
        return 0 if ok else 1

if __name__=="__main__":
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("--create-local-election",action="store_true",required=True)
    p.add_argument("--core",default="http://127.0.0.1:8000")
    a=p.parse_args()
    raise SystemExit(smoke(a.core))
