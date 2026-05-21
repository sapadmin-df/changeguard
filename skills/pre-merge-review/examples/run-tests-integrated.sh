#!/usr/bin/env bash
# run-tests-integrated.sh — 결정론 + LLM(mock) 통합 fixture 검증
#
# 각 fixture에 대해 deterministic.sh의 결과를 llm-adapter.py에 통과시켜
# 최종 통합 verdict를 expected.json과 비교한다.
# mock-response.json 이 있는 fixture는 mock 모드로 LLM 단계 실행,
# 없으면 --skip-llm 으로 결정론 결과만 사용.
#
# 사용:
#   ./run-tests-integrated.sh         # 전체 (llm-only 포함)
#   ./run-tests-integrated.sh M06     # 패턴 매칭

set -uo pipefail

SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
CHECKER="$SCRIPT_DIR/../checks/deterministic.sh"
ADAPTER="$SCRIPT_DIR/../checks/llm-adapter.py"
FIXTURE_ROOT="$SCRIPT_DIR"

[[ -x "$CHECKER" ]] || { echo "ERROR: checker not executable: $CHECKER" >&2; exit 2; }
[[ -x "$ADAPTER" ]] || { echo "ERROR: adapter not executable: $ADAPTER" >&2; exit 2; }

if [[ $# -gt 0 ]]; then
  FIXTURES=()
  for arg in "$@"; do
    while IFS= read -r f; do FIXTURES+=("$f"); done < <(find "$FIXTURE_ROOT" -path "*$arg*" -name '*.diff' | sort)
  done
else
  mapfile -t FIXTURES < <(find "$FIXTURE_ROOT" \
    \( -path "$FIXTURE_ROOT/malicious/*" \
      -o -path "$FIXTURE_ROOT/benign/*" \
      -o -path "$FIXTURE_ROOT/edge/*" \
      -o -path "$FIXTURE_ROOT/llm-only/*" \
      -o -path "$FIXTURE_ROOT/adversarial/*" \) \
    -name '*.diff' | sort)
fi

if [[ -t 1 ]]; then
  GREEN=$'\033[32m'; RED=$'\033[31m'; YELLOW=$'\033[33m'
  DIM=$'\033[2m'; CYAN=$'\033[36m'; RESET=$'\033[0m'
else
  GREEN=""; RED=""; YELLOW=""; DIM=""; CYAN=""; RESET=""
fi

PASS=0; FAIL=0
FAILURES=()

sev_rank() {
  case "$1" in
    critical) echo 4 ;;
    high)     echo 3 ;;
    medium)   echo 2 ;;
    low)      echo 1 ;;
    *)        echo 0 ;;
  esac
}

verdict_rank() {
  case "$1" in
    block)    echo 3 ;;
    advisory) echo 2 ;;
    pass)     echo 1 ;;
    *)        echo 0 ;;
  esac
}

echo "═══ Integrated (deterministic + mock LLM) Fixture Test ═══"
echo "Checker:  $CHECKER"
echo "Adapter:  $ADAPTER"
echo "Fixtures: ${#FIXTURES[@]}"
echo ""

