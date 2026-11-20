#!/usr/bin/env python3
"""
telemetry.py — 분석이 필요한 실행을 telemetry repo의 issue로 연결 (opt-in, v0.18+)

기본값은 꺼짐. 소비자가 reusable workflow 입력으로 명시적으로 켤 때만 동작한다:
  telemetry: off | minimal | full   (기본 off)
  telemetry_repo: owner/name         (기본 sapadmin-df/changeguard — 공개 공유 지점)
  telemetry_share_private: bool      (기본 false — private 소비자 소유자의 명시적 동의)
  secret CHANGEGUARD_TELEMETRY_TOKEN  (telemetry repo에 **아무 권한도 없는** 계정의 토큰.
                                       공개 repo라 issue·댓글은 누구나 쓸 수 있다 — REQ-C.3-5)

권한 없는 계정은 라벨을 붙일 수 없으므로 라벨에 의존하지 않는다: issue는 제목 접두사
`[cg-telemetry]` 와 본문 `cg-key` 로 식별하고, verdict·signal은 레코드에서 읽는다.
같은 키의 기존 issue는 **이 토큰의 작성자가 연 것만** 이어 쓴다 (남이 같은 키로 연 issue에
레코드가 끼어들지 않게). 라벨(cg-telemetry·fb:*)은 관리자가 붙이는 신뢰 신호다.

공개·비밀 경계는 docs/REQUIREMENTS.md REQ-C.3이 SSOT다. 이 파일의 prepare()가 그 구현:
  C.3-2  private 소스 → 공개 대상: minimal 강제. 소유자 동의(share_private) 시에만 full.
  C.3-3  공개 대상이면 high/critical finding의 경로·라인·문구와 LLM 서술을 빼고 규칙만 남김
         (아직 고치지 않은 취약점 상세를 공개 지점에 모으지 않음). 원본은 소비자 artifact에 있음.
  C.3-4  diff 유래 문구는 길이 제한, 표에서는 code span 안에만, 레코드는 깨지지 않는 fence 안에.

보내는 것: normalize.py 정규화 레코드 (signals 가 있는 실행만 — 판정 차단·advisory,
LLM이 정적보다 엄격, 정적 오탐 후보, LLM 미가용·거절·잘림·파싱 실패, self-lint).

이슈 단위: PR 실행은 PR마다 한 이슈, push 실행은 커밋마다 한 이슈. 같은 키의 다음
실행은 같은 이슈에 댓글로 레코드를 덧붙인다 (재실행·후속 커밋 추적).

minimal 에도 repo 이름과 run 링크는 들어간다.

실패해도 게이트에 영향 없음 (항상 exit 0, 경고만).
"""

import argparse
import json
import os
import re
import sys
import urllib.request
from pathlib import Path

API = "https://api.github.com"
DEFAULT_REPO = "sapadmin-df/changeguard"
CELL_MAX = 120          # 표 칸 문구 상한
TEXT_MAX = 300          # 레코드 안 diff 유래 문구(description 등) 상한
HIGH_PLUS = {"high", "critical"}
TITLE_PREFIX = "[cg-telemetry]"
FEEDBACK_HELP = (
    "### 피드백 (사후 분석 루프에 반영됨)\n"
    "- 이 실행 전체 판정에 대해 라벨 (관리자): `fb:true-positive` · `fb:false-positive` · `fb:noise` · "
    "`fb:missed` · `fb:llm-too-strict` · `fb:llm-too-lenient`\n"
    "- finding 하나에 대해 댓글: `/cg fp <finding_id> 메모` (`tp|fp|noise|missed`) — "
    "repo 권한자(OWNER·MEMBER·COLLABORATOR)의 댓글만 집계된다\n"
    "- 집계: `python3 scripts/cg-insights.py --issues <telemetry repo> --trust <작성 계정>`\n"
)


