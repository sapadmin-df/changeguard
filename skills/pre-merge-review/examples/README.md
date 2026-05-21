# Red-Team Fixtures

skill 회귀를 막기 위한 알려진 악성/정상 패턴 corpus.

## 구조

```
examples/
├── malicious/                                  반드시 잡혀야 하는 위협 (16개)
├── benign/                                     잡히면 안 되는 정상 변경 (7개)
├── edge/                                       경계가 모호한 케이스 (3개)
├── llm-only/                                   결정론으로는 못 잡는 — LLM 계층 필요 (1개)
├── adversarial/                                LLM이 격리 규칙을 어겼다 가정한 응답 (8개)
├── run-tests.sh                                결정론 계층 단독 검증
├── run-tests-integrated.sh                     결정론 + (mock) LLM + gating 시뮬레이션
└── verify-mock-vs-production.py                production LLM이 mock과 정합한지 검증
```

각 `.diff` 파일은 `.expected.json`을 가지며, 일부는 `.mock-response.json`(LLM 응답 시뮬레이션)도 가진다.

## Adversarial Fixtures의 역할

`adversarial/` 디렉터리는 실제 코드 위협이 아닌 **어댑터의 sanitize/통합 로직 자체를 검증**한다. LLM의 시스템 프롬프트 격리 규칙이 *코드 단에서 집행되지 않으면 prose에 불과*하다. Adversarial fixture는 다음과 같은 적대적 응답에 대해 어댑터가 verdict를 올바르게 유지하는지를 fixture로 못박는다:

- 결정론 finding을 LLM이 부정 시도 (A01)
- 비표준 severity/category 주입 (A02, A03)
- 거대 description 노이즈 (A04)
- 응답에 verdict 키 주입 (A05)
- 응답 형식 손상 — findings가 array 아님 (A06)
- type confusion — findings에 null/string/int (A07)
- 정보 없는 빈 dict finding (A08)

작성 당시 어댑터 sanitize가 발견한 결함(빈 dict silent 통과, 응답 손상 무가시화)이 fixture로 노출되어 어댑터를 강화하는 계기가 되었다. 이 fixture들이 미래 sanitize 로직 변경 시 회귀 안전망 역할을 한다.

## Test Runner 두 가지

### `run-tests.sh` — 결정론 계층만
빠른 회귀 검사. deterministic.sh의 변경에 대한 즉시 검증.

### `run-tests-integrated.sh` — 결정론 + LLM(mock) + 게이팅 시뮬레이션
production 동작에 가까운 검증. 다음 모드를 라벨로 표시:

| 라벨 | 의미 |
|---|---|
| `mock` | production에서 LLM 호출 + fixture에 mock 응답 존재 → 통합 로직 검증됨 |
| `gated` | LLM이 호출되지 않는 게 정책상 옳음 (markdown 전용 변경 등) |
| `no-mock` | production에서 LLM 호출되지만 mock 응답이 없어 LLM 출력 자체는 미검증 |
| `skip` | LLM 호출이 정책상 불필요 |

## 알려진 검증 공백

- **검증 공백 자체는 v0.6에서 0으로 해소** (`no-mock` 라벨 없음). 모든 fixture가
  production 동작 가정에 일치하게 mock으로 검증됨.
- **그러나 mock 자체의 본질적 한계는 남음**: mock 응답은 "이상적 LLM 출력"
  가정이지 보장이 아님. 모델 업데이트/시스템 프롬프트 변경으로 production 응답이
  mock과 어긋날 수 있다 → `verify-mock-vs-production.py`로 주기적 검증 필요.
- **특히 위험한 케이스**: M03 같은 prompt injection fixture. mock은 "LLM이 격리
  규칙을 잘 따랐다"고 가정하지만, 실제 LLM이 injection에 영향받는지는 production
  호출로만 검증 가능. mock이 통과해도 안심하지 말 것.

## Mock 회귀 검증 (`verify-mock-vs-production.py`)

