#!/usr/bin/env bash
# deterministic.sh — pre-merge diff에 대한 결정론적 보안 검사
#
# 사용:
#   ./deterministic.sh <diff-file-path>
#
# 출력:
#   findings.json 배열을 stdout으로. 각 finding:
#     { "severity": "critical|high|medium|low",
#       "category": "workflow|dependency|injection|pattern|binary|meta",
#       "location": "<file>:<line>" (가능한 경우),
#       "description": "..." }
#
# 종료 코드: 항상 0. verdict 판정은 호출자가 한다.

set -euo pipefail

DIFF="${1:-}"
if [[ -z "$DIFF" || ! -f "$DIFF" ]]; then
  echo "Usage: $0 <diff-file-path>" >&2
  exit 2
fi

# Markdown 파일 섹션을 제거한 diff를 별도로 만든다.
# 패턴/base64/injection 검사는 이 filtered 버전을 사용한다.
# 근거: markdown 파일은 실행되지 않고, LLM 영향은 시스템 프롬프트 격리로 처리됨.
# 구조적 검사(workflow/lockfile/binary 등)는 원본 DIFF를 그대로 사용.
DIFF_NO_MD=$(mktemp)
trap 'rm -f "$DIFF_NO_MD"' EXIT

awk '
  /^diff --git/ {
    # 파일 확장자가 .md / .markdown 이면 해당 섹션 전체 skip
    skip = ($0 ~ /\.(md|markdown)([ "\047]|$)/)
  }
  !skip
' "$DIFF" > "$DIFF_NO_MD"

# 결과 누적 배열 (jq로 조립)
findings='[]'

add_finding() {
  local severity="$1" category="$2" location="$3" description="$4"
  findings=$(echo "$findings" | jq \
    --arg s "$severity" --arg c "$category" \
    --arg l "$location" --arg d "$description" \
    '. += [{severity:$s, category:$c, location:$l, description:$d}]')
}

# ---------- 1. workflow 파일 변경 ----------
# .github/workflows/*.yml 변경은 항상 critical (스캐너 자체 무력화 우려)
if grep -E '^\+\+\+ b/\.github/workflows/' "$DIFF" >/dev/null 2>&1; then
  while IFS= read -r f; do
    add_finding "critical" "workflow" "$f" \
      "GitHub Actions workflow 파일 변경 — 스캐너 무력화 가능성 검토 필수"
  done < <(grep -E '^\+\+\+ b/\.github/workflows/' "$DIFF" | sed 's|^+++ b/||')
fi

# ---------- 2. lockfile 변경 ----------
LOCKFILES='package-lock\.json|yarn\.lock|pnpm-lock\.yaml|uv\.lock|poetry\.lock|Cargo\.lock|pom\.xml|Gemfile\.lock|go\.sum|composer\.lock'
if grep -E "^\+\+\+ b/.*($LOCKFILES)$" "$DIFF" >/dev/null 2>&1; then
  while IFS= read -r f; do
    # lockfile 변경 라인 수 카운트
    added=$(awk -v file="$f" '
      /^\+\+\+ b\// { current=$2; sub(/^b\//, "", current); inblock=(current==file) }
      inblock && /^\+[^+]/ { count++ }
      END { print count+0 }
    ' "$DIFF")
    sev="medium"
    [[ "$added" -gt 50 ]] && sev="high"
    add_finding "$sev" "dependency" "$f" \
      "Lockfile 변경 — 신규/업그레이드된 패키지 ${added}줄. 의도한 변경인지 확인 필요."
  done < <(grep -E "^\+\+\+ b/.*($LOCKFILES)$" "$DIFF" | sed 's|^+++ b/||')
fi

# ---------- 3. package.json lifecycle script 신규 추가 ----------
# diff에서 +로 시작하는 라인에 "postinstall", "preinstall", "prepare" 키가 새로 들어가는지
if grep -E '^\+\s*"(postinstall|preinstall|prepare|postuninstall|preuninstall)"\s*:' "$DIFF" >/dev/null 2>&1; then
  while IFS= read -r line; do
    add_finding "high" "dependency" "package.json" \
      "신규 npm lifecycle script 추가 감지: $(echo "$line" | sed 's/^+//' | tr -s ' ')"
  done < <(grep -E '^\+\s*"(postinstall|preinstall|prepare|postuninstall|preuninstall)"\s*:' "$DIFF")
fi

# ---------- 4. 의심 실행 패턴 ----------
# curl | sh, wget | bash, eval(atob, eval(Buffer.from
declare -A patterns=(
  ["curl[^|]*\|\s*(sh|bash)"]="critical:Pipe-to-shell 실행 패턴 (curl|sh)"
  ["wget[^|]*\|\s*(sh|bash)"]="critical:Pipe-to-shell 실행 패턴 (wget|bash)"
  ["eval\s*\(\s*atob\s*\("]="critical:base64 디코드 후 eval — 난독화 의심"
  ["eval\s*\(\s*Buffer\.from\s*\("]="critical:Buffer.from 디코드 후 eval — 난독화 의심"
  ["child_process.*exec.*\\\$\{"]="high:동적 셸 명령 조립"
)

for pattern in "${!patterns[@]}"; do
  spec="${patterns[$pattern]}"
  severity="${spec%%:*}"
  description="${spec#*:}"
  if grep -nE "^\+.*$pattern" "$DIFF_NO_MD" >/dev/null 2>&1; then
    while IFS= read -r match; do
      add_finding "$severity" "pattern" "diff:$match" "$description"
    done < <(grep -nE "^\+.*$pattern" "$DIFF_NO_MD" | head -10 | cut -d: -f1)
  fi
done

# ---------- 5. 긴 base64 문자열 ----------
# 100자 이상의 연속된 base64 문자만으로 구성된 라인
if grep -nE '^\+[^+].*[A-Za-z0-9+/]{100,}={0,2}' "$DIFF_NO_MD" >/dev/null 2>&1; then
  count=$(grep -cE '^\+[^+].*[A-Za-z0-9+/]{100,}={0,2}' "$DIFF_NO_MD" || true)
  add_finding "medium" "pattern" "diff" \
    "긴 base64 문자열 추가 감지 (${count}건) — 난독화된 페이로드 가능성 검토"
fi

# ---------- 6. 신규 binary 파일 ----------
if grep -E '^Binary files /dev/null and b/' "$DIFF" >/dev/null 2>&1; then
  while IFS= read -r f; do
    add_finding "high" "binary" "$f" \
      "신규 binary 파일 추가 — 코드 리뷰 불가, 도입 사유 확인 필요"
  done < <(grep -E '^Binary files /dev/null and b/' "$DIFF" | sed 's|^Binary files /dev/null and b/||; s| differ$||')
fi

# ---------- 7. Prompt injection 메타 키워드 ----------
# LLM 단계 진입 전에 이런 패턴을 잡아둔다. LLM이 영향받는 것을 차단.
# Markdown 파일은 제외 — 보안 문서가 위협 패턴을 *논의*하는 자연어 본문은
# false positive이며, LLM 영향은 시스템 프롬프트 격리로 별도 방어된다.
#
# 중요: injection 원문을 description에 *그대로* 싣지 않는다. 그렇게 하면
# 이 finding이 LLM adapter의 user 메시지나 로컬 Claude 컨텍스트에 들어갈 때
# 2차 injection 벡터가 된다. 대신 (1) 감지된 키워드, (2) 라인 번호만 보고하고
# 운영자가 직접 diff를 확인하도록 한다.
INJECTION_PATTERNS='(IGNORE\s+PREVIOUS|SYSTEM\s+PROMPT|CLAUDE\s+INSTRUCTION|ANTHROPIC\s+OVERRIDE|DISREGARD\s+(ABOVE|PRIOR)|NEW\s+INSTRUCTIONS?:|##\s*SYSTEM)'
if grep -niE "^\+.*$INJECTION_PATTERNS" "$DIFF_NO_MD" >/dev/null 2>&1; then
  while IFS= read -r line; do
    # 라인 번호 추출 (grep -n 출력의 맨 앞 숫자)
    line_no=$(echo "$line" | grep -oE '^[0-9]+' | head -1)
    # 어떤 키워드가 매치됐는지만 추출 (원문 전체가 아님)
    matched_kw=$(echo "$line" | grep -oiE "$INJECTION_PATTERNS" | head -1 | tr '[:lower:]' '[:upper:]' | tr -s ' ')
    add_finding "critical" "injection" "diff:line${line_no}" \
      "Prompt injection 메타 키워드 감지: '${matched_kw}' (diff 라인 ${line_no}). LLM 분석 우회 시도일 수 있음. 원문은 보안상 여기 포함하지 않음 — diff를 직접 확인할 것."
  done < <(grep -niE "^\+.*$INJECTION_PATTERNS" "$DIFF_NO_MD" | head -5)
fi

# ---------- 출력 ----------
echo "$findings"
