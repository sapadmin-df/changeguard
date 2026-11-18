# changeguard REQUIREMENTS — 고정 북극성 (Layer 0)

> **문서 버전:** 1.0 (2026-11-18) · 이 문서의 `REQ-*`는 **추상 수준에서 고정**된다.
> 구현(언어·CI 제품·모델명·파일 경로)이 바뀌어도 참이어야 하는 문장만 둔다. 구체 값과 위치는
> §D Traceability의 근거로만 쓴다.
>
> - **변경 규칙:** REQ 문장을 바꾸려면 **개정(amendment)** 절차를 거친다. 이유, 영향받는 spec,
>   버전 증가, 개정 이력이 필요하다. HOW만 바뀌면 spec(README, SKILL.md, FEEDBACK-LOOP.md,
>   INSTALL.md, 코드)을 자유롭게 고치고 §D만 갱신한다.
> - **판단 기준:** 처음부터 다른 기술로 다시 만들어도 지켜야 하는 목표인가? 그렇다면 REQ이고,
>   아니라면 spec이다. 애매하면 REQ로 취급한다.
> - **Spec 층 (Layer 1, 가변):** `README.md`(사상·구조), `skills/pre-merge-review/SKILL.md`(검사
>   절차), `docs/FEEDBACK-LOOP.md`(피드백 루프·telemetry·원장), `docs/INSTALL.md`(소비자 설치),
>   `skills/pre-merge-review/output-schema.json`(출력 계약)
> - **후보 층 (Layer 2):** 벤치마크, 연구, census 결과. spec의 *후보*일 뿐이며, 기본은 반박(채택
>   안 함)이다.

---

## §0 Scope Dial

| 축 | 좁힘 (축소) | 넓힘 (발산) |
|---|---|---|
| 기능 | 변경 검문 게이트 하나 (merge 전) | 여러 게이트 (commit 전, release, 의존성, 비밀정보) |
| 환경 | 한 CI 제품 + 로컬 AI 에이전트 | 여러 CI 제품, 에이전트 없는 로컬 hook |
| 규모 | 소유자가 같은 소비자 소수 | 서로 모르는 외부 소비자 다수 |
| 판정 계층 | 결정론만 | 결정론 + LLM (+ 모델 fallback) |
| 공개도·기여 | telemetry off, 실행 기록이 소비자 안에만 머묾 | 공개 telemetry, 판정 피드백을 소비자들이 함께 공유 |

**현재 위치 (2026-11-18):**
- 기능: merge 전 게이트 하나
- 환경: GitHub Actions + Claude Code
- 규모: 소유자가 같은 소비자 1개 (oh-my-bas)
- 판정 계층: 결정론 + LLM
- 공개도: telemetry를 **시험 단계**로 연다. 소유자가 동의한 private 소비자 1곳에서 시작한다.

**발산 트리거:**
- 외부 소비자가 처음 생기면 규모 축을 넓힌다. 이때 인증 방식, 소음 관리, 공개 수준을 다시
  점검한다(REQ-C.3, REQ-C.6).
- 두 번째 게이트가 필요해지면 기능 축을 넓힌다.

---

## A. 핵심 요구사항

### A.1 Purpose
외부에서 들어오는 코드 변경을 merge 전에 검사한다. 공급망 공격, 비밀정보 유출, 실행 경로 오염,
AI 에이전트 지시문 주입 같은 위험을 **로컬과 CI에서 같은 정책으로** 판정한다. 정책 자체는 검사
대상과 분리해 변조할 수 없게 한다. 판정 결과는 **정규화된 기록**으로 남긴다. 사람의 피드백과
여러 소비자의 실행 기록을 모아 정책을 **재귀적으로 개선**한다. 공개를 통해 changeguard와 이를 쓰는
저장소들이 함께 보안 수준을 높이는 것을 지향한다.

