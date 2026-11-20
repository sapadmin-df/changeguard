#!/usr/bin/env python3
"""
cg-insights.py — changeguard 실행 결과 집계 → 개선 후보 도출 (피드백 루프, v0.18+)

입력 (여러 개 가능):
  --dir DIR              review-result.json / review-record.json 들이 있는 디렉터리
                         (파일명이 <run_id>.json 이면 run_id로 쓴다 — 과거 artifact 소급 분석)
  --artifacts OWNER/REPO 소비자 repo의 pre-merge-review artifact를 내려받아 분석 (gh CLI 필요)
  --issues OWNER/REPO    telemetry issue (제목 [cg-telemetry] 또는 라벨 cg-telemetry) 의 레코드와 피드백을 수집
  --trust LOGIN          레코드 작성 계정 신뢰 목록 (공개 repo의 오염 방지)

피드백 (telemetry issue에서):
  - 라벨: fb:true-positive · fb:false-positive · fb:noise · fb:missed ·
          fb:llm-too-strict · fb:llm-too-lenient  (run 단위)
  - 댓글: `/cg fp <finding_id|rule> [메모]`  (tp|fp|noise|missed, finding 단위)

출력:
  Markdown 보고서 (기본 stdout) — 판정·LLM 가용성·비용·단계별 런타임·규칙별 통계·개선 후보
  --ledger DIR  통계 원장: runs.csv (run 1행) · findings.csv (finding 1행) · ledger.sqlite
                엑셀·스프레드시트로 바로 열거나 SQL로 질의 (예시: docs/FEEDBACK-LOOP.md)
  --sql "…"     ledger.sqlite 에 바로 질의 (--ledger 와 함께)
  --json FILE   분석 원자료
  --job-timing  (--artifacts) GitHub job·step 런타임과 대기 시간까지 수집 (run당 API 1회)

같은 입력이면 같은 보고서 — 판단 규칙(CANDIDATE_*, PERF_*)은 이 파일에 고정되어 있다.
"""

import argparse
import collections
import csv
import json
import sqlite3
import statistics
import os
import re
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "skills" / "pre-merge-review" / "checks"))
import normalize as nz  # noqa: E402

FB_LABELS = ("true-positive", "false-positive", "noise", "missed", "llm-too-strict", "llm-too-lenient")
FB_CMD = re.compile(r'^/cg\s+(tp|fp|noise|missed)\s+(\S+)(?:\s+(.*))?$', re.MULTILINE)
# fence 길이는 가변 (telemetry.record_block: 내용 속 백틱보다 길게) — 같은 길이의 닫는 fence까지
RECORD_BLOCK = re.compile(r'^(`{3,})json changeguard-record\n(.*?)\n\1[ \t]*$', re.DOTALL | re.MULTILINE)
TELEMETRY_TITLE = "[cg-telemetry]"
TRUSTED_ASSOC = {"OWNER", "MEMBER", "COLLABORATOR"}

# 개선 후보 판단 규칙
CANDIDATE_MIN_HITS = 3          # 규칙이 이 횟수 이상 걸린 run이 있어야 판단
CANDIDATE_INTENT_RATIO = 0.8    # 그 run들 중 LLM이 '의도된 변경'으로 본 비율
CANDIDATE_LLM_ONLY_MIN = 2      # 정적 0인데 LLM이 medium+로 같은 범주를 보고한 run 수
CANDIDATE_FB_FP_MIN = 2         # 사람 피드백 fp 건수

# 성능 판단 규칙
PERF_LLM_P90_MS = 60_000        # LLM p90 이 이 이상이면 후보
PERF_DET_P90_MS = 20_000        # 정적 검사 p90
PERF_SKIP_LLM_RATIO = 0.5       # LLM 호출 비율이 낮으면 정보로만 표시
PHASES = ("deprecation_ms", "bump_verify_ms", "deterministic_ms", "adapter_ms", "llm_ms", "total_ms")
SIZE_BUCKETS = ((10, "≤10 files"), (50, "11–50"), (200, "51–200"), (10**9, ">200"))

