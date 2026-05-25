# Quickstart — oh-my-bas (또는 다른 소비 repo)에 changeguard 적용

## 권한 구성 (이 케이스)

| 역할 | 계정/repo | 권한 |
|---|---|---|
| 정책 본부 | `sapadmin-df/changeguard` (public) | sapadmin-df: write / 그 외: read |
| 정책 검토자 | `wschoe2020` | changeguard read (PR 제안 가능, merge 불가) |
| 대상 repo | `dfocus-sapsol/oh-my-bas` | wschoe2020: write |

핵심 격리: **oh-my-bas를 수정할 수 있는 계정(wschoe2020)이 changeguard 정책은
바꿀 수 없다.** 정책 변경에는 sapadmin-df의 승인이 필요. 검사 도구와 검사 대상의
권한이 분리되어, oh-my-bas의 악성 PR이 정책 자체를 무력화할 수 없다.

changeguard가 public이므로 oh-my-bas의 Action은 토큰 없이 정책을 내려받는다.

## Step 0 — changeguard repo 준비 (한 번만, sapadmin-df 계정)

이미 `sapadmin-df/changeguard` 가 존재하고 `protect-main` ruleset이 설정되어
있다면 건너뛰기. 처음이면 changeguard `README.md` 의 "브랜치 보호 & 커밋 서명"
참조.

- **Settings → Rules → Rulesets**: `main` 보호 ruleset `protect-main` — 서명 필수,
  PR + Code Owner 승인, CI status check, force-push/삭제 차단. 규칙 상세는
  changeguard `README.md`의 "브랜치 보호 & 커밋 서명" 참조

## Step 1 — oh-my-bas에 자동 온보딩 (wschoe2020 계정)

```bash
git clone git@github.com:dfocus-sapsol/oh-my-bas.git
cd oh-my-bas

# 한 명령으로 끝남 — default branch(master) 자동 감지 + 두 워크플로우 생성 + PR 생성
bash <(curl -fsSL https://raw.githubusercontent.com/sapadmin-df/changeguard/main/scripts/bootstrap-consumer.sh)
```

(사전에 dry-run으로 검토:)

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/sapadmin-df/changeguard/main/scripts/bootstrap-consumer.sh) --dry-run
```

생성되는 파일 2개 (각 5-10줄):

- `.github/workflows/pre-merge-review.yml` — 게이트
- `.github/workflows/policy-bump-watcher.yml` — 정책 SHA 자동 갱신

## Step 2 — secrets 등록 (값은 prompt에서 직접 입력)

```bash
# Anthropic API key — 발급: https://console.anthropic.com/settings/keys
gh secret set ANTHROPIC_API_KEY --repo dfocus-sapsol/oh-my-bas

# Slack webhook — 발급: https://api.slack.com/messaging/webhooks
gh secret set SLACK_WEBHOOK_URL --repo dfocus-sapsol/oh-my-bas
```

| Secret | 용도 | 필수 |
|---|---|---|
| `ANTHROPIC_API_KEY` | LLM 분석 | 선택 (없으면 결정론만, advisory) |
| `SLACK_WEBHOOK_URL` | verdict 알림 | 선택 (없으면 stdout만) |

## Step 3 — PR review·merge

Step 1에서 bootstrap이 만든 PR이 사실상 *자기 자신을 검문*합니다 — 워크플로우 추가는
정책상 critical으로 마킹되지만, v0.11+ 의 SHA-only swap 강등 + v0.14+ 의
자동 검증으로 안전하게 처리됩니다. admin 승인으로 merge.

이후 모든 push/PR이 자동 검문됩니다.

## 정책 갱신 시 (이후 운영)

`policy-bump-watcher.yml` 가 매주 월요일 09:00 UTC 에 changeguard upstream main 의
최신 SHA를 폴링해 갱신 PR을 자동 생성합니다. PR body 에는 변경된 commit 목록 +
GitHub compare URL + pre-flight fixture 검증 결과가 자동 첨부됩니다.

사람이 review·merge만 하면 됩니다 (자동 merge 안 함 — 정책 변경은 *명시적*).

## 트러블슈팅

자세한 내용은 `INSTALL.md` 의 트러블슈팅 섹션 참조. oh-my-bas 특이사항:

- **이미 v0.15 이하로 설치된 경우**: `docs/UPGRADING.md` 의 마이그레이션 가이드
  참조. bootstrap 스크립트를 실행하면 기존 파일을 덮어쓸지 묻습니다.
- **default branch가 `master`임에도 동작**: bootstrap이 자동 감지합니다 — 추가
  손 안 댐.