### A.2 Target Users
| 사용자 | 시나리오 |
|---|---|
| 정책 관리자 | 정책을 바꾸고, fixture로 검증하고, 판정 통계로 개선 후보를 고른다 |
| 소비자 저장소 운영자 | 게이트를 설치하고, 정책 갱신을 받고, 모델이나 telemetry를 선택한다 |
| PR 작성자·리뷰어 | 판정과 근거를 보고, 오탐이나 의도된 변경을 피드백한다 |
| 기여 소비자 | telemetry를 켜서 분석할 실행을 공유하고, 사후 모니터링에 참여한다 |
| 로컬 AI 에이전트 | merge 전 같은 정책으로 검사 절차를 수행한다 |

### A.3 Design Goals
| ID | Goal | 설명 |
|---|---|---|
| G1 | 단일 정책 출처 | 판정 기준은 한 곳에만 있고, 로컬과 CI가 같은 기준을 쓴다 |
| G2 | 다층 방어 | 빠르고 설명 가능한 결정론 검사가 하한선이다. 의미 판단은 LLM이 보탠다 |
| G3 | 반증 가능성 | 정책의 효력은 주장하지 않고 fixture로 검증한다. 정책이 약해지면 테스트가 실패한다 |
| G4 | 불변 참조 | 소비자는 정책을 변경 불가능한 식별자로 참조한다. 갱신은 명시적이고 검증된다 |
| G5 | 관측 가능한 재귀 개선 | 모든 판정과 성능이 집계 가능하게 남는다. 개선 전후를 정책 버전별로 비교한다 |
| G6 | 개방과 상호 기여 | 정책, 판정 근거, 집계 결과는 최대한 공개한다. 공개 범위는 소비자가 정한다 |

### A.4 Functional Requirements

**REQ-A.1 일관 판정.** 같은 변경과 같은 정책 버전이면 로컬과 CI가 같은 판정 규칙을 적용한다.
판정은 `block`, `advisory`, `pass` 세 단계이며, 각 단계가 merge에 미치는 효과는 명확히 정의한다.

**REQ-A.2 다층 판정과 하한 보장.** 결정론 검사 결과가 판정의 하한선이다. LLM이나 다른 의미
계층은 판정을 **올릴 수만 있고 내릴 수는 없다.** 두 계층이 다르게 판단하면 그 사실을 기록한다.

**REQ-A.3 조용한 통과 금지.** 의미 계층이 실패하는 경우에도 결과에 구분 가능한 상태로 드러나야
한다. 해당하는 실패는 미설정, 과금·한도 초과, 거부, 출력 잘림, 응답 형식 오류, 입력 잘림이다.
"검사하지 않았음"이 "문제 없음"으로 보여서는 안 된다.

**REQ-A.4 정책 공급망.** 소비자는 정책을 불변 식별자로만 참조한다. 정책 갱신은 소비자가 명시적
변경으로 받아들인다. 갱신 변경 자체도 검증 대상이며, 출처가 정책 저장소 본선인지 확인한다.

**REQ-A.5 모델 선택.**
- 기본 모델은 정책이 명시적으로 고정하고 버전과 함께 기록한다.
- 소비자는 정책을 바꾸지 않고 모델을 덮어쓸 수 있다.
- 더 나은 후보 모델이 나오면 **감지해 사람에게 알린다.** 자동으로 따라가지는 않는다.
- 실제 응답한 모델과 요청한 모델이 다르면 기록한다. fallback이 이 경우에 해당한다.

**REQ-A.6 정규화 실행 기록.** 모든 실행은 기계로 일괄 집계할 수 있는 기록을 남긴다. 기록의
최소 내용은 다음과 같다.
- 판정, 판정 주체, 계층 간 불일치
- 분석이 필요한 이유(signal)
- 의미 계층의 실행 상태, 모델, 비용 단서
- 단계별 소요 시간
- finding 목록. 각 finding은 **문구와 무관한 안정 규칙 ID**, run을 넘어 추적할 수 있는 식별자,
  경로 성격을 가진다.

이전 형식의 기록도 소급 변환해 같은 질의로 집계할 수 있어야 한다.

**REQ-A.7 피드백 루프.**
- 사람은 특정 finding이나 실행에 피드백(오탐, 의도된 변경, 누락 등)을 붙일 수 있다.
- 집계는 피드백과 기록을 결합해 **개선 후보**를 낸다. 후보 종류는 완화·문맥화, 정적 규칙으로
  승격, 운영 문제, 성능이다.