# 비용 추정 (USD / 1M tokens, 입력·출력). 가격이 바뀌면 --price model=in/out 으로 덮어쓴다.
PRICES = {"claude-sonnet-5-5": (2.0, 10.0), "claude-sonnet-5": (2.0, 10.0),
          "claude-sonnet-4-6": (3.0, 15.0), "claude-opus-5-5": (4.0, 20.0),
          "claude-haiku-5-5": (0.10, 0.50)}


def gh(*args):
    out = subprocess.run(["gh", *args], capture_output=True, text=True, encoding="utf-8")
    if out.returncode != 0:
        raise RuntimeError(out.stderr.strip()[:300])
    return out.stdout


def load_dir(path):
    records = []
    for p in sorted(Path(path).glob("*.json")):
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if data.get("schema") == nz.SCHEMA:
            records.append(data)
        elif "verdict" in data:
            run = {"run_id": int(p.stem) if p.stem.isdigit() else p.stem}
            records.append(nz.normalize(data, run))
    return records


def _iso_ms(a, b):
    from datetime import datetime
    if not a or not b:
        return None
    f = "%Y-%m-%dT%H:%M:%SZ"
    return int((datetime.strptime(b, f) - datetime.strptime(a, f)).total_seconds() * 1000)


def job_timing(repo, run_id):
    """GitHub run/job 메타 → 대기·job·step 런타임 (ms). ci-caller 바깥(checkout 등)까지 본다."""
    try:
        runj = json.loads(gh("api", f"repos/{repo}/actions/runs/{run_id}"))
        jobs = json.loads(gh("api", f"repos/{repo}/actions/runs/{run_id}/jobs"))["jobs"]
    except (RuntimeError, json.JSONDecodeError, KeyError):
        return {}
    out = {"created_at": runj.get("created_at"), "event": runj.get("event"),
           "conclusion": runj.get("conclusion")}
    for j in jobs[:1]:
        out["queue_ms"] = _iso_ms(runj.get("created_at"), j.get("started_at"))
        out["job_ms"] = _iso_ms(j.get("started_at"), j.get("completed_at"))
        out["steps"] = {s["name"]: _iso_ms(s.get("started_at"), s.get("completed_at"))
                        for s in j.get("steps", []) if s.get("started_at")}
    return out


def load_artifacts(repo, limit, with_job_timing=False):
    arts = json.loads(gh("api", "--paginate", f"repos/{repo}/actions/artifacts?per_page=100",
                         "--jq", "[.artifacts[] | select(.name|startswith(\"pre-merge-review-\")) "
                                 "| select(.expired|not) | {id, run: .workflow_run.id}]"
                         ).replace("]\n[", ","))
    records = []
    with tempfile.TemporaryDirectory() as tmp:
        for a in arts[:limit]:
            z = Path(tmp) / f"{a['id']}.zip"
            try:
                z.write_bytes(subprocess.run(
                    ["gh", "api", f"repos/{repo}/actions/artifacts/{a['id']}/zip"],
                    capture_output=True).stdout)
                with zipfile.ZipFile(z) as zf:
                    names = zf.namelist()
                    name = "review-record.json" if "review-record.json" in names else "review-result.json"
                    data = json.loads(zf.read(name).decode("utf-8"))
            except (zipfile.BadZipFile, KeyError, json.JSONDecodeError):
                continue
            if data.get("schema") != nz.SCHEMA:
                data = nz.normalize(data, {"repo": repo, "run_id": a["run"],
                                           "run_url": f"https://github.com/{repo}/actions/runs/{a['run']}"})
            if with_job_timing:
                data["ci"] = job_timing(repo, a["run"])
            records.append(data)
    return records


def estimate_cost(record, prices):
    llm = record.get("llm") or {}
    usage = llm.get("usage") or {}
    price = prices.get(llm.get("model") or "")
    if not usage or not price:
        return None
    return round((usage.get("input_tokens") or 0) / 1e6 * price[0]
                 + (usage.get("output_tokens") or 0) / 1e6 * price[1], 6)


