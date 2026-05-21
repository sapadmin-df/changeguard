#!/usr/bin/env bash
# verify-workflow-pins.sh — 워크플로우의 uses: 가 모두 40자 commit SHA인지 검증
#
# 사용:
#   ./verify-workflow-pins.sh [workflow-file-path] ...
#   ./verify-workflow-pins.sh $(find .github/workflows -name '*.yml')
#
# 종료 코드:
#   0 - 모든 uses가 40자 SHA로 pin됨
#   1 - 하나 이상 floating ref 또는 FIXME 발견
#
# 운영 위치:
#   - 정책 repo 자신의 CI에서 정책 변경 PR마다 실행
#   - 대상 repo의 CI 첫 단계로도 실행 (정책 repo 권장)

set -uo pipefail

if [[ $# -eq 0 ]]; then
  # 인자 없으면 현재 디렉터리의 모든 워크플로우
  mapfile -t FILES < <(find .github/workflows -type f \( -name '*.yml' -o -name '*.yaml' \) 2>/dev/null)
else
  FILES=("$@")
fi

if [[ ${#FILES[@]} -eq 0 ]]; then
  echo "no workflow files found" >&2
  exit 0
fi

FAIL=0
for file in "${FILES[@]}"; do
  if [[ ! -f "$file" ]]; then
    echo "missing: $file" >&2
    FAIL=$((FAIL+1))
    continue
  fi

  # uses: <something>@<ref> 모든 라인 추출
  # template 파일은 건너뛰지 않음 — template도 사용 전에 미리 검증해야 함
  while IFS=: read -r lineno line; do
    # @ 뒤의 ref 추출
    ref=$(echo "$line" | sed -E 's/.*uses:[[:space:]]*[^@]+@([^[:space:]#]+).*/\1/')

    # 40자 hex SHA인가?
    if [[ "$ref" =~ ^[0-9a-f]{40}$ ]]; then
      continue
    fi

    # local action (./path) 또는 docker (docker://)는 예외
    if [[ "$ref" == "" ]] || [[ "$line" =~ uses:[[:space:]]*\./ ]] || [[ "$line" =~ uses:[[:space:]]*docker:// ]]; then
      continue
    fi

    # 그 외는 모두 위반
    echo "  ${file}:${lineno}: floating ref or placeholder: ${ref}"
    FAIL=$((FAIL+1))
  done < <(grep -nE '^\s*-?\s*uses:\s+\S+' "$file")
done

if [[ "$FAIL" -gt 0 ]]; then
  echo ""
  echo "Found $FAIL non-pinned references. All 'uses:' must be 40-char commit SHA."
  echo "How to get the SHA:"
  echo "  - Visit github.com/<owner>/<repo>/tags or /releases"
  echo "  - Or:  gh api /repos/<owner>/<repo>/git/refs/tags/<tag> --jq .object.sha"
  exit 1
fi

echo "All workflow uses: are pinned to commit SHA. OK."
exit 0
