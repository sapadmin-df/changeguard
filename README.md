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
│   ├── pre-merge-review.yml.template    대상 repo가 복사할 워크플로우 (FIXME 포함)
│   └── policy-self-test.yml             changeguard 자신의 CI (fixture 회귀)
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

## 버전

`VERSION` 파일 참조. 로컬/CI 양측 출력에 사용된 정책 SHA가 함께 기록되어야
"로컬에선 통과했는데 CI에서 막힘" 디버깅이 가능합니다.