- 채택된 개선은 **fixture를 먼저** 만든 뒤 정책을 바꾼다.
- 효과는 정책 버전별 재측정으로 확인한다.

**REQ-A.8 성능·비용 관측.** 단계별 소요 시간과 의미 계층의 비용 단서(토큰, 모델)는 성격별
(결정론, 의미 계층, 부대 검증)로 분리해 집계할 수 있어야 한다. 변경 크기 같은 입력 규모와도
연관 지어 볼 수 있어야 한다.

**REQ-A.9 telemetry (opt-in).**
- 소비자가 명시적으로 켰을 때만 동작한다. **기본은 off**다.
- 분석이 필요한 실행만 보낸다. signal이 없는 실행은 보내지 않는다.
- 같은 변경에 대한 반복 실행은 하나의 공유 지점에 모은다.
- 결과 링크를 소비자에게 되돌려준다.
- telemetry 실패가 소비자의 판정이나 CI 결과를 바꾸면 안 된다.

**REQ-A.10 집계 편의.** 소비자와 관리자는 기록을 전용 서버 없이 **흔한 도구**로 집계할 수 있어야
한다. 여기서 흔한 도구는 표 형식 파일, 내장형 SQL, 표준 출력 보고서를 뜻한다. 대표 질문에는 문서화된
질의 예시가 있어야 한다.

### A.5 Non-Functional Core Values
- **신뢰성:** 입력 인코딩이나 크기가 비정상이어도 검사 단계가 중단되지 않는다. 부분 실패는
  REQ-A.3에 따라 드러낸다.
- **성능:** 게이트는 PR 리뷰 흐름을 막지 않는 시간 안에 끝난다. 의미 계층의 지연은 별도로
  관측한다(REQ-A.8).
- **사용성:** 판정 근거와 피드백 방법이 PR 화면 안에서 바로 보인다. 설치는 한 번의 절차로 끝난다.

---

## B. 준수사항

### 표준기술

**REQ-B.1 실행 환경 최소 의존.** 엔진은 CI 러너에 기본으로 있는 도구와 언어 표준 라이브러리만
쓴다. 별도 패키지 설치 없이 동작해야 한다. 공급망 표면을 줄이기 위해서다.

**REQ-B.2 표준 준수.**
- 정책 버전은 Semantic Versioning을 따른다.
- 정책 저장소 본선은 서명된 커밋, 리뷰 승인, 필수 검사 통과로만 바뀐다.
- 외부 action과 재사용 워크플로우는 불변 식별자로 고정한다.
- 출력은 공개된 스키마 계약을 따른다.

**REQ-B.3 기준 corpus.** 악성, 정상, 경계, 적대적, 의미 계층 전용의 다섯 범주로 fixture corpus를
유지한다. 실측에서 발견된 누락이나 오탐은 반드시 corpus에 반영한다.

### 컨벤션

**REQ-B.4 명명·문서.**
- 규칙 ID는 `<계층>.<분류>.<이름>` 계층형으로 짓고, 한번 부여하면 바꾸지 않는다.
- 문서는 한국어로 쓰고, 기술 식별자는 원어로 둔다.
- 요구사항, 합의, 금지 변경은 version-controlled 문서(이 파일과 spec 문서)에 남긴다. 대화나
  메모리에만 두지 않는다.

**REQ-B.5 운영 환경.**
- 소비자 CI는 호스팅 CI(현재 GitHub Actions)를 쓴다.
- 로컬에서는 AI 코딩 에이전트(현재 Claude Code)를 쓴다.
- LLM은 공급자 API를 키 기반으로 호출한다.
- telemetry는 소비자가 발급한 최소 권한 자격으로 전송한다.

---

## C. 제약사항

### C.1 Scope Boundaries (실현가능성)

**REQ-C.1 범위.**
- 범위 안: 변경 diff 검사, 정책 갱신 검증, 판정 기록, 피드백 집계, opt-in telemetry.
- 범위 밖: 저장소 전체 정적 분석(SAST), 런타임 보안, 자동 수정, 자동 merge 결정의 위임.
- 정책 갱신과 모델 갱신은 사람이 승인한다.

