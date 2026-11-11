// In-memory stand-in for Core (frontend stage). Function names match the operationIds of
// contracts/core-api.openapi.yaml so the real client can replace this file without touching the screens.
import {
  ApiError,
  type BallotReceipt,
  type Election,
  type ElectionCreate,
  type Finding,
  type PublicElection,
  type RosterSummary,
  type RosterVoter,
  type Tally,
  type VoterInput,
} from "./types";

/** what this backend can do: everything, in memory */
export const capabilities = { mode: "mock" as "mock" | "real", manualOpenClose: true, tally: true, finalize: true };

const wait = (ms = 350) => new Promise((r) => setTimeout(r, ms));
const hex = (n: number) => Array.from(crypto.getRandomValues(new Uint8Array(n)), (b) => b.toString(16).padStart(2, "0")).join("");

export async function sha256Hex(text: string): Promise<string> {
  const buf = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(text));
  return Array.from(new Uint8Array(buf), (b) => b.toString(16).padStart(2, "0")).join("");
}

const now = Date.now();
const iso = (offsetMin: number) => new Date(now + offsetMin * 60_000).toISOString();

const elections: Election[] = [
  {
    election_id: "el-2026-student",
    title: "2026 총학생회장 선거",
    status: "OPEN",
    window_start: iso(-30),
    window_end: iso(90),
    time_bucket_seconds: 60,
    public_key: "demo",
    created_at: iso(-120),
    voter_count: 100,
    creation_check: "PASSED",
    open_checkpoint: 3,
    counters: { as_of_checkpoint: 7, tokens_active: 58, tokens_used: 42, ballots_received: 41 },
    candidates: [
      { candidate_id: "c1", name: "기호 1번 김하늘", description: "등록금 동결, 열람실 24시간 개방" },
      { candidate_id: "c2", name: "기호 2번 이바다", description: "셔틀버스 증차, 학생 복지 기금 확대" },
      { candidate_id: "c3", name: "기호 3번 박구름", description: "온라인 수강신청 서버 개선" },
    ],
  },
  {
    election_id: "el-2026-dorm",
    title: "기숙사 자치회 대표 선거",
    status: "TALLIED",
    window_start: iso(-60 * 26),
    window_end: iso(-60 * 24),
    time_bucket_seconds: 60,
    public_key: "demo",
    created_at: iso(-60 * 48),
    voter_count: 60,
    creation_check: "PASSED",
    open_checkpoint: 2,
    counters: { as_of_checkpoint: 15, tokens_active: 9, tokens_used: 51, ballots_received: 51 },
    candidates: [
      { candidate_id: "d1", name: "기호 1번 정새벽" },
      { candidate_id: "d2", name: "기호 2번 한노을" },
    ],
  },
  {
    election_id: "el-2026-club",
    title: "동아리연합회 회장 선거",
    status: "DRAFT",
    window_start: iso(60 * 24 * 3),
    window_end: iso(60 * 24 * 3 + 10),
    time_bucket_seconds: 60,
    public_key: "demo",
    created_at: iso(-30),
    voter_count: 0,
    creation_check: "PASSED",
    open_checkpoint: 0,
    counters: { as_of_checkpoint: 0, tokens_active: 0, tokens_used: 0, ballots_received: 0 },
    candidates: [{ candidate_id: "k1", name: "기호 1번 오솔길" }],
  },
];

const findings: Record<string, Finding[]> = {
  "el-2026-student": [
    { finding_id: "f-1", rule_id: "R1", result: "PASS", checkpoint: 7, summary: "체인 이벤트 순서와 상태 전이가 일치합니다" },
    { finding_id: "f-2", rule_id: "R2", result: "PASS", checkpoint: 7, summary: "토큰 발급·사용 수가 명단과 일치합니다" },
    { finding_id: "f-3", rule_id: "R3", result: "PASS", checkpoint: 7, summary: "접수된 투표 수가 원장과 일치합니다" },
    { finding_id: "f-4", rule_id: "R4", result: "FAIL", checkpoint: 7, summary: "명단 고정 이후 추가된 행 1건 (row_ledger_mismatches)" },
    { finding_id: "f-5", rule_id: "R5", result: "INCONCLUSIVE", checkpoint: 7, summary: "체크포인트 8이 아직 확정되지 않았습니다" },
  ],
  "el-2026-dorm": (["R1", "R2", "R3", "R4", "R5"] as const).map((r, i) => ({
    finding_id: `g-${i}`,
    rule_id: r,
    result: "PASS" as const,
    checkpoint: 15,
    summary: "TALLY 체크포인트에서 검사 통과",
  })),
};

const tallies: Record<string, Tally> = {
  "el-2026-dorm": {
    ballot_count: 51,
    invalid_ballots: 0,
    results: [
      { candidate_id: "d1", votes: 29 },
      { candidate_id: "d2", votes: 22 },
    ],
    result_hash: hex(32),
  },
};