production LLM이 실제로 mock과 일치하게 동작하는지 정기적으로 검증한다.
mock과 production이 어긋나기 시작하면 false positive/negative 누적의 신호.

```bash
export ANTHROPIC_API_KEY=...
./verify-mock-vs-production.py              # 전체 (실제 API 호출, 비용 발생)
./verify-mock-vs-production.py B04          # 특정 fixture만
```

권장 주기: 정책 repo의 주간 스케줄 Actions로 실행, 어긋남 발견 시 Slack 알림.
어긋남이 발견되면 두 경로 중 하나:
1. **mock이 outdated**: 실제 LLM 동작이 새 표준이라면 mock 갱신 + rationale 업데이트
2. **LLM이 회귀**: 모델 변경이 false positive 유발 → 시스템 프롬프트 보강 또는 모델 버전 고정 검토

## Mock 작성 규칙

각 mock-response.json은 다음 4개 키를 가져야 한다 (LLM에는 무시되지만 운영자에게 의미):

- `_rationale`: 왜 LLM이 이 응답을 내야 하는지의 *근거* (코드 분석 또는 정책)
- `_assumed_llm_behavior`: LLM이 무엇을 인식해서 이 응답을 만드는가
- `_failure_mode_if_violated`: 실제 LLM이 mock과 어긋나면 어떤 운영 영향이 있는가
- `findings`: 실제 LLM 응답 형식의 findings 배열

이 4개 메타 필드의 존재는 **mock을 의심 없이 수정하는 것을 막는 사회적 장치**다. mock을 바꾸려는 사람이 _rationale을 함께 갱신해야 하므로, 사고 정지 변경이 어려워진다.

## 현재 결정론 계층 성능 (17/17, llm-only 제외)

### 정책 변경 이력

#### v0.2: Markdown 파일 예외 정책 (2026-05)
- **변경**: `.md` / `.markdown` 파일은 패턴 / base64 / injection 검사에서 제외
- **이유**: 보안 문서가 위협 패턴을 *논의*하는 자연어 본문 때문에 critical로
  잡혀 운영 불가능했음 (구 B05 케이스)
- **근거**: markdown은 실행되지 않고, LLM 영향은 시스템 프롬프트 격리로 별도 방어
- **받아들인 tradeoff**: markdown 안의 fenced code block에 악성 코드가 있어도
  잡지 않음 (B06으로 문서화). 빌드 시스템이 markdown을 실행 코드로 변환하는
  경우는 별도 검토 필요.
- **회귀 방지**: M09, M10이 markdown 예외가 코드 파일까지 새어나가지 않음을 검증.

#### v0.3: LLM 어댑터 도입 + 게이팅 강화 (2026-05)
- **추가**: `checks/llm-adapter.py` (CI용 API 호출 + 결과 통합)
- **게이팅 변경**: 기존 "결정론 0 + diff 작음 → skip" 규칙은 M06 같은
  exfiltration을 놓침. 새 규칙은 **변경 카테고리 기반** —
  코드 파일 변경이 있으면 항상 LLM 호출.
- **회귀 방지**: M06이 통합 runner에서 advisory로 잡힘을 확인.

#### v0.4: Mock 기반 false positive 검증 (2026-05)
- **추가**: B01, B02, B04, E01, E02 — 코드 변경 fixture에 mock 응답.
  특히 B04(정상 리팩터)에 LLM이 false positive를 만들지 않는지 검증.
- **추가**: `verify-mock-vs-production.py` — production LLM 응답이 mock과
  어긋나는지 주기 검증.
- **추가**: mock 작성 규칙 — `_rationale` / `_assumed_llm_behavior` /
  `_failure_mode_if_violated` 메타 필드 의무화.
- **검증**: B02에서 LLM mock이 low metadata finding을 추가해도 verdict가
  결정론 critical 때문에 block 그대로 유지 → 격리 원칙 3이 작동함을 확인.

