# changeguard

코드 변경(change)을 검문하는 **보안 게이트들의 정책 본부**.

현재 제공: **pre-merge 게이트** — upstream을 merge하기 전에 로컬(Claude Code)과
CI(GitHub Actions)가 *동일한 정책*으로 incoming 변경을 검증합니다.
설계상 여러 게이트를 담는 우산이며, pre-merge는 그 첫 번째입니다.

> 이름은 지향을 담되 실체는 과장하지 않습니다. 지금 동작하는 것은 pre-merge
> 게이트 하나이고, pre-commit·release·dependency 등은 같은 엔진 위에 점진적으로
> 추가될 예정입니다. 아래 "확장 로드맵" 참조.

## 사상

- **단일 정책 출처(Single Source of Truth)**: 검사 로직·기준이 한 곳에만 존재.
  로컬과 CI가 같은 파일을 참조하므로 "로컬은 통과인데 CI는 막힘"의 기준 불일치가 없음.
- **별도 repo로 격리**: 정책이 검사 대상 repo 안에 있으면 악성 PR이 정책 자체를
  약화시킬 수 있음. 분리하면 정책 변경은 이 repo의 PR + CODEOWNERS 승인을 거침.
- **read는 공개, write는 통제**: 정책 내용은 비밀이 아니므로 public.
  진짜 보호 대상은 "누가 정책을 *바꾸느냐*"이며, 그것만 엄격히 통제.
- **결정론 + LLM 2계층**: 빠른 패턴 검사(결정론)가 1차 게이트, 의미·의도 판단이
  필요한 위협은 LLM이 담당. 각 계층의 책임은 fixture로 검증됨.
- **falsifiability**: 정책의 효력을 *주장*하지 않고 fixture corpus로 *검증*.
  정책을 약화시키는 변경은 대응 fixture가 FAIL로 알림.

## 왜 별도 repo인가

검사 대상 repo(예: 애플리케이션)는 이 repo를 **commit SHA로 고정 참조**만 합니다.

- 정책 변경은 changeguard의 PR + CODEOWNERS 승인을 거침
- 대상 repo는 read-only로 참조 (SHA 고정 — mutable tag/branch 금지)
- 정책 repo가 손상돼도 SHA가 고정이라 대상 repo는 영향받지 않음

## 구조

```
.
├── VERSION                              SemVer (출력에 SHA와 함께 기록)
├── CODEOWNERS                           정책 변경은 maintainer 승인 필수
├── docs/
│   └── INSTALL.md                       대상 repo 운영자용 설치 가이드
├── scripts/
│   ├── verify-workflow-pins.sh          모든 uses:가 40자 SHA인지 검증
│   └── pin-actions.sh                   FIXME 플레이스홀더 자동 채우기 보조
├── .github/workflows/
│   ├── pre-merge-review.yml.template     대상 repo의 게이트 워크플로우 (FIXME 포함)
│   ├── policy-bump-watcher.yml.template  대상 repo의 SHA 갱신 자동화 (v0.12+)
│   └── policy-self-test.yml              changeguard 자신의 CI (fixture 회귀)
└── skills/
    └── pre-merge-review/                게이트 #1 — merge 전 검증
        ├── SKILL.md                     정책 본문 (5단계 검사 절차)
        ├── checks/
        │   ├── deterministic.sh         1차 grep/diff 검사
        │   ├── llm-system-prompt.md     LLM 분석용 격리 규칙
        │   ├── llm-adapter.py           LLM 호출 + sanitize + 통합
        │   └── ci-caller.py             CI 워크플로우 진입점 (gating + Slack)
        ├── output-schema.json           로컬/CI 공용 JSON 계약
        └── examples/                    Fixture corpus (34개) + test runner
```

`checks/`의 엔진(`deterministic.sh`, `llm-adapter.py`, `ci-caller.py`)은 게이트에
종속되지 않은 공용 부품으로, 향후 게이트가 재사용합니다.

## 확장 로드맵

현재 `skills/pre-merge-review/`는 여러 게이트 중 첫 번째입니다. 같은 엔진 위에
추가될 수 있는 게이트:

| 게이트 | 검문 시점 | 상태 |
|---|---|---|
| `pre-merge-review` | upstream merge 전 | **제공 중** |
| `pre-commit-scan` | 로컬 commit 전 (git hook) | 예정 |
| `release-gate` | 배포 전 release 브랜치 검증 | 예정 |
| `dependency-watch` | lockfile 주기적 스캔 | 예정 |
| `secret-scan` | 자격증명 유출 탐지 | 예정 |

