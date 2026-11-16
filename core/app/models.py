"""Request bodies (core-api.openapi.yaml). additionalProperties: false → extra = forbid."""
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

TS = r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$"
ELC = r"^elc_[0-9A-HJKMNP-TV-Z]{26}$"
B64URL128 = r"^[A-Za-z0-9_-]{22}$"
HEX64 = r"^[0-9a-f]{64}$"
ORG = r"^org_[a-z0-9_]{2,32}$"


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CandidateIn(Strict):
    name: str = Field(min_length=1, max_length=100)


class ElectionCreate(Strict):
    title: str = Field(min_length=1, max_length=200)
    window_start: str = Field(pattern=TS)
    window_end: str = Field(pattern=TS)
    time_bucket_seconds: int = Field(ge=30, le=86400)
    checkpoint_interval_seconds: int = Field(ge=30)
    candidates: list[CandidateIn] = Field(min_length=1)
    org_ids: list[Annotated[str, Field(pattern=ORG)]] = Field(min_length=3)
    approval_threshold: int = Field(ge=2)
    proposal_ttl_hours: int = Field(default=24, ge=1, le=168)


class VoterInput(Strict):
    external_ref: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=100)
    email: str | None = Field(default=None, max_length=254, pattern=r"^[^@\s]+@[^@\s]+$")


class RosterIn(Strict):
    voters: list[VoterInput] = Field(min_length=1)


class IssueIn(Strict):
    delivery: Literal["CSV", "EMAIL"]


class LoginIn(Strict):
    username: str
    password: str


class RedeemIn(Strict):
    election_id: str = Field(pattern=ELC)
    token: str = Field(pattern=B64URL128)
    nullifier_hash: str = Field(pattern=HEX64)


class BallotIn(Strict):
    election_id: str = Field(pattern=ELC)
    credential: str = Field(pattern=B64URL128)
    sealed_ballot: str = Field(max_length=4096)


class ShareItem(Strict):
    d: str = Field(pattern=HEX64)
    c: str = Field(pattern=HEX64)
    z: str = Field(pattern=HEX64)


class ShareIn(Strict):
    """D-30 decryption share of one organization (contracts/ballot-elgamal.md)."""
    org_id: str = Field(pattern=ORG)
    shares: list[ShareItem] = Field(min_length=1, max_length=15)


class AlertIn(Strict):
    election_id: str = Field(pattern=ELC)
    checkpoint: int = Field(ge=0)
    kind: Literal["AUDIT_DELAYED", "LEDGER_UNAVAILABLE", "CHAIN_CONFIG_MISMATCH", "ANCHOR_REJECTED"]
    detail: str | None = Field(default=None, max_length=500)
