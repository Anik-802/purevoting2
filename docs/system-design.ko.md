# PureVoting 2.0 전체 설계안

> 작성일 2026-10-18 · 기준 `contracts/` **v1.0(동결)** · 작성 승민(팀장)
> 팀: 승민(Core API·웹·통합), Harits(원장·스마트 계약·Gateway), Anik(규칙 엔진·AI 감사 에이전트)
>
> 이 문서는 시스템 전체 그림과 팀 운영 방식을 정리한 것입니다. 구성 요소 사이의 세부 규칙(API 필드, 해시 계산, 상태 전이 조건)은
> **`contracts/`가 기준**이며, 이 문서와 다르면 contracts를 따릅니다. 설계 문서의 기준은 영어판 `system-design.md`이고, 이 문서는 참고용 번역입니다(D-19).

---

## 0. 읽는 순서

| 누구 | 먼저 읽을 것 | 그다음 |
|---|---|---|
| 전원 | 이 문서 1–3장, 7–10장 | `contracts/README.md`(공통 규칙·상태 머신·체크포인트) |
| 승민 | 8.1절, 그다음 `docs/roles/core-web.md` | `core-api.openapi.yaml`, `hashing.md`, `ledger-events.schema.json` |
| Harits | 8.2절, 그다음 `docs/roles/ledger.md` | `chain-interface.md`, `ledger-gateway.openapi.yaml`, `hashing.md` |
| Anik | 8.3절, 그다음 `docs/roles/rules-ai.md` | `contracts/README.md` "Audit rules", `finding.schema.json`, `audit-report.schema.json`, `chain-interface.md` "How R5 uses it" |

---

## 1. 목표와 범위

**한 줄 요약:** 일회용 링크로 투표하는 온라인 투표 시스템. 모든 중요한 기록을 여러 기관이 함께 운영하는 감사 원장(블록체인)에 남기고,
규칙 엔진이 먼저 판정한 뒤 AI가 근거를 정리해 보조 감사합니다.

| 구분 | 내용 |
|---|---|
| 기간 | 2개월(8주) |
| 투표 방식 | 단일 선택(1인 1표, 후보 1명) |
| 핵심 주장 | ① 여러 기관의 서명 승인 없이 한 관리자 변경(명단·기간 등)은 탐지됩니다. ② 이미 커밋된 체크포인트에 속한 행의 사후 변경과 집계 조작은 규칙 엔진이 FAIL로 탐지합니다(outbox 누락은 T09를 실행한 경우만). ③ view·원장·로그에는 투표자와 표를 잇는 키가 없고, 시간 연결은 구간·체크포인트 단위(구간당 예상 10명 이상, D-10)로 제한됩니다. 실제 구간 인원이 적거나 재발급·추가된 투표자는 연결될 수 있습니다(위협 모델 S1·S7). |
| 하지 않는 주장 | 선관위에 대한 투표 비밀(선관위는 DB 운영자이자 개인키 보유자). 강요 방지(영수증 없는 투표 아님). Core 내부자가 기권자 표를 채워 넣는 것의 탐지. Gateway의 전달 지연 방지(지연은 탐지만 함). 대리 투표 방지. 종단간 검증. 독립된 다기관 운영(시연은 한 사람이 모든 기관 운영, D-21). "스마트 계약이 늦은 표를 하나씩 거부"(D-22). 실행하지 않은 선택 기능(직접 제출, AI). 전체 목록은 위협 모델 "Claims we must NOT make in the MVP". |
| 설계만 하고 구현하지 않음 | 임계값 복호화, 블라인드 서명 등 고급 암호 |

---

## 2. 전체 구조

### 2.1 구성도