각 게이트는 `skills/<gate-name>/` 모듈로 추가되며, `checks/`의 공용 엔진과
`output-schema.json`의 출력 계약을 공유합니다.

## 사용처 (pre-merge-review 게이트)

### 로컬 (Claude Code)
`/git-pull` 커스텀 커맨드가 이 repo를 fetch하여 `SKILL.md`를 컨텍스트로 로드한 뒤
diff에 대해 검사 절차를 수행합니다. fetch와 merge를 분리하여, 검사 시점에는
working tree가 오염되지 않습니다.

### CI (GitHub Actions)
대상 repo의 워크플로우가 이 repo를 **commit SHA로 고정 참조**하여 동일한 검사를
수행합니다.

설치는 `docs/INSTALL.md` 참조.

## 정책 변경 절차

1. 본 repo에 PR 생성
2. CI(`policy-self-test.yml`)가 자동 실행:
   - `verify-workflow-pins.sh` — workflow 자신의 SHA pinning 검증
   - `run-tests.sh` — 결정론 fixture 회귀 (25개)
   - `run-tests-integrated.sh` — 통합 fixture 회귀 (34개)
   - (schedule trigger 시) `verify-mock-vs-production.py` — mock drift 검증
3. CODEOWNERS 승인
4. `VERSION` 파일 갱신 (SemVer)
5. 대상 repo의 Actions workflow는 새 SHA를 명시적으로 업데이트해야 적용됨
   (자동 floating 금지)

위 절차는 구두 약속이 아니라 아래 "브랜치 보호 & 커밋 서명"으로 GitHub에서
강제된다.

## 브랜치 보호 & 커밋 서명

`main` 브랜치는 ruleset `protect-main`으로 보호된다. "정책 변경 절차"가
기술적으로 강제되는 지점이다.

| 규칙 | 효과 |
|---|---|
| `required_signatures` | 검증된 서명이 없는 commit은 거부 |
| `pull_request` | 직접 push 불가 — PR + 승인 1개 + Code Owner 승인 필수 |
| `required_status_checks` | `policy-self-test`의 `workflow-pins`·`fixture-tests`가 통과해야 merge 가능 (브랜치 최신화 강제) |
| `non_fast_forward` / `deletion` | force-push와 `main` 삭제 차단 |

bypass는 저장소 Admin에 한해 `pull_request` 모드로만 허용된다 — 직접 push는
Admin도 막히고, 솔로 maintainer가 본인 PR을 self-merge할 때만 우회가 적용된다.

### 커밋 서명 설정 (기여자 필수)

`required_signatures` 때문에 서명되지 않은 commit은 PR 단계에서 거부된다.
SSH 서명 기준 설정:

```bash
git config gpg.format ssh
git config user.signingkey ~/.ssh/<your-key>.pub
git config commit.gpgsign true
```

그리고 공개키를 GitHub 계정에 **Signing Key** 타입으로 등록한다 (Settings →
SSH and GPG keys — Authentication Key와는 별도 항목). GitHub의 SSH 공개키는
계정당 유일하므로, 여러 계정으로 기여한다면 계정마다 별도의 키가 필요하다.

## 정책 버전 floor (deprecation)

`min-supported.txt`에 명시된 SHA보다 오래된 정책 SHA에 pin된 소비자는 다음 워크플로우
실행에서 자동 DEPRECATED 알림 + 워크플로우 실패 처리된다.

동작 (`ci-caller.py` v0.10+):

1. ci-caller.py가 시작 시 changeguard upstream main의 `min-supported.txt`를 fetch
2. 소비자 `POLICY_REPO_SHA` vs upstream floor를 GitHub compare API로 비교
3. behind/diverged → Slack DEPRECATED 알림 + `exit 1`
4. ahead/identical → 정상 진행

특징:

- **정책 *코드*는 여전히 SHA-pinned** (mutable upstream 안 함, RCE 차단) — floor만 동적
- 위험 표면: 갱신 강제(DoS) 한정, 코드 실행 변경(RCE) 없음
- 네트워크 실패시 fail-open (소비자 게이트가 멈추지 않도록)
- 활성 조건: 소비자가 `POLICY_REPO_SHA`를 ci-caller.py v0.10 이상으로 한 번 bump해야 함
  (이전 SHA는 이 코드가 없어 검사를 수행하지 않음 — bot-watcher가 그 갱신을 자동화)

