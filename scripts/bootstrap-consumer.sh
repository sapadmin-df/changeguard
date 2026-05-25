#!/usr/bin/env bash
# bootstrap-consumer.sh — changeguard 자동 온보딩 (v0.16+)
#
# 한 명령으로 changeguard pre-merge-review 게이트를 *자신의 repo*에 설치한다.
#
# 사용 (소비자 repo 안에서 실행):
#   bash <(curl -fsSL https://raw.githubusercontent.com/sapadmin-df/changeguard/main/scripts/bootstrap-consumer.sh)
#
#   또는 changeguard를 clone한 상태:
#   ./scripts/bootstrap-consumer.sh [옵션]
#
# 옵션:
#   --dry-run             변경 없이 무엇이 일어날지만 출력
#   --policy-sha <SHA>    특정 정책 SHA로 pin (기본: changeguard main HEAD)
#   --no-watcher          policy-bump-watcher 워크플로우 생성 안 함
#   --no-pr               PR 생성 안 함 (브랜치만 만들고 멈춤)
#   --yes                 모든 prompt에 yes (CI/script용)
#   -h, --help            이 도움말
#
# 동작:
#   1. 사전 체크 (gh CLI 설치/인증 + git repo 안인지)
#   2. default branch 자동 감지 (main/master/develop/...)
#   3. 최신 정책 SHA fetch (또는 --policy-sha)
#   4. 두 워크플로우 파일 생성 (5-10줄 reusable 호출 형태)
#   5. verify-workflow-pins.sh로 사후 검증
#   6. secrets 등록 안내 명령 출력 (값은 사용자가 직접 입력)
#   7. PR 생성 (옵션)
#
# 자동화 불가 (사용자 본인이):
#   - ANTHROPIC_API_KEY: console.anthropic.com 에서 발급 + gh secret set
#   - SLACK_WEBHOOK_URL: Slack workspace에서 webhook 생성 + gh secret set
#   - 대상 repo collaborator 권한 (없으면 gh pr create 실패 → 명확한 에러)
#
# 보안 원칙:
#   - secrets 값은 *절대* 인자나 stdin으로 받지 않음 (shell history 노출 방지)
#   - bootstrap 후 생성된 워크플로우는 SHA pin (mutable ref 금지)
#   - dry-run 권장 (변경 전에 무엇이 일어날지 검토)

set -euo pipefail

# ---------- 색상 + 출력 helper ----------
if [ -t 1 ]; then
  R=$'\033[31m' G=$'\033[32m' Y=$'\033[33m' B=$'\033[34m' D=$'\033[2m' Z=$'\033[0m'
else
  R='' G='' Y='' B='' D='' Z=''
fi
say()  { printf '%s\n' "$*" >&2; }
ok()   { printf '%s✓%s %s\n'  "$G" "$Z" "$*" >&2; }
err()  { printf '%s✗%s %s\n'  "$R" "$Z" "$*" >&2; }
warn() { printf '%s⚠%s %s\n'  "$Y" "$Z" "$*" >&2; }
info() { printf '%s→%s %s\n'  "$B" "$Z" "$*" >&2; }
hdr()  { printf '\n%s%s%s\n'  "$B" "$*" "$Z" >&2; }
dim()  { printf '%s%s%s\n'    "$D" "$*" "$Z" >&2; }

# ---------- 인자 파싱 ----------
POLICY_REPO="${POLICY_REPO:-sapadmin-df/changeguard}"
POLICY_SHA=""
DRY_RUN=0
NO_WATCHER=0
NO_PR=0
ASSUME_YES=0

usage() {
  sed -n '2,38p' "$0" | sed 's/^# \{0,1\}//' >&2
  exit "${1:-0}"
}

while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run)       DRY_RUN=1 ;;
    --policy-sha)    POLICY_SHA="$2"; shift ;;
    --policy-sha=*)  POLICY_SHA="${1#*=}" ;;
    --no-watcher)    NO_WATCHER=1 ;;
    --no-pr)         NO_PR=1 ;;
    --yes|-y)        ASSUME_YES=1 ;;
    -h|--help)       usage 0 ;;
    *)               err "알 수 없는 옵션: $1"; usage 1 ;;
  esac
  shift