def ledger_rows(records, prices):
    """run 1행 · finding 1행 평면 테이블 — 스프레드시트·SQL 집계용."""
    runs, finds = [], []
    for i, r in enumerate(records):
        run = r.get("run") or {}
        llm = r.get("llm") or {}
        usage = llm.get("usage") or {}
        t = r.get("timings") or {}
        ci = r.get("ci") or {}
        diff = r.get("diff") or {}
        key = run.get("run_id") or f"local-{i}"
        runs.append({
            "run_id": key, "repo": run.get("repo"), "event": run.get("event") or ci.get("event"),
            "pr_number": run.get("pr_number"), "created_at": ci.get("created_at"),
            "policy_version": (r.get("policy") or {}).get("version"),
            "verdict": r.get("verdict"), "verdict_driver": r.get("verdict_driver"),
            "disagreement": r.get("disagreement"), "signals": " ".join(r.get("signals", [])),
            "det_max": (r.get("severity_max") or {}).get("deterministic"),
            "llm_max": (r.get("severity_max") or {}).get("llm"),
            "gating_call_llm": (r.get("gating") or {}).get("call_llm"),
            "gating_reason": (r.get("gating") or {}).get("reason"),
            "llm_state": llm.get("state"), "llm_failure_class": llm.get("failure_class"),
            "llm_model": llm.get("model"), "llm_model_source": llm.get("model_source"),
            "llm_fallback": llm.get("fallback"), "llm_intent": llm.get("intent"),
            "input_tokens": usage.get("input_tokens"), "output_tokens": usage.get("output_tokens"),
            "cost_usd_est": estimate_cost(r, prices),
            "diff_files": diff.get("files_changed"), "diff_lines_added": diff.get("lines_added"),
            "diff_lines_removed": diff.get("lines_removed"), "diff_bytes": diff.get("bytes"),
            **{p: t.get(p) for p in PHASES},
            "ci_queue_ms": ci.get("queue_ms"), "ci_job_ms": ci.get("job_ms"),
            "ci_steps": ci.get("steps"),   # {step 이름: ms} — SQLite에서는 JSON 문자열 (json_extract 로 질의)
            "findings": len(r.get("findings", [])), "run_url": run.get("run_url"),
            "telemetry_issue": (r.get("telemetry") or {}).get("issue"),
            "telemetry_author": (r.get("telemetry") or {}).get("author"),
        })
        for f in r.get("findings", []):
            finds.append({"run_id": key, "finding_id": f["id"], "rule": f["rule"], "source": f["source"],
                          "severity": f["severity"], "category": f["category"],
                          "confidence": f.get("confidence"), "path_class": f["path_class"],
                          "path": f.get("path"), "line": f.get("line"),
                          "downgraded_from": f.get("downgraded_from"),
                          "verdict": r.get("verdict"), "llm_intent": llm.get("intent"),
                          "policy_version": (r.get("policy") or {}).get("version")})
    return runs, finds


def write_ledger(outdir, runs, finds, feedback):
    out = Path(outdir)
    out.mkdir(parents=True, exist_ok=True)
    for name, rows in (("runs", runs), ("findings", finds), ("feedback", feedback)):
        if not rows:
            continue
        cols = list(dict.fromkeys(k for row in rows for k in row))
        # utf-8-sig: 엑셀에서 바로 열어도 한글이 깨지지 않게
        with open(out / f"{name}.csv", "w", newline="", encoding="utf-8-sig") as fh:
            w = csv.DictWriter(fh, fieldnames=cols)
            w.writeheader()
            w.writerows(rows)
    db = out / "ledger.sqlite"
    if db.exists():
        db.unlink()
    con = sqlite3.connect(db)
    for name, rows in (("runs", runs), ("findings", finds), ("feedback", feedback)):
        if not rows:
            continue
        cols = list(dict.fromkeys(k for row in rows for k in row))
        con.execute(f"CREATE TABLE {name} ({', '.join(cols)})")
        con.executemany(f"INSERT INTO {name} VALUES ({', '.join('?' * len(cols))})",
                        [[json.dumps(row.get(c), ensure_ascii=False) if isinstance(row.get(c), (list, dict))
                          else row.get(c) for c in cols] for row in rows])
    con.commit()
    return con