DEPRECATED 알림은 소비 repo의 `policy-bump-watcher`(스케줄 워크플로우)가 이미 생성해
둔 갱신 PR이 있으면 그 PR 링크를 본문에 첨부해 즉시 해소 가능하게 안내한다.

## 알림 메시지의 스마트 링크

Slack 알림과 PR 코멘트 모두 다음 식별자를 클릭 가능한 GitHub 링크로 자동 변환:

| 표시 | 링크 대상 |
|---|---|
| repo | `https://github.com/{owner}/{name}` |
| commit SHA | `.../commit/{sha}` |
| 파일 location (`path:42-50`) | `.../blob/{sha}/path#L42-L50` |
| policy version | `.../blob/{policy_sha}/VERSION` |
| policy SHA | changeguard의 `.../commit/{policy_sha}` |
| workflow run | Actions 실행 페이지 |
| bump PR | 해당 PR 페이지 |

Slack은 `<url|text>` (mrkdwn), PR 코멘트는 `[text](url)` (markdown). 사용자가 SHA·파일
경로를 손으로 검색하지 않도록 *통합 관리*되는 메시지.

## v0.15 — Finding 메시지 hook 강제 (양치기 회피의 마지막 장치)

v0.14까지의 알림은 결정론 finding에서 종종 "diff를 직접 확인할 것" 같은
hand-wave 안내를 description에 그대로 실었다. 우리가 강조해온 *즉시 클릭
가능한 링크* 원칙의 정면 위반.

### 원인 — `deterministic.sh`의 location 형식

injection·pattern 검사가 grep -n 의 *diff 텍스트 안의 line number*를
"diff:line260" 같은 형식으로 location에 넣었음. 이건 ci-caller의
`_link_file()` 파서가 GitHub blob URL을 만들 수 없는 형식.

### 해결

1. **awk로 diff hunk header 파싱** — 추가된 라인마다 `(file_path,
   line_in_new_file, content)`를 추출하는 `scan_added_lines` helper.
   injection/pattern 검사 모두 이 helper로 통일.
2. **location 형식 = `<file>:<line>`** — `_link_file()`이 자동으로
   `https://github.com/{owner}/{repo}/blob/{sha}/{file}#L{line}` 생성.
3. **description에서 hand-wave 제거** — "diff를 직접 확인할 것" 같은
   문구를 "해당 위치 클릭 시 GitHub에서 원문 확인 가능"으로 대체.

### Hook 강제 — `lint_finding_messages()` 자체 검증

ci-caller가 알림 발송 직전에 모든 finding을 self-lint. hand-wave 패턴이
location에 link 가능 정보 없이 있으면 → **`source: "self-lint"`** 의
meta finding을 추가해 운영자에게 보고. 같은 안티패턴이 다시 등장하면
즉시 가시.

```python
HAND_WAVE_PHRASES = (
    "직접 확인", "수동으로 확인", "diff를 직접", "diff 라인",
    "원문은 보안상", "포함하지 않음 — diff", ...
)
```

이건 changeguard 자체의 메시지 품질을 *정책으로 강제*한다 — 일이 일을
만드는 안티패턴이 코드에 다시 들어오면 self-report로 즉시 잡힘.

## v0.14 — 자동 검증 + 인간친화 알림 (일이 일을 만들지 않게)

v0.13에서 LLM이 narrative를 작성하기 시작했지만, 그 narrative가 종종 "X를 확인하세요"
같은 *사람에게 떠넘기는* 항목으로 끝났다. public repo의 SHA bump는 코드가
직접 확인할 수 있는 사실이 대부분이다 — 그런 것을 사람에게 다시 묻는 건
"일이 일을 만드는" 안티패턴.

### `policy-bump-verify.py` 신규 — 자동 검증 4종

`POLICY_REPO_SHA` 변경을 diff에서 감지하면 public GitHub API로 *사람이 직접
확인하지 않아도 되는* 사실을 모두 검증:

| 항목 | API | 결과 |
|---|---|---|
| **exists** | `GET /repos/{repo}/commits/{sha}` | 200 vs 404/422 |
| **verified** | 위 응답의 `.commit.verification.verified` | true/false + reason |
| **reachable_from_main** | `GET /repos/{repo}/compare/main...{sha}` | `identical`/`behind`/`ahead`/`diverged` |
| **author / committer** | commits API 응답 | login, email domain |

종합 `overall`:
- 🟢 `trusted` — 세 가지 모두 ✓
- 🟡 `unverified` — 서명만 누락 (main 도달 가능)
- 🔴 `suspicious` — exists ✗ 또는 main 도달 불가
- ⚪ `unknown` — 네트워크 실패 (fail-open)

GITHUB_TOKEN (Actions 기본 제공) + public repo로 충분 — 추가 secret 불필요.

### LLM에게 자동 검증 결과를 *전제*로 제공

`llm-adapter.py`의 user message에 verification 결과가 별도 섹션으로 들어가서,
LLM은 "확인하라" 대신 "확인됨" / "확인 못 됨"으로 narrative를 작성한다.
system prompt의 정직성 원칙에 명시:

> "자동 검증된 사실을 다시 묻지 마라. user message의 자동 검증 결과 섹션에서
> trusted 상태로 끝난 항목은 reviewer_focus에 절대 포함하지 마라."

### Slack / PR comment 4-섹션 구조

알림의 정보 밀도와 가시성을 분리했다:

```
TL;DR        verdict + intent badge + policy 링크
자동 검증    POLICY_REPO_SHA 변경 시: ✓/✗ 한 줄 사실 + 종합 배지 + compare URL
LLM 분석     intent별 한 단락 narrative (반복 없음)
사람 액션    AC 항목 체크박스 (코드가 못 확인하는 것만; 보통 0-2개)
Findings    접을 수 있는 details (요청 시만 확인)
```

### 결과 — 양치기 소년 해소의 마지막 조각

v0.13까지는 "확인 사항 4개" 같은 padding이 매번 발화되어 무시 위험.
v0.14부터는 코드가 검증을 끝낸 항목이 ✓로 표시되고, 사람 액션은 코드가 *못
확인하는* 것만 남음 — 보통 0-2개. **하면 할수록 좋은 보안 가드가 피로감으로
오히려 뚫리는 안티패턴을 회피**.

## v0.13 — LLM 분석 narrative (양치기 소년 문제 해소)