done

confirm() {
  if [ "$ASSUME_YES" = 1 ] || [ "$DRY_RUN" = 1 ]; then
    return 0
  fi
  printf '%s? %s [y/N]: %s' "$Y" "$1" "$Z" >&2
  read -r ans </dev/tty 2>/dev/null || ans=""
  case "$ans" in y|Y|yes) return 0 ;; *) return 1 ;; esac
}

# ---------- 사전 체크 ----------
hdr "[1/7] 사전 체크"

command -v gh >/dev/null 2>&1 || { err "gh CLI 필요 — https://cli.github.com 설치"; exit 2; }
command -v git >/dev/null 2>&1 || { err "git 필요"; exit 2; }
command -v jq >/dev/null 2>&1 || { err "jq 필요"; exit 2; }

if ! gh auth status >/dev/null 2>&1; then
  err "gh CLI 미인증 — 'gh auth login' 실행 후 재시도"
  exit 2
fi
ok "gh CLI 인증됨 ($(gh api user --jq .login))"

if ! git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  err "현재 디렉터리가 git repo가 아닙니다 — 대상 repo 안에서 실행하세요"
  exit 2
fi
TARGET_REPO=$(gh repo view --json nameWithOwner --jq .nameWithOwner 2>/dev/null || true)
if [ -z "$TARGET_REPO" ]; then
  err "대상 repo를 식별할 수 없음 (gh repo view 실패) — repo 안에서 실행 + remote 'origin' 설정 확인"
  exit 2
fi
ok "대상 repo: $TARGET_REPO"

# 대상 repo의 collaborator 권한 사전 확인 (PR 생성에 필요)
PERM=$(gh api "repos/$TARGET_REPO" --jq '.permissions | to_entries | map(select(.value==true)) | map(.key) | join(",")' 2>/dev/null || echo "")
if [ -z "$PERM" ] || ! echo "$PERM" | grep -qE 'push|admin|maintain'; then
  warn "대상 repo write 권한 없음 — PR 생성 단계에서 실패할 수 있음 (현재 권한: ${PERM:-none})"
fi

# ---------- 정책 SHA 결정 ----------
hdr "[2/7] 정책 SHA 결정"
if [ -z "$POLICY_SHA" ]; then
  POLICY_SHA=$(gh api "repos/$POLICY_REPO/commits/main" --jq .sha 2>/dev/null || echo "")
  if [ -z "$POLICY_SHA" ]; then
    err "$POLICY_REPO/main HEAD 조회 실패 — 네트워크/권한 확인"
    exit 3
  fi
  info "정책 SHA (latest main): $POLICY_SHA"
else
  if ! [[ "$POLICY_SHA" =~ ^[a-f0-9]{40}$ ]]; then
    err "--policy-sha 는 40자 hex SHA 필요 ($POLICY_SHA)"
    exit 1
  fi
  info "정책 SHA (지정): $POLICY_SHA"
fi
POLICY_VERSION=$(gh api "repos/$POLICY_REPO/contents/VERSION?ref=$POLICY_SHA" --jq .content 2>/dev/null \
  | base64 -d 2>/dev/null | head -1 || echo "?")
ok "정책: $POLICY_REPO @ $POLICY_SHA (v$POLICY_VERSION)"

# ---------- default branch 자동 감지 ----------
hdr "[3/7] 대상 repo default branch 자동 감지"
DEFAULT_BRANCH=$(gh api "repos/$TARGET_REPO" --jq .default_branch)
ok "default branch: $DEFAULT_BRANCH"

# ---------- 워크플로우 파일 생성 ----------
hdr "[4/7] 워크플로우 파일 작성"

WF_DIR=".github/workflows"
WF_GATE="$WF_DIR/pre-merge-review.yml"
WF_WATCHER="$WF_DIR/policy-bump-watcher.yml"

mkdir -p "$WF_DIR" 2>/dev/null || true

