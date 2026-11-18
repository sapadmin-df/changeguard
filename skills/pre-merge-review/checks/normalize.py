#!/usr/bin/env python3
"""
normalize.py — 리뷰 결과 → 정규화 레코드 (changeguard.review/v1, v0.18+)

목적: 실행 결과를 *단순 쿼리*로 역공학할 수 있게 한다. 사람용 문구(description,
summary)가 바뀌어도 집계가 깨지지 않도록 안정적인 키를 붙인다.

  - finding: rule(규칙 ID) · id(rule+경로+줄 해시) · path_class · source · severity
  - run: repo · event · run_id · PR · SHA · URL — 다른 기록과 조인하는 키
  - llm: state · failure_class · model · fallback · usage · latency
  - 판정 분석: verdict_driver · severity_max(det/llm) · disagreement · signals

v0.17 이하 결과(rule·llm_run 없음)도 description/summary에서 추론해 소급 변환한다
(scripts/cg-insights.py 가 과거 artifact를 집계할 때 사용).

공개 범위 (level):
  full    — description·location·rationale 포함 (private 저장소용)
  minimal — 규칙·심각도·경로 분류·카운트·링크만. 코드 경로·문구·서술 제외

stdlib만 사용.
"""

import hashlib
import json
import os
import re
import sys

SCHEMA = "changeguard.review/v1"
SEV_RANK = {"critical": 4, "high": 3, "medium": 2, "low": 1}
RANK_SEV = {v: k for k, v in SEV_RANK.items()}

# 정적 규칙 ID 목록 (deterministic.sh add_finding 의 5번째 인자와 일치해야 한다)
DET_RULES = {
    "det.workflow.change": "GitHub Actions workflow 파일 변경",
    "det.dependency.lockfile": "lockfile 변경",
    "det.dependency.lifecycle_script": "npm lifecycle script 추가",
    "det.pattern.pipe_to_shell": "원격 스크립트를 셸로 바로 실행",
    "det.pattern.eval_decode": "디코드 후 eval",
    "det.pattern.dynamic_shell": "동적 셸 명령 조립",
    "det.pattern.long_base64": "긴 base64 문자열",
    "det.binary.new": "신규 binary 파일",
    "det.injection.keyword": "prompt injection 키워드",
}

# v0.17 이하 결과의 description → rule 추론 (소급 변환 전용)
_LEGACY_DET = [
    ("GitHub Actions workflow", "det.workflow.change"),
    ("Lockfile 변경", "det.dependency.lockfile"),
    ("lifecycle script", "det.dependency.lifecycle_script"),
    ("Pipe-to-shell", "det.pattern.pipe_to_shell"),
    ("디코드 후 eval", "det.pattern.eval_decode"),
    ("동적 셸 명령", "det.pattern.dynamic_shell"),
    ("긴 base64", "det.pattern.long_base64"),
    ("binary 파일", "det.binary.new"),
    ("Prompt injection", "det.injection.keyword"),
]
_LEGACY_LLM_META = [
    ("분석을 거절", "llm.refused"),
    ("max_tokens", "llm.truncated"),
    ("파싱하지 못함", "llm.parse_error"),
    ("parsed as JSON", "llm.parse_error"),
    ("array가 아님", "llm.response_malformed"),
    ("invalid 항목", "llm.response_malformed"),
    ("상한(", "llm.diff_truncated"),
    ("ANTHROPIC_API_KEY 미설정", "llm.unavailable.no_key"),
    ("LLM 호출 실패", "llm.unavailable"),
]

# 경로 분류 — 규칙별 오탐 분석의 핵심 축 (테스트·문서·에이전트 지시 파일 등)
_AGENT_RE = re.compile(
    r'(^|/)(claude(\.local)?|agents?|gemini|skill|copilot-instructions)\.md$'
    r'|\.(instructions|prompt)\.md$|(^|/)\.claude/'
    r'|(^|/)plugins/[^/]+/(commands|agents|skills)/.*\.md$')
_TEST_RE = re.compile(r'(^|/)(tests?|__tests__|spec|fixtures?)/|\.(test|spec)\.[a-z0-9]+$|(^|/)test_[^/]+$')
_LOCK_RE = re.compile(r'(package-lock\.json|yarn\.lock|pnpm-lock\.yaml|uv\.lock|poetry\.lock|'
                      r'Cargo\.lock|pom\.xml|Gemfile\.lock|go\.sum|composer\.lock)$')