for fixture in "${FIXTURES[@]}"; do
  name=$(basename "$fixture" .diff)
  expected_file="${fixture%.diff}.expected.json"
  mock_file="${fixture%.diff}.mock-response.json"

  [[ -f "$expected_file" ]] || { echo "${YELLOW}SKIP${RESET} $name (no expected.json)"; continue; }

  # 1. 결정론 검사
  det_findings_file=$(mktemp)
  "$CHECKER" "$fixture" > "$det_findings_file" 2>/dev/null

  # 1.5. 게이팅 시뮬레이션 (SKILL.md 2단계 규칙)
  # caller가 production에서 LLM을 호출할지 결정하는 로직을 동일하게 적용.
  # 이것 없이는 mock 호출이 production 실제 동작과 어긋날 수 있다.
  det_crit=$(jq '[.[] | select(.severity=="critical")] | length' "$det_findings_file")
  det_high=$(jq '[.[] | select(.severity=="high" or .severity=="medium")] | length' "$det_findings_file")
  # diff에 코드 파일 변경이 있는가? (markdown/lockfile/binary가 아닌 파일)
  has_code_change=$(awk '
    /^diff --git/ {
      # b/<path> 추출
      match($0, /b\/[^ "]+/)
      if (RSTART) {
        path = substr($0, RSTART+2, RLENGTH-2)
        # markdown/lockfile/binary 패턴 제외
        if (path !~ /\.(md|markdown)$/ &&
            path !~ /(package-lock\.json|yarn\.lock|pnpm-lock\.yaml|uv\.lock|poetry\.lock|Cargo\.lock|pom\.xml|Gemfile\.lock|go\.sum|composer\.lock)$/) {
          found = 1
        }
      }
    }
    # binary는 별도 형식이라 위에서 잡힘 (확장자 기반 휴리스틱)
    END { print (found ? "1" : "0") }
  ' "$fixture")

  should_call_llm="no"
  if [[ "$det_crit" -gt 0 || "$det_high" -gt 0 ]]; then
    should_call_llm="yes"
  elif [[ "$has_code_change" == "1" ]]; then
    should_call_llm="yes"
  fi

  # 2. 어댑터로 통합 (gating 시뮬레이션 반영)
  if [[ -f "$mock_file" && "$should_call_llm" == "yes" ]]; then
    result=$("$ADAPTER" --diff "$fixture" --findings "$det_findings_file" \
                       --mock "$mock_file" --context ci 2>/dev/null)
    llm_mode="${CYAN}mock${RESET}    "
  elif [[ -f "$mock_file" && "$should_call_llm" == "no" ]]; then
    # mock이 있어도 게이팅이 LLM 호출을 막은 경우 — 정책상 의도된 skip
    result=$("$ADAPTER" --diff "$fixture" --findings "$det_findings_file" \
                       --skip-llm --context ci 2>/dev/null)
    llm_mode="${YELLOW}gated${RESET}   "
  elif [[ "$should_call_llm" == "yes" ]]; then
    # 코드 변경이라 production에서는 LLM 호출되지만, mock이 없어 검증 불가
    result=$("$ADAPTER" --diff "$fixture" --findings "$det_findings_file" \
                       --skip-llm --context ci 2>/dev/null)
    llm_mode="${YELLOW}no-mock${RESET} "
  else
    result=$("$ADAPTER" --diff "$fixture" --findings "$det_findings_file" \
                       --skip-llm --context ci 2>/dev/null)
    llm_mode="${DIM}skip${RESET}    "
  fi
  rm -f "$det_findings_file"

  computed_verdict=$(echo "$result" | jq -r '.verdict')
  finding_count=$(echo "$result" | jq '.findings | length')
  llm_count=$(echo "$result" | jq '[.findings[] | select(.source=="llm")] | length')
  det_count=$(echo "$result" | jq '[.findings[] | select(.source=="deterministic")] | length')

  # 가장 높은 severity
  max_sev="none"
  for s in critical high medium low; do
    cnt=$(echo "$result" | jq --arg s "$s" '[.findings[] | select(.severity==$s)] | length')
    if [[ "$cnt" -gt 0 ]]; then max_sev="$s"; break; fi
  done

  expected_verdict=$(jq -r '.expected_verdict' "$expected_file")
  fixture_type=$(jq -r '.type' "$expected_file")

  ok=true
  reasons=()

  if [[ "$fixture_type" == "malicious" ]]; then
    # verdict가 expected 이상
    if [[ $(verdict_rank "$computed_verdict") -lt $(verdict_rank "$expected_verdict") ]]; then
      ok=false
      reasons+=("verdict $computed_verdict < expected $expected_verdict")
    fi
    # must_detect + must_detect_via_llm 합쳐 검증
    must=$(jq -c '(.must_detect // []) + (.must_detect_via_llm // [])' "$expected_file")
    while IFS= read -r req; do
      [[ -z "$req" ]] && continue
      req_cat=$(echo "$req" | jq -r '.category')
      req_min=$(echo "$req" | jq -r '.min_severity')
      req_min_rank=$(sev_rank "$req_min")
      found_sevs=$(echo "$result" | jq --arg c "$req_cat" \
        '[.findings[] | select(.category==$c) | .severity] | unique')
      satisfied=false
      for s in $(echo "$found_sevs" | jq -r '.[]?'); do
        if [[ $(sev_rank "$s") -ge "$req_min_rank" ]]; then satisfied=true; break; fi
      done
      if ! $satisfied; then
        ok=false
        reasons+=("missing required: $req_cat >= $req_min (found: $found_sevs)")
      fi
    done < <(echo "$must" | jq -c '.[]')

  elif [[ "$fixture_type" == "benign" || "$fixture_type" == "edge" || "$fixture_type" == "adversarial" ]]; then
    max_allowed=$(jq -r '.max_severity_allowed // "low"' "$expected_file")
    if [[ $(sev_rank "$max_sev") -gt $(sev_rank "$max_allowed") ]]; then
      ok=false
      reasons+=("max severity $max_sev > allowed $max_allowed")
    fi
    # 추가 assertion: description 길이 등
    max_desc_len=$(jq -r '.additional_assertions.max_description_length // empty' "$expected_file")
    if [[ -n "$max_desc_len" ]]; then
      actual_max=$(echo "$result" | jq '[.findings[].description | length] | max // 0')
      if [[ "$actual_max" -gt "$max_desc_len" ]]; then
        ok=false
        reasons+=("description length $actual_max > limit $max_desc_len")
      fi
    fi
  fi

  if $ok; then
    PASS=$((PASS+1))
    printf "  ${GREEN}PASS${RESET}  %-40s  ${DIM}v=%-9s det=%d llm=%d (%b)${RESET}\n" \
      "$name" "$computed_verdict" "$det_count" "$llm_count" "$llm_mode"
  else
    FAIL=$((FAIL+1))
    FAILURES+=("$name")
    printf "  ${RED}FAIL${RESET}  %-40s  ${DIM}v=%-9s det=%d llm=%d (%b)${RESET}\n" \
      "$name" "$computed_verdict" "$det_count" "$llm_count" "$llm_mode"
    for r in "${reasons[@]}"; do
      echo "          ${RED}└${RESET} $r"
    done
  fi
done

echo ""
echo "═══ 요약 ═══"
echo "통과: ${GREEN}$PASS${RESET}    실패: ${RED}$FAIL${RESET}"
[[ "$FAIL" -gt 0 ]] && exit 1 || exit 0