const tokens = new Map<string, "ISSUED" | "USED">([["demo-token", "ISSUED"]]);
const nullifiers = new Set<string>();
const receipts = new Map<string, BallotReceipt>();

const find = (id: string) => {
  const e = elections.find((x) => x.election_id === id);
  if (!e) throw new ApiError("NOT_FOUND", "선거를 찾을 수 없습니다");
  return e;
};

// ───────────── voter ─────────────
export async function getPublicElection(id: string): Promise<PublicElection> {
  await wait();
  const { election_id, title, status, window_start, window_end, time_bucket_seconds, candidates, public_key } = find(id);
  return { election_id, title, status, window_start, window_end, time_bucket_seconds, candidates, public_key };
}

export async function redeemToken(election_id: string, token: string, nullifier_hash: string) {
  await wait();
  if (find(election_id).status !== "OPEN") throw new ApiError("ELECTION_NOT_OPEN", "지금은 투표 기간이 아닙니다");
  const state = tokens.get(token) ?? (token ? "ISSUED" : undefined); // any non-empty token works in the mock
  if (!state) throw new ApiError("TOKEN_INVALID", "유효하지 않은 투표 링크입니다");
  if (state === "USED") throw new ApiError("TOKEN_ALREADY_USED", "이미 사용된 투표 링크입니다");
  tokens.set(token, "USED");
  nullifiers.add(nullifier_hash);
  return { election_id, status: "REDEEMED" as const };
}

export async function submitBallot(election_id: string, credential: string, candidate_id: string): Promise<BallotReceipt> {
  await wait(700);
  const nullifier_hash = await sha256Hex(credential);
  if (!nullifiers.has(nullifier_hash)) throw new ApiError("CREDENTIAL_UNKNOWN", "등록되지 않은 자격 증명입니다");
  // sealed box with the election public key comes in stage 4 (libsodium); the mock only hashes the choice
  const ballot_hash = await sha256Hex(`${candidate_id}:${hex(16)}`);
  const receipt_hash = await sha256Hex(JSON.stringify({ ballot_hash, election_id, nullifier_hash }));
  const receipt: BallotReceipt = { receipt_hash, status: "RECEIVED" };
  receipts.set(receipt_hash, receipt);
  find(election_id).counters.ballots_received += 1;
  // the ballot is anchored with the next time bucket (demo: a few seconds)
  setTimeout(() => receipts.set(receipt_hash, { receipt_hash, status: "ANCHORED", anchor_id: `anc-${hex(4)}`, tx_hash: `0x${hex(32)}` }), 6000);
  return receipt;
}

/** the mock keeps nothing in the browser */
export const hasPendingBallot = async (_credential: string) => false;
export const prepareBallot = () => {};
export const discardBallot = async (_credential: string) => {};

export async function getReceipt(receipt_hash: string): Promise<BallotReceipt> {
  await wait();
  const r = receipts.get(receipt_hash.trim().toLowerCase());
  if (!r) throw new ApiError("NOT_FOUND", "영수증을 찾을 수 없습니다");
  return r;
}

// ───────────── staff ─────────────
export async function login(username: string, password: string) {
  await wait();
  if (!username || !password) throw new ApiError("UNAUTHORIZED", "아이디와 비밀번호를 입력하세요");
  sessionStorage.setItem("pv_staff", username);
  return { username };
}

export const currentStaff = () => sessionStorage.getItem("pv_staff");
export const logout = () => sessionStorage.removeItem("pv_staff");

export async function listElections(): Promise<Election[]> {
  await wait();
  return elections.map((e) => ({ ...e }));
}

export async function getElection(id: string): Promise<Election> {
  await wait();
  return { ...find(id) };
}

export async function createElection(body: ElectionCreate, _idempotencyKey?: string): Promise<Election> {
  await wait();
  const e: Election = {
    ...body,
    election_id: `el-${hex(4)}`,
    status: "DRAFT",
    public_key: "demo",
    created_at: new Date().toISOString(),
    voter_count: 0,
    creation_check: "PENDING",
    open_checkpoint: 0,
    counters: { as_of_checkpoint: 0, tokens_active: 0, tokens_used: 0, ballots_received: 0 },
    candidates: body.candidates.map((c, i) => ({ candidate_id: `c${i + 1}`, ...c })),
  };
  elections.unshift(e);
  // creation check: Core reads the pinned node and compares ELECTION_CREATED (a few seconds in the mock)
  setTimeout(() => (e.creation_check = "PASSED"), 2500);
  return e;
}

const rosters = new Map<string, RosterVoter[]>();
const issued = new Map<string, Set<string>>(); // election → voter_keys that already have a link