def classify_path(path):
    if not path or path in ("n/a", "diff", "llm_response"):
        return "n/a"
    p = path.lower()
    if p.startswith(".github/workflows/"):
        return "workflow"
    if _AGENT_RE.search(p):
        return "agent"
    if _TEST_RE.search(p):
        return "test"
    if _LOCK_RE.search(p):
        return "lockfile"
    if p.endswith((".md", ".markdown", ".txt", ".rst")):
        return "doc"
    return "code"


def split_location(loc):
    """'path:12-20' → ('path', 12). LLM은 'path (설명)' 형태도 쓴다 — 첫 토큰을 경로로."""
    loc = (loc or "").strip()
    m = re.match(r'^([^\s:(]+)(?::(\d+))?', loc)
    if not m:
        return loc, None
    return m.group(1), int(m.group(2)) if m.group(2) else None


def infer_rule(f):
    if f.get("rule"):
        return f["rule"]
    desc = f.get("description") or ""
    if f.get("source") == "deterministic":
        return next((r for k, r in _LEGACY_DET if k in desc), "det.unknown")
    if f.get("source") == "self-lint":
        return "self.finding_quality"
    if f.get("category") == "meta":
        rule = next((r for k, r in _LEGACY_LLM_META if k in desc), "llm.note")
        if rule == "llm.unavailable":
            rule = f"llm.unavailable.{_legacy_failure_class(desc)}"
        return rule
    return f"llm.{f.get('category') or 'unknown'}"


def _legacy_failure_class(text):
    t = text.lower()
    if "credit balance" in t:
        return "billing"
    if "usage limit" in t:
        return "quota"
    if "http 401" in t or "authentication" in t:
        return "auth"
    if "http 404" in t or "not_found" in t:
        return "model_not_found"
    if any(k in t for k in ("http 429", "http 5", "timeout", "urlerror", "overloaded")):
        return "transient"
    return "other"


def finding_id(rule, path, line):
    return hashlib.sha1(f"{rule}|{path}|{line or ''}".encode("utf-8")).hexdigest()[:12]


def _legacy_llm_run(result):
    """v0.17 결과의 summary 'LLM status: …' 문자열 → llm_run 추정."""
    status = ""
    for line in (result.get("summary") or "").splitlines():
        if line.startswith("LLM status:"):
            status = line.split(":", 1)[1].strip()
    run = {"state": "skipped"}
    if status.startswith("api"):
        run = {"state": "ok", "model": result.get("llm_model"),
               "model_source": result.get("llm_model_source")}
        if "refused" in status:
            run["state"] = "refused"
        elif "truncated" in status:
            run["state"] = "truncated"
        elif "fallback from" in status:
            run["fallback"] = True
    elif status.startswith("failed"):
        run = {"state": "failed", "failure_class": _legacy_failure_class(status)}
    elif status.startswith("api_key_missing"):
        run = {"state": "no_key", "failure_class": "no_key"}
    elif status.startswith("mock"):
        run = {"state": "mock"}
    if any(infer_rule(f) == "llm.parse_error" for f in result.get("findings", [])):
        run["state"] = "parse_error"
    return run


def run_context(env=None):
    """GitHub Actions 환경변수 → run 키. 로컬 실행이면 대부분 None."""
    env = env if env is not None else os.environ
    server = env.get("GITHUB_SERVER_URL", "https://github.com")
    repo = env.get("GITHUB_REPOSITORY")
    run_id = env.get("GITHUB_RUN_ID")
    pr = None
    ref = env.get("GITHUB_REF", "")
    m = re.match(r'refs/pull/(\d+)/', ref)
    if m:
        pr = int(m.group(1))
    return {
        "repo": repo,
        "event": env.get("GITHUB_EVENT_NAME"),
        "run_id": int(run_id) if run_id and run_id.isdigit() else run_id,
        "run_attempt": env.get("GITHUB_RUN_ATTEMPT"),
        "run_url": f"{server}/{repo}/actions/runs/{run_id}" if repo and run_id else None,
        "pr_number": pr,
        "head_sha": env.get("INCOMING_REF") or env.get("GITHUB_SHA"),
        "ref": ref or None,
    }


