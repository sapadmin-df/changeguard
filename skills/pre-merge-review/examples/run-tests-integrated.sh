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
CALLER="$SCRIPT_DIR/../checks/ci-caller.py"
FIXTURE_ROOT="$SCRIPT_DIR"

# Windows용 jq는 CRLF를 출력한다 — `for s in $(jq -r ...)` 가 "critical\r"을 받아
# sev_rank 0이 되는 오탐을 막는다 (Linux에서는 no-op).
jq() { command jq "$@" | tr -d '\r'; }

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

  # 1.5. 게이팅 (SKILL.md 2단계 규칙)
  # v0.17: production(ci-caller.py)의 강등·게이팅 함수를 *그대로* 호출한다.
  # 이전엔 awk로 재구현해 두어 caller와 어긋날 수 있었다 (에이전트 지시 파일 등).
  should_call_llm=$(python3 - "$CALLER" "$fixture" "$det_findings_file" <<'PYEOF'
import importlib.util, json, sys
caller, diff_path, det_path = sys.argv[1:4]
spec = importlib.util.spec_from_file_location("ci_caller", caller)
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
diff = open(diff_path, encoding="utf-8", errors="replace").read()
det = m.downgrade_sha_only_workflow_findings(diff, json.load(open(det_path, encoding="utf-8")))
json.dump(det, open(det_path, "w", encoding="utf-8"))
print("yes" if m.should_call_llm(det, diff)[0] else "no")
PYEOF
)
  should_call_llm_bool=$([[ "$should_call_llm" == "yes" ]] && echo true || echo false)

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
    # v0.17: 비-malicious fixture도 must_detect 지원 (예: refusal → meta medium)
    while IFS= read -r req; do
      [[ -z "$req" ]] && continue
      req_cat=$(echo "$req" | jq -r '.category')
      req_min_rank=$(sev_rank "$(echo "$req" | jq -r '.min_severity')")
      found_sevs=$(echo "$result" | jq --arg c "$req_cat" '[.findings[] | select(.category==$c) | .severity] | unique')
      satisfied=false
      for s in $(echo "$found_sevs" | jq -r '.[]?'); do
        if [[ $(sev_rank "$s") -ge "$req_min_rank" ]]; then satisfied=true; break; fi
      done
      $satisfied || { ok=false; reasons+=("missing required: $req (found: $found_sevs)"); }
    done < <(jq -c '(.must_detect // [])[]' "$expected_file")
  fi

  # v0.17 공통 assertion
  # - min_verdict: verdict 하한 (refusal/잘림이 조용히 pass로 새지 않는지)
  # - expect_llm_called: 게이팅 결과 고정 (에이전트 지시 파일은 LLM 호출, 순수 문서는 skip)
  min_verdict=$(jq -r '.min_verdict // empty' "$expected_file")
  if [[ -n "$min_verdict" && $(verdict_rank "$computed_verdict") -lt $(verdict_rank "$min_verdict") ]]; then
    ok=false
    reasons+=("verdict $computed_verdict < min_verdict $min_verdict")
  fi
  expect_llm=$(jq -r 'if has("expect_llm_called") then .expect_llm_called|tostring else empty end' "$expected_file")
  if [[ -n "$expect_llm" && "$expect_llm" != "$should_call_llm_bool" ]]; then
    ok=false
    reasons+=("gating: expect_llm_called=$expect_llm but caller decided call_llm=$should_call_llm_bool")
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
