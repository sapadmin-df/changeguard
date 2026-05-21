#!/usr/bin/env bash
# run-tests.sh — fixture corpus를 deterministic.sh에 돌려 결과 검증
#
# 사용:
#   ./run-tests.sh                # 전체 실행
#   ./run-tests.sh malicious/M02* # 특정 패턴만
#
# 종료 코드:
#   0 - 모든 fixture가 expected와 일치
#   1 - 하나 이상 불일치 (회귀 또는 미해결 약점)

set -uo pipefail

SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
CHECKER="$SCRIPT_DIR/../checks/deterministic.sh"
FIXTURE_ROOT="$SCRIPT_DIR"

if [[ ! -x "$CHECKER" ]]; then
  echo "ERROR: deterministic.sh를 찾을 수 없거나 실행 불가: $CHECKER" >&2
  exit 2
fi

# 인자가 있으면 그 패턴만, 없으면 전체
if [[ $# -gt 0 ]]; then
  FIXTURES=()
  for arg in "$@"; do
    while IFS= read -r f; do FIXTURES+=("$f"); done < <(find "$FIXTURE_ROOT" -path "*$arg*" -name '*.diff' -not -path '*/llm-only/*' | sort)
  done
else
  # llm-only/ 는 별도 runner가 처리. 결정론 검사는 malicious/benign/edge만.
  mapfile -t FIXTURES < <(find "$FIXTURE_ROOT" \( -path "$FIXTURE_ROOT/malicious/*" -o -path "$FIXTURE_ROOT/benign/*" -o -path "$FIXTURE_ROOT/edge/*" \) -name '*.diff' | sort)
fi

# 색상
if [[ -t 1 ]]; then
  GREEN=$'\033[32m'; RED=$'\033[31m'; YELLOW=$'\033[33m'; DIM=$'\033[2m'; RESET=$'\033[0m'
else
  GREEN=""; RED=""; YELLOW=""; DIM=""; RESET=""
fi

PASS=0; FAIL=0
FAILURES=()

# severity 순위 (높을수록 심각)
sev_rank() {
  case "$1" in
    critical) echo 4 ;;
    high)     echo 3 ;;
    medium)   echo 2 ;;
    low)      echo 1 ;;
    *)        echo 0 ;;
  esac
}

# verdict 계산: critical→block, high→advisory, 그 외→pass (SKILL.md 규칙)
compute_verdict() {
  local findings="$1"
  local crit=$(echo "$findings" | jq '[.[] | select(.severity=="critical")] | length')
  local high=$(echo "$findings" | jq '[.[] | select(.severity=="high")] | length')
  local med=$(echo "$findings"  | jq '[.[] | select(.severity=="medium")] | length')
  if [[ "$crit" -gt 0 ]]; then echo "block"
  elif [[ "$high" -gt 0 ]]; then echo "advisory"
  elif [[ "$med"  -gt 0 ]]; then echo "advisory"
  else echo "pass"; fi
}

# 가장 높은 severity 추출
max_severity() {
  local findings="$1"
  local max="none"; local max_rank=0
  for s in critical high medium low; do
    local cnt=$(echo "$findings" | jq --arg s "$s" '[.[] | select(.severity==$s)] | length')
    if [[ "$cnt" -gt 0 ]]; then
      local r=$(sev_rank "$s")
      if [[ "$r" -gt "$max_rank" ]]; then max="$s"; max_rank="$r"; fi
    fi
  done
  echo "$max"
}

echo "═══ Pre-Merge Review Fixture Test ═══"
echo "Checker: $CHECKER"
echo "Fixtures: ${#FIXTURES[@]}"
echo ""

