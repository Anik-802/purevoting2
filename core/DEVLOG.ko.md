# Core 개발 기록 (승민)

> Core API·웹 담당의 작업 기록입니다. 무엇을 했는지, 왜 그렇게 했는지, 무엇을 확인했고 무엇이 아직 확인되지 않았는지를 적습니다.
> 기준 문서: `docs/roles/core-web.md`, `contracts/` v1.0.

---

## 2026-10-19 — 1주차 시작: FastAPI 골격, docker-compose, 해시 라이브러리·벡터 테스트, DB init SQL

### 한눈에 보기

| 항목 | 결과 | 확인 방법 |
|---|---|---|
| 해시 라이브러리 | 공용 벡터 39개 모두 재현 | pytest 통과 |
| FastAPI 골격 | `/healthz`, `/readyz` 동작 | pytest 통과 |
| DB init SQL | 역할 3개, 스키마 2개, 테이블, 투표 쪽 함수 2개 | 파서 검사 통과 → **실제 PostgreSQL 16.4에서 실행·스모크 테스트 통과**(7절) |
| docker-compose | DB + Core | **실제 기동 확인**: 두 컨테이너 healthy, `/readyz` = ready(7절) |
| CI | 단위 테스트 job, 실제 PostgreSQL 16에서 init·스모크 테스트를 돌리는 job | push 후 GitHub Actions에서 처음 실행됨 |

전체 단위 테스트는 45개 통과입니다. 경고 1건은 테스트 도구(Starlette TestClient) 내부의 것이라 우리 코드와는 관계없습니다.

### 1. 해시 라이브러리와 벡터 테스트

**만든 것**
- `core/app/hashing.py`: `contracts/hashing.md`의 계산을 그대로 옮겼습니다.
  - 기본: `H`, `canonical`(RFC 8785), `Hc`, `merkle_root`, `set_root`
  - 값: leaf_hash, rejection_leaf, rejections_root, receipt_hash, roster_hash, token_set_root, ballot_set_root, target_hash, snapshot_hash
  - 서명 다이제스트: 요청·승인, finding 기록, 검토
- `core/tests/test_hash_vectors.py`: `tests/vectors/hash-vectors.json`의 벡터를 하나씩 계산해 기대값과 비교합니다.
  - 새 벡터가 추가됐는데 Core 계산이 없으면 실패하게 했습니다(그냥 건너뛰지 않도록).
  - 벡터 개수도 함께 확인합니다.

**왜**
- 세 모듈(Core, Gateway, 규칙 엔진)은 같은 값을 각자 계산하고, R4·R5가 그 값을 서로 비교합니다. 한 비트라도 다르면 R5가 계속 FAIL을 냅니다.
- 그래서 설계안 7장 #3에서 "공용 벡터 재현"을 1주차 필수 조건으로 정했고, Core가 가장 먼저 끝냈습니다.

**바로잡은 것: 벡터 수 41개 → 39개**
- 지금까지 문서와 보고에서 벡터가 41개라고 했는데, 실제로는 **39개**입니다.
- 처음에 `"name"` 글자 수를 세면서 후보 정보 안의 `name` 필드까지 같이 셌기 때문입니다. 개수 확인 테스트를 넣자마자 드러났습니다.
- 저장소 문서에는 개수가 적혀 있지 않았습니다. 다운로드 폴더의 진행 현황 md는 39개로 고쳤습니다.
- 팀원에게 개수를 말할 일이 있으면 39개라고 하면 됩니다.

### 2. DB init SQL

**만든 것**
- `core/db/init/00_roles.sh`: DB를 처음 만들 때 한 번 실행됩니다. 역할 세 개를 만듭니다.
  - `pv2_credential`: 로그인 불가. `credential` 스키마의 주인입니다.
  - `pv2_ballot`: 로그인 불가. `ballot` 스키마의 주인입니다.
  - `pv2_core`: Core가 접속할 때 쓰는 계정입니다.
- `core/db/init/10_schema.sql`: 스키마, 테이블, 함수, 권한을 만듭니다.
  - `credential` 스키마
    - 선거와 상태 이력
    - 스냅샷 대상 테이블: `roster`, `token_status`, `admin_actions`(+상태 이력), `rejected_attempts`
    - 운영용 테이블: seq 카운터, outbox, 체크포인트, 직원 계정
  - `ballot` 스키마
    - 스냅샷 대상 테이블: `credential_issuance`, `ballot_receipts`
    - 투표 쪽 함수 2개: `register_nullifier`, `cast_ballot`
- `core/db/tests/smoke.sql`: `pv2_core`로 접속해 다음을 확인합니다.
  - 투표권 등록과 중복 거부
  - 투표 결과 네 가지(UNKNOWN / CREATED / RETRY / CONFLICT)
  - `pv2_core`가 투표 테이블을 직접 수정·삭제할 수 없음
  - 사용된 토큰은 폐기할 수 없음
  - `track_commit_timestamp`가 꺼져 있음

**왜 이렇게 했나**

| 결정 | 이유 |
|---|---|
| 스키마 2개를 서로 다른 역할이 소유 | D-06의 권한 분리. 명단 쪽과 투표 쪽을 같은 주인이 갖지 않게 함 |
| 투표 쪽 쓰기는 SECURITY DEFINER 함수 2개로만 | 권한 분리와 "redeem은 한 트랜잭션"을 둘 다 지키려면 방법이 필요했음(감독관 DB 검토의 지적). Core 계정은 함수만 호출할 수 있고, 투표 행을 직접 수정·삭제할 수 없음. 한 번 쓴 투표 행은 바뀌지 않음 |
| 두 스키마 사이에 잇는 열이 없음 | 토큰과 투표권을 연결하지 못하게 하는 설계(D-07, 위협 모델 S2) |
| 투표 쪽 행에는 시각 대신 구간·체크포인트만 저장, 기본키는 무작위 ID나 해시 | contracts "Shared conventions"의 투표 쪽 저장 규칙 |
| 바뀌는 값은 바뀐 체크포인트 번호와 함께 저장(`used_checkpoint` 등), 상태는 이력 테이블로 관리 | view를 "체크포인트 k 기준"으로 언제 읽어도 같은 결과가 나오게 하기 위함(스냅샷 해시 재계산, R5) |
| seq는 SERIAL·SEQUENCE를 쓰지 않고 카운터 테이블 사용 | SEQUENCE는 실패한 트랜잭션에서 번호가 비어 seq에 빈칸이 생김. contracts는 빈칸 없는 seq를 요구함 |
| `track_commit_timestamp=off` 강제 | 커밋 시각이 남으면 토큰 사용과 투표권 등록을 시각으로 짝지을 수 있음 |
| `CHECK (used_checkpoint IS NULL OR revoked_checkpoint IS NULL)` | "사용된 토큰은 폐기할 수 없다"는 규칙(R1)을 DB 차원에서도 막음 |
| 선거 설정 테이블은 스냅샷 대상 아님 | contracts 정의상 `election_config`는 스냅샷 대상이 아니고 `config_hash`로 확인함 |