```
                         ┌──── Gateway: 중립 기관 · 규칙 엔진·AI: 참관인 ───────────────┐
 투표자 웹 ─┐            │                                                              │
            ├─ REST ─> Core API ── outbox(FIFO) ──> Ledger Gateway ──> 검증 노드 (스마트 계약)
 관리자 웹 ─┘   (선관위)   │  ▲                       ▲ 조회 API            ▲ 직접 조회(참관인의 고정 노드)
 (기관 승인자가           │  └─ /internal/v1 view ─────┼──── 규칙 엔진 (R1–R5) ┘──> finding 등록·본문 저장
  자기 키로 서명)         │                            └──── AI 감사 에이전트 (읽기 전용) ──> 보고서
                         └── 직원은 Core API로 finding·보고서·경보를 보고 서명 검토
```

### 2.2 구성 요소

| 구성 요소 | 담당 | 하는 일 | 운영 기관(시연) |
|---|---|---|---|
| 투표자 웹 | 승민 | 링크 사용, **브라우저에서 투표권 생성**, 표 봉인·제출, 영수증 확인 | 선관위 |
| 관리자 웹 | 승민 | 선거 생성, 명단, 링크 발급, 관리 행위 요청·승인 서명, finding 검토, 개표·확정 | 선관위(승인자는 각 기관) |
| Core API | 승민 | 선거 상태 머신, DB, outbox, 체크포인트, view, 개표 | 선관위 |
| Ledger Gateway | Harits | 원장 이벤트 검증·기록, 투표 이벤트 배치 앵커, 제안·승인 중계, 조회 API | 중립 기관 |
| 스마트 계약 | Harits | 기관 레지스트리, 선거 상태, 승인 정족수, finding 기록·검토, 확정 조건 강제 | 검증 노드(3개, 기관마다 1개, D-03) |
| 규칙 엔진 | Anik | 체크포인트마다 R1–R5 판정, finding 서명·등록 | 참관인(D-25, v1.1) |
| AI 감사 에이전트 | Anik | 읽기 전용 도구로 근거를 모아 보고서 작성(선택 기능) | 참관인(D-25, v1.1) |

### 기술 스택

| 항목 | 선택 | 근거 | 비용 |
|---|---|---|---|
| 서버(백엔드) | Python 3.11 + FastAPI | D-05 | 무료 |
| 웹 | TypeScript + React | D-05 | 무료 |
| DB | PostgreSQL (DB 1개, 스키마 2개) | D-06 | 무료 |
| 실행 위치 | 각자 PC에서 `docker compose up`, 시연도 PC 한 대(클라우드 안 씀) | 7장 #6, 10장 | 무료 |
| 블록체인 | PureChain 사설 네트워크(go-ethereum 계열, EVM. Harits 결정, v1.1) | D-02 | 무료 |
| 메일 | 개발은 MailHog(가짜 메일함), 시연은 CSV | D-16 | 무료 |
| 저장소·CI | GitHub 모노레포 + GitHub Actions | D-18 | 무료 |
| mock 서버 | prism(OpenAPI 파일로 생성) | 7장 #4 | 무료 |
| AI 모델 | 로컬에서 돌리는 무료 공개 모델, Anik이 선택. 유료 API 금지, 안 되면 AI 없는 템플릿 보고서. 에이전트는 선택 기능 | D-15(v1.2) | 비용 0만 허용 |

블록체인 플랫폼과 AI 모델은 담당자가 정하며, `chain-interface.md` 인터페이스와 Gateway API만 지키면 됩니다.

### 2.3 시연용 기관 구성

| 기관 | 역할 |
|---|---|
| 선관위 (`org_commission`) | 운영 기관. Core·웹 운영, 선거 생성·개표. finding 검토 정족수에서 제외 |
| 참관인 (`org_observer`) | 규칙 엔진·AI 운영(D-25, v1.1), 관리 행위 승인, finding 검토, **고정 노드** 운영(Core와 규칙 엔진이 직접 읽는 노드) |
| 중립 기관 (`org_neutral`) | Gateway 운영, 관리 행위 승인, finding 검토 |

