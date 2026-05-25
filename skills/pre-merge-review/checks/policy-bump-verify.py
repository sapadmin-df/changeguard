#!/usr/bin/env python3
"""
policy-bump-verify.py — POLICY_REPO_SHA bump의 자동 검증 (v0.14+)

`POLICY_REPO_SHA: "<sha>"` 변경이 diff에 있으면, public GitHub API로
*사람이 직접 확인하지 않아도 되는* 사실을 모두 자동 검증한다:

  1. exists           — 새 SHA가 실제 policy repo에 존재하는가
  2. verified         — commit이 서명되어 GitHub에 의해 검증됐는가
  3. reachable_from_main — 새 SHA가 main에서 도달 가능한가 (identical/behind)
  4. author/committer — 누가 만들었는가
  5. compare_url      — old → new 사이의 변경을 보는 GitHub compare 페이지

설계 원칙:
- 사람의 노동을 자동화하는 게 핵심 — "확인하라" 만 외치지 말고 *확인하라*.
- 검증 가능한 사실은 코드가, 의미·의도는 LLM이.
- public repo + GITHUB_TOKEN (Actions 환경) 만으로 충분, 추가 secret 불필요.
- 네트워크 실패 시 fail-open (해당 필드 null, 진단 메시지만 남김 — 게이트를
  멈추지 않음).

호출:
  python3 policy-bump-verify.py --diff <path> [--policy-repo owner/name]

출력: JSON to stdout. SHA bump이 *없으면* `{"bump_detected": false}` 반환.

종료 코드: 항상 0 (검증 자체가 게이트를 막지 않음 — caller가 결과를 사용).
"""

import argparse
import json
import os
import re
import sys
import urllib.request
import urllib.error
from pathlib import Path

SHA_RE = re.compile(r'[a-f0-9]{40}')
POLICY_REPO_DEFAULT = "sapadmin-df/changeguard"
HTTP_TIMEOUT_SEC = 10


def extract_bump(diff_content):
    """diff에서 POLICY_REPO_SHA 변경을 추출. (old, new) 또는 None."""
    m_old = re.search(r'^-\s*POLICY_REPO_SHA:\s*"([a-f0-9]{40})"', diff_content, re.MULTILINE)
    m_new = re.search(r'^\+\s*POLICY_REPO_SHA:\s*"([a-f0-9]{40})"', diff_content, re.MULTILINE)
    if m_old and m_new:
        return m_old.group(1), m_new.group(1)
    return None


def gh_api(path, token=None):
    """https://api.github.com{path} GET → JSON. (data, http_status) 반환. 실패시 (None, status)."""
    url = f"https://api.github.com{path}"
    req = urllib.request.Request(url, headers={
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "changeguard-policy-bump-verify",
    })
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_SEC) as resp:
            return json.loads(resp.read()), resp.status
    except urllib.error.HTTPError as e:
        return None, e.code
    except (urllib.error.URLError, OSError, json.JSONDecodeError) as e:
        print(f"[verify] api error {url}: {e}", file=sys.stderr)
        return None, 0


def verify(policy_repo, old_sha, new_sha, token):
    """4종 검증 + URL 생성. 부분 실패 허용."""
    result = {
        "bump_detected": True,
        "policy_repo": policy_repo,
        "old_sha": old_sha,
        "new_sha": new_sha,
        "old_sha_short": old_sha[:12],
        "new_sha_short": new_sha[:12],
        "exists": None,
        "verified": None,
        "verification_reason": None,
        "reachable_from_main": None,
        "ancestor_status": None,
        "author_login": None,
        "author_email_domain": None,
        "committer_login": None,
        "message_first_line": None,
        "commit_url": f"https://github.com/{policy_repo}/commit/{new_sha}",
        "compare_url": f"https://github.com/{policy_repo}/compare/{old_sha}...{new_sha}",
        "overall": "unknown",   # trusted / unverified / suspicious / unknown
        "verdict_reasons": [],
    }

    # 1) commits/{new_sha} — exists + verification + author/committer
    data, status = gh_api(f"/repos/{policy_repo}/commits/{new_sha}", token)
    if status == 200 and isinstance(data, dict):
        result["exists"] = True
        v = (data.get("commit") or {}).get("verification") or {}
        result["verified"] = bool(v.get("verified"))
        result["verification_reason"] = v.get("reason")
        result["author_login"] = (data.get("author") or {}).get("login")
        author_email = ((data.get("commit") or {}).get("author") or {}).get("email") or ""
        # email full 노출 회피 — 도메인만
        result["author_email_domain"] = author_email.split("@", 1)[-1] if "@" in author_email else None
        result["committer_login"] = (data.get("committer") or {}).get("login")
        msg = ((data.get("commit") or {}).get("message") or "").split("\n", 1)[0]
        result["message_first_line"] = msg[:200]
    elif status in (404, 422):
        result["exists"] = False
        result["verdict_reasons"].append(f"new SHA 존재하지 않음 ({status})")
    # else: 네트워크 실패 — exists=None 그대로

    # 2) compare main...new_sha — main에서 도달 가능?
    cmp_data, cmp_status = gh_api(f"/repos/{policy_repo}/compare/main...{new_sha}", token)
    if cmp_status == 200 and isinstance(cmp_data, dict):
        ancestor_status = cmp_data.get("status")  # ahead/behind/identical/diverged
        result["ancestor_status"] = ancestor_status
        # identical = new_sha == main HEAD; behind = main이 new_sha보다 앞 (new_sha는 main의 조상)
        # 둘 다 main에서 도달 가능 = 신뢰
        result["reachable_from_main"] = ancestor_status in ("identical", "behind")
        if ancestor_status in ("ahead", "diverged"):
            result["verdict_reasons"].append(f"main에서 도달 불가 (status={ancestor_status})")
    # else: None 유지

    # overall 종합 판정
    if result["exists"] is False:
        result["overall"] = "suspicious"
    elif result["exists"] and result["reachable_from_main"] and result["verified"]:
        result["overall"] = "trusted"
    elif result["exists"] and result["reachable_from_main"] and not result["verified"]:
        result["overall"] = "unverified"   # main 도달 가능하지만 commit 서명 없음
        result["verdict_reasons"].append(f"서명 미검증 (reason={result['verification_reason']})")
    elif result["exists"] and result["reachable_from_main"] is False:
        result["overall"] = "suspicious"
    # else: 모두 None — overall=unknown

    return result


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--diff", required=True)
    p.add_argument("--policy-repo", default=os.environ.get("POLICY_REPO", POLICY_REPO_DEFAULT))
    args = p.parse_args()

    try:
        diff_content = Path(args.diff).read_text()
    except OSError as e:
        print(json.dumps({"bump_detected": False, "error": f"diff read: {e}"}))
        sys.exit(0)

    bump = extract_bump(diff_content)
    if not bump:
        print(json.dumps({"bump_detected": False}))
        sys.exit(0)

    old_sha, new_sha = bump
    token = os.environ.get("GITHUB_TOKEN", "")
    result = verify(args.policy_repo, old_sha, new_sha, token)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    sys.exit(0)


if __name__ == "__main__":
    main()