#### v0.5: Adversarial fixture + sanitize 강화 (2026-05)
- **추가**: `examples/adversarial/` 디렉터리 (8개 fixture) — 어댑터의 sanitize/
  통합 로직 자체를 검증.
- **fixture가 발견한 결함**:
  - 빈 dict / 필드 누락 dict이 silent하게 통과되어 출력 오염 (A07/A08)
  - findings가 list 아닐 때 응답 손상이 가시화되지 않음 (A06)
- **어댑터 강화** (`llm-adapter.py:sanitize_llm_findings`):
  - description 누락/빈 finding 자동 무시
  - 무시된 항목 수를 meta finding으로 가시화 (LLM 응답 품질 신호)
  - findings가 list가 아닐 때 medium severity meta finding 추가
    (응답 형식 손상은 prompt injection 시도일 수 있음 → advisory verdict)
- **회귀 방지**: 8개 adversarial fixture로 모든 강화가 못박힘.

#### v0.6: M 시리즈 mock 완성 + 검증 공백 해소 (2026-05)
- **추가**: M01–M10 mock 응답 9개. `no-mock` 라벨이 14 → 0이 되어 검증 공백 완전 해소.
- **발견**: M05 (lockfile typosquat) verdict가 결정론 단독 advisory → LLM 통합 block으로
  자동 강화됨. LLM 계층이 결정론으로는 닿지 않는 카테고리(개별 패키지명의 typosquat
  식별)에서 결정적 가치 추가.
- **계층별 책임 분담의 fixture 검증** — 아래 "계층별 책임 분담" 섹션 참조.

### 결정론 계층이 강한 영역
- workflow 파일 변조 (M01, M07)
- npm lifecycle script 신규 추가 (M02)
- 명시적 pipe-to-shell `curl|sh`, `wget|bash` (M01, M02, M10)
- `eval(atob(...))` 난독화 (M04)
- prompt injection 메타 키워드 — 코드 파일에서 (M03, M09, M10)
- 대량 lockfile 변경 (M05)
- 신규 binary 파일 (M08)

### 결정론 단독으로는 불가능, LLM 계층 의존
- **평범한 HTTP 기반 exfiltration** (M06): `process.env` + `https.request` 패턴.
  v0.3의 게이팅 규칙과 mock 검증으로 production에서 잡힐 것으로 기대 — 단,
  실제 LLM 호출이 mock과 유사한 응답을 내는지는 production 누적 데이터로 확인 필요.
- **typosquat 패키지 식별** (M05): 결정론은 "대량 lockfile 변경"까지만.
  개별 패키지명 의심 판단은 LLM 또는 socket.dev 같은 외부 도구.
- **SHA pin 해제 의미** (M07): workflow 변경으로 잡히지만, 변경 *내용*의 의미는 LLM 영역.
- **Markdown fenced block 악성 코드** (B06): 정책상 의도적 미검출.

### 계층별 책임 분담 (fixture로 검증된 역할)

| 위협 유형 | 결정론이 담당 | LLM이 담당 |
|---|---|---|
| 파일 경로 기반 (workflow, lockfile, binary) | ✓ | 변경의 *의미* 해석 |
| 명시적 위험 패턴 (curl\|sh, eval(atob)) | ✓ | 정황 보강 (도메인 평판, 컨텍스트) |
| Prompt injection 키워드 | ✓ | 격리 규칙 준수 + 실제 RCE 메커니즘 식별 |
| 변경 카테고리 (대량 lockfile 변경) | ✓ | 개별 항목의 의심도 (typosquat 식별) |
| HTTP exfiltration | (잡지 못함) | ✓ 단독 책임 |
| 동적 코드 실행 (exec, dynamic shell) | 일부 | ✓ 실제 위험 경로 식별 |
| 변경의 *의도/의미* 해석 | (불가) | ✓ 단독 책임 |
| 응답 형식 손상 감지 | (해당 없음) | 어댑터 sanitize가 담당 |