- 기관 3곳, 승인 임계값 2: 관리 행위는 **요청 기관을 제외한** 2개 기관의 서명이 필요하고, finding 검토는 선관위를 제외한 2개 기관(참관인·중립 기관)의 서명이 필요합니다(`docs/test-plan.md` 공통 설정과 같음).
- 검증 노드는 3개로 기관마다 1개입니다(D-03). 고정 노드는 참관인 노드가 첫째, 선관위 노드가 둘째이며, Gateway 운영 기관(중립 기관)의 노드는 쓰지 않습니다(chain-interface.md, D-26).
- 모든 기관이 한 PC의 docker에서 돌아갑니다. 실제로 독립된 기관이 아니라는 점은 위협 모델에 공개돼 있습니다.

---

## 3. 핵심 흐름

### 3.1 선거 상태

```
DRAFT ──명단 고정──> ROSTER_FROZEN ──시작 시각──> OPEN ──종료 시각/조기 마감──> CLOSED ──개표──> TALLIED ──확정──> FINALIZED
  └────────────── 다기관 승인을 받은 무효 처리(ELECTION_VOID) ──────────────> VOIDED
```

- **엄격 전이**(명단 고정, 마감, 기간 단축): Core가 즉시 적용합니다. 원장이 거부해도 되돌리지 않고, 경보를 낸 뒤 사람이 해결합니다.
- **허용 전이**(개시, 확정, 기간 연장): 원장이 ACCEPTED한 뒤에만 적용합니다.

### 3.2 선거 준비
1. 관리자가 선거를 만듭니다. Core가 `ELECTION_CREATED`를 보내고, Gateway가 계약에 기록합니다.
2. **생성 인자 대조:** Core가 고정 노드를 직접 읽어 계약에 저장된 참여 기관, 임계값 등을 원장 기록과 대조합니다.
   - 통과(`creation_check = PASSED`)해야 다음 단계로 갈 수 있습니다.
   - 불일치하면 그 선거는 폐기하고 Core에서 삭제합니다.
3. 명단을 등록하고 고정합니다. 이때 시간 구간당 10명 이상이 되는지 검사합니다.
4. 링크를 발급합니다. 원본 토큰은 CSV나 메일로만 나가고, 서버에는 해시만 남습니다.

### 3.3 투표
```
투표자 ──링크 클릭──> 투표자 웹: 브라우저가 투표권(128bit 난수) 생성
        ──redeem(token, nullifier_hash)──> Core: 토큰 사용 처리 + 투표권 해시 등록 (한 트랜잭션)
        ──ballot(credential, 봉인된 표)──> Core: 표 저장, 영수증 발급
Core ──outbox──> Gateway: TOKEN_USED, BALLOT_RECEIVED (무작위 evr_ ID, 시간 구간 단위 시각, 순서 섞어 배치 전송)
Gateway ──> 계약: 배치 머클 루트 앵커 → 영수증 상태 ANCHORED
```
- 토큰 사용과 표는 서로 다른 DB 스키마에 저장되며, 두 기록을 잇는 키가 없습니다.

### 3.4 원장 기록과 체크포인트
- Core는 DB 변경과 outbox 기록을 한 트랜잭션으로 처리합니다. 전송은 최소 1회이고 `event_id`는 바뀌지 않습니다.
- 시스템 이벤트는 선거별 순번(`seq`)을 가지며, Gateway는 순서대로만 적용합니다.
- **체크포인트**는 감사의 기준점입니다.
  - 일정 주기(시간 구간의 정수배)로 커밋하고, 상태가 바뀔 때, 마감 직후(CLOSE_FINAL), 개표 때(TALLY)도 커밋합니다.
  - 커밋할 때 6개 테이블의 스냅샷 해시와 개수를 원장에 남깁니다.
  - view는 항상 "체크포인트 k 기준"으로 읽고, 같은 k를 읽으면 항상 같은 결과가 나옵니다.

### 3.5 관리 행위 승인
1. 요청 기관 관리자가 행위(명단 정정, 무효 처리 등)를 작성하고, 자기 기관 키로 서명해 제출합니다. 그러면 원장에 제안이 열립니다.
2. 다른 기관 승인자들이 화면의 대상을 확인합니다. 클라이언트가 해시를 다시 계산해 확인한 뒤 서명합니다.
3. 임계값에 도달하면 APPROVED가 됩니다. Core가 실행하고 `ADMIN_ACTION_EXECUTED`와 그 효과 이벤트를 보냅니다.
- 기관 서명키는 각 기관이 체인 레지스트리에 직접 등록합니다. 선관위는 다른 기관의 키를 지정할 수 없습니다.

