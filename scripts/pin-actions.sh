#!/usr/bin/env bash
# pin-actions.sh — 워크플로우의 <FIXME-...-SHA> 플레이스홀더를 실제 SHA로 채우는 보조
#
# 요구사항: gh CLI (인증된 상태) 또는 GITHUB_TOKEN
#
# 사용:
#   ./pin-actions.sh                           # 추천 mapping 출력만
#   ./pin-actions.sh --apply <workflow-file>   # 실제 in-place 교체
#
# 운영 원칙:
#   - 이 스크립트는 *최신 release tag의 SHA*를 가져온다. 운영자는 출력을
#     검토하고 명시적으로 --apply를 수행해야 한다. 자동 적용 금지.
#   - 적용 후 반드시 verify-workflow-pins.sh로 재검증.
#   - SHA 갱신은 정책 변경이므로 PR로 검토받아야 한다.

set -uo pipefail

# 플레이스홀더 → action 매핑
# <FIXME>를 채울 때 사용할 표준 action 목록.
declare -A ACTION_MAP=(
  ["<FIXME-CHECKOUT-SHA>"]="actions/checkout"
  ["<FIXME-UPLOAD-ARTIFACT-SHA>"]="actions/upload-artifact"
  ["<FIXME-GITHUB-SCRIPT-SHA>"]="actions/github-script"
  ["<FIXME-SETUP-PYTHON-SHA>"]="actions/setup-python"
  ["<FIXME-SETUP-NODE-SHA>"]="actions/setup-node"
)

get_latest_release_sha() {
  local repo="$1"
  if command -v gh >/dev/null 2>&1; then
    # gh CLI가 있으면 사용
    gh api "/repos/$repo/releases/latest" --jq '.tag_name' 2>/dev/null | \
      xargs -I{} gh api "/repos/$repo/git/refs/tags/{}" --jq '.object.sha' 2>/dev/null
  elif [[ -n "${GITHUB_TOKEN:-}" ]]; then
    # gh 없으면 curl + token
    local tag=$(curl -fsS -H "Authorization: token $GITHUB_TOKEN" \
      "https://api.github.com/repos/$repo/releases/latest" | \
      grep -oE '"tag_name":\s*"[^"]+' | sed 's/.*"//')
    [[ -n "$tag" ]] && curl -fsS -H "Authorization: token $GITHUB_TOKEN" \
      "https://api.github.com/repos/$repo/git/refs/tags/$tag" | \
      grep -oE '"sha":\s*"[a-f0-9]{40}"' | head -1 | sed 's/.*"\([a-f0-9]\{40\}\)"/\1/'
  else
    echo "ERR: install 'gh' CLI or set GITHUB_TOKEN" >&2
    return 1
  fi
}

MODE="show"
WORKFLOW=""
for arg in "$@"; do
  case "$arg" in
    --apply) MODE="apply" ;;
    *)
      if [[ -z "$WORKFLOW" ]]; then
        WORKFLOW="$arg"
      fi
      ;;
  esac
done

if [[ "$MODE" == "apply" && -z "$WORKFLOW" ]]; then
  echo "Usage: $0 --apply <workflow-file>" >&2
  exit 2
fi

echo "Resolving latest release SHAs..."
echo ""
declare -A RESOLVED
for placeholder in "${!ACTION_MAP[@]}"; do
  repo="${ACTION_MAP[$placeholder]}"
  printf "  %-30s → %s ... " "$placeholder" "$repo"
  sha=$(get_latest_release_sha "$repo")
  if [[ -n "$sha" && "$sha" =~ ^[a-f0-9]{40}$ ]]; then
    RESOLVED["$placeholder"]="$sha"
    echo "$sha"
  else
    echo "FAILED"
  fi
done

if [[ "$MODE" == "show" ]]; then
  echo ""
  echo "수동 교체용 sed 명령:"
  for placeholder in "${!RESOLVED[@]}"; do
    echo "  sed -i 's|$placeholder|${RESOLVED[$placeholder]}|g' <file>"
  done
  echo ""
  echo "또는 자동 적용:  $0 --apply <workflow-file>"
  exit 0
fi

# apply mode
if [[ ! -f "$WORKFLOW" ]]; then
  echo "missing: $WORKFLOW" >&2
  exit 2
fi

# 원본 백업
cp "$WORKFLOW" "$WORKFLOW.bak"
echo ""
echo "Applying to $WORKFLOW (backup: $WORKFLOW.bak)..."
for placeholder in "${!RESOLVED[@]}"; do
  sha="${RESOLVED[$placeholder]}"
  count=$(grep -c "$placeholder" "$WORKFLOW" || true)
  if [[ "$count" -gt 0 ]]; then
    sed -i "s|$placeholder|$sha|g" "$WORKFLOW"
    echo "  replaced $placeholder ($count occurrences)"
  fi
done

echo ""
echo "검증 실행 중..."
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
"$SCRIPT_DIR/verify-workflow-pins.sh" "$WORKFLOW"
