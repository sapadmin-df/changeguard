#!/usr/bin/env python3
"""
ci-caller.py — Actions workflow에서 호출하는 통합 caller (v0.10+)

역할:
1. SKILL.md v0.5 gating 규칙 집행 (변경 카테고리 기반 LLM 호출 결정)
2. llm-adapter.py 호출 (LLM 호출이 필요한 경우)
3. verdict별 Slack 알림 디스패치 (INFO/REVIEW/ALERT) — 식별자에 GitHub 링크 자동 포함
4. exit code로 워크플로우 결과 전달
5. (v0.10) 정책 SHA가 upstream floor 미만이면 즉시 deprecation 차단 + 갱신 PR 안내

게이팅 규칙 (SKILL.md 2단계 표) — 변경 없음:
- 결정론 critical/high/medium 있음 → LLM 호출
- 결정론 0 + 코드 파일 변경 있음 → LLM 호출
- 결정론 0 + markdown/lockfile만 변경 → skip
- diff 비어있음 → skip

이 caller는 워크플로우의 단일 책임 진입점이다. workflow YAML은 이것만 호출하고
복잡한 로직은 여기 모인다. 변경/테스트가 fixture corpus로 가능.

환경변수:
  POLICY_PATH        - 정책 repo가 checkout된 경로 (기본 ".policy")
  POLICY_VERSION     - VERSION 파일 내용
  POLICY_SHA         - 정책 repo의 commit SHA (deprecation 비교에 사용)
  POLICY_REPO        - 정책 repo의 owner/name (기본 "sapadmin-df/changeguard")
  ANTHROPIC_API_KEY  - LLM 호출용
  SLACK_WEBHOOK_URL  - 알림용
  GITHUB_REPOSITORY  - 알림 메시지에 포함
  GITHUB_SHA         - 알림 메시지에 포함 (대상 repo commit SHA)
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


# ===== Smart link helpers (Slack mrkdwn 형식) =====

def _gh_server():
    return os.environ.get("GITHUB_SERVER_URL", "https://github.com")


def _policy_repo():
    return os.environ.get("POLICY_REPO", POLICY_REPO_DEFAULT)


def _slk(url, text):
    """Slack mrkdwn link: <url|text>"""
    return f"<{url}|{text}>"


def _link_repo(repo):
    return _slk(f"{_gh_server()}/{repo}", repo)


def _link_commit(repo, sha, short=12):
    if not sha or sha == "unknown":
        return "`unknown`"
    return _slk(f"{_gh_server()}/{repo}/commit/{sha}", f"`{sha[:short]}`")


def _link_file(repo, sha, location):
    """location: 'path' | 'path:42' | 'path:42-50' → 라인 anchor 포함 linked file."""
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


# ===== Bump PR lookup (스마트 통합: deprecation 알림에 첨부) =====

def find_open_bump_pr():
    """consumer repo에 열려있는 policy-bump PR 1건 검색.
    매칭 기준: label='policy-bump' 또는 title prefix 'ci: bump changeguard policy SHA'.
    Returns (number, html_url) or None.
    """
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


# ===== Deprecation check (upstream floor 대비 현재 pin 검증) =====

def fetch_upstream_floor(policy_repo):
    """changeguard upstream main의 min-supported.txt에서 floor SHA 추출. None on failure."""
    try:
        url = f"https://raw.githubusercontent.com/{policy_repo}/main/min-supported.txt"
        req = urllib.request.Request(url, headers={"User-Agent": "changeguard-ci-caller"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            content = resp.read().decode("utf-8")
        for line in content.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            m = re.search(r"\b[a-f0-9]{40}\b", line)
            if m:
                return m.group(0)
    except Exception as e:
        print(f"[deprecation] upstream fetch error: {e}", file=sys.stderr)
    return None


def compare_sha(policy_repo, base, head):
    """GitHub compare API. 'ahead'/'behind'/'identical'/'diverged' or None."""
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
    """Returns (deprecated: bool, floor_sha: Optional[str]).
    fail-open: 네트워크/응답 실패시 deprecated=False (게이트는 정상 진행).
    """
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
    """DEPRECATED 전용 Slack 메시지 (회색 sidebar)."""
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


# ===== Gating logic (변경 없음) =====

def has_code_change(diff_content):
    """Diff에 코드 파일 변경이 포함되어 있는지.

    "코드 파일" = markdown(.md/.markdown), lockfile, 순수 binary가 아닌 모든 파일.
    SKILL.md v0.5 게이팅 표의 정의를 충실히 따른다.
    """
    files = re.findall(r'^diff --git a/\S+ b/(\S+)', diff_content, re.MULTILINE)
    for path in files:
        if path.endswith(".md") or path.endswith(".markdown"):
            continue
        if any(path.endswith(p) for p in LOCKFILE_PATTERNS):
            continue
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


# ===== Slack send & message builders =====

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
        print(f"[slack] error: {e}", file=sys.stderr)


def build_slack_message(result, kind):
    """Verdict별 Slack 메시지. 모든 식별자에 GitHub 링크 (mrkdwn `<url|text>`).

    Linked: repo, commit SHA, finding location(파일:라인 범위), policy version/SHA, workflow run.
    """
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

    text_lines = [
        f"{emoji_by_kind[kind]} *{kind}* — pre-merge review on {repo_link} @ {sha_link}",
        f"Verdict: `{verdict}` · Policy: {policy_ver_link} ({policy_sha_link})",
        "",
        summary.replace("\n", " · "),
    ]
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

    # 0. Deprecation 체크 (정책 SHA가 upstream floor 아래로 떨어지면 즉시 차단)
    #    fail-open: 네트워크 실패·POLICY_SHA 미설정시 통과 (이 검사가 게이트를 부수지 않도록)
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
        has_high = any(f.get("severity") in {"critical", "high"} for f in result["findings"])
        kind = "REVIEW" if has_high else "INFO"
        post_slack(webhook, build_slack_message(result, kind))
    # pass는 알림 없음 (noise 방지)

    sys.exit(1 if verdict == "block" else 0)


if __name__ == "__main__":
    main()