# (a) pre-merge-review.yml (소비자 측)
# 정책 SHA는 두 곳에 같은 값으로 — 'uses: ...@<SHA>' 와 'with: policy_sha: <SHA>'.
# GitHub Actions의 reusable workflow가 호출 SHA를 자동 알려주지 않는 제약 회피.
# watcher가 sed로 SHA 치환할 때 두 곳이 자동으로 동시 갱신됨.
GATE_CONTENT=$(cat <<EOF
# Auto-generated by changeguard bootstrap-consumer.sh
# 정책 본부: https://github.com/$POLICY_REPO
# 갱신: policy-bump-watcher.yml 가 매주 갱신 PR을 제안 (수동 merge).

name: pre-merge-review

on:
  push:
    branches: [$DEFAULT_BRANCH]
  pull_request: {}

permissions:
  contents: read
  pull-requests: write

jobs:
  review:
    uses: $POLICY_REPO/.github/workflows/pre-merge-review.yml@$POLICY_SHA
    with:
      policy_sha: $POLICY_SHA
    secrets:
      ANTHROPIC_API_KEY: \${{ secrets.ANTHROPIC_API_KEY }}
      SLACK_WEBHOOK_URL: \${{ secrets.SLACK_WEBHOOK_URL }}
EOF
)

# (b) policy-bump-watcher.yml (소비자 측)
WATCHER_CONTENT=$(cat <<EOF
# Auto-generated by changeguard bootstrap-consumer.sh
# 매주 changeguard upstream main을 폴링하고, 새 SHA가 있으면 갱신 PR 생성.

name: policy-bump-watcher

on:
  schedule:
    - cron: '0 9 * * 1'   # 매주 월요일 09:00 UTC
  workflow_dispatch: {}

permissions:
  contents: write
  pull-requests: write

jobs:
  bump:
    uses: $POLICY_REPO/.github/workflows/policy-bump-watcher.yml@$POLICY_SHA
    with:
      policy_sha: $POLICY_SHA
    secrets:
      SLACK_WEBHOOK_URL: \${{ secrets.SLACK_WEBHOOK_URL }}
EOF
)

write_file() {
  local path="$1" content="$2"
  if [ -f "$path" ]; then
    if [ "$DRY_RUN" = 1 ]; then
      info "[dry-run] 기존 $path 와 비교 — diff:"
      diff -u "$path" <(printf '%s\n' "$content") | head -50 >&2 || true
      return 0
    fi
    if ! confirm "기존 $path 를 새 reusable 호출 형태로 *덮어쓸까요*?"; then
      warn "$path 건너뜀"
      return 0
    fi
  fi
  if [ "$DRY_RUN" = 1 ]; then
    info "[dry-run] $path 작성 (skip)"
    return 0
  fi
  printf '%s\n' "$content" > "$path"
  ok "작성: $path"
}

write_file "$WF_GATE" "$GATE_CONTENT"
if [ "$NO_WATCHER" = 0 ]; then
  write_file "$WF_WATCHER" "$WATCHER_CONTENT"
else
  info "--no-watcher: watcher 생성 생략"
fi

# ---------- 사후 검증 ----------
hdr "[5/7] SHA pinning 사후 검증"
TMP_VERIFY=$(mktemp -d)
trap 'rm -rf "$TMP_VERIFY"' EXIT
if [ "$DRY_RUN" = 0 ]; then
  # 정책 repo에서 verify-workflow-pins.sh 가져와 실행
  curl -fsSL "https://raw.githubusercontent.com/$POLICY_REPO/$POLICY_SHA/scripts/verify-workflow-pins.sh" \
    -o "$TMP_VERIFY/verify.sh" 2>/dev/null
  if [ -s "$TMP_VERIFY/verify.sh" ]; then
    chmod +x "$TMP_VERIFY/verify.sh"
    if bash "$TMP_VERIFY/verify.sh" "$WF_GATE" ${NO_WATCHER:+} ${NO_WATCHER:-$WF_WATCHER}; then
      ok "모든 uses: 가 40자 SHA로 pin됨"
    else
      err "pinning 검증 실패 — 생성된 파일 확인"
      exit 4
    fi
  else
    warn "verify-workflow-pins.sh fetch 실패 — 검증 건너뜀"
  fi
fi

