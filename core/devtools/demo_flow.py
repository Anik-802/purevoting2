"""Week 2 demo: one election from creation to close, in real time, against the running stack (about 3 minutes).

    docker compose --profile mock up -d --build
    docker compose exec core python -m devtools.demo_flow

It talks to Core over HTTP exactly as the admin web and the voter web will (login, create, roster, freeze, links,
redeem, ballot, receipt) and prints each step with what Core answered. The Gateway is the fake one (no chain); the
creation check reads its /rpc node stand-in (PINNED_NODE_URL=http://ledger-gateway:8100/rpc in .env). It creates a throwaway
admin account with a random password; no secrets are printed.
"""
import base64
import json
import secrets
import sys
import time
from datetime import datetime, timedelta, timezone

import httpx
import nacl.public
from sqlalchemy import text

from app import hashing
from app.auth import hash_password
from app.db import get_engine
from app.ids import ulid
from app.timeutil import iso

CORE = "http://127.0.0.1:8000"
B = 30            # time bucket and checkpoint interval (seconds)
WINDOW = 60       # voting window: 2 buckets
VOTERS = 20       # D-10: VOTERS * B >= 10 * WINDOW
VOTES = 3
KST = timezone(timedelta(hours=9))

c = httpx.Client(base_url=CORE, timeout=10)
step_no = 0


def clock(t: float | None = None) -> str:
    return datetime.fromtimestamp(time.time() if t is None else t, KST).strftime("%H:%M:%S")


def step(title: str) -> None:
    global step_no
    step_no += 1
    print(f"\n[{clock()}] {step_no}. {title}", flush=True)


def ok(msg: str) -> None:
    print(f"           ✔ {msg}", flush=True)


def info(msg: str) -> None:
    print(f"             {msg}", flush=True)


def fail(msg: str) -> None:
    print(f"           ✘ {msg}", flush=True)
    sys.exit(1)


def expect(r: httpx.Response, *codes: int) -> httpx.Response:
    if r.status_code not in codes:
        fail(f"HTTP {r.status_code}: {r.text[:300]}")
    return r


def wait_until(t: int, why: str) -> None:
    left = t - time.time()
    if left > 0:
        info(f"⏳ {why}까지 {int(left) + 1}초 대기 ({clock(t)})")
        time.sleep(left)


def poll(fn, timeout: int, why: str):
    end = time.time() + timeout
    while time.time() < end:
        v = fn()
        if v:
            return v
        time.sleep(1)
    fail(f"{timeout}초 안에 {why} 확인 실패")