### C.2 Anti-patterns (금지 변경)

**REQ-C.2** 아래 변경은 금지한다. "실증"은 실제 운영에서 겪은 사례다.

| 금지 | 이유 / 실증 |
|---|---|
| 의미 계층이 결정론 판정을 낮추는 변경 | 하한 보장(REQ-A.2) 붕괴 |
| 의미 계층 실패를 pass로 흘리는 변경 | **실증:** 과금·한도 실패 42건이 조용히 지나갈 뻔함 (2026-10 census) |
| 최신 모델 자동 추종을 기본값으로 두기 | 판정 기준이 예고 없이 바뀜. 감지와 통지만 한다(REQ-A.5) |
| 변경 가능한 참조(branch, tag)로 정책이나 action을 호출 | 공급망 원칙 위반(REQ-A.4) |
| telemetry 기본 on, 또는 묵시적 활성화 | **사용자 지시:** 기본 동작은 당연히 NO |
| 사람용 문구로 집계하거나 규칙 ID를 바꾸기 | 이력 집계가 깨짐(REQ-A.6) |
| 소비자 CI를 갭 분석 없이 덮어쓰기 | **실증:** oh-my-bas 고유 설정 손실 위험. 갭 분석 후 반영한다 |
| 입력 디코딩 실패로 검사 중단 | **실증:** 비 UTF-8 diff로 5건 크래시 (v0.17.1) |
| fixture 없이 정책 완화나 강화 | 반증 가능성(G3) 상실 |

### C.3 Security

**REQ-C.3 공개와 비밀의 경계.** 공개는 기본 지향(G6)이다. 다만 아래 경계는 고정한다.
1. 비밀정보(키, 토큰, 웹훅)는 어떤 기록, 로그, 문서, telemetry에도 남지 않는다.
2. **비공개 소스의 코드 내용**(경로, 라인, 문구, 근거)은 공개 대상으로 나가지 않는다. 예외는 소스
   소유자가 명시적으로 동의한 경우뿐이다. 동의하지 않으면 집계용 최소 정보(규칙, 분류, 판정,
   signal, 시간)만 나간다.
3. **아직 고치지 않은 취약점의 상세**를 한곳에 모아 증폭하지 않는다. 소스가 공개여도 같다.
4. 변경에서 유래한 텍스트는 모두 신뢰하지 않는 입력으로 다룬다. 공개 지점으로 옮길 때 다른 도구나
   에이전트에게 지시로 해석되지 않는 형태로만 싣는다.
5. 소비자가 주는 자격은 공유 지점에 쓰기만 가능한 최소 권한이다. 정책 저장소의 코드나 워크플로우를
   바꿀 수 없어야 한다.
6. 공유 지점에서 일어나는 이벤트가 정책 저장소의 자동 실행을 유발해서는 안 된다.

### C.4 Cost / Budget (경제성)

**REQ-C.4**
- 의미 계층 비용은 실행마다 관측할 수 있어야 한다(REQ-A.8).
- 같은 품질이면 더 싼 모델이나 경로를 고른다.
- 한 실행의 출력 상한과 시간 상한을 둔다.
- 결정론만으로 판정이 끝나는 경우의 비용 최적화는 spec에서 다룬다.

### C.5 Maintainability / Ops

**REQ-C.5**
- 정책 변경은 fixture 회귀, 고정 참조 검증, 재사용 워크플로우 자체 검사를 통과해야 merge된다.
- 운영 경보(모델 노후화, mock 드리프트, 새 후보 모델)는 사람이 볼 수 있는 곳에 자동으로 올린다.

### C.6 Scale Limits

**REQ-C.6**
- 의미 계층에 넣을 수 있는 입력 크기에는 한계가 있다. 이를 넘으면 잘린 사실을 판정과 기록에
  드러낸다(REQ-A.3).
- telemetry 공유 지점은 소비자가 늘어도 개발 이슈와 분리해 걸러볼 수 있어야 한다.

---

## §D Traceability & Gaps

### 근거 (verified, 2026-11-18 · 정책 v0.18.0 = PR #15 브랜치)