`v0.12`까지의 알림은 결정론 verdict의 boilerplate("Critical 발견사항 존재 — merge
금지.")만 반복했다. workflow 파일 변경 같은 결정론 critical은 거의 모든 PR에서
발화하므로, 사용자가 매번 같은 메시지를 받으면 *진짜 위협일 때도* 무시하게 된다
— 양치기 소년 문제.

v0.13의 해결:

- **`assessment` 객체** — `output-schema.json`에 optional top-level 필드로 추가
  - `intent`: `intentional` / `suspicious` / `unclear` — LLM의 의도 판단
  - `rationale`: 1-3 문장의 한국어 narrative — *이 변경*의 고유 맥락
  - `reviewer_focus`: 사람이 특히 확인할 점 (0-5개)
- **`llm-system-prompt.md`**에 정직성 원칙 명시: 결정론을 앵무새처럼 반복 금지,
  각 변경의 고유 맥락을 담아라, 의도가 명백하면 명백하다고 단언, 의심스러우면
  구체적으로.
- **`llm-adapter.py`**가 LLM 응답에서 assessment를 파싱·sanitize. 형식 손상 시
  fail-open (assessment 없이 v0.12 동작).
- **Slack 메시지**: verdict 라인 옆에 intent 배지(✓/⚠/?), 본문에 LLM narrative
  섹션과 reviewer_focus.
- **PR 코멘트**: 동일 intent 배지 + "LLM 분석" 별도 마크다운 섹션.

### 핵심 보안 모델 — 변경 *없음*

LLM은 여전히 **결정론 finding을 강등/제거 못한다**. `assessment`는 verdict를
바꾸지 않는다. block은 block. 사용자가 받는 정보가 풍부해질 뿐 권위 구조는
그대로다.

### 정직성 채널

intent의 세 값은 다음을 의미한다:

- `intentional` — diff가 일관된 의도(feature/refactor/정책 갱신)를 보이고
  결정론 findings가 그 의도의 부수효과. supply chain 위협 단서 없음.
- `suspicious` — findings가 단순 부수효과가 아니라 그 자체가 의도로 보임,
  또는 결정론이 못 잡은 추가 단서 발견.
- `unclear` — 판단 근거 부족 — 모호하면 정직하게 unclear로 두고 reviewer_focus에
  사람이 확인할 점을 구체화.

### 호환성

- `assessment`는 schema에서 optional — v0.12 이하 소비자 영향 없음.
- LLM이 형식 손상된 assessment를 반환하면 `sanitize_assessment()`가 None으로
  떨어뜨려 v0.12 동작으로 자동 fallback.

## v0.12 — 갱신 자동화 template

소비 repo의 `POLICY_REPO_SHA` 갱신을 사람이 직접 추적하지 않도록 `policy-bump-watcher.yml.template`을 추가했다. 매주 changeguard upstream main을 폴링해 새 SHA가 있으면:

1. **Pre-flight 검증** — 새 SHA의 fixture corpus(결정론+통합)를 미리 돌려 회귀 사전 확인
2. **갱신 PR 자동 생성** — body에 commit 목록 + **GitHub compare URL** (Bitbucket의 commit-to-commit diff 시뮬레이션과 동등) + pre-flight 결과
3. **gate 즉시 디스패치** — bump 브랜치에서 pre-merge-review를 `workflow_dispatch`로 트리거 (GITHUB_TOKEN-created PR이 자동 트리거 안 되는 제약 회피)
4. **Slack 통합 알림** — bump PR URL · compare URL · pre-flight 배지 · commit 수를 한 메시지에

자동 *merge*는 하지 않는다. bump PR도 사람 리뷰·승인을 거친다 (changeguard 정책 정면 위반 방지).

대상 repo의 default branch를 `gh api repos/$GITHUB_REPOSITORY --jq .default_branch`로 자동 감지하므로 `main`/`master` 모두 호환.

## v0.11 추가 통합

소비자 액션과 게이트 출력 사이의 마찰을 더 줄이기 위한 통합 4종:

### 1. SHA-only swap 강등 (bump PR 자기-루프 해소)

`.github/workflows/*.yml` 변경의 모든 차분(-/+ 라인)이 **40-hex commit SHA 값
교체만**으로 이뤄지면, 결정론 워크플로우 finding의 severity를 critical → low로
강등한다. 다른 패턴의 워크플로우 변경은 영향 없음.

이는 `policy-bump-watcher`가 만드는 PR이 매번 block 받던 자기-루프를 해소.
LLM은 여전히 호출되어 새 SHA 대상의 의심점을 별도로 검토할 수 있다.
강등된 finding의 `description`에 `[SHA-only swap downgrade: critical→low]`
prefix가 붙어 추적 가능.

### 2. POLICY_UPDATED informational 알림

master push 이벤트의 diff에 `POLICY_REPO_SHA` 값 변경이 포함되면 verdict와
별개로 `🔄 POLICY UPDATED` Slack 메시지를 informational(blue)로 발송. 본문에
old → new SHA의 **GitHub compare URL**(`/compare/old...new`)을 포함해서
"무엇이 적용됐는가"의 diff를 즉시 클릭 가능. (Bitbucket의 commit-to-commit
diff 시뮬레이션과 동등.)

### 3. PR diff inline annotations

finding의 `location` 필드(`path:42-50` 형식 포함)를 GitHub Actions의 annotation
시스템으로 등록 → PR "Files changed" 탭에서 해당 라인 옆에 직접 표시.
- critical/high → 빨간 ❌ 아이콘 (`core.error`)
- medium → 노란 ⚠️ (`core.warning`)
- low → 파란 ℹ️ (`core.notice`)

### 4. PR body 상단 요약 prepend

block/advisory 시 PR 본문의 최상단에 `<!-- pre-merge-summary-start -->`
delimiter로 감싼 요약 블록을 idempotent하게 prepend:

```
> 🚨 [Pre-Merge block] · 2 critical, 1 high · policy 0.11.0 (abc123) · workflow run
```

PR 목록에서도 미리보기로 가시. 재실행 시 delimiter로 식별해 교체 (중복 누적 방지).
이 기능은 `permissions: pull-requests: write` 필수 (코멘트와 동일 권한).

## 버전

`VERSION` 파일 참조. 로컬/CI 양측 출력에 사용된 정책 SHA가 함께 기록되어야
"로컬에선 통과했는데 CI에서 막힘" 디버깅이 가능합니다.