export async function replaceRoster(id: string, voters: VoterInput[]): Promise<RosterSummary> {
  await wait();
  const e = find(id);
  if (e.status !== "DRAFT") throw new ApiError("ROSTER_FROZEN", "확정된 명단은 바꿀 수 없어요 (명단 정정은 기관 승인 필요)");
  if (!voters.length) throw new ApiError("VALIDATION_ERROR", "명단이 비어 있어요");
  if (new Set(voters.map((v) => v.external_ref)).size !== voters.length) throw new ApiError("VALIDATION_ERROR", "학번(external_ref)이 중복돼요");
  rosters.set(id, voters.map((v) => ({ voter_key: `vtr_${hex(13)}`, status: "ACTIVE", external_ref: v.external_ref, name: v.name })));
  e.voter_count = voters.length;
  const roster_hash = await sha256Hex(JSON.stringify(voters.map((v) => v.external_ref).sort()));
  return { voter_count: voters.length, roster_hash };
}

export async function readRoster(id: string): Promise<RosterVoter[]> {
  await wait(200);
  return (rosters.get(id) ?? []).map((v) => ({ ...v }));
}

export async function freezeRoster(id: string): Promise<Election> {
  await wait();
  const e = find(id);
  if (e.creation_check !== "PASSED") throw new ApiError("INVALID_STATE_TRANSITION", "선거 생성 검사가 끝나야 명단을 확정할 수 있어요");
  if (e.voter_count === 0) throw new ApiError("VALIDATION_ERROR", "명단을 먼저 등록하세요");
  e.status = "ROSTER_FROZEN";
  return { ...e };
}

/** CSV with columns external_ref,name,link (delivery = CSV); empty when everyone already has a link */
export async function issueTokens(id: string): Promise<string> {
  await wait();
  const e = find(id);
  if (e.creation_check !== "PASSED" || (e.status !== "ROSTER_FROZEN" && e.status !== "OPEN"))
    throw new ApiError("INVALID_STATE_TRANSITION", "명단 확정 후에만 링크를 발급할 수 있어요");
  const done = issued.get(id) ?? new Set<string>();
  issued.set(id, done);
  const voters = rosters.get(id) ?? Array.from({ length: e.voter_count }, (_, i) => ({ voter_key: `vtr_demo${i}`, status: "ACTIVE" as const, external_ref: `voter-${String(i + 1).padStart(3, "0")}`, name: `투표자 ${i + 1}` }));
  const todo = voters.filter((v) => v.status === "ACTIVE" && !done.has(v.voter_key));
  if (!todo.length) return "";
  todo.forEach((v) => done.add(v.voter_key));
  e.counters.tokens_active += todo.length;
  // CSV cells: neutralise spreadsheet formulas, quote when needed (the real Core must do the same)
  const q = (raw: string) => {
    const s = /^[=+\-@]/.test(raw) ? `'${raw}` : raw;
    return /[",\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
  };
  // token: 22-char base64url (hashing.md token_hash); Idempotency-Key and {delivery: "CSV"} come with the real client
  const token = () => btoa(String.fromCharCode(...crypto.getRandomValues(new Uint8Array(16)))).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
  return ["external_ref,name,link", ...todo.map((v) => [v.external_ref ?? "", v.name ?? "", `${location.origin}/vote/${id}#t=${token()}`].map(q).join(","))].join("\n");
}

export async function openElection(id: string) {
  await wait();
  find(id).status = "OPEN";
}

export async function closeElection(id: string) {
  await wait();
  find(id).status = "CLOSED";
}

export async function tally(id: string): Promise<Tally> {
  await wait(800);
  const e = find(id);
  e.status = "TALLIED";
  const total = e.counters.ballots_received;
  let left = total;
  const results = e.candidates.map((c, i) => {
    const votes = i === e.candidates.length - 1 ? left : Math.floor(left * (0.45 + 0.1 * Math.random()));
    left -= votes;
    return { candidate_id: c.candidate_id, votes };
  });
  tallies[id] = { ballot_count: total, invalid_ballots: 0, results, result_hash: hex(32) };
  return tallies[id];
}

export async function getTally(id: string): Promise<Tally | undefined> {
  await wait(150);
  return tallies[id];
}

export async function finalize(id: string): Promise<Election> {
  await wait();
  const open = (findings[id] ?? []).filter((f) => f.result !== "PASS" && !f.reviewed);
  if (open.length) throw new ApiError("FINDINGS_UNREVIEWED", `검토하지 않은 탐지 결과가 ${open.length}건 있습니다`);
  const e = find(id);
  e.status = "FINALIZED";
  return { ...e };
}

export async function listFindings(id: string): Promise<Finding[]> {
  await wait(200);
  return (findings[id] ?? []).map((f) => ({ ...f }));
}