이 표는 fixture로 검증된 *실제* 분담이지 이론적 주장이 아니다. 각 항목에
대응하는 fixture가 corpus 안에 존재한다. 누군가 결정론 또는 LLM 계층을
약화시켜 책임 경계를 이동시키면 대응 fixture가 FAIL로 알린다.

### 남아있는 정책 긴장
- **B02 (workflow comment only)**: 주석 한 줄 추가도 critical로 잡혀 block.
  의도된 마찰이지만 우회 압력 유발 가능. LLM이 "comment-only 변경"을
  탐지해 advisory로 강등할지 검토 필요.
- **E02 (base64 data URI)**: legitimate PNG 데이터 URI가 medium으로 잡힘.
  frontend repo에서 빈도 높으면 noise. `data:image/*;base64,` 화이트리스트 가능.

## 다음 우선순위

1. **실제 API 호출 통합 테스트** — `verify-mock-vs-production.py`를 production
   환경에서 1회 실행하고 결과를 baseline으로 보관. 특히 M03/M09(prompt injection
   포함 fixture)와 adversarial fixture에 대한 실제 LLM 응답이 mock과 정합한지 확인.
2. **Actions 워크플로우 완성** — self-test.yml의 TODO 채우기, 실제 CI 연결,
   Slack 알림 페이로드 설계, 정책 repo 자신의 CI에서 매 PR마다 전체 fixture suite 실행.
3. **fixture 확장**: logic bomb (날짜/호스트명 조건), 권한 escalation (sudo/setuid),
   미묘한 obfuscation (charcode 산술, 문자열 reverse), 대규모 정상 리팩터.
4. **`/git-pull` 커맨드의 실제 동작 검증** — 로컬 Claude Code 세션에서 mock 없이도
   skill을 따라 실행할 수 있는지, 사용자 승인 플로우가 자연스러운지 통합 시나리오 테스트.
5. **mock의 주기적 신선도 검증** — production LLM 응답을 누적하여 mock과 drift가
   N% 이상 발생하면 자동 갱신 제안 (반자동 PR).

## 변경 시 주의

- fixture 추가/수정은 정책 변경에 준하는 PR 절차를 거친다 (CODEOWNERS 승인)
- expected.json 변경은 *왜* 기대치를 바꾸는지 rationale 갱신 필수
- 기존 PASS fixture가 FAIL이 되면 회귀. checker를 고치거나, 정책의 의도된
  변화임을 문서화 후 expected 업데이트.
- 새 예외/허용 정책 도입 시 반드시 *반대 방향*에서 회귀 방지 fixture를
  함께 추가한다 (markdown 예외 도입 시 M09, M10이 그 역할을 한 것처럼).
- mock 응답은 LLM 동작에 대한 *가정*이지 보장이 아니다. production 호출 누적으로
  주기적으로 mock의 현실성을 재검증해야 한다.

---

## v0.7 운영 계층 추가 (정책 repo의 fixture/skill에는 영향 없음)

v0.7은 *운영 계층*만 추가하여 fixture corpus 자체는 변경하지 않았다:
- `checks/ci-caller.py` — workflow YAML과 adapter 사이의 통합 진입점
- `.github/workflows/pre-merge-review.yml.template` — 완성된 대상 repo용 템플릿
- `.github/workflows/policy-self-test.yml` — 정책 repo 자신의 CI
- `scripts/verify-workflow-pins.sh` — SHA pin 검증
- `scripts/pin-actions.sh` — FIXME 자동 채우기 보조
- `docs/INSTALL.md` — 운영자 가이드

corpus 검증 결과는 v0.6과 동일 (17/17, 26/26).

---

## v0.8 Fixture 카테고리 확장 (2026-05)

다음 카테고리 fixture 8개 추가:

| Fixture | 카테고리 | 결정론 가능 | LLM 의존 |
|---|---|---|---|
| M11 | 날짜 logic bomb (credential exfil 결합) | ✗ pass | ✓ block |
| M12 | hostname logic bomb (setuid 결합) | ✗ pass | ✓ block |
| M13 | sudo NOPASSWD 영구화 | ✗ pass | ✓ block |
| M14 | container capabilities (CAP_SYS_ADMIN 등) | ✗ pass | ✓ block |
| **M15** | **charcode 산술 obfuscation** | ✗ pass | ✓ block |
| **M16** | **문자열 reverse obfuscation** | ✗ pass | ✓ block |
| B07 | 대규모 정상 리팩터 (80+ 라인) | n/a | false positive 검증 |
| E03 | feature flag (M11과 표면 유사한 정상) | n/a | M11과 구분 능력 검증 |

### 핵심 발견 — 결정론 한계의 정직한 노출

M15/M16 작성 중 발견: 첫 버전은 페이로드(`curl|sh`, `wget|bash`)가 eval 인자로
직접 등장하여 결정론 정규식에 잡혔다. 이는 fixture 의도("LLM 단독 책임 영역") 위반.
페이로드까지 charcode/reverse로 숨기는 버전으로 재작성하여 결정론이 *원리적으로*
잡을 수 없는 영역을 정직하게 표현.

이런 정직성 없이는 LLM 계층의 존재 이유가 흐려진다. fixture가 "잘 잡혔다"는
표면 결과에 만족하지 말고, 잡은 *경로*가 정책 의도와 일치하는지 확인하는 게
중요하다.

### 검증 결과

- **결정론 단독 25/25 통과**: M11~M16이 모두 `pass`로 떨어져 LLM 의존 명문화
- **통합 34/34 통과**: LLM mock이 모든 신규 위협 카테고리 식별
- **M11 vs E03 대조**: 둘 다 날짜/환경 조건문이지만 LLM이 logic bomb과 feature
  flag를 구분 (위험 작업 결합 여부로 판단)
- **B07 대규모 리팩터**: 80+ 라인 변경에도 LLM이 변경량에 압도되지 않고 false positive 없음

---

## v0.9 로컬 경로 end-to-end 검증 (2026-05)

실제 git repo(upstream/local clone)로 `/git-pull` 커맨드 단계를 재현하여
로컬 경로를 처음으로 end-to-end 검증. 두 가지 실제 결함 발견 및 수정:

### 결함 1: injection 원문이 finding에 그대로 유입 (수정됨)

결정론의 injection finding이 감지된 원문(`IGNORE PREVIOUS analysis...`,
`SYSTEM PROMPT OVERRIDE...`)을 description에 **그대로** 실었다. 이 finding이
LLM adapter의 user 메시지나 로컬 Claude 컨텍스트에 들어가면 **2차 injection
벡터**가 된다.

수정: `deterministic.sh`가 (1) 매치된 키워드 이름, (2) 라인 번호만 보고하고
원문은 싣지 않음. 운영자는 diff를 직접 확인. M03 fixture로 무력화 검증.

### 결함 2: 셸 변수 캡처 시 multi-byte 손상 (방어 추가)

adapter 출력을 `result=$(...)` 로 캡처 후 `echo | jq` 하면 일부 셸 환경에서
한글(multi-byte) 문자가 손상되어 JSON parse 실패. **단, 실제 운영 경로 두 가지
(파일 경유, ci-caller.py의 subprocess)는 모두 영향 없음** — 손상은 수동 셸
캡처에서만 발생.

방어: adapter에 `--ascii` 옵션 추가 (출력을 ASCII-safe 인코딩). 로컬에서 셸
파이프로 다룰 때 권장. `/git-pull` 커맨드 문서에 파일 경유 처리 원칙 명시.

### 검증된 로컬 경로 동작

실제 git 명령으로 확인:
- `git fetch` 후 working tree에 악성 변경이 적용되지 않음 (merge 전 안전)
- block verdict 시 `git merge` 실행되지 않아 악성 package.json이 working tree에
  나타나지 않음
- ci-caller.py 운영 경로: verdict=block, exit=1, injection 원문 유출 없음

corpus 검증: 결정론 25/25, 통합 34/34 (회귀 없음).