def pctl(values, q):
    v = sorted(x for x in values if x is not None)
    if not v:
        return None
    return v[min(len(v) - 1, int(round(q * (len(v) - 1))))]


def perf(records, runs):
    """성격별 런타임 분포: 단계 × (LLM 호출 여부 · diff 크기 · 정책 버전)."""
    out = {"phases": {}, "by_size": {}, "by_policy": {}, "slowest": [], "measured": 0}
    timed = [r for r in runs if r.get("total_ms") is not None]
    out["measured"] = len(timed)
    for ph in PHASES + ("ci_queue_ms", "ci_job_ms"):
        vals = [r.get(ph) for r in runs]
        if any(v is not None for v in vals):
            out["phases"][ph] = {"p50": pctl(vals, .5), "p90": pctl(vals, .9), "max": pctl(vals, 1),
                                 "n": sum(v is not None for v in vals)}
    for limit, label in SIZE_BUCKETS:
        lo = next((l for l, _ in reversed(SIZE_BUCKETS) if l < limit), 0)
        rows = [r for r in timed if r.get("diff_files") is not None and lo < r["diff_files"] <= limit]
        if rows:
            out["by_size"][label] = {"n": len(rows), "total_p50": pctl([r["total_ms"] for r in rows], .5),
                                     "llm_p50": pctl([r.get("llm_ms") for r in rows], .5),
                                     "tokens_in_p50": pctl([r.get("input_tokens") for r in rows], .5)}
    for ver in sorted({r.get("policy_version") for r in runs if r.get("policy_version")}):
        rows = [r for r in runs if r.get("policy_version") == ver]
        costs = [r["cost_usd_est"] for r in rows if r.get("cost_usd_est") is not None]
        out["by_policy"][ver] = {
            "runs": len(rows),
            "block": sum(r["verdict"] == "block" for r in rows),
            "advisory": sum(r["verdict"] == "advisory" for r in rows),
            "llm_called": sum(bool(r.get("gating_call_llm")) for r in rows),
            "llm_unavailable": sum(r.get("llm_state") in ("failed", "no_key") for r in rows),
            "total_p50": pctl([r.get("total_ms") for r in rows], .5),
            "llm_p90": pctl([r.get("llm_ms") for r in rows], .9),
            "cost_usd_est": round(sum(costs), 4) if costs else None,
        }
    out["slowest"] = sorted(timed, key=lambda r: -r["total_ms"])[:5]
    return out


def perf_candidates(p, runs, records):
    c = []
    ph = p["phases"]
    if (ph.get("llm_ms") or {}).get("p90") and ph["llm_ms"]["p90"] >= PERF_LLM_P90_MS:
        c.append({"kind": "perf", "rule": "llm_ms",
                  "why": f"LLM p90 {ph['llm_ms']['p90']/1000:.0f}s — 큰 diff 분할·effort 조정·생성 파일 제외 검토"})
    if (ph.get("deterministic_ms") or {}).get("p90") and ph["deterministic_ms"]["p90"] >= PERF_DET_P90_MS:
        c.append({"kind": "perf", "rule": "deterministic_ms",
                  "why": f"정적 검사 p90 {ph['deterministic_ms']['p90']/1000:.0f}s — finding당 jq 호출 등 병목 점검"})
    trunc = [r for r in records if "llm.diff_truncated" in (r.get("rule_counts") or {})]
    if trunc:
        files = sorted((r.get("diff") or {}).get("files_changed") or 0 for r in trunc)
        c.append({"kind": "perf", "rule": "llm.diff_truncated",
                  "why": f"diff 상한 초과로 LLM이 앞부분만 검토 {len(trunc)}회 (변경 파일 중앙값 "
                         f"{files[len(files)//2]}) — 대형 merge는 advisory가 반복됨. "
                         "생성 파일·artifacts 제외 또는 분할 검토"})
    llm_called = sum(bool(r.get("gating_call_llm")) for r in runs)
    if runs and llm_called / len(runs) < PERF_SKIP_LLM_RATIO:
        c.append({"kind": "info", "rule": "gating",
                  "why": f"LLM 호출 {llm_called}/{len(runs)} — 게이팅으로 비용 절감 중 (md·lockfile only skip)"})
    return c