### 3.6 감사
1. 체크포인트가 원장에 반영되고 해당 이벤트가 앵커되면, 규칙 엔진이 R1–R5를 실행합니다.
2. 결과(finding)를 서명해 원장에 등록하고, 본문은 Core에 저장합니다.
3. FAIL·INCONCLUSIVE는 선관위를 제외한 기관 2곳의 서명 검토가 있어야 해결됩니다. 같은 규칙이 나중에 PASS를 내서 INCONCLUSIVE가 대체되는 경우도 해결로 칩니다.
4. 실행할 수 없을 때(원장 지연, Gateway 불통)는 finding 대신 경보만 남깁니다.

| 규칙 | 검사 내용 |
|---|---|
| R1 중복·상태 | 같은 토큰·투표권의 중복 수락, 모르는 토큰, 폐기 후 사용 |
| R2 시간 | 실효 투표 기간 밖의 수락 이벤트 |
| R3 개수 | 명단·토큰·투표권·표 개수 관계, view와 원장 개수 일치, 개표 수 |
| R4 관리 행위 | 승인 없는 변경, 서명 위조, 중복 실행, 승인 대상과 실행 결과 불일치 |
| R5 무결성 | 과거 체크포인트 스냅샷 재계산, 행 단위 원장 대조, 앵커 루트, 개인키로 재복호화, 계약 상태 대조 |

### 3.7 개표와 확정
1. 마감하고 outbox가 비면 개표합니다. 원장에서 수락된 표만 셉니다.
2. 선관위가 개인키를 공개합니다. 규칙 엔진(R5)은 다시 복호화해 결과를 맞춰 봅니다.
3. 확정 조건은 계약이 직접 확인합니다.
   - TALLY 체크포인트의 R1–R5 finding이 모두 기록돼 있어야 합니다.
   - 미해결 FAIL·INCONCLUSIVE가 없어야 합니다.
   - CONFIRMED_ISSUE가 없어야 합니다.
4. 문제가 확인되면 무효 처리(VOID)만 가능합니다.

---

## 4. 데이터 설계

### 4.1 Core DB (PostgreSQL 1개, 스키마 2개)

| 스키마 | 테이블 | 비고 |
|---|---|---|
| credential | `roster`, `token_status`, `admin_actions` (선거 설정도 여기 저장하지만 **스냅샷 대상이 아니며** `config_hash`로 확인) | 선거·명단·토큰 해시·관리자 작업 |
| ballot | `credential_issuance`, `ballot_receipts` | 투표권 해시·봉인된 표·영수증. credential 스키마와 잇는 공통 키 없음 |

스냅샷 대상 6개 테이블은 `common.schema.json#/$defs/SnapshotTable`과 정확히 같습니다: `roster`, `token_status`,
`credential_issuance`, `ballot_receipts`, `admin_actions`, **`rejected_attempts`**. `rejected_attempts`(와 outbox)는 어느 스키마에
두어도 되지만 `rejected_attempts`는 매 체크포인트 스냅샷에 포함됩니다. `election_config`는 포함되지 않습니다.

- 링크 사용 처리와 투표권 등록은 한 트랜잭션입니다.
- 투표자 관련 행은 정확한 시각 대신 시간 구간과 체크포인트만 저장합니다. 기본키도 무작위로 만듭니다.
- 두 스키마는 서로 다른 DB 권한을 씁니다. 다만 DB 관리자는 둘을 연결할 수 있으며, 이는 공개된 한계입니다.
- 규칙 엔진과 AI는 DB에 접근하지 않습니다. view API만 씁니다.

### 4.2 view(규칙 엔진·AI가 읽는 데이터)
`election_config`, `roster`, `token_status`, `credential_issuance`, `ballot_receipts`, `admin_actions`, `rejected_attempts`. 모두 `?checkpoint=k`로 읽습니다. 이름이나 개인정보는 들어가지 않습니다.

