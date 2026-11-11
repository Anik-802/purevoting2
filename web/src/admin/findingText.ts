import type { Finding } from "../api/types";

// The Rule Engine writes `summary` in English and signs it into the finding record (its hash is on chain), so the
// stored text never changes; the screen shows a Korean reading of it and keeps the original one click away.

export const RULE_NAME: Record<Finding["rule_id"], string> = {
  R1: "중복·상태",
  R2: "투표 시간",
  R3: "개수 맞춤",
  R4: "관리 작업",
  R5: "기록 무결성",
};

const REASON: Record<string, string> = {
  BATCH_NOT_ANCHORED: "아직 체인에 충분히 확정되지 않은 묶음이 있어 판단을 미뤘어요. 다음 검사에서 다시 봐요.",
  NODE_UNAVAILABLE: "블록체인 노드를 읽지 못해 판단을 미뤘어요. 다음 검사에서 다시 봐요.",
  VIEW_UNAVAILABLE: "Core의 기록을 읽지 못해 판단을 미뤘어요. 다음 검사에서 다시 봐요.",
};

// "Checks not implemented: ..." items of rules/ (R4, R5)
const CHECK: Record<string, string> = {
  "Verify request, approval and review signatures against node-read organization keys and quorum":
    "요청·승인·검토 서명을 노드에서 읽은 기관 키와 정족수로 검증",
  "Replay roster/token effects as exact sets in seq order, including effect windows and roster-hash chains":
    "명단·토큰 변경을 순서대로 다시 적용해 정확히 맞는지 확인",
  "Compare creation organizations/operator and proposal state to independent contract getters":
    "선거 생성 기관·운영 기관·제안 상태를 계약에서 직접 읽어 대조",
  "Detect all row changes between historical checkpoints without matching administrative events":
    "관리 작업 없이 바뀐 과거 기록 찾기",
  "Read pinned-node anchorAt and independently verify event/rejection Merkle proofs and roots":
    "고정 노드에서 앵커를 읽어 이벤트·거부 기록의 머클 증명을 직접 검증",
  "Replay full election state and compare electionStateAtCheckpoint and electionGovernance getters":
    "선거 상태 전체를 다시 계산해 계약의 상태 값과 대조",
  "Recompute all roster/token/config roots and verify revealed key derivation and commitment":
    "명단·토큰·설정 지문을 다시 계산하고 공개된 개인키가 맞는지 확인",
  "Decrypt ledger-accepted sealed ballots and independently reproduce tally results and invalid set":
    "장부에 기록된 표를 직접 열어 개표 결과와 무효표를 다시 계산",
};

export function findingKo(f: Finding): { text: string; missing: string[] } {
  if (f.result === "PASS") return { text: "구현된 검사를 모두 통과했어요.", missing: [] };
  if (f.result === "FAIL") return { text: "이상이 발견됐어요. 원문에서 자세한 내용을 확인하세요.", missing: [] };
  // a reason other than OTHER (anchor not deep, node or view unreadable) is the cause of this INCONCLUSIVE
  if (f.inconclusive_reason && f.inconclusive_reason !== "OTHER" && REASON[f.inconclusive_reason])
    return { text: REASON[f.inconclusive_reason], missing: [] };
  const m = /Checks not implemented: (.*?)\.?$/s.exec(f.summary);
  if (m) {
    const missing = m[1].split("; ").map((c) => CHECK[c.trim().replace(/\.$/, "")] ?? c.trim());
    return { text: "확인한 범위에서는 위반이 없지만, 아직 구현되지 않은 검사가 있어 판단을 보류했어요.", missing };
  }
  return { text: REASON[f.inconclusive_reason ?? ""] ?? "판단을 보류했어요. 원문을 확인하세요.", missing: [] };
}