def parse_issues(issues, trust=None):
    """telemetry issue 목록 → (records, feedback, rejected). 네트워크 없음 — 단위 테스트 대상.

    공개 issue는 누구나 쓸 수 있으므로 신뢰하지 않는 입력으로 다룬다 (REQ-C.3-7):
    - 레코드: issue 작성자의 본문·댓글에서만. trust(작성 계정 목록)가 있으면 그 계정만.
    - 실행 피드백(fb:* 라벨): 라벨은 triage 권한자만 붙일 수 있으므로 그대로 신뢰.
    - finding 피드백(/cg 댓글): repo 권한자(OWNER·MEMBER·COLLABORATOR) 댓글만.
    """
    trust = {t.lower() for t in (trust or [])}
    records, feedback, rejected = [], [], collections.Counter()
    for it in issues:
        author = ((it.get("author") or {}).get("login") or "").lower()
        if trust and author not in trust:
            rejected["untrusted_author"] += 1
            continue
        texts = [it.get("body") or ""] + [c.get("body") or "" for c in it.get("comments", [])
                                         if ((c.get("author") or {}).get("login") or "").lower() == author]
        recs = []
        for t in texts:
            for _fence, blk in RECORD_BLOCK.findall(t):
                try:
                    recs.append(json.loads(blk))
                except json.JSONDecodeError:
                    rejected["bad_record"] += 1
        for r in recs:
            r.setdefault("telemetry", {}).update({"issue": it.get("url"), "author": author})
        records += recs
        for lab in (l["name"] for l in it.get("labels", [])):
            if lab.startswith("fb:") and lab[3:] in FB_LABELS:
                for r in recs:
                    feedback.append({"scope": "run", "kind": lab[3:], "issue": it.get("url"),
                                     "rules": list(r.get("rule_counts", {}))})
        for c in it.get("comments", []):
            cmds = FB_CMD.findall(c.get("body") or "")
            if cmds and c.get("authorAssociation") not in TRUSTED_ASSOC:
                rejected["untrusted_feedback"] += len(cmds)
                continue
            for kind, target, note in cmds:
                feedback.append({"scope": "finding", "kind": kind, "target": target,
                                 "note": note, "issue": it.get("url"),
                                 "by": (c.get("author") or {}).get("login")})
    return records, feedback, rejected


def load_issues(repo, trust=None):
    """telemetry issue 수집: 제목 접두사 [cg-telemetry] (v0.18.1+) + 라벨 cg-telemetry (v0.18.0)."""
    fields = "number,url,labels,body,comments,author"
    seen = {}
    for args in (["--search", f'in:title "{TELEMETRY_TITLE}"'], ["--label", "cg-telemetry"]):
        try:
            out = gh("issue", "list", "-R", repo, *args, "--state", "all", "--limit", "1000", "--json", fields)
        except Exception:
            continue
        for it in json.loads(out or "[]"):
            seen[it["number"]] = it
    records, feedback, rejected = parse_issues(list(seen.values()), trust)
    if not trust:
        print("[warn] --trust 없음 — 모든 작성자의 레코드를 집계 (공개 repo면 오염 가능)", file=sys.stderr)
    if rejected:
        print(f"[info] 제외: {dict(rejected)}", file=sys.stderr)
    return records, feedback


def pct(n, d):
    return f"{(100 * n / d):.0f}%" if d else "-"


