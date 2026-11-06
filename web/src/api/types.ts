// Field names follow contracts/core-api.openapi.yaml and contracts/common.schema.json.

export type ElectionStatus = "DRAFT" | "ROSTER_FROZEN" | "OPEN" | "CLOSED" | "TALLIED" | "FINALIZED" | "VOIDED";
export type RuleId = "R1" | "R2" | "R3" | "R4" | "R5";
export type RuleResult = "PASS" | "FAIL" | "INCONCLUSIVE";
export type ReceiptStatus = "RECEIVED" | "ANCHORED" | "LEDGER_REJECTED";

export interface Candidate {
  candidate_id: string;
  name: string;
  description?: string;
}

export interface PublicElection {
  election_id: string;
  title: string;
  status: ElectionStatus;
  window_start: string;
  window_end: string;
  time_bucket_seconds: number;
  candidates: Candidate[];
  public_key: string;
}

export interface Election extends PublicElection {
  created_at: string;
  voter_count: number;
  creation_check: "PENDING" | "PASSED" | "MISMATCH";
  open_checkpoint: number;
  counters: { as_of_checkpoint: number; tokens_active: number; tokens_used: number; ballots_received: number };
}

export interface BallotReceipt {
  receipt_hash: string;
  status: ReceiptStatus;
  anchor_id?: string;
  tx_hash?: string;
}

export interface Finding {
  finding_id: string;
  rule_id: RuleId;
  result: RuleResult;
  checkpoint: number;
  summary: string;
  reviewed?: boolean;
  confirmed_issue?: boolean;
}

export interface Tally {
  ballot_count: number;
  invalid_ballots: number;
  results: { candidate_id: string; votes: number }[];
  result_hash: string;
}

export interface ElectionCreate {
  title: string;
  time_bucket_seconds: number; // D-10: no default, chosen per election
  window_start: string;
  window_end: string;
  candidates: { name: string; description?: string }[];
}

/** core-api VoterInput: one roster row (PII; stays in the credential DB) */
export interface VoterInput {
  external_ref: string;
  name: string;
  email?: string;
}

export interface RosterSummary {
  voter_count: number;
  roster_hash: string;
}

export interface RosterVoter {
  voter_key: string;
  status: "ACTIVE" | "REMOVED";
  external_ref?: string; // omitted for auditors
  name?: string;
}

/** RFC 7807 problem with the Core error code, e.g. TOKEN_ALREADY_USED */
export class ApiError extends Error {
  constructor(public code: string, message: string) {
    super(message);
  }
}