### 4.3 원장 이벤트(`ledger-events.schema.json`)

| 종류 | 이벤트 |
|---|---|
| 선거 상태 | ELECTION_CREATED, ROSTER_FROZEN, ELECTION_OPENED, ELECTION_CLOSED, TALLY_PUBLISHED, ELECTION_FINALIZED, ELECTION_VOIDED |
| 명단·토큰 | ROSTER_AMENDED, TOKEN_BATCH_ISSUED, TOKEN_REVOKED |
| 투표(배치 앵커) | TOKEN_USED, BALLOT_RECEIVED |
| 관리 행위 | ADMIN_ACTION_REQUESTED, APPROVAL_RECORDED, PROPOSAL_CLOSED, ADMIN_ACTION_EXECUTED / FAILED, WINDOW_CHANGED |
| 감사 | CHECKPOINT_COMMITTED, FINDING_RECORDED, FINDING_REVIEWED |

### 4.4 공통 규칙(요약)
- **ID:** 접두어 + 26자. 시스템 객체는 ULID를 씁니다. 투표 쪽(`evr_`, `vtr_` 등)은 시각이 들어가지 않는 무작위 ID입니다.
- **시각:** UTC, 초 단위. 투표 쪽 시각은 시간 구간으로 내림합니다.
- **해시·서명:** RFC 8785(JCS) 정규화 후 SHA-256. 서명 다이제스트는 고정 32바이트 배치를 쓰고, EIP-191 secp256k1로 서명합니다(EVM 기반 PureChain, D-02). 자세한 내용은 `hashing.md`에 있습니다.
- **오류:** `application/problem+json`. 오류 코드는 `common.schema.json`의 `ProblemCode`에 있습니다.

---

## 5. 인터페이스 요약

| 인터페이스 | 파일 | 주요 경로 |
|---|---|---|
| Core API(웹·직원) | `core-api.openapi.yaml` | `/v1/credentials:redeem`, `/v1/ballots`, `/v1/elections/*`(생성·명단·발급·개표·확정), `/v1/admin-actions/*`, `/v1/findings/*` |
| Core API(내부 서비스) | 같은 파일 | `/internal/v1/elections`, `/checkpoints`, `/views/{view}`, `/findings`, `/audit-alerts`, `/audit-reports`, `/service-logs` |
| Gateway API | `ledger-gateway.openapi.yaml` | `/v1/events`, `/v1/proposals/*`, `/v1/finding-records/*`, `/v1/anchors/*`, `/v1/rejections`, `/v1/elections/{id}/state`, `/v1/chain` |
| 스마트 계약 | `chain-interface.md` | 레지스트리, `electionState`·`electionGovernance`·`electionStateAtCheckpoint`, Gateway 쓰기 함수, 직접 제출 함수 |
| 감사 결과 | `finding.schema.json`, `audit-report.schema.json` | finding 본문, AI 보고서와 도구 목록 |

**권한(요약):** 서비스마다 다른 키(`coreKey`, `ruleKey`, `agentKey`, `gatewayKey`)를 쓰고, 직원은 JWT 역할(`admin`, `approver`, `auditor`)로 구분합니다. AI 에이전트는 읽기와 보고서 작성만 할 수 있습니다. 전체 권한표는 `contracts/README.md` "Permission matrix"에 있습니다.

---

## 6. 보안·익명성(요약)
- 투표자와 표의 분리: 브라우저 생성 투표권, 무작위 ID, 시간 구간, 순서를 섞은 배치 전송, 스키마 분리
- Gateway를 믿지 않는 장치
  - 서명된 승인·검토·finding은 위조할 수 없고, 서명자는 체인에 직접 제출할 수 있습니다.
  - 계약 주소와 고정 노드는 배포 설정에 고정합니다.
  - Core와 R5가 생성 인자를 대조합니다.
- 남는 위험은 `docs/threat-model.md`에 공개돼 있습니다. 발표할 때 "하지 않는 주장"(1장)을 넘어서지 않도록 주의합니다.

