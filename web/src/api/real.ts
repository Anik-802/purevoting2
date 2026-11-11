// Real Core client (contracts/core-api.openapi.yaml). Same exported names and shapes as ./mock, so the screens do not
// change; ./index picks one at build time (VITE_API_MODE). Paths are same-origin: nginx (compose `web`, D-29) or the
// Vite dev proxy forwards /v1/ to Core.
import { H, receiptHash } from "../crypto/hashing";
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

/** what this backend can do today; Core's tally / finalize arrive 11-09, opening and closing follow the window */
export const capabilities = { mode: "real" as const, manualOpenClose: false, tally: true, finalize: true };

// demo organizations (docs/system-design.md 2.3); their signer keys come from the on-chain registry, never from here
const ORG_IDS = (import.meta.env.VITE_ORG_IDS ?? "org_commission,org_neutral,org_observer").split(",");
const APPROVAL_THRESHOLD = 2;

const KO: Record<string, string> = {
  TOKEN_INVALID: "유효하지 않은 투표 링크예요. 받은 링크를 그대로 열어 주세요.",
  TOKEN_ALREADY_USED: "이미 사용된 투표 링크예요.",
  NULLIFIER_ALREADY_REGISTERED: "이미 등록된 투표권이에요.",
  CREDENTIAL_UNKNOWN: "이 링크는 이미 다른 기기나 브라우저에서 사용됐어요. 본인이 투표하지 않았다면 선거관리 담당자에게 알려 주세요.",
  CONFLICT: "링크는 이미 발급됐어요. 원문은 처음 받은 CSV에만 있고, 다시 받으려면 기관 승인이 필요한 재발급(TOKEN_REISSUE)이 필요해요 (화면 준비 중).",
  CREDENTIAL_ALREADY_USED: "이 투표권으로 이미 다른 표를 냈어요.",
  ELECTION_NOT_OPEN: "지금은 투표 기간이 아니에요.",
  RATE_LIMITED: "요청이 많아요. 잠시 뒤 다시 시도해 주세요.",
  ROSTER_FROZEN: "확정된 명단은 바꿀 수 없어요 (명단 정정은 기관 승인 필요).",
  INVALID_STATE_TRANSITION: "지금 상태에서는 할 수 없는 작업이에요.",
  FINDINGS_UNREVIEWED: "확정할 수 없어요: TALLY 체크포인트의 R1–R5 결과가 모두 기록되고, 이상 탐지·판단 보류가 기관 검토로 해결돼야 해요.",
  AUDIT_UNRESOLVED: "문제가 확정된 탐지 결과가 있어 결과를 확정할 수 없어요. 선거 무효(기관 승인)만 가능해요.",
  OUTBOX_NOT_DRAINED: "마감 직후라 원장 기록이 아직 끝나지 않았어요. 잠시 뒤 다시 개표해 주세요.",
  LEDGER_UNAVAILABLE: "원장(Gateway)에 연결할 수 없어요. 잠시 뒤 다시 시도해 주세요.",
  UNAUTHORIZED: "다시 로그인해 주세요.",
  NOT_FOUND: "찾을 수 없어요.",
};

const SESSION = "pv_staff_session";
type Session = { token: string; name: string; expires: number };

function session(): Session | undefined {
  try {
    const s = JSON.parse(sessionStorage.getItem(SESSION) ?? "null") as Session | null;
    return s && s.expires > Date.now() ? s : undefined;
  } catch {
    return undefined;
  }
}

async function call<T>(method: string, path: string, opts: { body?: unknown; auth?: boolean; headers?: Record<string, string>; text?: boolean } = {}): Promise<T> {
  const headers: Record<string, string> = { ...opts.headers };
  if (opts.body !== undefined) headers["Content-Type"] = "application/json";
  if (opts.auth) {
    const s = session();
    if (!s) throw new ApiError("UNAUTHORIZED", KO.UNAUTHORIZED);
    headers.Authorization = `Bearer ${s.token}`;
  }
  let res: Response;
  for (let attempt = 0; ; attempt++) {
    try {
      res = await fetch(path, { method, headers, body: opts.body === undefined ? undefined : JSON.stringify(opts.body) });
    } catch {
      throw new ApiError("NETWORK", "서버에 연결할 수 없어요. 잠시 뒤 다시 시도해 주세요.");
    }
    // 429 comes from the public entry before Core sees the request (D-29), so repeating it is always safe
    if (res.status !== 429 || attempt >= 3) break;
    await new Promise((r) => setTimeout(r, (Number(res.headers.get("Retry-After")) || 2) * 1000 * (attempt + 1)));
  }
  if (!res.ok) {
    const p = (await res.json().catch(() => ({}))) as { code?: string; detail?: string; title?: string };
    const code = p.code ?? `HTTP_${res.status}`;
    if (res.status === 401 && opts.auth) {
      sessionStorage.removeItem(SESSION);
      location.assign("/admin/login");
    }
    throw new ApiError(code, KO[code] ?? p.detail ?? p.title ?? `요청이 실패했어요 (${res.status})`);
  }
  if (opts.text) return (await res.text()) as T;
  return (res.status === 204 ? undefined : await res.json()) as T;
}

