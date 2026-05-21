#!/usr/bin/env python3
"""
ci-caller.py — Actions workflow에서 호출하는 통합 caller

역할:
1. SKILL.md v0.5 gating 규칙 집행 (변경 카테고리 기반 LLM 호출 결정)
2. llm-adapter.py 호출 (LLM 호출이 필요한 경우)
3. verdict별 Slack 알림 디스패치 (INFO/REVIEW/ALERT)
4. exit code로 워크플로우 결과 전달

게이팅 규칙 (SKILL.md 2단계 표):
- 결정론 critical/high/medium 있음 → LLM 호출
- 결정론 0 + 코드 파일 변경 있음 → LLM 호출
- 결정론 0 + markdown/lockfile만 변경 → skip
- diff 비어있음 → skip

이 caller는 워크플로우의 단일 책임 진입점이다. workflow YAML은 이것만 호출하고
복잡한 로직은 여기 모인다. 변경/테스트가 fixture corpus로 가능.

환경변수:
  POLICY_PATH        - 정책 repo가 checkout된 경로 (기본 ".policy")
  POLICY_VERSION     - VERSION 파일 내용
  POLICY_SHA         - 정책 repo의 commit SHA
  ANTHROPIC_API_KEY  - LLM 호출용 (없으면 adapter가 fail-open + meta finding)
  SLACK_WEBHOOK_URL  - 알림용 (없으면 stdout만)
  GITHUB_REPOSITORY  - 알림 메시지에 포함
  GITHUB_SHA         - 알림 메시지에 포함
  GITHUB_SERVER_URL  - 링크 생성용

종료 코드:
  0 - verdict pass 또는 advisory
  1 - verdict block
  2 - 입력/설정 오류
"""

import argparse
import json
import os
import re
import subprocess
import sys
import urllib.request
import urllib.error
from pathlib import Path

LOCKFILE_PATTERNS = (
    "package-lock.json", "yarn.lock", "pnpm-lock.yaml", "uv.lock",
    "poetry.lock", "Cargo.lock", "pom.xml", "Gemfile.lock",
    "go.sum", "composer.lock",
)


def has_code_change(diff_content):
    """Diff에 코드 파일 변경이 포함되어 있는지.

    "코드 파일" = markdown(.md/.markdown), lockfile, 순수 binary가 아닌 모든 파일.
    SKILL.md v0.5 게이팅 표의 정의를 충실히 따른다.
    """
    files = re.findall(r'^diff --git a/\S+ b/(\S+)', diff_content, re.MULTILINE)
    for path in files:
        # markdown?
        if path.endswith(".md") or path.endswith(".markdown"):
            continue
        # lockfile?
        if any(path.endswith(p) for p in LOCKFILE_PATTERNS):
            continue
        # 그 외 → 코드 파일
        return True
    return False


def should_call_llm(deterministic_findings, diff_content):
    """SKILL.md 2단계 게이팅 규칙."""
    if not diff_content.strip():
        return False, "empty diff"
    severities = {f.get("severity") for f in deterministic_findings}
    if severities & {"critical", "high", "medium"}:
        return True, "deterministic findings present"
    if has_code_change(diff_content):
        return True, "code file change detected"
    return False, "markdown/lockfile only, no deterministic findings"