---

## 7. 팀 운영 결정(확정)

| # | 항목 | 결정 |
|---|---|---|
| 1 | 저장소 | GitHub 모노레포 하나. `core/`·`web/`=승민, `ledger/`=Harits, `rules/`=Anik, `contracts/`=공용. 자기 폴더는 자유롭게 수정, `contracts/` 변경만 PR |
| 2 | contracts 동결 | v1.0으로 동결. 구현하다 막힐 때만 PR로 수정(관련자 @멘션 + CHANGELOG 한 줄, 필수 승인 없음) |
| 3 | 공용 코드 | 공용 라이브러리는 만들지 않음. 해시·서명·머클은 각자 구현하고 `tests/vectors/hash-vectors.json`(`tools/make_hash_vectors.py`가 `hashing.md` 기준으로 생성)의 모든 벡터를 재현해야 함. 각 모듈 CI에서 검사 |
| 4 | 따로 개발 | 처음에는 상대 서비스를 OpenAPI mock(prism)으로 대신함. 실제 연결은 통합 시점부터 |
| 5 | 통합 시점 | M1 11-11(T01), M2 11-19(데모 버전), M3 11-26(필수 시험), `docs/schedule.md`(D-28). 통합 담당은 승민 |
| 6 | 실행 방식 | `docker compose up` 한 번으로 전체 실행. 포트·서비스 키·기관 키는 `.env` 하나에서 관리. 시연 설정은 별도. 각 담당자가 11-09까지(`docs/schedule.md`) Dockerfile, healthcheck가 있는 compose 서비스 블록, 자기 `.env` 키를 내고 승민이 합침. 기동 순서: 노드 → 계약 배포(고정 주소 또는 `CONTRACT_ADDRESS`를 쓰는 init 작업) → 레지스트리 설정 스크립트(Harits) → 직원 계정 설정 스크립트(승민) → Core·Gateway·규칙 엔진 |
| 7 | 구현 범위 | 필수/선택 단계로 구분(9장) |
| 8 | 진행 공유 | GitHub Issues로 작업 관리. 주 1회 "한 것 / 막힌 것 / contracts 변경 요청" 공유 |

플랫폼(D-02)과 AI 모델(D-15)은 각 담당자가 정합니다. 그 밖의 설계 결정은 `docs/decisions.md`에 확정돼 있습니다(v1.0).

---

## 8. 역할 분담

### 8.1 승민 — Core API·웹·통합

| 구분 | 내용 |
|---|---|
| 필수 | 선거 상태 머신, 명단·링크 발급, redeem/ballot(두 스키마), outbox(FIFO·seq·재시도·구간 단위 전송), 체크포인트·스냅샷 해시, view API(`?checkpoint=k`), 생성 인자 대조, 관리 행위(ROSTER_AMEND·ELECTION_VOID) 실행, 개표·확정, finding 본문 저장·검토 중계, 투표자 웹·관리자 웹(승인 서명 포함) |
| 선택 | WINDOW_CHANGE·ELECTION_CLOSE_EARLY·TOKEN_REISSUE, AI 보고서 저장·검토 화면, service-logs |
| 기준 파일 | `core-api.openapi.yaml`, `contracts/README.md`(상태 머신·체크포인트·일관성 모델), `hashing.md`, `ledger-events.schema.json` |
| 개발 중 상대 | Gateway mock(`ledger-gateway.openapi.yaml`) |
| 통합 책임 | `docker-compose.yml`, `.env.example`, 시연 시나리오 스크립트 |

### 8.2 Harits — 원장·스마트 계약·Gateway

