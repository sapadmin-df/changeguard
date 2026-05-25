#!/usr/bin/env python3
"""
ci-caller.py — Actions workflow에서 호출하는 통합 caller (v0.11+)

역할:
1. SKILL.md v0.5 gating 규칙 집행 (변경 카테고리 기반 LLM 호출 결정)
2. llm-adapter.py 호출 (LLM 호출이 필요한 경우)
3. verdict별 Slack 알림 디스패치 (INFO/REVIEW/ALERT) — 식별자에 GitHub 링크 자동 포함
4. exit code로 워크플로우 결과 전달
5. (v0.10+) 정책 SHA가 upstream floor 미만이면 deprecation 차단 + 갱신 PR 안내
6. (v0.11+) `.github/workflows/` 변경이 40-hex SHA-only swap이면 critical → low 강등
            (bump PR 자기-루프 해소). 다른 변경은 여전히 critical.
7. (v0.11+) master push에서 POLICY_REPO_SHA 변경 감지 시 POLICY_UPDATED informational
            Slack 알림 (compare URL 포함).

게이팅 규칙 (SKILL.md 2단계 표) — 변경 없음:
- 결정론 critical/high/medium 있음 → LLM 호출
- 결정론 0 + 코드 파일 변경 있음 → LLM 호출
- 결정론 0 + markdown/lockfile만 변경 → skip
- diff 비어있음 → skip

환경변수:
  POLICY_PATH        - 정책 repo가 checkout된 경로 (기본 ".policy")
  POLICY_VERSION     - VERSION 파일 내용
  POLICY_SHA         - 정책 repo의 commit SHA (deprecation 비교에 사용)
  POLICY_REPO        - 정책 repo의 owner/name (기본 "sapadmin-df/changeguard")
  ANTHROPIC_API_KEY  - LLM 호출용
  SLACK_WEBHOOK_URL  - 알림용
  GITHUB_REPOSITORY  - 알림 메시지에 포함
  GITHUB_SHA         - 알림 메시지에 포함 (대상 repo commit SHA)
  GITHUB_EVENT_NAME  - push/pull_request/workflow_dispatch — POLICY_UPDATED 게이팅에 사용
  GITHUB_RUN_ID      - workflow run 링크
  GITHUB_SERVER_URL  - 링크 도메인 (기본 https://github.com)
  GITHUB_TOKEN       - bump PR 검색용 (없으면 검색 생략, fail-open)

종료 코드:
  0 - verdict pass 또는 advisory
  1 - verdict block 또는 deprecation
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

POLICY_REPO_DEFAULT = "sapadmin-df/changeguard"
SHA_RE = re.compile(r'\b[a-f0-9]{40}\b')


# ===== Smart link helpers (Slack mrkdwn 형식) =====

def _gh_server():
    return os.environ.get("GITHUB_SERVER_URL", "https://github.com")


def _policy_repo():
    return os.environ.get("POLICY_REPO", POLICY_REPO_DEFAULT)


def _slk(url, text):
    return f"<{url}|{text}>"


def _link_repo(repo):
    return _slk(f"{_gh_server()}/{repo}", repo)


def _link_commit(repo, sha, short=12):
    if not sha or sha == "unknown":
        return "`unknown`"
    return _slk(f"{_gh_server()}/{repo}/commit/{sha}", f"`{sha[:short]}`")


def _link_file(repo, sha, location):
    if not location or not sha:
        return f"`{location or '?'}`"
    m = re.match(r'^([^:]+?)(?::(\d+)(?:-(\d+))?)?$', location)
    if not m:
        return f"`{location}`"
    path, l1, l2 = m.groups()
    url = f"{_gh_server()}/{repo}/blob/{sha}/{path}"
    if l1:
        url += f"#L{l1}"
        if l2:
            url += f"-L{l2}"
    return _slk(url, f"`{location}`")


def _link_run(repo, run_id):
    if not run_id:
        return ""
    return _slk(f"{_gh_server()}/{repo}/actions/runs/{run_id}", "workflow run")


def _link_pr(html_url, number):
    return _slk(html_url, f"#{number}")


def _link_policy_version(policy_repo, policy_sha, version):
    if not policy_sha:
        return f"`{version}`"
    return _slk(f"{_gh_server()}/{policy_repo}/blob/{policy_sha}/VERSION", f"`{version}`")


# ===== Bump PR lookup =====

def find_open_bump_pr():
    token = os.environ.get("GITHUB_TOKEN", "")
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    if not token or not repo:
        return None
    try:
        req = urllib.request.Request(
            f"https://api.github.com/repos/{repo}/pulls?state=open&per_page=30",
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "changeguard-ci-caller",
            },
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            pulls = json.loads(resp.read())
        for pr in pulls:
            labels = {l.get("name", "") for l in pr.get("labels") or []}
            title = pr.get("title", "") or ""
            if "policy-bump" in labels or title.startswith("ci: bump changeguard policy SHA"):
                return pr.get("number"), pr.get("html_url")
    except Exception as e:
        print(f"[bump-pr-lookup] {e}", file=sys.stderr)
    return None


# ===== Deprecation check =====

def fetch_upstream_floor(policy_repo):
    try:
        url = f"https://raw.githubusercontent.com/{policy_repo}/main/min-supported.txt"
        req = urllib.request.Request(url, headers={"User-Agent": "changeguard-ci-caller"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            content = resp.read().decode("utf-8")
        for line in content.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            m = SHA_RE.search(line)
            if m:
                return m.group(0)
    except Exception as e:
        print(f"[deprecation] upstream fetch error: {e}", file=sys.stderr)
    return None


def compare_sha(policy_repo, base, head):
    try:
        url = f"https://api.github.com/repos/{policy_repo}/compare/{base}...{head}"
        req = urllib.request.Request(url, headers={
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "changeguard-ci-caller",
        })
        token = os.environ.get("GITHUB_TOKEN", "")
        if token:
            req.add_header("Authorization", f"Bearer {token}")
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
        return data.get("status")
    except Exception as e:
        print(f"[deprecation] compare error: {e}", file=sys.stderr)
    return None


def check_deprecation(policy_repo, current_policy_sha):
    if not current_policy_sha or current_policy_sha == "unknown":
        return False, None
    floor = fetch_upstream_floor(policy_repo)
    if not floor:
        print("[deprecation] no upstream floor — skipped (fail-open)", file=sys.stderr)
        return False, None
    if floor == current_policy_sha:
        return False, floor
    status = compare_sha(policy_repo, floor, current_policy_sha)
    if status in ("identical", "ahead"):
        return False, floor
    if status in ("behind", "diverged"):
        return True, floor
    print(f"[deprecation] compare status indeterminate (={status}) — fail-open", file=sys.stderr)
    return False, floor


def build_deprecation_message(current_sha, floor_sha, bump_pr):
    repo = os.environ.get("GITHUB_REPOSITORY", "unknown")
    run_id = os.environ.get("GITHUB_RUN_ID", "")
    policy_repo = _policy_repo()
    server = _gh_server()

    current_link = _link_commit(policy_repo, current_sha)
    floor_link = _link_commit(policy_repo, floor_sha)
    diff_link = _slk(
        f"{server}/{policy_repo}/compare/{current_sha}...{floor_sha}",
        "정책 diff",
    )
    repo_link = _link_repo(repo)
    run_link = _link_run(repo, run_id)

    lines = [
        f"⛔ *DEPRECATED POLICY* — {repo_link}",
        f"현재 pin: {current_link} · 최소 허용: {floor_link} ({diff_link})",
        "",
        "이 정책 SHA는 deprecated되었습니다. `POLICY_REPO_SHA` 갱신 필수.",
    ]
    if bump_pr:
        number, html_url = bump_pr
        lines.append(f"✅ 적용 가능한 갱신 PR: {_link_pr(html_url, number)} — 검토 후 merge하면 해소됩니다.")
    else:
        lines.append("⏳ 자동 갱신 PR이 아직 없습니다. `policy-bump-watcher` 다음 실행에서 생성 예정.")
    if run_link:
        lines.append("")
        lines.append(run_link)

    return {
        "_kind": "DEPRECATED",
        "attachments": [{
            "color": "#4a4a4a",
            "text": "\n".join(lines),
            "mrkdwn_in": ["text"],
        }],
    }


# ===== v0.11+ SHA-only swap downgrade (bump PR 자기-루프 해소) =====

def _extract_file_diff_lines(diff_content, file_path):
    """diff에서 file_path 섹션의 (-, +) 라인 쌍 추출. Returns (removed, added) 또는 ([], [])."""
    removed, added = [], []
    in_file = False
    in_hunk = False
    for line in diff_content.splitlines():
        if line.startswith('diff --git'):
            in_file = (f' b/{file_path}' in line) or line.endswith(f' b/{file_path}')
            in_hunk = False
            continue
        if not in_file:
            continue
        if line.startswith('@@'):
            in_hunk = True
            continue
        if not in_hunk:
            continue
        # skip --- /  +++ header lines just in case
        if line.startswith('---') or line.startswith('+++'):
            continue
        if line.startswith('-') and len(line) >= 1:
            removed.append(line[1:])
        elif line.startswith('+') and len(line) >= 1:
            added.append(line[1:])
    return removed, added


def is_sha_only_swap(diff_content, file_path):
    """file_path의 변경이 40-hex SHA 값 교체만으로 구성되는지.
    조건: -/+ 라인 수 동일, SHA만 placeholder로 치환했을 때 원본 동일, 실제로 변경 존재.
    """
    removed, added = _extract_file_diff_lines(diff_content, file_path)
    if not removed or len(removed) != len(added):
        return False
    if removed == added:
        return False
    for r, a in zip(removed, added):
        if SHA_RE.sub('SHA', r) != SHA_RE.sub('SHA', a):
            return False
    return True


def downgrade_sha_only_workflow_findings(diff_content, findings):
    """결정론 workflow critical/high/medium 중 SHA-only-swap 케이스를 low로 강등.
    다른 변경 패턴은 강등 없음.
    """
    out = []
    for f in findings:
        is_target = (
            f.get('source') == 'deterministic'
            and f.get('category') == 'workflow'
            and f.get('severity') in ('critical', 'high', 'medium')
        )
        if is_target:
            loc = f.get('location') or ''
            # location may have line range like "path:42-50" — strip to file path
            file_path = loc.split(':', 1)[0] if loc else ''
            if file_path and is_sha_only_swap(diff_content, file_path):
                f = dict(f)
                original = f.get('severity', '?')
                f['severity'] = 'low'
                f['description'] = (
                    f"[SHA-only swap downgrade: {original}→low] "
                    + (f.get('description') or '')
                    + " — 워크플로우 파일의 모든 변경이 40-hex commit SHA 값 교체. 새 SHA 대상의 신뢰성은 별도 검토 필요."
                )
                f['downgraded_from'] = original
        out.append(f)
    return out


# ===== v0.11+ POLICY_REPO_SHA 변경 감지 → POLICY_UPDATED 알림 =====

def diff_contains_policy_bump(diff_content):
    """diff에 POLICY_REPO_SHA 값 변경이 있으면 (old_sha, new_sha) 반환, 아니면 None."""
    SHA_PAT = r'[a-f0-9]{40}'
    m_old = re.search(r'^-\s*POLICY_REPO_SHA:\s*"(' + SHA_PAT + r')"', diff_content, re.MULTILINE)
    m_new = re.search(r'^\+\s*POLICY_REPO_SHA:\s*"(' + SHA_PAT + r')"', diff_content, re.MULTILINE)
    if m_old and m_new:
        return m_old.group(1), m_new.group(1)
    return None


def build_policy_updated_message(old_sha, new_sha):
    """POLICY_UPDATED — bump이 master에 반영됐을 때 informational 알림."""
    repo = os.environ.get("GITHUB_REPOSITORY", "unknown")
    run_id = os.environ.get("GITHUB_RUN_ID", "")
    policy_repo = _policy_repo()
    server = _gh_server()

    old_link = _link_commit(policy_repo, old_sha)
    new_link = _link_commit(policy_repo, new_sha)
    compare_link = _slk(
        f"{server}/{policy_repo}/compare/{old_sha}...{new_sha}",
        "정책 변경 diff (GitHub compare)",
    )
    repo_link = _link_repo(repo)
    run_link = _link_run(repo, run_id)

    lines = [
        f"🔄 *POLICY UPDATED* — {repo_link}",
        f"정책 SHA: {old_link} → {new_link}",
        f"{compare_link}",
    ]
    if run_link:
        lines.append("")
        lines.append(run_link)

    return {
        "_kind": "POLICY_UPDATED",
        "attachments": [{
            "color": "#1e7eb6",
            "text": "\n".join(lines),
            "mrkdwn_in": ["text"],
        }],
    }


# ===== Gating logic (변경 없음) =====

def has_code_change(diff_content):
    files = re.findall(r'^diff --git a/\S+ b/(\S+)', diff_content, re.MULTILINE)
    for path in files:
        if path.endswith(".md") or path.endswith(".markdown"):
            continue
        if any(path.endswith(p) for p in LOCKFILE_PATTERNS):
            continue
        return True
    return False


def should_call_llm(deterministic_findings, diff_content):
    if not diff_content.strip():
        return False, "empty diff"
    severities = {f.get("severity") for f in deterministic_findings}
    if severities & {"critical", "high", "medium"}:
        return True, "deterministic findings present"
    if has_code_change(diff_content):
        return True, "code file change detected"
    return False, "markdown/lockfile only, no deterministic findings"


# ===== Slack send & message builders =====

def post_slack(webhook_url, payload):
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
        print(f"[slack] error: {e}", file=sys.stderr)


def build_slack_message(result, kind):
    repo = os.environ.get("GITHUB_REPOSITORY", "unknown")
    sha_full = os.environ.get("GITHUB_SHA", "")
    run_id = os.environ.get("GITHUB_RUN_ID", "")
    policy_repo = _policy_repo()

    verdict = result["verdict"]
    summary = result.get("summary", "")
    findings = result.get("findings", [])
    policy_version = result.get("policy_version", "?")
    policy_sha = result.get("policy_sha", "")

    repo_link = _link_repo(repo)
    sha_link = _link_commit(repo, sha_full)
    policy_sha_link = _link_commit(policy_repo, policy_sha)
    policy_ver_link = _link_policy_version(policy_repo, policy_sha, policy_version)
    run_link = _link_run(repo, run_id)

    emoji_by_kind = {"INFO": "ℹ️", "REVIEW": "⚠️", "ALERT": "🚨"}
    color_by_kind = {"INFO": "#36a64f", "REVIEW": "#daa520", "ALERT": "#cc0000"}

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
        loc_link = _link_file(repo, sha_full, loc) if sha_full else f"`{loc}`"
        cat = f.get("category", "?")
        sev = f.get("severity", "?")
        desc = (f.get("description") or "")[:200]
        finding_lines.append(f"• [{sev}] `{cat}` @ {loc_link} — {desc}")

    omitted = max(0, len(findings) - len(top_findings))
    if omitted:
        finding_lines.append(f"… 외 {omitted}개 finding")

    # v0.13+: assessment 가 있으면 intent 배지를 verdict 라인 옆에 노출
    intent_badge = ""
    intent_label_map = {
        "intentional": "✓ 의도된 변경으로 판단",
        "suspicious":  "⚠ 의심 단서 존재",
        "unclear":     "? 판단 보류",
    }
    assessment = result.get("assessment")
    if assessment and assessment.get("intent"):
        intent_label = intent_label_map.get(assessment["intent"], assessment["intent"])
        intent_badge = f" · LLM: {intent_label}"

    text_lines = [
        f"{emoji_by_kind[kind]} *{kind}* — pre-merge review on {repo_link} @ {sha_link}",
        f"Verdict: `{verdict}` · Policy: {policy_ver_link} ({policy_sha_link}){intent_badge}",
        "",
        summary.replace("\n", " · "),
    ]

    # v0.13+: LLM 분석 narrative 섹션 (양치기 소년 회피)
    if assessment and assessment.get("rationale"):
        text_lines.append("")
        text_lines.append(f"*LLM 분석*: {assessment['rationale']}")
        if assessment.get("reviewer_focus"):
            text_lines.append("*리뷰자 주의*:")
            for f in assessment["reviewer_focus"]:
                text_lines.append(f"  • {f}")

    if finding_lines:
        text_lines.append("")
        text_lines.extend(finding_lines)
    if run_link:
        text_lines.append("")
        text_lines.append(run_link)

    return {
        "_kind": kind,
        "attachments": [{
            "color": color_by_kind[kind],
            "text": "\n".join(text_lines),
            "mrkdwn_in": ["text"],
        }],
    }


# ===== Main =====

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

    # 0. Deprecation 체크 (fail-open)
    current_policy_sha = os.environ.get("POLICY_SHA", "")
    if current_policy_sha and current_policy_sha != "unknown":
        deprecated, floor = check_deprecation(_policy_repo(), current_policy_sha)
        if deprecated:
            bump_pr = find_open_bump_pr()
            post_slack(
                os.environ.get("SLACK_WEBHOOK_URL", ""),
                build_deprecation_message(current_policy_sha, floor, bump_pr),
            )
            print(f"::error::POLICY DEPRECATED — pinned {current_policy_sha[:12]} below floor {floor[:12]}",
                  file=sys.stderr)
            sys.exit(1)

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

    # 1.5. SHA-only swap 강등 (v0.11+) — bump PR 자기-루프 해소
    pre_count = sum(1 for f in det_findings if f.get('severity') == 'critical')
    det_findings = downgrade_sha_only_workflow_findings(diff_content, det_findings)
    post_count = sum(1 for f in det_findings if f.get('severity') == 'critical')
    if pre_count > post_count:
        print(f"[sha-only-swap] downgraded {pre_count - post_count} critical workflow finding(s) to low",
              file=sys.stderr)

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
        has_high = any(f.get("severity") in {"critical", "high"} for f in result["findings"])
        kind = "REVIEW" if has_high else "INFO"
        post_slack(webhook, build_slack_message(result, kind))
    # pass는 알림 없음 (noise 방지)

    # 5.5. POLICY_UPDATED informational 알림 (v0.11+)
    #      master push 이벤트에서 POLICY_REPO_SHA 변경이 감지되면 추가 발송.
    event_name = os.environ.get("GITHUB_EVENT_NAME", "")
    if event_name == "push":
        bump = diff_contains_policy_bump(diff_content)
        if bump:
            old_sha, new_sha = bump
            post_slack(webhook, build_policy_updated_message(old_sha, new_sha))

    sys.exit(1 if verdict == "block" else 0)


if __name__ == "__main__":
    main()