# ---------- secrets 안내 ----------
hdr "[6/7] secrets 등록 안내 (값은 직접 입력)"
say ""
say "다음 명령으로 secrets를 등록하세요. 값은 ${B}그 자리에서 prompt로 입력${Z}됩니다 — 쉘 history에 안 남음:"
say ""
say "  ${G}gh secret set ANTHROPIC_API_KEY --repo $TARGET_REPO${Z}     # 필수 (없으면 결정론 검사만)"
say "  ${G}gh secret set SLACK_WEBHOOK_URL --repo $TARGET_REPO${Z}    # 선택 (없으면 stdout만)"
say ""
say "API key 발급:"
say "  • ANTHROPIC: https://console.anthropic.com/settings/keys"
say "  • Slack webhook: https://api.slack.com/messaging/webhooks"
say ""

# ---------- PR 생성 ----------
hdr "[7/7] PR 생성"
if [ "$DRY_RUN" = 1 ]; then
  info "[dry-run] PR 생성 단계 생략"
  exit 0
fi
if [ "$NO_PR" = 1 ]; then
  info "--no-pr: PR 생성 생략. 다음 단계는 수동:"
  say "  git checkout -b feat/changeguard-onboarding"
  say "  git add $WF_GATE${NO_WATCHER:+ }${NO_WATCHER:-$WF_WATCHER}"
  say "  git commit -m 'ci: onboard changeguard pre-merge-review'"
  say "  git push -u origin feat/changeguard-onboarding"
  say "  gh pr create"
  exit 0
fi

if ! confirm "PR을 자동 생성할까요? (브랜치 + commit + push + gh pr create)"; then
  warn "PR 생성 건너뜀. 위 NO_PR 단계 명령을 수동 실행하세요."
  exit 0
fi

# 기존 작업 트리에 미커밋 변경이 있으면 경고
if ! git diff --quiet HEAD -- 2>/dev/null; then
  warn "작업 트리에 미커밋 변경이 있습니다. 본 PR에 *워크플로우 파일만* 포함하려면 [Enter]로 계속 (기타 변경은 stash됩니다)"
  if [ "$ASSUME_YES" = 0 ]; then read -r _ </dev/tty 2>/dev/null || true; fi
  git stash push -m "bootstrap-consumer.sh: pre-onboarding stash" -- ':!.github/workflows' 2>/dev/null || true
fi

BRANCH="feat/changeguard-onboarding-${POLICY_SHA:0:12}"
git checkout -b "$BRANCH" 2>/dev/null || git checkout "$BRANCH"
git add "$WF_GATE"
[ "$NO_WATCHER" = 0 ] && git add "$WF_WATCHER"
git commit -m "ci: onboard changeguard pre-merge-review (policy $POLICY_SHA)" \
  -m "Generated by bootstrap-consumer.sh from $POLICY_REPO @ $POLICY_SHA (v$POLICY_VERSION)."
git push -u origin "$BRANCH"

PR_URL=$(gh pr create \
  --base "$DEFAULT_BRANCH" \
  --head "$BRANCH" \
  --title "ci: onboard changeguard pre-merge-review" \
  --body "$(cat <<EOF
## changeguard 자동 온보딩

이 PR은 \`scripts/bootstrap-consumer.sh\` 가 자동 생성했습니다.

### 변경
- \`.github/workflows/pre-merge-review.yml\` — pre-merge security gate
- $([ "$NO_WATCHER" = 0 ] && echo "\`.github/workflows/policy-bump-watcher.yml\` — 정책 SHA 자동 갱신" || echo "(watcher 생략 — --no-watcher)")

### 정책 본부
- repo: $POLICY_REPO
- SHA: $POLICY_SHA
- 버전: $POLICY_VERSION

### merge 전 체크
- [ ] ANTHROPIC_API_KEY secret 등록됨 (없어도 결정론 검사는 동작)
- [ ] SLACK_WEBHOOK_URL secret 등록됨 (선택)
- [ ] 워크플로우 트리거(branch: $DEFAULT_BRANCH)가 본 repo 환경에 맞음

### 동작 검증
이 PR이 merge되면 다음 push/PR부터 pre-merge-review가 자동 실행됩니다.

EOF
)")

ok "PR 생성됨: $PR_URL"
say ""
ok "온보딩 완료. 위 secrets 등록 후 PR을 review·merge하세요."