const idemKey = () => crypto.randomUUID();
const enc = encodeURIComponent;

// ───────────── voter ─────────────
export const sha256Hex = (text: string) => H(text);

const publicCache = new Map<string, PublicElection>();
export async function getPublicElection(id: string): Promise<PublicElection> {
  const e = await call<PublicElection>("GET", `/v1/public/elections/${enc(id)}`);
  publicCache.set(id, e);
  return e;
}

export async function redeemToken(election_id: string, token: string, nullifier_hash: string) {
  return call<{ election_id: string; status: "REDEEMED" }>("POST", "/v1/credentials:redeem", { body: { election_id, token, nullifier_hash } });
}

// The sealed ballot is kept only until Core confirms it, keyed by H(credential) so voters sharing a browser never mix;
// the choice itself is never stored in clear (threat-model S9).
const sealedKey = async (credential: string) => `pv_sealed_${await H(credential)}`;

/** a sealed ballot from an earlier attempt whose answer was lost: it must be resent as is, never re-sealed */
export const hasPendingBallot = async (credential: string) => localStorage.getItem(await sealedKey(credential)) !== null;

/** drops a sealed ballot that can never be accepted (its credential is unknown to Core) */
export const discardBallot = async (credential: string) => localStorage.removeItem(await sealedKey(credential));

/** starts loading libsodium while the voter is choosing (the seal chunk is large) */
export const prepareBallot = () => void import("../crypto/seal").catch(() => {});

/**
 * Seals the choice and stores the sealed bytes BEFORE sending (a retry must send identical bytes, core-api
 * submitBallot); an earlier stored ballot is always resent instead. Core's receipt is checked against the one the browser
 * computes itself (hashing.md receipt_hash), then the stored bytes are deleted.
 */
export async function submitBallot(election_id: string, credential: string, candidate_id: string): Promise<BallotReceipt> {
  const e = publicCache.get(election_id) ?? (await getPublicElection(election_id));
  const key = await sealedKey(credential);
  let sealed = localStorage.getItem(key);
  if (!sealed) {
    if (!e.candidates.some((c) => c.candidate_id === candidate_id)) throw new ApiError("VALIDATION_ERROR", "후보를 다시 선택해 주세요.");
    sealed = await (await import("../crypto/seal")).sealBallot(e.public_key, candidate_id);
  }
  localStorage.setItem(key, sealed);
  const receipt = await call<BallotReceipt>("POST", "/v1/ballots", { body: { election_id, credential, sealed_ballot: sealed } });
  const bytes = Uint8Array.from(atob(sealed), (c) => c.charCodeAt(0));
  const mine = await receiptHash(election_id, await H(credential), await H(bytes));
  if (mine !== receipt.receipt_hash) throw new ApiError("RECEIPT_MISMATCH", "서버가 준 영수증이 내 브라우저가 계산한 값과 달라요. 화면을 캡처해 선거관리 담당자에게 알려 주세요.");
  localStorage.removeItem(key);
  return receipt;
}

export const getReceipt = (receipt_hash: string) => call<BallotReceipt>("GET", `/v1/public/receipts/${enc(receipt_hash.trim().toLowerCase())}`);

// ───────────── staff ─────────────
export async function login(username: string, password: string) {
  if (!username || !password) throw new ApiError("UNAUTHORIZED", "아이디와 비밀번호를 입력하세요");
  try {
    const r = await call<{ access_token: string; expires_in: number; user: { display_name: string } }>("POST", "/v1/auth/login", { body: { username, password } });
    sessionStorage.setItem(SESSION, JSON.stringify({ token: r.access_token, name: r.user.display_name, expires: Date.now() + r.expires_in * 1000 }));
    return { username: r.user.display_name };
  } catch (err) {
    if (err instanceof ApiError && err.code === "UNAUTHORIZED") throw new ApiError("UNAUTHORIZED", "아이디 또는 비밀번호가 맞지 않아요");
    throw err;
  }
}

export const currentStaff = () => session()?.name ?? null;
export const logout = () => sessionStorage.removeItem(SESSION);

export const listElections = async () => (await call<{ items: Election[] }>("GET", "/v1/elections", { auth: true })).items;
export const getElection = (id: string) => call<Election>("GET", `/v1/elections/${enc(id)}`, { auth: true });