| 구분 | 내용 |
|---|---|
| 필수 | `chain-interface.md`의 계약 구현(레지스트리, 선거 상태, 체크포인트별 상태 저장, 제안·승인 정족수, finding 기록·검토, 확정 조건), Gateway 이벤트 검증(순번, 투표 기간, 중복, 토큰 상태), 투표 이벤트 배치·머클 앵커, 거부 기록, 앵커 거부 경보, 조회 API, 노드 구성(참관인이 운영하는 고정 노드 포함), 레지스트리 설정 스크립트 |
| 선택 | 체인 직접 제출 경로(`submitDecision`·`submitFindingRecord`·`submitFindingReview`)의 인덱싱 |
| 기준 파일 | `chain-interface.md`, `ledger-gateway.openapi.yaml`, `hashing.md`(머클·다이제스트), `ledger-events.schema.json` |
| 개발 중 상대 | Core 대신 `contracts/examples/`의 이벤트 예제로 시험 |
| 결정 | 블록체인 플랫폼(D-02)과 노드 구성. 필수 조건: 승인·검토·finding 서명 검증을 체인(계약 또는 체인코드)에서 할 수 있어야 함(D-11) |

### 8.3 Anik — 규칙 엔진·AI 감사 에이전트

| 구분 | 내용 |
|---|---|
| 필수 | 체크포인트 감지(`/internal/v1/elections`·`/checkpoints` 폴링), R1–R5 구현(`observed` 키 포함), INCONCLUSIVE 처리, finding 서명·등록과 본문 저장, 경보(AUDIT_DELAYED·LEDGER_UNAVAILABLE), 고정 노드 직접 조회(앵커·레지스트리·계약 상태) |
| 선택 | AI 감사 에이전트(읽기 전용 도구, 근거 인용 보고서), 수동 재실행 요청 처리 |
| 기준 파일 | `contracts/README.md` "Audit rules", `finding.schema.json`, `audit-report.schema.json`, `chain-interface.md` "How R5 uses it", `hashing.md` |
| 개발 중 상대 | Core mock, Gateway mock, `contracts/examples/` |
| 결정 | 어떤 무료 로컬 모델과 프롬프트를 쓸지(D-15 v1.2: 비용 0, 대안은 템플릿 보고서) |
| 주의 | 결함 시험의 정답 라벨은 Anik이 아닌 Harits가 먼저 작성합니다(실험 공정성). |

---

## 9. 일정과 구현 범위

8주 계획은 2026-11-04에 **`docs/schedule.md`**(D-28)로 바뀌었습니다. 데모 버전은 2026-11-19이고, 모든 작업은 2026-11-30까지 끝냅니다.
단계, 담당별 날짜, 넘겨주는 항목, 대체안은 그 문서에만 둡니다.

| # | 날짜 | 단계 |
|---|---|---|
| M1 | 11-11 | 통합 1: 실제 구성으로 T01 |
| M2 | 11-19 | **데모 버전** (정상 선거 확정, T05·T08 결함 탐지와 확정 거부) |
| M3 | 11-26 | 필수 범위 완성 (명단 정정, 무효, 서명 검토, T01·T02·T04·T05·T08·T15) |

**필수:** 정상 투표, 명단 정정, 무효 처리, 규칙 R1–R5, 시험 T01·T02·T04·T05·T08·T15.
**만들지 않음(D-28):** 기간 변경, 조기 마감, 링크 재발급, 체인 직접 제출 경로, 나머지 시험. AI 감사 에이전트는 비용 0만 허용(D-15 v1.2): 로컬 무료 모델, 안 되면 템플릿 보고서, 그것도 안 되면 후속 연구.

---

## 10. 개발 환경

### 10.1 저장소 구조
```
purevoting2/
├─ contracts/        # 통신 규칙 (v1.0 동결, 변경은 PR)
├─ docs/             # 이 설계안, 결정 사항, 위협 모델, 시험 계획
├─ core/             # 승민: Core API
├─ web/              # 승민: 투표자 웹·관리자 웹
├─ ledger/           # Harits: 스마트 계약, Gateway, 노드 설정
├─ rules/            # Anik: 규칙 엔진, AI 감사 에이전트
├─ tests/scenarios/  # 결함 시험 시나리오와 정답 라벨(expected.json)
├─ tests/vectors/    # hash-vectors.json (공용 해시 테스트 벡터)
├─ tools/            # validate_contracts.py, make_hash_vectors.py
├─ docker-compose.yml
└─ .env.example
```