| REQ | 현재 구현 근거 | 상태 |
|---|---|---|
| A.1 | `skills/pre-merge-review/SKILL.md`(로컬) ↔ `.github/workflows/pre-merge-review.yml`(CI)가 같은 `checks/`를 사용 | verified |
| A.2 | `ci-caller.py` 판정 = 최고 심각도. 의미 계층은 추가만 함. `normalize.py`의 `disagreement` | verified |
| A.3 | `llm-adapter.py`의 `llm_run.state` / `classify_llm_failure` / `llm.diff_truncated`. 미가용 시 advisory | verified |
| A.4 | 재사용 워크플로우 `@<SHA>` + `policy_sha` 일치 검사, `policy-bump-verify.py`, `verify-workflow-pins.sh` | verified |
| A.5 | 기본 `claude-sonnet-5-5` 고정, `vars.LLM_MODEL` 덮어쓰기, `model_select.py` + `policy-self-test.yml` model-watch job(issue 생성), PR comment의 모델/fallback 표시 | verified |
| A.6 | `normalize.py` 스키마 `changeguard.review/v1`, 규칙 ID, `id = sha1(rule\|path\|line)[:12]`, 소급 변환 | verified |
| A.7 | `fb:*` 라벨, `/cg fp <id>` 댓글, `scripts/cg-insights.py` 후보 규칙, `--policy-version` 비교 | verified (실사용 피드백 0건) |
| A.8 | `timings`(deprecation/bump_verify/deterministic/adapter/llm/total ms), `diff.bytes`, 크기 버킷, 토큰 단가표 | verified |
| A.9 | `telemetry.py`: `CG_TELEMETRY` 기본 off, signal 있을 때만 전송, `cg-key` 중복 제거, 항상 exit 0 | verified (실전송 0건) |
| A.10 | `cg-insights.py --ledger` → runs/findings/feedback.csv + ledger.sqlite, `--sql`, FEEDBACK-LOOP.md 질의 8개 | verified |
| B.1 | Python 표준 라이브러리, bash/jq/awk만 사용 | verified |
| B.2 | ruleset `protect-main`(서명, PR, 필수 검사), `VERSION` SemVer | verified |
| B.3 | `examples/` malicious/benign/edge/adversarial/llm-only, run-tests 32 / integrated 46 | verified |
| C.3-2~4 | `telemetry.py` `prepare()`: 비공개→공개 minimal 강제, `--share-private` 동의 시 full, 공개 대상 high+ redaction, code span·300자·가변 fence. 단위 테스트 `test_normalize_telemetry.py` | verified (단위), 실전송은 DP-6 |
| C.3-6 | changeguard에는 `issues`/`issue_comment` 이벤트로 시작하는 workflow가 없음 (2026-11-18 확인) | verified |
| C.6 | 입력 상한 600k 문자, 잘리면 `llm.diff_truncated` | verified (처리 개선은 Gap) |

### 실측 근거 (Layer 2, 2026-10 census · oh-my-bas 751 runs, v0.17.1)
- 판정 분포: pass 657, advisory 88, block 6
- LLM이 결정론보다 엄격했던 경우 17건: 정당 약 13, 경계 2, 과잉 1~2
- LLM 실패 42건: billing 36, quota 6
- 입력 잘림 22건이 advisory 17건을 만듦
- 결정론 누락: 환경변수 prefix가 붙은 pipe-to-shell → fixture M20으로 보완
- job 소요: p50 21s, p90 34s. LLM 호출 시 평균 27s, 미호출 시 16s

### 합의 기록 (사용자 지시·합의)
| 날짜 | 내용 | 반영 REQ |
|---|---|---|
| 2026-10 | 기본 모델은 최신 중급 Sonnet으로 **고정**, 소비자 변수로 덮어쓰기 가능, 새 모델은 감지 후 issue | A.5 |
| 2026-11 | telemetry 기본 동작은 **NO**. 목적은 changeguard와 소비자 저장소의 상호 기여와 사후 모니터링 | A.9, C.2 |
| 2026-11-18 | changeguard는 **최대한 공개**되어야 의미 있다. private telemetry 저장소는 채택하지 않는다 | G6, C.3 |
| 2026-11-18 | 공개가 가능하면 별도 저장소 없이 **changeguard 본 저장소**를 공유 지점으로 쓰는 방향 | C.6 (spec) |
| 2026-11-18 | oh-my-bas(private, 소유자 본인)로 telemetry를 **시험**한다 | §0 현재 위치 |