for fixture in "${FIXTURES[@]}"; do
  name=$(basename "$fixture" .diff)
  expected_file="${fixture%.diff}.expected.json"
  if [[ ! -f "$expected_file" ]]; then
    echo "${YELLOW}SKIP${RESET} $name (no expected.json)"
    continue
  fi

  findings=$("$CHECKER" "$fixture" 2>/dev/null || echo "[]")
  computed_verdict=$(compute_verdict "$findings")
  max_sev=$(max_severity "$findings")
  finding_count=$(echo "$findings" | jq 'length')

  expected_verdict=$(jq -r '.expected_verdict' "$expected_file")
  fixture_type=$(jq -r '.type' "$expected_file")

  ok=true
  reasons=()

  if [[ "$fixture_type" == "malicious" ]]; then
    # 결정론 단독 기대 verdict가 별도로 있으면 우선 사용 (LLM 통합 후 verdict와 다른 경우)
    local_expected=$(jq -r '.deterministic_expected_verdict // .expected_verdict' "$expected_file")
    # 1. verdict가 local_expected 이상이어야 (block > advisory > pass)
    exp_rank=$(case "$local_expected" in block) echo 3;; advisory) echo 2;; pass) echo 1;; esac)
    got_rank=$(case "$computed_verdict"  in block) echo 3;; advisory) echo 2;; pass) echo 1;; esac)
    if [[ "$got_rank" -lt "$exp_rank" ]]; then
      ok=false
      reasons+=("verdict $computed_verdict < expected $local_expected (deterministic only)")
    fi

    # 2. 각 must_detect 항목이 실제로 잡혔는지 (must_detect_via_llm 은 무시)
    must=$(jq -c '.must_detect // []' "$expected_file")
    while IFS= read -r req; do
      [[ -z "$req" ]] && continue
      req_cat=$(echo "$req" | jq -r '.category')
      req_min=$(echo "$req" | jq -r '.min_severity')
      req_min_rank=$(sev_rank "$req_min")
      found=$(echo "$findings" | jq --arg c "$req_cat" --arg s "$req_min" \
        '[.[] | select(.category==$c)] | map(.severity) | unique')
      satisfied=false
      for s in $(echo "$found" | jq -r '.[]?'); do
        if [[ $(sev_rank "$s") -ge "$req_min_rank" ]]; then satisfied=true; break; fi
      done
      if ! $satisfied; then
        ok=false
        reasons+=("missing required: $req_cat >= $req_min (found: $found)")
      fi
    done < <(echo "$must" | jq -c '.[]')

  elif [[ "$fixture_type" == "benign" || "$fixture_type" == "edge" ]]; then
    max_allowed=$(jq -r '.max_severity_allowed // "low"' "$expected_file")
    max_allowed_rank=$(sev_rank "$max_allowed")
    got_max_rank=$(sev_rank "$max_sev")
    if [[ "$got_max_rank" -gt "$max_allowed_rank" ]]; then
      ok=false
      reasons+=("max severity $max_sev > allowed $max_allowed")
    fi
  fi

  if $ok; then
    PASS=$((PASS+1))
    printf "  ${GREEN}PASS${RESET}  %-40s  ${DIM}verdict=%-9s findings=%d max=%s${RESET}\n" \
      "$name" "$computed_verdict" "$finding_count" "$max_sev"
  else
    FAIL=$((FAIL+1))
    FAILURES+=("$name")
    printf "  ${RED}FAIL${RESET}  %-40s  ${DIM}verdict=%-9s findings=%d max=%s${RESET}\n" \
      "$name" "$computed_verdict" "$finding_count" "$max_sev"
    for r in "${reasons[@]}"; do
      echo "          ${RED}└${RESET} $r"
    done
  fi
done

echo ""
echo "═══ 요약 ═══"
echo "통과: ${GREEN}$PASS${RESET}"
echo "실패: ${RED}$FAIL${RESET}"
if [[ "$FAIL" -gt 0 ]]; then
  echo ""
  echo "실패 fixture는 다음 둘 중 하나를 의미합니다:"
  echo "  1. 결정론적 검사의 미해결 약점 (개선 필요)"
  echo "  2. 의도된 한계 — LLM 계층에서 보완 필요 (expected.json의 rationale 참조)"
  exit 1
fi
exit 0