def api(method, path, token, body=None):
    req = urllib.request.Request(
        f"{API}{path}", method=method,
        data=json.dumps(body).encode("utf-8") if body is not None else None,
        headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
                 "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "changeguard-telemetry"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        raw = resp.read()
        return json.loads(raw) if raw else None


def visibility(repo, token):
    try:
        return "private" if api("GET", f"/repos/{repo}", token).get("private") else "public"
    except Exception:
        return "unknown"


def issue_key(record):
    run = record.get("run") or {}
    if run.get("pr_number"):
        return f"{run.get('repo')}#pr-{run['pr_number']}"
    return f"{run.get('repo')}@{(run.get('head_sha') or '')[:12]}"


def summary_md(record):
    run = record.get("run") or {}
    llm = record.get("llm") or {}
    lines = [
        f"**{run.get('repo')}** · {run.get('event')} · verdict `{record.get('verdict')}` "
        f"(driver: {record.get('verdict_driver')}) · policy `{(record.get('policy') or {}).get('version')}`",
        "",
        f"- run: {run.get('run_url') or '-'}",
        f"- signals: {', '.join(record.get('signals', [])) or '-'}",
        f"- 정적/LLM 최고 심각도: {record['severity_max'].get('deterministic')} / "
        f"{record['severity_max'].get('llm')} ({record.get('disagreement')})",
        f"- LLM: {llm.get('state')} {llm.get('model') or ''} {llm.get('failure_class') or ''} "
        f"intent={llm.get('intent') or '-'}",
        "",
        "| id | rule | severity | path_class | 위치 |",
        "|---|---|---|---|---|",
    ]
    for f in record.get("findings", [])[:30]:
        if f.get("redacted"):
            loc = "(비공개: high+)"
        elif f.get("path"):
            loc = code_span(f"{f.get('path')}:{f.get('line') or ''}".rstrip(":"))
        else:
            loc = "-"
        lines.append(f"| `{f['id']}` | `{f['rule']}` | {f['severity']} | {f['path_class']} | {loc} |")
    return "\n".join(lines)


def code_span(text, limit=CELL_MAX):
    """diff 유래 문구를 표 칸에 안전하게: 백틱·파이프·개행 제거, 길이 제한, code span 안에만
    (@멘션·링크·HTML이 렌더링되지 않게)."""
    t = str(text).replace("`", "'").replace("|", "/").replace("\r", " ").replace("\n", " ")
    if len(t) > limit:
        t = t[:limit - 1] + "…"
    return f"`{t}`"


def record_block(record):
    body = json.dumps(record, ensure_ascii=False, indent=1)
    longest = max((len(m) for m in re.findall(r"`+", body)), default=0)
    fence = "`" * max(3, longest + 1)          # 내용 속 백틱이 fence를 닫지 못하게
    return f"{fence}json changeguard-record\n{body}\n{fence}"


def find_issue(repo, token, key):
    """같은 키로 **이 토큰의 계정이 연** issue만 찾는다 (author:@me). 남이 같은 키로 연
    issue에 레코드를 덧붙이는 하이재킹을 막는다. 검색 실패 시 새 issue (중복은 감수)."""
    q = urllib.request.quote(f'repo:{repo} is:issue author:@me in:title "{TITLE_PREFIX}" '
                             f'"cg-key: {key}" in:body')
    try:
        items = api("GET", f"/search/issues?q={q}&per_page=5", token).get("items") or []
        # 키 끝 경계까지 일치 (pr-9 가 pr-90 issue에 붙지 않게)
        exact = re.compile(rf"<!-- cg-key: {re.escape(key)} -->")
        for it in items:
            if exact.search(it.get("body") or ""):
                return it["number"]
        return None
    except Exception:
        return None


def prepare(record, level_requested, source_visibility, target_visibility, share_private=False):
    """REQ-C.3 경계를 적용한 (record, level, notes). 네트워크 없음 — 단위 테스트 대상."""
    level = level_requested
    notes = []
    target_public = target_visibility != "private"     # unknown 은 공개로 간주 (보수적)
    if level == "full" and source_visibility != "public" and target_public:
        if share_private:
            notes.append(f"소비자 repo({source_visibility})의 소유자가 공개 공유에 동의함 "
                         f"(telemetry_share_private) — full 전송.")
        else:
            level = "minimal"
            notes.append(f"level=full 요청이었으나 소비자 repo({source_visibility})보다 telemetry "
                         f"repo({target_visibility})가 넓게 공개되어 minimal로 낮춤.")
    r = minimal(record) if level == "minimal" else json.loads(json.dumps(record))
    if level == "full":
        for f in r.get("findings", []):
            if isinstance(f.get("description"), str) and len(f["description"]) > TEXT_MAX:
                f["description"] = f["description"][:TEXT_MAX - 1] + "…"
    if target_public:
        hidden = 0
        for f in r.get("findings", []):
            if f.get("severity") in HIGH_PLUS and any(k in f for k in ("path", "line", "description")):
                for k in ("path", "line", "description"):
                    f.pop(k, None)
                f["redacted"] = "high+"
                hidden += 1
        llm = r.get("llm") or {}
        if any(f.get("severity") in HIGH_PLUS for f in r.get("findings", [])) and \
                any(k in llm for k in ("rationale", "reviewer_focus")):
            llm.pop("rationale", None)
            llm.pop("reviewer_focus", None)
            hidden += 1
        if hidden:
            notes.append("공개 telemetry repo이므로 high/critical 상세(경로·라인·문구·LLM 서술)는 "
                         "싣지 않음 — 원본은 소비자 run의 artifact `review-record.json`.")
    r["level"] = level
    return r, level, notes


def post(record, repo, token, level_requested, source_visibility, share_private=False):
    target_visibility = visibility(repo, token)
    record, level, notes = prepare(record, level_requested, source_visibility,
                                   target_visibility, share_private)
    note = "".join(f"> {n}\n" for n in notes) + ("\n" if notes else "")
    key = issue_key(record)
    body = (f"<!-- cg-key: {key} -->\ncg-key: `{key}`\n\n{note}{summary_md(record)}\n\n{FEEDBACK_HELP}\n"
            f"<details><summary>정규화 레코드 ({record['schema']}, level={level})</summary>\n\n"
            f"{record_block(record)}\n\n</details>\n")
    number = find_issue(repo, token, key)
    if number:
        api("POST", f"/repos/{repo}/issues/{number}/comments", token, {"body": body})
        issue = api("GET", f"/repos/{repo}/issues/{number}", token)
    else:
        title = (f"{TITLE_PREFIX} [{record.get('verdict')}] {(record.get('run') or {}).get('repo')} — "
                 f"{', '.join(record.get('signals', [])[:3])} ({key.split('/')[-1]})")
        # 라벨 없이 생성 — 권한 없는 계정의 라벨은 무시되고, 권한 있는 토큰은 쓰지 않는다 (C.3-5)
        issue = api("POST", f"/repos/{repo}/issues", token, {"title": title[:250], "body": body})
    return issue.get("html_url"), level


def minimal(record):
    """경로·문구·서술 제거. 규칙·심각도·경로 분류·카운트·링크만 남긴다."""
    r = json.loads(json.dumps(record))
    r["level"] = "minimal"
    for f in r.get("findings", []):
        for k in ("path", "line", "description"):
            f.pop(k, None)
    for k in ("rationale", "reviewer_focus"):
        (r.get("llm") or {}).pop(k, None)
    return r


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--record", required=True, help="normalize.py 레코드 (full)")
    p.add_argument("--level", default=os.environ.get("CG_TELEMETRY", "off"),
                   choices=["off", "minimal", "full"])
    p.add_argument("--repo", default=os.environ.get("CG_TELEMETRY_REPO") or DEFAULT_REPO)
    p.add_argument("--share-private", action="store_true",
                   default=os.environ.get("CG_TELEMETRY_SHARE_PRIVATE", "").lower() == "true",
                   help="private 소비자 소유자가 공개 telemetry repo로의 full 전송에 동의")
    p.add_argument("--out", help="생성·갱신한 issue URL을 기록할 파일")
    args = p.parse_args()

    if args.level == "off":
        print("[telemetry] off — 전송 안 함")
        return 0
    token = os.environ.get("CHANGEGUARD_TELEMETRY_TOKEN", "")
    if not token:
        print("::warning::telemetry 켜짐이지만 CHANGEGUARD_TELEMETRY_TOKEN 이 없음 — 전송 생략")
        return 0
    record = json.loads(Path(args.record).read_text(encoding="utf-8"))
    if not record.get("signals"):
        print("[telemetry] 분석 신호 없음 — 전송 안 함")
        return 0
    src_repo = (record.get("run") or {}).get("repo") or ""
    src_vis = "unknown"
    gh_token = os.environ.get("GITHUB_TOKEN", "")
    if src_repo and gh_token:
        src_vis = visibility(src_repo, gh_token)
    try:
        url, level = post(record, args.repo, token, args.level, src_vis, args.share_private)
    except Exception as e:   # 게이트를 막지 않는다
        print(f"::warning::telemetry 전송 실패: {type(e).__name__}: {str(e)[:200]}")
        return 0
    print(f"[telemetry] {level} → {url}")
    if args.out:
        Path(args.out).write_text(url or "", encoding="utf-8")
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary and url:
        with open(summary, "a", encoding="utf-8") as fh:
            fh.write(f"\n**changeguard telemetry** ({level}): {url}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