def analyze(records, feedback, prices=PRICES):
    runs = len(records)
    out = {"runs": runs}
    out["ledger_runs"], out["ledger_findings"] = ledger_rows(records, prices)
    out["perf"] = perf(records, out["ledger_runs"])
    costs = [r["cost_usd_est"] for r in out["ledger_runs"] if r.get("cost_usd_est") is not None]
    out["cost"] = {"usd_est": round(sum(costs), 4) if costs else None, "runs": len(costs),
                   "per_llm_run": round(sum(costs) / len(costs), 4) if costs else None}
    out["verdict"] = collections.Counter(r["verdict"] for r in records)
    out["driver"] = collections.Counter(r["verdict_driver"] for r in records)
    out["signals"] = collections.Counter(s for r in records for s in r["signals"])
    out["policy"] = collections.Counter((r.get("policy") or {}).get("version") for r in records)
    out["llm_state"] = collections.Counter((r.get("llm") or {}).get("state") for r in records)
    out["llm_failure"] = collections.Counter((r.get("llm") or {}).get("failure_class")
                                             for r in records if (r.get("llm") or {}).get("failure_class"))
    out["llm_model"] = collections.Counter((r.get("llm") or {}).get("model")
                                           for r in records if (r.get("llm") or {}).get("model"))
    tokens = [((r.get("llm") or {}).get("usage") or {}) for r in records]
    out["tokens"] = {"input": sum(t.get("input_tokens") or 0 for t in tokens),
                     "output": sum(t.get("output_tokens") or 0 for t in tokens),
                     "measured_runs": sum(1 for t in tokens if t)}

    # 규칙별: 걸린 run 수, verdict를 좌우한 run 수, 그 run에서 LLM이 '의도된 변경'이라 본 비율
    by_rule = collections.defaultdict(lambda: {"findings": 0, "runs": set(), "driving": set(),
                                               "intentional": set(), "sev": collections.Counter(),
                                               "path": collections.Counter(), "examples": []})
    fid_rule = {}
    for i, r in enumerate(records):
        key = (r.get("run") or {}).get("run_id") or i
        top = max((nz.SEV_RANK.get(f["severity"], 0) for f in r["findings"]), default=0)
        intent = (r.get("llm") or {}).get("intent")
        for f in r["findings"]:
            s = by_rule[f["rule"]]
            s["findings"] += 1
            s["runs"].add(key)
            s["sev"][f["severity"]] += 1
            s["path"][f["path_class"]] += 1
            fid_rule[f["id"]] = f["rule"]
            if nz.SEV_RANK.get(f["severity"], 0) == top and top >= 2 and r["verdict"] != "pass":
                s["driving"].add(key)
            if intent == "intentional":
                s["intentional"].add(key)
            if len(s["examples"]) < 3 and f.get("path"):
                s["examples"].append({"run": (r.get("run") or {}).get("run_url") or key,
                                      "loc": f"{f['path']}:{f.get('line') or ''}".rstrip(":"),
                                      "severity": f["severity"]})
    fb = collections.defaultdict(collections.Counter)
    for x in feedback:
        if x["scope"] == "finding":
            rule = fid_rule.get(x["target"], x["target"])
            fb[rule][x["kind"]] += 1
        else:
            for rule in x["rules"]:
                fb[rule][x["kind"]] += 1
    out["rules"] = {k: {"findings": v["findings"], "runs": len(v["runs"]), "driving": len(v["driving"]),
                        "intentional_ratio": (len(v["intentional"] & v["runs"]) / len(v["runs"]))
                        if v["runs"] else 0,
                        "severity": dict(v["sev"]), "path_class": dict(v["path"]),
                        "feedback": dict(fb.get(k, {})), "examples": v["examples"]}
                    for k, v in sorted(by_rule.items())}

    # 정적 0인데 LLM이 medium+ 를 보고한 범주 — 정적 규칙 승격 후보
    llm_only = collections.Counter()
    for r in records:
        if not (r.get("severity_max") or {}).get("deterministic"):
            for f in r["findings"]:
                if f["source"] == "llm" and f["category"] != "meta" and nz.SEV_RANK.get(f["severity"], 0) >= 2:
                    llm_only[f["rule"]] += 1
    out["llm_only"] = llm_only

    cands = []
    for rule, s in out["rules"].items():
        if not rule.startswith("det."):
            continue
        if s["driving"] >= CANDIDATE_MIN_HITS and s["intentional_ratio"] >= CANDIDATE_INTENT_RATIO:
            cands.append({"kind": "relax-or-contextualize", "rule": rule,
                          "why": f"verdict 좌우 {s['driving']}회, 그 중 LLM '의도된 변경' {s['intentional_ratio']:.0%}",
                          "path_class": s["path_class"]})
        if s["feedback"].get("false-positive", 0) + s["feedback"].get("fp", 0) >= CANDIDATE_FB_FP_MIN:
            cands.append({"kind": "human-fp", "rule": rule, "why": f"사람 피드백 fp {s['feedback']}"})
    for rule, n in llm_only.most_common():
        if n >= CANDIDATE_LLM_ONLY_MIN:
            cands.append({"kind": "promote-to-static", "rule": rule,
                          "why": f"정적 검사 0인 run에서 LLM medium+ {n}회 — 정적 규칙화 검토"})
    ops = {k: v for k, v in out["llm_failure"].items() if k in ("billing", "quota", "auth", "model_not_found")}
    if ops:
        cands.append({"kind": "ops", "rule": "llm.unavailable",
                      "why": f"LLM 미가용 {ops} — 운영 조치 (키·크레딧·모델 설정)"})
    cands += perf_candidates(out["perf"], out["ledger_runs"], records)
    out["candidates"] = cands
    return out


