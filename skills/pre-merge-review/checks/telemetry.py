#!/usr/bin/env python3
"""
telemetry.py — 분석이 필요한 실행을 telemetry repo의 issue로 연결 (opt-in, v0.18+)

기본값은 꺼짐. 소비자가 reusable workflow 입력으로 명시적으로 켤 때만 동작한다:
  telemetry: off | minimal | full   (기본 off)
  telemetry_repo: owner/name         (필수 — 기본값 없음)
  secret CHANGEGUARD_TELEMETRY_TOKEN  (telemetry repo의 Issues: Read & write 만 가진
                                       fine-grained PAT 권장)

보내는 것: normalize.py 정규화 레코드 (signals 가 있는 실행만 — 판정 차단·advisory,
LLM이 정적보다 엄격, 정적 오탐 후보, LLM 미가용·거절·잘림·파싱 실패, self-lint).

이슈 단위: PR 실행은 PR마다 한 이슈, push 실행은 커밋마다 한 이슈. 같은 키의 다음
실행은 같은 이슈에 댓글로 레코드를 덧붙인다 (재실행·후속 커밋 추적).

공개 범위 보호: 소비자 repo가 private 이고 telemetry repo가 public 이면 level을
minimal로 강제한다 (파일 경로·finding 문구·LLM 서술이 공개 저장소로 나가지 않게).
minimal 에도 repo 이름과 run 링크는 들어간다 — 그것도 싫으면 private telemetry repo를 쓴다.

실패해도 게이트에 영향 없음 (항상 exit 0, 경고만).
"""

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

API = "https://api.github.com"
LABEL_COLORS = {"cg-telemetry": "1e7eb6", "verdict:": "cc0000", "signal:": "daa520",
                "fb:": "36a64f", "repo:": "5319e7"}
FEEDBACK_HELP = (
    "### 피드백 (사후 분석 루프에 반영됨)\n"
    "- 이 실행 전체 판정에 대해 라벨: `fb:true-positive` · `fb:false-positive` · `fb:noise` · "
    "`fb:missed` · `fb:llm-too-strict` · `fb:llm-too-lenient`\n"
    "- finding 하나에 대해 댓글: `/cg fp <finding_id> 메모` (`tp|fp|noise|missed`)\n"
    "- 집계: `python3 scripts/cg-insights.py --issues <telemetry repo>`\n"
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


def labels_for(record):
    run = record.get("run") or {}
    labels = ["cg-telemetry", f"verdict:{record.get('verdict')}"]
    labels += [f"signal:{s}" for s in record.get("signals", [])]
    if run.get("repo"):
        labels.append(f"repo:{run['repo'].split('/')[-1]}")
    return labels[:20]


def ensure_labels(repo, token, labels):
    for name in labels:
        color = next((c for p, c in LABEL_COLORS.items() if name.startswith(p)), "cfd3d7")
        try:
            api("POST", f"/repos/{repo}/labels", token, {"name": name, "color": color})
        except urllib.error.HTTPError as e:
            if e.code != 422:   # 422 = 이미 존재
                raise


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
        loc = f"{f.get('path')}:{f.get('line') or ''}".rstrip(":") if f.get("path") else "-"
        lines.append(f"| `{f['id']}` | `{f['rule']}` | {f['severity']} | {f['path_class']} | {loc} |")
    return "\n".join(lines)


def record_block(record):
    return "```json changeguard-record\n" + json.dumps(record, ensure_ascii=False, indent=1) + "\n```"


def find_issue(repo, token, key):
    q = urllib.request.quote(f'repo:{repo} is:issue label:cg-telemetry "cg-key: {key}" in:body')
    try:
        items = api("GET", f"/search/issues?q={q}&per_page=1", token).get("items") or []
        return items[0]["number"] if items else None
    except Exception:
        return None


def post(record, repo, token, level_requested, source_visibility):
    target_visibility = visibility(repo, token)
    level = level_requested
    note = ""
    if level == "full" and source_visibility != "public" and target_visibility != "private":
        level, note = "minimal", (f"> level=full 요청이었으나 소비자 repo({source_visibility})보다 "
                                  f"telemetry repo({target_visibility})가 넓게 공개되어 minimal로 낮춤.\n\n")
    if level == "minimal" and record.get("level") != "minimal":
        record = minimal(record)
    key = issue_key(record)
    labels = labels_for(record)
    ensure_labels(repo, token, labels)
    body = (f"<!-- cg-key: {key} -->\n{note}{summary_md(record)}\n\n{FEEDBACK_HELP}\n"
            f"<details><summary>정규화 레코드 ({record['schema']}, level={level})</summary>\n\n"
            f"{record_block(record)}\n\n</details>\n")
    number = find_issue(repo, token, key)
    if number:
        api("POST", f"/repos/{repo}/issues/{number}/comments", token, {"body": body})
        api("POST", f"/repos/{repo}/issues/{number}/labels", token, {"labels": labels})
        issue = api("GET", f"/repos/{repo}/issues/{number}", token)
    else:
        title = (f"[{record.get('verdict')}] {(record.get('run') or {}).get('repo')} — "
                 f"{', '.join(record.get('signals', [])[:3])} ({key.split('/')[-1]})")
        issue = api("POST", f"/repos/{repo}/issues", token,
                    {"title": title[:250], "body": body, "labels": labels})
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
    p.add_argument("--repo", default=os.environ.get("CG_TELEMETRY_REPO", ""))
    p.add_argument("--out", help="생성·갱신한 issue URL을 기록할 파일")
    args = p.parse_args()

    if args.level == "off":
        print("[telemetry] off — 전송 안 함")
        return 0
    token = os.environ.get("CHANGEGUARD_TELEMETRY_TOKEN", "")
    if not args.repo or not token:
        print("::warning::telemetry 켜짐이지만 telemetry_repo 또는 CHANGEGUARD_TELEMETRY_TOKEN 이 없음 — 전송 생략")
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
        url, level = post(record, args.repo, token, args.level, src_vis)
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