def main() -> None:
    print("=" * 64)
    print(" PureVoting 2.0 Core — 선거 진행 흐름 시연 (실제 시간)")
    print(f" 서버 {CORE} · 가짜 Gateway · 시간 구간 {B}초 · 투표 시간 {WINDOW}초")
    print("=" * 64)

    step("서버 상태 확인")
    expect(c.get("/readyz"), 200)
    ok("Core API와 DB가 응답함 (/readyz 200)")

    step("관리자 로그인 (선관위 admin)")
    username, password = "demo_" + secrets.token_hex(3), secrets.token_urlsafe(16)
    with get_engine().begin() as conn:
        conn.execute(text("INSERT INTO credential.staff_users (user_id, username, password_hash, org_id, role) "
                          "VALUES (:i, :u, :p, 'org_commission', 'admin')"),
                     {"i": ulid("usr"), "u": username, "p": hash_password(password)})
    tok = expect(c.post("/v1/auth/login", json={"username": username, "password": password}), 200).json()
    auth = {"Authorization": f"Bearer {tok['access_token']}"}
    ok(f"시연용 계정 {username} 로그인 → 접근 토큰(JWT) 발급")

    step("선거 생성")
    now = time.time()
    start = (int(now) + 100) // B * B + B          # room for the creation check, freeze and link issue
    end = start + WINDOW
    r = expect(c.post("/v1/elections", headers=auth, json={
        "title": "시연 선거", "window_start": iso(start), "window_end": iso(end),
        "time_bucket_seconds": B, "checkpoint_interval_seconds": B,
        "candidates": [{"name": "후보 김"}, {"name": "후보 이"}],
        "org_ids": ["org_commission", "org_neutral", "org_observer"], "approval_threshold": 2}), 201).json()
    eid = r["election_id"]
    ok(f"선거 ID {eid}")
    info(f"상태 {r['status']} · 투표 시간 {clock(start)} ~ {clock(end)} · 후보 2명 · 참여 기관 3곳")
    info("선거 생성 기록(ELECTION_CREATED)을 블록체인 Gateway로 보내는 중")

    step("유권자 명단 등록")
    voters = [{"external_ref": f"S{i:03d}", "name": f"유권자 {i}"} for i in range(1, VOTERS + 1)]
    expect(c.put(f"/v1/elections/{eid}/roster", headers=auth, json={"voters": voters}), 200)
    ok(f"{VOTERS}명 등록")

    step("블록체인 생성 확인 대기")
    early = c.post(f"/v1/elections/{eid}/roster:freeze", headers=auth)
    if early.status_code == 409:
        ok(f"확인 전 명단 확정 시도 → 거부됨 ({early.json().get('code')}): 확인 전에는 다음 단계로 못 감")
    e = poll(lambda: (x := c.get(f"/v1/elections/{eid}", headers=auth).json())["creation_check"] == "PASSED" and x,
             90, "생성 확인")
    ok(f"Gateway가 생성 기록 수락({e['transition']['ledger_status']}) → 생성 확인 PASSED")

    step("명단 확정 (이후 명단 변경 불가)")
    r = expect(c.post(f"/v1/elections/{eid}/roster:freeze", headers=auth), 202).json()
    ok(f"상태 {r['status']} · 체크포인트 0 기록(그 시점 DB 상태 고정)")
    again = c.put(f"/v1/elections/{eid}/roster", headers=auth, json={"voters": voters})
    ok(f"확정 후 명단 수정 시도 → 거부됨 ({again.status_code} {again.json().get('code')})")

    step("일회용 투표 링크 발급")
    r = expect(c.post(f"/v1/elections/{eid}/tokens:issue",
                      headers={**auth, "Idempotency-Key": "demo-" + secrets.token_hex(4)},
                      json={"delivery": "CSV"}), 200)
    lines = r.text.strip().splitlines()
    tokens = [ln.rsplit("#t=", 1)[1] for ln in lines[1:]]
    ok(f"링크 {len(tokens)}개 발급 (CSV, 서버에는 링크 원문을 저장하지 않음)")
    link = lines[1].rsplit(",", 1)[1]
    info(f"예시: {link.split('#t=')[0]}#t=••••••")

    step("투표 시작 시각에 자동 개시")
    wait_until(start, "투표 시작")
    pub = poll(lambda: (x := c.get(f"/v1/public/elections/{eid}").json())["status"] == "OPEN" and x, 30, "자동 개시")
    ok(f"상태 OPEN (Gateway가 개시 기록을 수락한 뒤에만 열림) · {clock()}")

    step(f"투표자 {VOTES}명 투표")
    pk = nacl.public.PublicKey(base64.b64decode(pub["public_key"]))
    receipts = []
    for i in range(VOTES):
        cand = pub["candidates"][i % 2]
        credential = base64.urlsafe_b64encode(secrets.token_bytes(16)).rstrip(b"=").decode()
        nullifier = hashing.H(credential)
        expect(c.post("/v1/credentials:redeem",
                      json={"election_id": eid, "token": tokens[i], "nullifier_hash": nullifier}), 200)
        sealed = nacl.public.SealedBox(pk).encrypt(json.dumps({"candidate_id": cand["candidate_id"]}).encode())
        rec = expect(c.post("/v1/ballots", json={"election_id": eid, "credential": credential,
                                                 "sealed_ballot": base64.b64encode(sealed).decode()}), 201).json()
        receipts.append(rec["receipt_hash"])
        ok(f"투표자 {i + 1}: 링크 사용 → 익명 투표권 생성 → 암호화한 표 제출 → 영수증 {rec['receipt_hash'][:12]}… ({rec['status']})")
    info("링크와 표는 따로 저장되어 누가 어떤 표를 냈는지 연결할 수 없음")

    step("부정 시도 차단 확인")
    r = c.post("/v1/credentials:redeem",
               json={"election_id": eid, "token": tokens[0], "nullifier_hash": hashing.H("again-" + secrets.token_hex(4))})
    ok(f"이미 쓴 링크로 다시 투표 시도 → 거부됨 ({r.status_code} {r.json().get('code')})")
    r = c.post("/v1/credentials:redeem", json={"election_id": eid, "token": "A" * 22, "nullifier_hash": "0" * 64})
    ok(f"가짜 링크로 투표 시도 → 거부됨 ({r.status_code} {r.json().get('code')})")

    step("투표 종료 시각에 자동 마감")
    wait_until(end, "투표 종료")
    r = c.post("/v1/credentials:redeem",
               json={"election_id": eid, "token": tokens[VOTES], "nullifier_hash": hashing.H("late")})
    ok(f"마감 후 투표 시도 → 거부됨 ({r.status_code} {r.json().get('code')})")
    pub = poll(lambda: (x := c.get(f"/v1/public/elections/{eid}").json())["status"] == "CLOSED" and x, 30, "마감")
    ok(f"상태 {pub['status']}")

    step("블록체인 기록과 집계 확인")
    e = poll(lambda: (x := c.get(f"/v1/elections/{eid}", headers=auth).json())["counters"]["ballots_received"]
             == VOTES and x["transition"]["ledger_status"] == "ACCEPTED" and x, 60, "마감 기록")
    cn = e["counters"]
    ok(f"체크포인트 {cn['as_of_checkpoint']} 기준: 사용된 링크 {cn['tokens_used']}개 · 접수된 표 {cn['ballots_received']}개 "
       f"· 발급된 링크 {cn['tokens_active']}개")
    ok(f"마감 기록(ELECTION_CLOSED)을 Gateway가 수락: {e['transition']['ledger_status']}")
    for i, h in enumerate(receipts):
        rec = poll(lambda: (x := c.get(f"/v1/public/receipts/{h}").json())["status"] == "ANCHORED" and x, 60,
                   "영수증 블록체인 기록")
        ok(f"영수증 {i + 1} {h[:12]}… → {rec['status']} (블록체인 기록됨)")

    print("\n" + "=" * 64)
    print(f" 시연 완료 [{clock()}]: 생성 → 명단 → 확정 → 링크 → 개시 → 투표 → 마감 → 기록 확인")
    print(" ※ 개표·결과 공개는 이후 주차 범위, Gateway는 아직 가짜(3주차에 실제로 교체)")
    print("=" * 64)


if __name__ == "__main__":
    main()