def render(a):
    L = [f"# changeguard insights — {a['runs']} runs", ""]
    L.append("## 판정 개요")
    L.append(f"- verdict: {dict(a['verdict'])}")
    L.append(f"- 판정 주체(verdict_driver): {dict(a['driver'])}")
    L.append(f"- 신호(signals): {dict(a['signals'])}")
    L.append(f"- 정책 버전: {dict(a['policy'])}")
    L.append("")
    L.append("## LLM 가용성·비용")
    L.append(f"- state: {dict(a['llm_state'])}")
    L.append(f"- failure_class: {dict(a['llm_failure']) or '-'}")
    L.append(f"- model: {dict(a['llm_model'])}")
    t = a["tokens"]
    L.append(f"- tokens: input {t['input']:,} / output {t['output']:,} (측정 run {t['measured_runs']})")
    c = a["cost"]
    L.append(f"- 추정 비용: {('$' + format(c['usd_est'], ',.2f')) if c['usd_est'] is not None else '측정 없음'}"
             f" (run당 {('$' + str(c['per_llm_run'])) if c['per_llm_run'] else '-'}, PRICES 기준 추정)")
    L.append("")
    p = a["perf"]
    L.append(f"## 런타임 (ms, 측정 run {p['measured']})")
    if p["phases"]:
        L.append("| 단계 | p50 | p90 | max | n |")
        L.append("|---|---|---|---|---|")
        for ph, v in p["phases"].items():
            L.append(f"| {ph} | {v['p50']} | {v['p90']} | {v['max']} | {v['n']} |")
    else:
        L.append("- 런타임 기록 없음 (v0.18 이전 결과) — v0.18 이후 실행 또는 --job-timing 으로 수집")
    if p["by_size"]:
        L.append("")
        L.append("| diff 크기 | runs | total p50 | LLM p50 | 입력 토큰 p50 |")
        L.append("|---|---|---|---|---|")
        for k, v in p["by_size"].items():
            L.append(f"| {k} | {v['n']} | {v['total_p50']} | {v['llm_p50']} | {v['tokens_in_p50']} |")
    L.append("")
    L.append("## 정책 버전별 (개선 전후 비교)")
    L.append("| policy | runs | block | advisory | LLM 호출 | LLM 미가용 | total p50 | LLM p90 | 비용 추정 |")
    L.append("|---|---|---|---|---|---|---|---|---|")
    for ver, v in p["by_policy"].items():
        L.append(f"| {ver} | {v['runs']} | {v['block']} | {v['advisory']} | {v['llm_called']} | "
                 f"{v['llm_unavailable']} | {v['total_p50']} | {v['llm_p90']} | {v['cost_usd_est']} |")
    L.append("")
    L.append("## 규칙별")
    L.append("| rule | findings | runs | verdict 좌우 | LLM '의도된 변경' | 경로 분류 | 피드백 |")
    L.append("|---|---|---|---|---|---|---|")
    for rule, s in sorted(a["rules"].items(), key=lambda kv: (-kv[1]["driving"], -kv[1]["runs"])):
        L.append(f"| `{rule}` | {s['findings']} | {s['runs']} | {s['driving']} | "
                 f"{s['intentional_ratio']:.0%} | {s['path_class']} | {s['feedback'] or '-'} |")
    L.append("")
    L.append("## 정적 검사 0인데 LLM이 medium+ 로 본 범주")
    for rule, n in a["llm_only"].most_common():
        L.append(f"- `{rule}`: {n}")
    L.append("")
    L.append("## 개선 후보")
    if not a["candidates"]:
        L.append("- (없음)")
    for c in a["candidates"]:
        L.append(f"- **{c['kind']}** `{c['rule']}` — {c['why']}"
                 + (f" · 경로 {c['path_class']}" if c.get("path_class") else ""))
    L.append("")
    L.append("> 후보는 결론이 아니라 검토 시작점이다. 채택하면 fixture를 먼저 추가하고 정책 PR을 연다 "
             "(docs/FEEDBACK-LOOP.md).")
    return "\n".join(L)


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--dir", action="append", default=[])
    p.add_argument("--artifacts", action="append", default=[])
    p.add_argument("--issues", action="append", default=[])
    p.add_argument("--trust", action="append", default=[], metavar="LOGIN",
                   help="--issues 레코드를 이 작성 계정의 것만 집계 (반복 가능). 공개 repo면 지정 권장")
    p.add_argument("--limit", type=int, default=2000, help="--artifacts 최대 개수")
    p.add_argument("--job-timing", action="store_true", help="--artifacts 에서 job·step 런타임 수집")
    p.add_argument("--policy-version", help="이 정책 버전의 run만 (개선 전후 비교용)")
    p.add_argument("--price", action="append", default=[], metavar="MODEL=IN/OUT",
                   help="비용 추정 단가 덮어쓰기 (USD/1M tokens), 예: claude-sonnet-5-5=2/10")
    p.add_argument("--ledger", help="통계 원장 출력 디렉터리 (runs.csv · findings.csv · ledger.sqlite)")
    p.add_argument("--sql", help="ledger.sqlite 에 질의하고 결과를 표로 출력 (--ledger 필요)")
    p.add_argument("--json", help="분석 원자료를 JSON으로 저장")
    args = p.parse_args()

    prices = dict(PRICES)
    for spec in args.price:
        model, _, io = spec.partition("=")
        i, _, o = io.partition("/")
        prices[model] = (float(i), float(o))

    records, feedback = [], []
    for d in args.dir:
        records += load_dir(d)
    for repo in args.artifacts:
        records += load_artifacts(repo, args.limit, args.job_timing)
    for repo in args.issues:
        r, f = load_issues(repo, args.trust)
        records += r
        feedback += f
    if args.policy_version:
        records = [r for r in records if (r.get("policy") or {}).get("version") == args.policy_version]
    if not records:
        print("입력 레코드 없음", file=sys.stderr)
        return 2
    a = analyze(records, feedback, prices)
    if args.json:
        Path(args.json).write_text(json.dumps(a, ensure_ascii=False, indent=2, default=list),
                                   encoding="utf-8")
    if args.ledger:
        con = write_ledger(args.ledger, a["ledger_runs"], a["ledger_findings"], feedback)
        print(f"[ledger] {args.ledger}: runs {len(a['ledger_runs'])} · findings {len(a['ledger_findings'])} "
              f"· feedback {len(feedback)}", file=sys.stderr)
        if args.sql:
            cur = con.execute(args.sql)
            cols = [d[0] for d in cur.description]
            print("| " + " | ".join(cols) + " |")
            print("|" + "---|" * len(cols))
            for row in cur.fetchall():
                print("| " + " | ".join("" if v is None else str(v) for v in row) + " |")
            return 0
    print(render(a))
    return 0


if __name__ == "__main__":
    sys.exit(main())