### 결정 분기점 (재평가 대상)
되돌릴 수 있는 spec 결정이다. 시험 운영 후 "재평가 기준"으로 판정하고, 결과를 이 표에 덧붙인다.
REQ 문장은 바뀌지 않는다. 분기 결과가 REQ를 움직여야 한다면 그때 개정 절차를 밟는다.

| ID | 결정 (2026-11-18) | 대안 | 재평가 기준 | 재평가 결과 |
|---|---|---|---|---|
| DP-1 | telemetry 공유 지점 = **changeguard 본 저장소** (`telemetry_repo` 기본값) | 별도 public repo, private repo | 개발 issue가 telemetry에 묻히는가(`-label:cg-telemetry`로 충분한가), 알림 소음, 토큰 오남용 흔적 | (시험 후) |
| DP-2 | private 소스는 기본 minimal, **소유자 명시 동의**(`telemetry_share_private`) 시 full | 무조건 minimal, 공개 repo면 full | 동의 경로로 보낸 내용 중 공개되면 곤란한 것이 있었는가. minimal만으로 분석이 되었는가 | (시험 후) |
| DP-3 | 공개 대상에는 **high/critical 상세(경로·라인·문구·LLM 서술) 미게재**, 규칙·id만 | merge 전 PR만 숨김, 전부 게재 | 숨긴 탓에 피드백·원인 분석이 막혔는가(artifact 왕복 비용). 숨겨야 했던 사례가 실제로 있었는가 | (시험 후) |
| DP-4 | diff 유래 문구: 표는 code span, 레코드 300자 제한, 가변 길이 fence | 문구 미게재 | 렌더링 깨짐·멘션 오발송·잘림으로 분석 손실이 있었는가 | (시험 후) |
| DP-5 | 인증 = 소비자가 만든 **fine-grained PAT** (공유 지점 Issues R/W만) | GitHub App, classic PAT | 외부 소비자가 생기면 즉시 재평가 (fine-grained PAT는 다른 owner repo 불가) | (시험 후) |
| DP-6 | 시험 대상 = **oh-my-bas** (private, 소유자 본인), `full` + 동의 | minimal로 시작 | 1회전(기록 → issue → 피드백 → 집계 → 후보)이 실제로 돌았는가 | (시험 후) |

### Gaps (TBD, spec 작업 후보)
| # | 내용 | 관련 REQ |
|---|---|---|
| 1 | ~~telemetry 기본 공유 지점을 changeguard 본 저장소로~~ → DP-1로 구현 (v0.18.0). 라벨 사전 생성 포함 | A.9, C.6 |
| 2 | ~~소유자 동의 시 비공개 소스 `full`~~ → DP-2로 구현 | C.3-2 |
| 3 | ~~high/critical 상세 비공개~~ → DP-3으로 구현 (merge 전/후 구분 없이 공개 대상 전체) | C.3-3 |
| 4 | ~~diff 유래 문구 안전 렌더링~~ → DP-4로 구현 | C.3-4 |
| 5 | 외부 소비자용 인증 방식(GitHub App 등). fine-grained PAT는 다른 owner의 저장소에 쓸 수 없음 | C.3-5, §0 발산 트리거 |
| 6 | 대형 diff 처리(분할 또는 우선순위 선별). 잘림 22건, advisory 17건 | C.6, A.3 |
| 7 | 제안 B(코드 파일의 주석 줄에서 실행 패턴 제외), 제안 C(test 경로 critical → high) | A.7 후보, fixture 선행 |
| 8 | 피드백, telemetry 실사용 데이터 0건. 시험 운영으로 루프 1회전 검증 필요 | A.7, A.9 |

---

## 개정 이력
| 버전 | 날짜 | 내용 |
|---|---|---|
| 1.0 | 2026-11-18 | 최초 고정. v0.18.0 기준으로 A/B/C 정리. 공개 지향과 telemetry 시험 합의를 반영 |