### 10.2 실행과 설정
- `docker compose up`으로 DB, Core, 웹, 노드, Gateway, 규칙 엔진, (AI)가 모두 실행됩니다.
- `.env`에서 관리하는 값
  - 서비스 포트
  - 서비스 키(`CORE_KEY`, `RULE_KEY`, `AGENT_KEY`, `GATEWAY_KEY`)
  - 기관별 서명키(시연용)
  - 고정 체인 설정(chain_id, 계약 주소, 고정 노드 URL)
  - 스크립트가 쓰는 시연용 선거 설정(Core에는 구간 기본값이 없음, D-10)
- 비밀키는 저장소에 올리지 않습니다. `.env.example`에는 형식만 둡니다.

| 설정 | 평가용 | 시연용 |
|---|---|---|
| 명단 | 100명 | 100명 |
| 투표 기간 | 2시간 | 10분 |
| 시간 구간 | 900초 | 60초 |
| 체크포인트 주기 | 900초 | 60초 |
| 구간당 예상 인원(10 이상) | 12.5 | 10 |

### 10.3 협업 규칙
- 자기 폴더는 자유롭게 커밋합니다. `contracts/` 변경은 PR로 하고, 관련자를 @멘션하고 `CHANGELOG.md`에 한 줄을 남깁니다.
- CI가 매 push마다 `tools/validate_contracts.py`를 실행합니다. 각 모듈의 단위 시험도 CI에 추가합니다.
- 각자 GitHub Issue로 작업을 등록합니다. 주 1회 "한 것 / 막힌 것 / contracts 변경 요청"을 공유합니다.
- 큰 설계 변경이나 통합 시험 뒤에는 감독관 에이전트(`.claude/agents/psp1-project-supervisor.md`)로 점검합니다.

---

## 11. 시험과 시연
- **시험:** T01–T16의 상세 절차와 통과 기준은 `docs/test-plan.md`에 있습니다. 결함 시나리오는 `tests/scenarios/<id>/`에 두고, 정답 라벨을 실행 전에 커밋합니다.
- **실험 범위:** `docs/test-plan.md`의 절차가 목표이고, MVP 최소 범위는 필수 시나리오 × seed 1–3(B1, 에이전트가 있으면 B2도)입니다.
- **실험 비교:**
  - B0: 기존 PureVoting
  - B1: 규칙만
  - B2: 규칙 + AI(로컬 무료 모델, 11-17에 진행이 확정된 경우만)
  - 지표: FAIL 정밀도·재현율, 원인 정확도, 근거 유효율, AI 토큰·소요 시간(비용 0)
- **시연 흐름(안):**
  1. 정상 투표
  2. 승인 없는 명단 변경(T05) → R4 FAIL
  3. DB 표 변조(T08) → R5 FAIL
  4. 기관 서명 검토
  5. 확정 거부 확인

---

## 12. 위험과 대응

| 위험 | 대응 |
|---|---|
| 계약 규모에 비해 일정이 짧음 | 필수/선택 구분, M1–M3 통합 시점에 D-28의 대체안으로 범위를 다시 조정 |
| 해시·서명 계산이 모듈마다 어긋남 | `tests/vectors/hash-vectors.json`을 모든 모듈 CI의 필수 검사로 두고, 첫 통합 전에 끝냄 |
| 서로 기다리느라 멈춤 | mock으로 독립 개발 |
| contracts 해석 차이 | 막히면 Issue 등록 → PR로 contracts 수정. 구두 합의로 끝내지 않음 |
| 발표에서 과장 | 1장 "하지 않는 주장"과 위협 모델을 지킴 |

---

## 부록. 설계 검토 경과
- 감독관 에이전트가 contracts를 17차까지 검토했습니다.
  - 1–15차는 매번 FAIL이었고, 지적을 반영해 수정했습니다.
  - 16차(v0.16)와 17차(v0.17)는 연속 PASS(P1 0건)였습니다.
- 17차의 남은 지적을 v0.18에 반영한 뒤 v1.0으로 동결했습니다. 변경 이력은 `contracts/CHANGELOG.md`에 있습니다.