**남은 한계(공개된 것)**
- DB 최고 관리자(superuser)는 여전히 두 스키마를 연결하거나 행을 고칠 수 있습니다(위협 모델 S2). 그래서 R5가 사후 변조를 탐지합니다.
- `pv2_core`는 view와 개표 때문에 투표 테이블을 읽을 수는 있습니다.

### 3. FastAPI 골격

**만든 것**
- `core/app/main.py`
  - `/healthz`: 프로세스가 떠 있는지 확인합니다.
  - `/readyz`: DB까지 응답하는지 확인합니다. DB가 없으면 503을 돌려주고, 오류 내용은 응답에 넣지 않습니다.
- `core/app/config.py`: 환경 변수에서 설정을 읽습니다. DB URL, 서비스 키, 고정 체인 설정 등입니다.
- `core/app/db.py`: SQLAlchemy 엔진입니다. "변경과 outbox 기록은 한 트랜잭션" 원칙을 주석으로 적어 두었습니다.
- `requirements.txt`(버전 고정), `requirements-dev.txt`, `Dockerfile`, `.dockerignore`, `pytest.ini`

**왜**
- 2주차부터 API를 붙이려면 앱, 설정, DB 연결의 뼈대가 먼저 있어야 합니다.
- `/readyz`는 docker-compose의 healthcheck에 씁니다. 다른 서비스가 Core가 준비됐는지 확인하고 기동하게 하기 위해서입니다(설계안 7장 #6 기동 순서).
- 라이브러리 버전은 고정했습니다. 새 PC에서 같은 결과가 나와야 하기 때문입니다(T14).

### 4. docker-compose와 CI

**만든 것**
- 저장소 루트 `docker-compose.yml`
  - `db`(postgres:16.4): init 스크립트를 마운트하고, `track_commit_timestamp=off`, healthcheck를 둡니다.
  - `core`: Dockerfile로 빌드하고, DB가 healthy가 된 뒤 기동합니다. healthcheck는 `/readyz`입니다.
  - 포트는 `127.0.0.1`에만 엽니다. 외부에 노출하지 않기 위해서입니다.
- `.env.example`: DB 비밀번호 항목을 `DB_CORE_PASSWORD` 하나로 정리했습니다. 스키마 주인 역할은 로그인하지 않으므로 비밀번호가 필요 없습니다.
- `.github/workflows/core.yml`
  - `unit`: 단위 테스트를 돌립니다(해시 벡터 39개 포함).
  - `db`: GitHub Actions의 PostgreSQL 16 서비스에서 `00_roles.sh` → `10_schema.sql` → `smoke.sql`(pv2_core로 접속)을 실제로 실행합니다.

**왜**
- 설계안 7장 #6에 따라 `docker compose up` 한 번으로 전체가 떠야 합니다. 1주차에는 DB와 Core부터 시작하고, Harits·Anik이 2주차까지 자기 서비스 블록을 보내면 합칩니다.
- 이 PC에는 Docker와 PostgreSQL이 없어서, SQL이 실제 DB에서 도는지 로컬에서 확인할 수 없었습니다. 그래서 무료인 GitHub Actions에서 실제 PostgreSQL로 검사하는 job을 넣었습니다.

### 5. 확인한 것 / 아직 확인하지 않은 것

**확인함**
- 단위 테스트 45개 통과: 해시 벡터 39개, 벡터 개수, health 2개, SQL 파싱 2개, SERIAL·IDENTITY 금지 1개
- `10_schema.sql`과 `smoke.sql`이 PostgreSQL 문법으로 파싱되고, PL/pgSQL 함수 본문 2개와 DO 블록 2개도 파싱됨(pglast)
- `docker-compose.yml`과 CI YAML의 구조

**아직 확인하지 않음**
- GitHub Actions의 `core` 워크플로(`unit`, `db` job). 커밋·push 후 처음 실행됩니다. 로컬 실제 DB에서는 같은 내용을 확인했습니다(7절).

### 6. 다음 할 일(2주차)
1. 커밋·push하고, CI `core` 워크플로의 `unit`·`db` job이 통과하는지 확인합니다.
2. 선거 생성(공개키 쌍, `config_hash`, 생성 인자 대조 stub)과 명단·명단 고정(D-10 구간 크기 검사)을 만듭니다.
3. redeem(토큰 잠금 → USED → `register_nullifier` → outbox, 한 트랜잭션), ballots(`cast_ballot`)를 만듭니다.
4. outbox 워커(seq, 구간 단위 전송, 결과 처리), 체크포인트·view, 내부 API(`/internal/v1/elections`, `/checkpoints`, `/views`, `POST /internal/v1/findings`)를 만듭니다.
5. 직원 계정 설정 스크립트를 만듭니다(2주차, T01에 admin JWT 필요).

### 7. 실제 DB 실행 확인 (같은 날 추가)

**한 것**
- 이 PC에 Docker Desktop 4.95.0을 설치했습니다(winget). Docker 29.8.2, Compose v5.6.0이고 WSL2를 씁니다.
- PostgreSQL은 Windows에 따로 설치하지 않았습니다. Docker가 받은 공식 이미지 `postgres:16.4`가 실제 PostgreSQL입니다. Windows에 따로 깔면 같은 5432 포트로 충돌할 수 있습니다.
- 로컬 `.env`를 만들었습니다. 비밀번호와 키는 무작위로 생성했고, `.gitignore`에 걸려 Git에 올라가지 않는 것을 확인했습니다.
- `docker compose up --build -d`로 DB와 Core를 기동했습니다.

**결과**
- 컨테이너: `db`, `core` 둘 다 `healthy`
- DB 초기화 로그: `00_roles.sh`(CREATE ROLE 3회), `10_schema.sql` 실행, 오류 없음
- `GET /healthz` → `{"status":"ok"}`, `GET /readyz` → `{"status":"ready"}`. Core가 `pv2_core` 계정으로 DB에 접속됨
- 스모크 테스트(`core/db/tests/smoke.sql`)를 `pv2_core`로 실제 DB에서 실행해 `smoke ok`가 나왔습니다. 테스트 데이터가 남지 않게 트랜잭션으로 감싸고 롤백했고, 끝난 뒤 남은 행이 0개인 것도 확인했습니다.
- 구조 확인
  - 역할: `pv2_core`(login), `pv2_credential`·`pv2_ballot`(nologin)
  - 스키마 주인: `credential` = `pv2_credential`, `ballot` = `pv2_ballot`
  - 테이블: credential 11개, ballot 2개
  - `pv2_core`의 `ballot.ballot_receipts` 권한: select만 true, insert·update·delete는 false
  - 함수 2개: SECURITY DEFINER, 주인 `pv2_ballot`
  - `track_commit_timestamp=off`, 서버 16.4

**참고**
- 설치 직후 이 터미널에는 Docker 경로(`C:\Program Files\Docker\Docker
esourcesin`)가 PATH에 없어서, `docker-credential-desktop`을 찾지 못하는 오류가 한 번 났습니다. 세션 PATH에 추가해서 해결했고, 새로 로그인하거나 새 터미널을 열면 자동으로 잡힙니다.
- DB init 스크립트는 `pgdata` 볼륨이 비어 있을 때만 실행됩니다. 스크립트를 고친 뒤에는 `docker compose down -v`로 볼륨을 지우고 다시 올려야 합니다.
- 멈추기: `docker compose down`(데이터 유지), `docker compose down -v`(데이터까지 삭제).

---

## 2026-10-26 — 2주차: 선거 → 명단 → 링크 → 투표 → 장부 전송 → 체크포인트 → 감사 창구

### 한눈에 보기

| 항목 | 결과 | 확인 방법 |
|---|---|---|
| 선거 생성 | 암호화 키 쌍, `config_hash`, 기관 레지스트리 확인, ELECTION_CREATED | 흐름 테스트 + 실제 컨테이너 |
| 생성 검사 | 지금은 `stub`(개발용 대역). 3주차에 실제 노드 읽기로 교체 | 흐름 테스트 |
| 명단·고정·링크 | 명단 등록, D-10 규칙(구간당 10명), 링크 CSV(15분 재시도) | 흐름 테스트 + 실제 컨테이너 |
| 투표 | redeem(한 트랜잭션), ballots(같은 표 재전송은 200) | 흐름 테스트 |
| 장부 전송(outbox) | 시간 구간 단위로 투표 이벤트(섞어서) → 시스템 이벤트(순번 순) | 흐름 테스트(전송 순서 검사) |
| 체크포인트 | 상태 변경·주기·마감·CLOSE_FINAL, 6개 테이블 지문과 개수 | 흐름 테스트(지문 일치 검사) |
| 감사 창구 | `/internal/v1/elections`, `/checkpoints`, `/views*`, `POST /internal/v1/findings`, 경보 | 흐름 테스트 |
| 직원 계정 스크립트 | `python -m app.scripts.create_staff` | 실제 컨테이너 |
| 테스트 | **53개 통과**(단위 50 + 실제 DB 흐름 3) | pytest, CI `db` job에도 추가 |

### 1. 만든 것 (쉽게)

- **선거 만들기:** 관리자가 제목·후보·투표 시간을 넣으면 Core가 암호화 열쇠 한 쌍을 만들고, 설정 전체의 지문(`config_hash`)을 계산해 "선거 생성" 장부 기록을 예약합니다. 참여 기관과 서명키는 로그인 정보가 아니라 블록체인 기관 레지스트리(Gateway `/v1/organizations`)에서 가져옵니다.
- **생성 검사:** 장부에 기록된 선거 설정이 우리가 보낸 것과 같은지 확인하는 단계입니다. 통과하기 전에는 명단 고정·링크 발급·다른 장부 전송이 모두 막힙니다. 지금은 역할 설계서가 허용한 개발용 대역(`stub`)이라 항상 통과하고, 3주차에 Harits의 계약 함수 형식을 받아 실제 노드를 읽도록 바꿉니다.
- **명단 → 고정 → 링크:** 명단은 DRAFT에서만 바꿀 수 있습니다. 고정할 때 "시간 구간당 10명 이상" 규칙을 검사합니다. 링크는 CSV(external_ref, name, link)로만 나가고 서버에는 토큰 해시만 남습니다. 응답을 못 받았을 때를 위해 같은 Idempotency-Key로 15분 안에 다시 요청하면 같은 CSV를 줍니다(메모리에 암호화해서 보관 후 삭제).
- **투표:** 브라우저가 만든 투표권의 해시를 등록하는 redeem과, 봉인된 표를 내는 ballots입니다. redeem은 "토큰 사용 처리 + 투표권 등록 + 장부 예약"을 한 트랜잭션으로 합니다. 같은 표를 다시 보내면 원래 영수증(200), 다른 표면 거부(409)입니다.
- **자동 일정(scheduler):** 1초마다 선거를 보고, 시작 시각에 열기, 주기마다 체크포인트, 종료 시각에 닫기를 합니다.
- **장부 전송 일꾼(outbox):** 예약된 기록을 시간 구간이 끝날 때마다 Gateway로 보냅니다. 결과(수락·중복·거부·충돌·나중에 재시도)에 따라 계약의 일관성 규칙대로 처리합니다.
- **감사 창구:** Anik의 규칙 엔진이 읽을 view 7개와 체크포인트 목록, 감사 결과 저장 창구, 경보 창구입니다.
- **가짜 Gateway(`devtools/fake_gateway.py`):** Harits의 진짜 Gateway가 오기 전까지 쓰는 대역입니다. 테스트에서 쓰고, `docker compose --profile mock up`으로 띄울 수도 있습니다. 블록체인도 서명 검사도 없습니다.

### 2. 왜 이렇게 했나

| 결정 | 이유 |
|---|---|
| 투표 요청은 선거 잠금을 잡고, 그 시점에 밀린 경계 작업(열기·체크포인트·닫기)을 먼저 처리한 뒤 시각을 찍음 | 계약의 "경계 직렬화": 경계 T 이후 요청은 반드시 T의 체크포인트·마감 뒤에 처리돼야 함 |
| 거부된 요청은 저장점(savepoint)만 되돌리고 거부 기록(rejected_attempts)은 남김 | 거부도 감사 대상이고(R1 등), 되돌리기 전에 처리한 경계 작업은 유지해야 함 |
| 체크포인트 시각은 실제 처리 시각이 아니라 경계 시각(T) | chain-interface "Boundary timestamps", R5의 주기 검사 |
| 마감 때 STATE_CHANGE와 CLOSE_FINAL 두 번 커밋 | 계약: 상태 전이마다 STATE_CHANGE, 마감 후 한 번 CLOSE_FINAL(개표 기준점) |
| 열기는 장부가 수락한 뒤에만 OPEN, 닫기는 즉시 CLOSED | 허용 전이 vs 엄격 전이 규칙 |
| 재시도 대기 시간은 DB가 아니라 메모리에 둠 | 투표 쪽 행에는 정확한 시각을 남기지 않는 규칙 |
| uvicorn 접속 로그 끔, DB 오류에서 파라미터 숨김 | 영수증 해시·토큰 해시·evr_ ID가 로그에 남지 않게(README "PII") |
| Docker 빌드 범위를 저장소 전체로 바꾸고 `contracts/`를 이미지에 포함 | 감사 결과를 계약 스키마로 검사하려면 스키마 파일이 필요 |
| 이메일 발송은 422로 막음 | 시연은 CSV(D-16). 메일은 나중 |

### 3. 확인한 것

- **실제 DB 흐름 테스트 3개 통과** (`tests/test_flow_db.py`, 시계를 고정해 시간을 넘기며 진행)
  - 선거 생성 → 생성 검사 → 명단 → 고정 → 링크 100개 → 시작 → 투표 → 주기 체크포인트 → 마감 → 감사 결과 저장
  - view의 `snapshot_hash`가 장부에 보낸 CHECKPOINT_COMMITTED 값과 같음
  - 보낸 장부 이벤트 전부가 계약 스키마(`LedgerEventSubmission`)에 맞음
  - 순번(seq)에 빈칸 없음, 같은 구간 안에서 투표 이벤트가 시스템 이벤트보다 먼저 전송됨
  - 주기 체크포인트 시각이 시작 시각 + 60초 × n과 정확히 일치
  - 장부가 선거 생성을 거부하면 경보 후 삭제 가능, D-10 규칙 위반 시 BUCKET_TOO_SMALL
- **실제 컨테이너 확인:** `docker compose --profile mock up`으로 DB·Core·가짜 Gateway를 띄우고, 직원 계정 스크립트 → 로그인 → 선거 생성 → 약 10초 뒤 생성 검사 PASSED → 명단 100명 → 고정 → 링크 100개까지 실제 시간으로 동작
- 스모크 테스트도 새 스키마에서 `smoke ok`

### 4. 아직 안 한 것 / 3주차에 할 것

- 생성 검사의 실제 노드 읽기(`CREATION_CHECK_MODE=node`) — Harits의 계약 함수 형식 필요. T01 전에 반드시 교체
- 장부에 기록된 이벤트와 outbox 비교(OUTBOX_MISMATCH)
- 진짜 Gateway·규칙 엔진과 연결(3주차 통합), 팀원 compose 블록 합치기(아직 받지 못함)
- 관리 행위·개표·확정·검토 중계(5주차), 투표자·관리자 웹

### 5. 감독관 1차 검토(CHANGES NEEDED) 반영

| 지적 | 등급 | 조치 |
|---|---|---|
| 경계 작업과 요청 시각에 시계를 두 번 읽어, 경계 직전 요청이 체크포인트와 구간이 어긋날 수 있음 | P1 | 선거 잠금 안에서 시계를 **한 번만** 읽고 그 값으로 경계 작업과 요청을 모두 처리(`tx.run_election`). 경계 시각 정각에 온 요청이 새 체크포인트·새 구간으로 들어가는지 테스트 추가 |
| 선거 생성이 Idempotency-Key를 무시 | P1 | DB 테이블 `credential.idempotency`로 24시간 같은 키+같은 본문이면 원래 응답, 다른 본문이면 409. 테스트 추가 |
| 링크 CSV 재시도 캐시가 빈 CSV로 덮어써질 수 있음, 재시작 후 재시도가 409가 아님 | P1 | 키 확인을 선거 잠금 안으로 옮기고 기존 항목은 덮어쓰지 않음. 키는 DB에 남겨 CSV가 지워진 뒤(15분, 재시작)에는 409. 테스트 추가 |
| 500건 넘는 묶음을 나눠 보낼 때 앞 요청 결과를 기다리지 않음 | P2 | 묶음마다 결과를 반영한 뒤 다음 묶음 전송 |
| 직원 요청은 경계 작업을 먼저 하지 않음 | P2 | 명단·고정·링크 발급도 같은 방식(잠금 → 시계 1회 → 경계 작업 → 요청) |
| 생성 검사가 기본값 stub(항상 통과), 재시도 없음 | P2 | 프로그램 기본값을 `node`로 바꾸고 개발용 compose에서만 명시적으로 stub. stub이면 시작 시 경고 로그. 스케줄러가 PENDING인 생성 검사를 계속 재시도(노드 읽기 자체는 3주차) |
| Gateway 4xx를 일시 장애로 처리 | P2 | 4xx는 경보(LEDGER_UNAVAILABLE, "Gateway answered HTTP 400")로 남김. 테스트 추가 |
| 잘못된 cursor·limit이 500, 같은 finding 동시 저장이 500, WINDOW_CHANGED 거부 시 FIFO 정지, 영수증 조회 5초 대기 | P3 | 400으로 응답, ON CONFLICT 처리로 200, 경보만(정지 안 함), 영수증 조회의 Gateway 대기를 1.5초로 |

남겨 둔 P3(이유): ELECTION_OPENED 수락 시 이미 커밋된 체크포인트의 상태를 바꾸는 점(스냅샷 대상 아님, 영향 작음), 가짜 Gateway가 이미지에 포함된 점(개발용 compose 편의), 직원 계정 스크립트가 운영기관 확인을 못 하는 점(레지스트리를 모르는 시점에 실행됨. 대신 API가 운영기관 admin만 허용).

테스트: **56개 통과**(단위 50 + 실제 DB 흐름 6), 스모크 테스트 `smoke ok`.

**재검토: APPROVED** (감독관, 2026-10-26). 남은 P3 중 "분할 전송 중 FIFO 정지 확인"은 바로 반영. 재검토 직후 찾은 문제 하나도 고침: 스모크 테스트가 시험용 선거 행을 되돌리지 않고 남겨, 같은 DB에서 이어 도는 흐름 테스트(CI 순서와 같음)가 깨졌음 → 스모크 전체를 트랜잭션으로 감싸고 마지막에 ROLLBACK.

---

## 2026-11-02 — 3주차 1부: 생성 검사 실제 노드 읽기, 재시도 간격(contracts v1.1)

### 한눈에 보기

| 항목 | 결과 | 확인 방법 |
|---|---|---|
| 고정 노드 직접 읽기(`app/chain.py`) | 완료 | 단위 테스트(가짜 노드가 인코딩, Core가 디코딩) |
| 생성 검사 `node` 모드 | 완료 | 실제 DB 흐름 테스트 5개 + 컨테이너 시연 |
| RETRY_LATER 재시도 간격 | 완료 | 단위 테스트 |
| Gateway `/v1/chain` 비교 | 완료 | 실제 DB 흐름 테스트 |
| T01(진짜 Gateway·규칙 엔진) | 대기 | Harits·Anik 서비스와 compose 블록을 아직 받지 못함 |

### 1. 만든 것 (쉽게)

- **고정 노드 읽기**: Gateway를 거치지 않고 블록체인 노드에 직접 물어봅니다(JSON-RPC). 웹3 라이브러리 없이 필요한 함수 두 개(`electionState`, `electionGovernance`)와 최신 블록 번호만 읽습니다. 함수 선택자는 Harits 계약과 같은 값입니다.
- **노드 주소 여러 개**: `PINNED_NODE_URL`에 쉼표로 여러 주소를 적으면 앞에서부터 차례로 시도합니다(참관인 노드 → 선관위 노드, D-03).
- **생성 검사**:
  - 노드의 최신 블록이 `생성 블록 + D`(기본 2)보다 작으면 아직 확정 전이라 기다림(D-27)
  - 노드가 60초 동안 응답하지 않으면 LEDGER_UNAVAILABLE 경보, 계속 기다림
  - 확정됐는데 선거가 없으면(configHash 0) 60초 뒤 MISMATCH "not found"
  - 값이 다르면 MISMATCH, 경보에 다른 항목 이름을 적음(예: `differs: window_end, approval_threshold`)
- **체인 설정 비교**: Gateway가 알려 주는 `/v1/chain`(체인 ID, 계약 주소, 확정 깊이)이 Core에 고정된 값과 다르면 CHAIN_CONFIG_MISMATCH 경보만 남깁니다. Core는 항상 고정된 값을 씁니다.
- **재시도 간격**: Gateway가 RETRY_LATER라고 하면 처음 30초는 1초마다, 그 뒤로는 2·4·8…초로 늘려 최대 60초. Gateway 장애(연결 실패, 5xx)는 예전처럼 1초부터 두 배씩.
- **가짜 노드**: 가짜 Gateway에 `/rpc`를 붙여 진짜 노드처럼 응답하게 했습니다. 노드 지연, 다운, 값 조작을 테스트에서 켤 수 있습니다. 인코딩은 Core 코드와 따로 작성해 서로 검증합니다.

### 2. 왜 이렇게 했나

| 결정 | 이유 |
|---|---|
| web3 라이브러리 대신 직접 구현(약 100줄) | 읽는 함수가 두 개뿐이고, 의존성을 늘리지 않음. 선택자는 Harits 계약과 같은 keccak 값으로 확인 |
| 60초 타이머는 메모리에 | 재시작하면 처음부터 다시 셈. 경보나 MISMATCH가 늦어질 뿐 잘못 판정하지는 않음 |
| `/v1/chain` 비교는 선거마다 한 번, 경보만 | 계약 문서상 Gateway 값은 대조용. 판정에는 고정 값만 씀 |
| compose 기본값 `node`, 가짜 Gateway의 `/rpc`를 노드로 사용 | 역할 문서상 stub은 3주차까지. 진짜 노드가 오면 `PINNED_NODE_URL`만 바꿈. stub 코드는 개발용으로만 남김 |

### 3. 확인한 것

- **테스트 67개 통과**(단위 + 실제 DB 흐름). 새로 추가한 것:
  - 노드가 1블록 뒤처짐 → PENDING, 명단 확정 409 → 따라잡으면 PASSED
  - 노드 값 조작 → MISMATCH, 경보에 다른 항목 이름, 선거 삭제 가능
  - 선거 없음 → 59초까지 PENDING, 60초에 MISMATCH "not found"
  - 노드 다운 → 60초 뒤 LEDGER_UNAVAILABLE, 복구되면 PASSED
  - Gateway가 다른 확정 깊이를 알림 → CHAIN_CONFIG_MISMATCH 경보, 검사는 PASSED
  - 첫 번째 노드 주소가 죽어 있으면 두 번째 주소로 넘어감(모든 노드 테스트에서)
  - 재시도 간격: 1초×30회 → 2, 4, 8, 16, 32, 60초 → 이후 60초
- **실제 컨테이너**: `CREATION_CHECK_MODE=node`, 첫 주소는 없는 주소, 둘째는 가짜 노드로 띄우고 시연 스크립트 12단계 전부 성공(생성 확인 PASSED까지 약 10초). 검토 반영 후 기본 설정(`node`, 가짜 노드 하나)으로 다시 실행해 12단계 전부 성공.

### 4. 아직 안 한 것

- T01 전체 흐름: 진짜 Gateway·규칙 엔진 필요(Harits·Anik의 Dockerfile·compose 블록 대기)
- 장부 이벤트와 outbox 비교(OUTBOX_MISMATCH)
- 확정 깊이 D=2는 잠정값. Harits 측정(11/9) 뒤 `.env` 값만 바꾸면 됨

### 5. 감독관 1차 검토(CHANGES NEEDED) 반영

| 지적 | 등급 | 조치 |
|---|---|---|
| 블록 번호는 노드 A, 선거 값은 노드 B에서 읽으면 지연이 되돌릴 수 없는 MISMATCH가 될 수 있음 | P1 | 한 번의 읽기(최신 블록 + 두 함수)는 **한 노드에서, 그 노드의 최신 블록 기준으로**. 중간에 실패하면 다음 노드에서 처음부터. 테스트: A는 호출 오류, B는 지연 → 3분 넘게 PENDING 유지 |
| 깨진 응답(hex 아님, JSON-RPC 오류, JSON 아님)이 잡히지 않는 예외가 되어 outbox 결과까지 롤백 | P2 | 모든 디코딩 오류를 "노드 응답 불가"로 바꾸고 다음 노드로 넘어감. Gateway `/v1/chain`도 같은 처리. 단위 테스트 추가 |
| 선거 잠금을 쥔 채 네트워크 호출 | P2 | 생성 검사는 스케줄러만 실행. 노드·Gateway 읽기는 트랜잭션 밖, 결과만 잠금 안에서 반영. outbox는 생성 블록만 저장 |
| 3주차인데 stub이 기본값 | P2 | compose·`.env.example` 기본값 `node`. 가짜 Gateway의 `/rpc`가 개발용 노드 |
| `/v1/chain` 비교에 노드 목록 빠짐, 체인 ID를 글자로 비교 | P3 | 고정 노드가 Gateway 목록에 있는지 확인, 체인 ID는 숫자로 비교("7777" = "0x1e61") |
| 두 함수가 각각 주는 운영 기관 ID 중 하나만 비교 | P3 | 둘 다 비교 |
| 디코딩 방어 약함 | P3 | 배열 위치가 head 안쪽이면 거부, uint8/32/64 범위 검사, 배열 길이가 데이터와 정확히 맞아야 함 |
| 장애 후 RETRY_LATER로 바뀌면 1초 재시도 구간이 사라짐 | P3 | RETRY_LATER가 처음 나올 때부터 30초를 다시 셈 |
| 주석이 저장소에 없는 파일을 근거로 듦 | P3 | "두 개의 독립 keccak 구현으로 계산"으로 바꾸고, Harits 계약이 스택에 들어오면 실제 노드로 확인 |
| 빠진 테스트 | P3 | 기관 순서만 바뀜, DUPLICATE로 시작, 노드 혼용, 깨진 응답, 재시작 후 이어서 검사, 경보 1회만 |

`.env`(로컬, 커밋 안 함)의 `PINNED_NODE_URL` 자리표시자를 가짜 노드 주소로 바꿈.

**재검토: APPROVED** (감독관, 2026-11-02). 남은 P3 중 두 건은 바로 반영: 첫 노드가 응답은 하지만 멈춰 있으면 다음 노드로 넘어가 확인(테스트 추가), node 모드인데 노드 주소나 계약 주소가 비어 있으면 시작할 때 경고. 남긴 P3: 노드 미응답 경보가 같은 체크포인트의 다른 LEDGER_UNAVAILABLE 경보와 합쳐짐(경보 키 구조상, 동작은 허용 범위), Gateway가 보고하는 노드 주소와 Core 고정 주소의 표기가 달라 거짓 경보가 날 수 있음(진짜 Gateway가 오면 주소 표기 규칙을 정함).

## 2026-11-04 — 3주차 2부: Harits PR 병합, ledger CI, 진짜 계약 대조, 장부-outbox 비교

### 한눈에 보기

| 항목 | 결과 | 확인 방법 |
|---|---|---|
| Harits PR #1(해시 모듈), #2(계약 뼈대) | main에 병합 | 감독관 검토: #1 APPROVED, #2 조건부 APPROVED(P1 없음) |
| ledger CI(`.github/workflows/ledger.yml`) | 완료 | GitHub Actions 전 단계 통과 |
| 생성 검사 ↔ Harits 실제 계약 | 8/8 일치 | 로컬 ganache에 계약 배포 후 Core 코드로 읽기 |
| 장부 이벤트와 outbox 비교(OUTBOX_MISMATCH) | 완료 | 실제 DB 흐름 테스트 6개 + 단위 테스트 1개, 전체 74개 통과 |

### 1. 만든 것 (쉽게)

- **ledger CI**: Harits 코드가 바뀔 때마다 체인 없이 할 수 있는 검사를 자동으로 돌립니다.
  - 해시 벡터 테스트, 서명 모듈 테스트
  - 공식 solc 0.8.28(sha256 고정)로 컴파일(optimizer 켬, shanghai)
  - 계약 함수 서명·이벤트가 `chain-interface.md`와 같은지(`check_interface.py`)
- **진짜 계약과 생성 검사 대조**: 내 PC의 ganache 체인에 Harits 계약을 배포하고, 조직 등록 후 선거를 만들어 Core 코드(`app/chain.py`, `creation_check.compare`)로 읽었습니다. 결과 8/8:
  - 확정 전(D=2) → 기다림, 확정 뒤 같은 값 → 통과
  - 창 종료·제안 TTL을 바꿔 넣음 → 그 두 항목 이름으로 불일치
  - 같은 조직을 오름차순이 아닌 순서로 넣음 → `org_ids` 불일치
  - 없는 선거 → configHash 0, 조직 목록 비어 있음
  - 함수 선택자 일치
  - 스크립트: scratchpad `nodecheck/run.py`(저장소에는 넣지 않음. ganache·solc-js가 필요하고 PureChain이 아님)
- **장부-outbox 비교**(`app/ledger_compare.py`): 30초마다 outbox를 먼저 읽고, Gateway `GET /v1/events`로 선거의 모든 이벤트를 읽어 맞춰 봅니다.
  - Gateway 이벤트 종류이면서 `source: gateway`인 것만 빼고 모두 Core 이벤트로 봄(source 표시만 바꿔 피하는 것 차단)
  - event_id 형식이 맞고, outbox에 있고, Gateway가 REJECTED라고 답했던 것이 아니어야 함
  - 보낸 이벤트 전체와 같아야 함: Gateway가 붙이는 `received_at`·`anchor`만 빼고 비교(hashing.md의 leaf_hash 범위). 종류·seq·체크포인트·시각·행위자·payload·payload_hash가 모두 포함되고, 다른 항목 이름을 경보에 적음
  - 반대 방향: outbox에서 ACCEPTED/DUPLICATE(저장됨)인데 목록에 없으면, 연속 두 번 없을 때 보고(목록이 한 번 늦는 것은 허용)
  - 두 번 읽는 사이에 전송된 이벤트(목록에는 있고 처음 읽은 outbox에는 없음)는 outbox를 한 번 더 읽어 확인
  - 새 문제 → OUTBOX_MISMATCH 경보(Core의 현재 체크포인트, 이후 문제는 같은 경보에 덧붙임) + FIFO 정지. 문제는 이벤트 ID(없으면 목록 위치)와 내용으로 구별: 같은 문제로는 다시 멈추지 않고(운영자가 재개한 FIFO 유지), 다른 이벤트를 끼워 넣거나 같은 이벤트를 또 바꾸면 다시 멈춤
  - 목록을 90초 동안 못 읽으면 LEDGER_UNAVAILABLE 경보(전송은 계속, 장애는 불일치가 아님)

### 2. 왜 이렇게 했나

| 결정 | 이유 |
|---|---|
| 매번 전체 목록을 읽음(커서 안 씀) | 끼워 넣은 이벤트는 과거 시각을 달 수 있어서 커서 뒤만 보면 놓침. 같다고 확인한 이벤트는 그 내용의 해시를 기억해, 내용이 바뀌면 다시 비교 |
| payload만이 아니라 이벤트 전체 비교 | 역할 문서 문구는 (event_id, payload_hash)지만, 체크포인트·seq만 바꿔도 규칙 엔진 판정이 달라짐. leaf_hash와 같은 범위 |
| 반대 방향도 확인 | 계약상 ACCEPTED는 "저장됨". 집계·"outbox 비움" 판단이 이를 믿으므로 빠지면 불일치. 역할 문서 문구는 한 방향이라 decisions 기록 필요(팀 확인) |
| outbox가 아직 PENDING인 이벤트는 정상 | ACCEPTED 응답만 잃어버린 경우(재전송하면 DUPLICATE) |
| 경보 체크포인트는 Core 값 | Gateway가 보낸 값(예: 2**64)이 경보 저장을 깨뜨리지 못하게 |
| 비교 중 예외는 "unreadable"로 보고, 목록 파싱 예외는 모두 Gateway 장애로 | 이벤트 하나의 NUL 문자·깊은 중첩은 그 이벤트의 불일치로 보고. 응답 전체가 파싱 불가(깊은 중첩 JSON 포함)면 장애로 처리되어 90초 뒤 경보 |
| 경보에는 시스템 이벤트 ID만, 투표자 이벤트는 종류(와 개수)만 | README 개인정보 규칙. Gateway가 보낸 종류는 계약의 이벤트 종류일 때만 옮김 |
| 목록 페이지 최대 1000(이벤트 100만), 같은 커서 반복 시 중단 | 끝없는 목록으로 Core 메모리를 고갈시키지 못하게 |
| 30초 간격 | 탐지가 목적(예방 불가, D-26) |

한계(문서화): 키마다 다른 목록을 보여 주는 Gateway는 Core가 탐지할 수 없음. 기억은 메모리에 있어 재시작하면 처음부터 다시 비교하고 알려진 문제도 다시 보고함(재개 기능을 만들 때 DB에 저장해야 함). 종료된 선거도 계속 읽음.

### 3. 확인한 것

- 테스트 74개 통과(실제 DB). 새로 추가한 것:
  - 정직한 Gateway: 두 번 읽어도 문제 없음, PENDING 행·Gateway 이벤트 종류는 문제 아님
  - 끼워 넣기, 내용 변경(해시 맞춤/안 맞춤), 체크포인트만 변경, Core 종류를 gateway로 표시, 투표자 이벤트 끼워 넣기, 깨진 ID → 각각 보고, 투표자 ID·해시는 경보에 없음
  - 재개 뒤 같은 문제로는 다시 멈추지 않고, 새 문제는 경보에 덧붙고 다시 멈춤(투표자 이벤트를 하나 더 끼워 넣은 경우 포함). 멈춘 동안 다음 이벤트가 전송되지 않음
  - 비교가 outbox를 읽은 뒤·목록을 읽기 전에 이벤트가 전송됨 → 문제 아님
  - NUL 문자 ID, 체크포인트 2**64, 5000단 중첩 → 예외 없이 보고
  - 저장됐던 이벤트가 목록에서 사라짐 → 두 번째 읽기에서 보고
  - 목록 장애 → 90초 뒤 LEDGER_UNAVAILABLE, 전송은 계속. REJECTED였던 이벤트가 목록에 있음 → 보고
  - Gateway 목록 여러 페이지, 깨진 페이지, 10만 단 중첩 JSON, 끝없는 목록·반복 커서
- 실제 컨테이너 시연(새 코드): 12단계 성공, 시연 선거에 OUTBOX_MISMATCH 없음
- 개발 환경 주의: 가짜 Gateway는 메모리에만 저장해서, mock 스택을 다시 띄우면 이전 선거의 이벤트가 목록에서 사라집니다. 그러면 이전 선거들은 "accepted but not listed"로 FIFO가 멈춥니다(실제로 확인: 재시작 전 시연 선거와 같은 DB를 쓰는 테스트 선거). 진짜 Gateway가 저장소를 잃은 경우와 같은 상황이라 의도한 동작입니다. 새로 만든 선거는 영향 없음

### 4. 감독관 1차 검토(CHANGES NEEDED) 반영

| 지적 | 등급 | 조치 |
|---|---|---|
| source만 gateway로 바꾸면 비교를 피함 | P1 | Gateway 이벤트 종류 + `source: gateway`만 제외 |
| Gateway 값(체크포인트 2**64, NUL ID, 깊은 중첩)으로 경보 전에 실패 | P1 | 경보 체크포인트는 Core 값, ID 형식 먼저 확인, 비교 예외는 "unreadable"로 보고 |
| 재개 뒤 조용히 다시 정지 | P2 | 이미 보고한 문제로는 다시 멈추지 않음 |
| 새 불일치가 경보에 안 보임 | P2 | 같은 경보 키면 detail에 덧붙임(`raise_alert(append=True)`) |
| 기억이 event_id만 | P2 | 내용 해시를 기억, 바뀌면 다시 비교 |
| payload만 비교 | P2 | 이벤트 전체(leaf_hash 범위) 비교 |
| 목록 장애 시 조용히 건너뜀 | P2 | 90초 뒤 LEDGER_UNAVAILABLE |
| 페이지 수 상한 없음 | P2 | 최대 1000페이지, 반복 커서 중단 |
| REJECTED 거짓 양성 경합 | P2 | outbox를 먼저 읽음 |
| 반대 방향 미확인 | 권고 | 연속 두 번 없으면 보고 |
| 경보에 옮기는 종류 제한, ACCEPTED 없는 선거도 비교, 한계 문서화, `run.py` 주석·실행 로그 | P3 | 반영. 실행 로그: scratchpad `nodecheck/run_output.txt`, `w3p2/pytest_db_run.txt` |

재검토(CHANGES NEEDED) 반영:

| 지적 | 등급 | 조치 |
|---|---|---|
| outbox를 먼저 읽게 바꾸면서, 두 읽기 사이에 전송된 이벤트가 "outbox에 없음"이 되어 정직한 흐름에서 FIFO가 멈춤 | P1 | 목록에 있는데 처음 읽은 outbox에 없는 ID만 다시 읽음. REJECTED 판정은 처음 읽은 상태로만. 경합 재현 테스트 추가 |
| 깊게 중첩된 JSON 응답이면 파싱 예외가 장애로 바뀌지 않아 경보 없이 계속 건너뜀 | P1 | 목록 파싱의 모든 예외를 Gateway 장애로. 원시 JSON 바이트 테스트 추가 |
| 문제를 경보 문구로 구별해서, 투표자 이벤트를 하나 더 끼워 넣으면 재개 뒤 다시 멈추지 않음 | P2 | 이벤트 ID(또는 목록 위치)와 내용 해시로 구별. 테스트 추가 |
| 기억한 이벤트가 나중에 REJECTED가 되어도 못 잡음, 이름 없는 "differs:" | P3 | REJECTED 확인을 기억보다 먼저, 이름이 없으면 "encoding" |
| CONFLICT 이중 보고, 재시작 시 재정지 | P3 | 모듈 설명에 한계로 적음(재개 기능 구현 때 DB 저장) |

**재검토 2: APPROVED** (감독관, 2026-11-04). 남은 권고(P3): 형식이 틀린 항목은 목록 위치로 구별해서 투표가 진행되면 같은 항목이 새 문제로 보일 수 있음, 보고된 문제는 메모리에만 있음 → outbox:resume·ELECTION_VOID를 만들 때 위치와 무관한 키와 DB 저장으로 바꿈. 목록만 계속 못 읽으면 경보만 나고 전송은 계속(운영 문서에 "I12 의심"으로 적을 것). 이벤트 전체 비교와 반대 방향 검사는 역할 문서 문구보다 넓으므로 decisions 기록은 팀장 판단. 진짜 Gateway가 오면 `GET /v1/events` 동작(확정 전 이벤트 포함 여부, 커서, 반영 지연)을 Harits와 확인하고 두 번 유예가 충분한지 다시 봄. 시연 전에는 DB와 mock을 함께 초기화.

### 5. Harits에게 전할 것 (PR #2 검토에서)

- 테스트 주석의 "오름차순" 조직 순서가 실제와 다름. Gateway는 org_id 문자열 오름차순(commission, neutral, observer)으로 보내야 함. 진짜 계약에서도 순서가 다르면 생성 검사가 불일치를 냄(위 대조에서 확인)
- `setup_registry.py`는 `PINNED_NODE_URL`을 쉼표 목록(v1.1)으로 유지해야 함
- 서명 검증 단계에서 레지스트리 등록 봉인(P2-1)·계정 변경 시 진행 중 선거 보호(P2-2) 검토

### 6. 아직 안 한 것

- T01 전체 흐름: 진짜 Gateway(아직 없음)·규칙 엔진 compose 블록, T01 정답 라벨 대기
- PureChain 위에서의 생성 검사(이번 대조는 ganache)