def normalize(result, run=None, level="full"):
    """결과 JSON → 정규화 레코드. level: full | minimal"""
    findings = []
    for f in result.get("findings", []):
        rule = infer_rule(f)
        path, line = split_location(f.get("location"))
        item = {
            "id": finding_id(rule, path, line),
            "rule": rule,
            "source": f.get("source"),
            "severity": f.get("severity"),
            "category": f.get("category"),
            "confidence": f.get("confidence"),
            "path_class": classify_path(path),
        }
        if f.get("downgraded_from"):
            item["downgraded_from"] = f["downgraded_from"]
        if level == "full":
            item.update({"path": path, "line": line, "description": f.get("description")})
        findings.append(item)

    def sev_max(pred):
        return max((SEV_RANK.get(x["severity"], 0) for x in findings if pred(x)), default=0)

    det_max = sev_max(lambda x: x["source"] == "deterministic")
    llm_threat_max = sev_max(lambda x: x["source"] == "llm" and x["category"] != "meta")
    unavailable_max = sev_max(lambda x: x["source"] == "llm" and x["category"] == "meta")

    verdict = result.get("verdict")
    top = max(det_max, llm_threat_max, unavailable_max,
              sev_max(lambda x: x["source"] not in ("deterministic", "llm")))
    if verdict == "pass" or top < 2:
        driver = "none"
    elif det_max == top:
        driver = "deterministic"
    elif llm_threat_max == top:
        driver = "llm"
    elif unavailable_max == top:
        driver = "llm_unavailable"
    else:
        driver = "other"

    if det_max and llm_threat_max:
        disagreement = ("agree" if det_max == llm_threat_max else
                        "llm_stricter" if llm_threat_max > det_max else "det_stricter")
    elif llm_threat_max >= 2:
        disagreement = "llm_stricter"
    elif det_max >= 2:
        disagreement = "det_only"
    else:
        disagreement = "none"

    llm_run = dict(result.get("llm_run") or _legacy_llm_run(result))
    assessment = result.get("assessment") or {}
    intent = assessment.get("intent")

    signals = []
    if verdict in ("block", "advisory"):
        signals.append(f"verdict_{verdict}")
    if llm_threat_max > det_max and llm_threat_max >= 2:
        signals.append("llm_stricter")
    # 정적 critical/high인데 LLM이 '의도된 변경'으로 보고 같은 수준의 위협을 못 찾음 → 오탐 후보
    if det_max >= 3 and intent == "intentional" and llm_threat_max < det_max:
        signals.append("det_fp_candidate")
    if llm_run.get("state") in ("failed", "refused", "truncated", "parse_error", "no_key"):
        signals.append(f"llm_{llm_run['state']}")
    if any(x["source"] == "self-lint" for x in findings):
        signals.append("self_lint")

    llm = {k: llm_run.get(k) for k in ("state", "failure_class", "requested_model", "model",
                                         "model_source", "fallback", "refusal_category",
                                         "usage", "latency_ms", "diff_truncated")
           if llm_run.get(k) is not None}
    if intent:
        llm["intent"] = intent
        llm["focus_count"] = len(assessment.get("reviewer_focus") or [])
        if level == "full":
            llm["rationale"] = assessment.get("rationale")
            llm["reviewer_focus"] = assessment.get("reviewer_focus")

    rule_counts = {}
    for x in findings:
        rule_counts[x["rule"]] = rule_counts.get(x["rule"], 0) + 1

    record = {
        "schema": SCHEMA,
        "level": level,
        "policy": {"version": result.get("policy_version"), "sha": result.get("policy_sha")},
        "run": run or {},
        "verdict": verdict,
        "verdict_driver": driver,
        "severity_max": {"deterministic": RANK_SEV.get(det_max), "llm": RANK_SEV.get(llm_threat_max)},
        "disagreement": disagreement,
        "signals": signals,
        "gating": result.get("_gating"),
        "llm": llm,
        "diff": result.get("diff_stats"),
        # 단계별 런타임 ms (v0.18+): deprecation · bump_verify · deterministic · adapter · llm · total
        "timings": result.get("timings") or {},
        "rule_counts": rule_counts,
        "findings": findings,
    }
    if result.get("policy_bump_verification"):
        record["policy_bump"] = {"overall": result["policy_bump_verification"].get("overall")}
    return record


def main():
    import argparse
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("result", help="review-result.json 경로")
    p.add_argument("--level", choices=["full", "minimal"], default="full")
    p.add_argument("--no-env", action="store_true", help="GitHub Actions 환경변수를 run 키로 쓰지 않음")
    args = p.parse_args()
    with open(args.result, encoding="utf-8") as fh:
        result = json.load(fh)
    run = {} if args.no_env else run_context()
    json.dump(normalize(result, run, args.level), sys.stdout, ensure_ascii=False, indent=2)
    print()


if __name__ == "__main__":
    main()