def post_slack(webhook_url, payload):
    """Slack webhook으로 페이로드 전송. 실패해도 워크플로우는 계속."""
    if not webhook_url:
        print("[slack] SLACK_WEBHOOK_URL 미설정 — 알림 생략", file=sys.stderr)
        print(f"[slack] would send: {json.dumps(payload, ensure_ascii=False)[:200]}", file=sys.stderr)
        return
    try:
        req = urllib.request.Request(
            webhook_url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            resp.read()
        print(f"[slack] sent ({payload.get('_kind', 'unknown')})", file=sys.stderr)
    except (urllib.error.URLError, urllib.error.HTTPError, OSError) as e:
        # 알림 실패가 워크플로우를 막아서는 안 됨
        print(f"[slack] error: {e}", file=sys.stderr)


def build_slack_message(result, kind):
    """Verdict별 Slack 메시지. kind: INFO/REVIEW/ALERT."""
    repo = os.environ.get("GITHUB_REPOSITORY", "unknown")
    sha = os.environ.get("GITHUB_SHA", "unknown")[:12]
    server = os.environ.get("GITHUB_SERVER_URL", "https://github.com")
    run_id = os.environ.get("GITHUB_RUN_ID", "")
    run_url = f"{server}/{repo}/actions/runs/{run_id}" if run_id else ""

    verdict = result["verdict"]
    summary = result.get("summary", "")
    findings = result.get("findings", [])

    # Verdict별 emoji + 색상
    emoji_by_kind = {"INFO": "ℹ️", "REVIEW": "⚠️", "ALERT": "🚨"}
    color_by_kind = {"INFO": "#36a64f", "REVIEW": "#daa520", "ALERT": "#cc0000"}

    # 상위 3 finding만 노출 (메시지 크기 제한)
    top_findings = sorted(
        findings,
        key=lambda f: ["critical", "high", "medium", "low"].index(
            f.get("severity", "low") if f.get("severity") in
            {"critical", "high", "medium", "low"} else "low"
        ),
    )[:3]
    finding_lines = []
    for f in top_findings:
        loc = f.get("location", "?")
        cat = f.get("category", "?")
        sev = f.get("severity", "?")
        desc = f.get("description", "")[:200]
        finding_lines.append(f"• [{sev}] {cat} @ {loc} — {desc}")

    omitted = max(0, len(findings) - len(top_findings))
    if omitted:
        finding_lines.append(f"… 외 {omitted}개 finding")

    text_lines = [
        f"{emoji_by_kind[kind]} *{kind}* — pre-merge review on `{repo}` @ `{sha}`",
        f"Verdict: `{verdict}` · Policy: {result.get('policy_version', '?')} ({result.get('policy_sha', '?')[:12]})",
        "",
        summary.replace("\n", " · "),
    ]
    if finding_lines:
        text_lines.append("")
        text_lines.extend(finding_lines)
    if run_url:
        text_lines.append("")
        text_lines.append(f"<{run_url}|workflow run>")

    return {
        "_kind": kind,
        "attachments": [{
            "color": color_by_kind[kind],
            "text": "\n".join(text_lines),
            "mrkdwn_in": ["text"],
        }],
    }


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--diff", required=True)
    p.add_argument("--policy-path", default=os.environ.get("POLICY_PATH", ".policy"))
    p.add_argument("--output", help="결과 JSON 저장 경로 (기본 stdout)")
    args = p.parse_args()

    policy_path = Path(args.policy_path)
    skill_dir = policy_path / "skills" / "pre-merge-review"
    checker = skill_dir / "checks" / "deterministic.sh"
    adapter = skill_dir / "checks" / "llm-adapter.py"
    system_prompt = skill_dir / "checks" / "llm-system-prompt.md"

    for f in (checker, adapter, system_prompt):
        if not f.exists():
            print(f"missing policy file: {f}", file=sys.stderr)
            sys.exit(2)

    # diff 로드
    try:
        diff_content = Path(args.diff).read_text()
    except OSError as e:
        print(f"diff read error: {e}", file=sys.stderr)
        sys.exit(2)

    # 1. 결정론 검사
    det_proc = subprocess.run(
        ["bash", str(checker), args.diff],
        capture_output=True, text=True,
    )
    if det_proc.returncode != 0:
        print(f"deterministic check failed: {det_proc.stderr}", file=sys.stderr)
        sys.exit(2)

    try:
        det_findings = json.loads(det_proc.stdout or "[]")
    except json.JSONDecodeError:
        det_findings = []

    det_path = Path("/tmp/det-findings.json")
    det_path.write_text(json.dumps(det_findings))

    # 2. 게이팅
    call_llm, gating_reason = should_call_llm(det_findings, diff_content)
    print(f"[gating] call_llm={call_llm} reason={gating_reason}", file=sys.stderr)

    # 3. 어댑터 호출
    adapter_cmd = [
        str(adapter),
        "--diff", args.diff,
        "--findings", str(det_path),
        "--context", "ci",
        "--policy-sha", os.environ.get("POLICY_SHA", "unknown"),
        "--policy-version", os.environ.get("POLICY_VERSION", "unknown"),
        "--system-prompt", str(system_prompt),
        "--base-ref", os.environ.get("BASE_REF", "HEAD"),
        "--incoming-ref", os.environ.get("INCOMING_REF", "FETCH_HEAD"),
    ]
    if not call_llm:
        adapter_cmd.append("--skip-llm")

    adapter_proc = subprocess.run(adapter_cmd, capture_output=True, text=True)
    # adapter는 verdict block일 때 exit 1, 그 외 0. stderr는 진단 정보.
    if adapter_proc.stderr:
        print(adapter_proc.stderr, file=sys.stderr)
    if adapter_proc.returncode not in (0, 1):
        print(f"adapter unexpected exit: {adapter_proc.returncode}", file=sys.stderr)
        sys.exit(2)

    try:
        result = json.loads(adapter_proc.stdout)
    except json.JSONDecodeError as e:
        print(f"adapter output parse error: {e}", file=sys.stderr)
        print(adapter_proc.stdout[:500], file=sys.stderr)
        sys.exit(2)

    # gating reason을 결과에 기록 (디버깅 추적성)
    result["_gating"] = {"call_llm": call_llm, "reason": gating_reason}

    # 4. 결과 출력
    output_text = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        Path(args.output).write_text(output_text)
    print(output_text)

    # 5. Slack 알림 디스패치
    webhook = os.environ.get("SLACK_WEBHOOK_URL", "")
    verdict = result["verdict"]
    if verdict == "block":
        post_slack(webhook, build_slack_message(result, "ALERT"))
    elif verdict == "advisory":
        # high/critical findings 있으면 REVIEW, 그 외 INFO
        has_high = any(f.get("severity") in {"critical", "high"} for f in result["findings"])
        kind = "REVIEW" if has_high else "INFO"
        post_slack(webhook, build_slack_message(result, kind))
    # pass는 알림 없음 (noise 방지)

    sys.exit(1 if verdict == "block" else 0)


if __name__ == "__main__":
    main()