/** idempotencyKey: one per form submission, so a lost answer or a double click never creates two elections */
export async function createElection(body: ElectionCreate, idempotencyKey: string = idemKey()): Promise<Election> {
  return call<Election>("POST", "/v1/elections", {
    auth: true,
    headers: { "Idempotency-Key": idempotencyKey },
    body: {
      title: body.title,
      window_start: body.window_start.replace(/\.\d{3}Z$/, "Z"),
      window_end: body.window_end.replace(/\.\d{3}Z$/, "Z"),
      time_bucket_seconds: body.time_bucket_seconds,
      checkpoint_interval_seconds: body.time_bucket_seconds, // D-14 default: 1 × bucket
      // the contract's Candidate has no description; pledges stay a mock-only field for now
      candidates: body.candidates.map((c) => ({ name: c.name })),
      org_ids: ORG_IDS,
      approval_threshold: APPROVAL_THRESHOLD,
    },
  });
}

export const replaceRoster = (id: string, voters: VoterInput[]) => call<RosterSummary>("PUT", `/v1/elections/${enc(id)}/roster`, { auth: true, body: { voters } });

export const readRoster = async (id: string) => (await call<{ voters: RosterVoter[] }>("POST", `/v1/elections/${enc(id)}/roster:read`, { auth: true })).voters;

export const freezeRoster = (id: string) => call<Election>("POST", `/v1/elections/${enc(id)}/roster:freeze`, { auth: true });

/** CSV (external_ref,name,link); the same Idempotency-Key is reused for 14 of the contract's 15 minutes, so a lost
 * response returns the same CSV */
export async function issueTokens(id: string): Promise<string> {
  const slot = `pv_issue_${id}`;
  const saved = JSON.parse(sessionStorage.getItem(slot) ?? "null") as { key: string; at: number } | null;
  const key = saved && Date.now() - saved.at < 14 * 60_000 ? saved.key : idemKey();
  sessionStorage.setItem(slot, JSON.stringify({ key, at: saved?.key === key ? saved.at : Date.now() }));
  const csv = await call<string>("POST", `/v1/elections/${enc(id)}/tokens:issue`, { auth: true, headers: { "Idempotency-Key": key }, body: { delivery: "CSV" }, text: true });
  sessionStorage.removeItem(slot);
  return csv;
}

type StoredFinding = { finding_id: string; rule_id: Finding["rule_id"]; result: Finding["result"]; checkpoint: number; summary: string; review: { status: string } };
export async function listFindings(id: string): Promise<Finding[]> {
  const { items } = await call<{ items: StoredFinding[] }>("GET", `/v1/elections/${enc(id)}/findings`, { auth: true });
  return items.map((f) => ({
    finding_id: f.finding_id,
    rule_id: f.rule_id,
    result: f.result,
    checkpoint: f.checkpoint,
    summary: f.summary,
    reviewed: ["RESOLVED", "SUPERSEDED", "NOT_REQUIRED"].includes(f.review?.status),
    confirmed_issue: f.review?.status === "CONFIRMED_ISSUE", // reviewed, and it blocks finalization
  }));
}

const notYet = (what: string) => async (_id: string): Promise<never> => {
  throw new ApiError("NOT_IMPLEMENTED", `${what}은 아직 Core에 없어요 (Core 11-09). 지금은 mock 모드에서만 볼 수 있어요.`);
};
export const openElection = notYet("수동 투표 시작");
export const closeElection = notYet("수동 투표 마감");
export const tally = (id: string) => call<Tally>("POST", `/v1/elections/${enc(id)}/tally`, { auth: true });

/** 202: requested; the election becomes FINALIZED once the Gateway accepts ELECTION_FINALIZED */
export async function finalize(id: string): Promise<Election> {
  try {
    return await call<Election>("POST", `/v1/elections/${enc(id)}:finalize`, { auth: true });
  } catch (err) {
    // TALLIED but the TALLY checkpoint is not on the ledger yet (it follows the accepted TALLY_PUBLISHED)
    if (err instanceof ApiError && err.code === "INVALID_STATE_TRANSITION")
      throw new ApiError(err.code, "개표 결과가 아직 원장에 기록되는 중이에요. 1~2분 뒤 다시 시도해 주세요.");
    throw err;
  }
}

export async function getTally(id: string): Promise<Tally | undefined> {
  try {
    return await call<Tally>("GET", `/v1/elections/${enc(id)}/tally`, { auth: true });
  } catch (err) {
    if (err instanceof ApiError && err.code === "NOT_FOUND") return undefined;
    throw err;
  }
}
