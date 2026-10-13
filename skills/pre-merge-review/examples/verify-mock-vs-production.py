#!/usr/bin/env python3
"""
verify-mock-vs-production.py — 실제 LLM이 fixture 기대치를 만족하는지 검증

mock 응답은 fixture 작성 시점의 *가정*이지 *현실*이 아니다. 시간이 지나며
모델 업데이트, 정책 변경, 시스템 프롬프트 변경으로 실제 LLM 동작이 mock과
어긋날 수 있다. 이 스크립트는 mock이 있는 fixture에 대해 실제 API를 호출하고:

  - DRIFT (실패): 실제 결과가 expected.json 계약을 어김
      malicious → verdict ≥ expected_verdict, must_detect(+_via_llm) 충족
      그 외     → max severity ≤ max_severity_allowed (기본 low), min_verdict 충족
  - note (정보): mock과 실제의 LLM finding (severity, category) 서명이 다름
      — 비결정적 모델에서 정확 일치는 기대하지 않는다 (v0.17: 실패 → 정보로 변경)

caller(ci-caller.py)와 같은 SHA-only swap 강등·게이팅을 적용한다. 게이팅이 LLM을
부르지 않는 fixture는 production에서도 LLM이 호출되지 않으므로 건너뛴다.
adversarial/ 는 *일부러 틀린* LLM 응답으로 어댑터 방어를 검증하므로 대상이 아니다.

모델: LLM_MODEL 환경변수 (비우면 정책 기본값, 'auto' 가능) — 후보 모델 사전 검증용.

비용 주의: fixture마다 실제 호출. 정책 repo의 주간 스케줄 CI 또는 수동 트리거로 실행.

사용:
  export ANTHROPIC_API_KEY=...
  python verify-mock-vs-production.py [fixture-pattern] [--max N]

종료 코드: 0 전부 계약 충족 / 1 하나 이상 DRIFT·오류 / 2 환경 설정 오류
"""

import argparse
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CHECKS = SCRIPT_DIR.parent / "checks"
ADAPTER = CHECKS / "llm-adapter.py"
CHECKER = CHECKS / "deterministic.sh"
CALLER = CHECKS / "ci-caller.py"

ALLOWED_FIXTURES_PER_RUN = 40  # 안전 가드 — 한 번에 너무 많이 호출하지 않게
SEV_RANK = {"critical": 4, "high": 3, "medium": 2, "low": 1}
VERDICT_RANK = {"block": 3, "advisory": 2, "pass": 1}


def find_bash():
    """deterministic.sh 실행용 bash. Windows에서 PATH의 bash는 WSL(System32)일 수 있어
    Git Bash를 우선한다 — WSL bash로는 checker가 실패해 결정론 결과가 빈 배열이 된다."""
    if os.name == "nt":
        for cand in (r"C:\Program Files\Git\bin\bash.exe", r"C:\Program Files\Git\usr\bin\bash.exe"):
            if Path(cand).exists():
                return cand
    return shutil.which("bash") or "bash"


def load_caller():
    spec = importlib.util.spec_from_file_location("ci_caller", CALLER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def call_adapter(diff_path, det_findings_path, mock_path=None):
    """어댑터 호출. mock_path가 없으면 실제 API."""
    cmd = [sys.executable, str(ADAPTER), "--diff", str(diff_path),
           "--findings", str(det_findings_path), "--context", "ci"]
    if mock_path:
        cmd.extend(["--mock", str(mock_path)])
    result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8")
    if result.returncode not in (0, 1):
        raise RuntimeError(f"adapter failed: {result.stderr[-500:]}")
    return json.loads(result.stdout)


def contract_violations(result, expected):
    """expected.json 계약 위반 목록 (run-tests-integrated.sh 와 같은 규칙)."""
    out = []
    findings = result.get("findings", [])
    verdict = result.get("verdict")
    ftype = expected.get("type")

    def max_sev():
        ranks = [SEV_RANK.get(f.get("severity"), 0) for f in findings]
        return max(ranks, default=0)

    def check_must(reqs):
        for req in reqs:
            need = SEV_RANK.get(req.get("min_severity"), 0)
            got = [SEV_RANK.get(f.get("severity"), 0) for f in findings
                   if f.get("category") == req.get("category")]
            if not any(g >= need for g in got):
                out.append(f"missing {req.get('category')} >= {req.get('min_severity')}")

    if ftype == "malicious":
        exp = expected.get("expected_verdict")
        if exp and VERDICT_RANK.get(verdict, 0) < VERDICT_RANK.get(exp, 0):
            out.append(f"verdict {verdict} < expected {exp}")
        check_must((expected.get("must_detect") or []) + (expected.get("must_detect_via_llm") or []))
    else:
        allowed = expected.get("max_severity_allowed", "low")
        if max_sev() > SEV_RANK.get(allowed, 1):
            sev = [f"{f.get('severity')}/{f.get('category')}" for f in findings
                   if SEV_RANK.get(f.get("severity"), 0) > SEV_RANK.get(allowed, 1)]
            out.append(f"severity above allowed {allowed}: {sev}")
        check_must(expected.get("must_detect") or [])
    min_v = expected.get("min_verdict")
    if min_v and VERDICT_RANK.get(verdict, 0) < VERDICT_RANK.get(min_v, 0):
        out.append(f"verdict {verdict} < min_verdict {min_v}")
    return out


def llm_signature(result):
    return sorted((f["severity"], f["category"]) for f in result.get("findings", [])
                  if f.get("source") == "llm")


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("pattern", nargs="?", default="",
                   help="fixture 이름 패턴 (지정 시 해당하는 것만)")
    p.add_argument("--max", type=int, default=ALLOWED_FIXTURES_PER_RUN,
                   help="최대 호출 fixture 수 (비용 안전장치)")
    p.add_argument("--json-out", help="fixture별 결과를 JSON으로 저장")
    args = p.parse_args()

    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("ANTHROPIC_API_KEY 환경변수가 필요합니다.", file=sys.stderr)
        sys.exit(2)

    caller = load_caller()
    fixtures = []
    for diff_file in sorted(SCRIPT_DIR.rglob("*.diff")):
        if "adversarial" in diff_file.parts:
            continue
        mock_file = diff_file.with_name(diff_file.stem + ".mock-response.json")
        expected_file = diff_file.with_name(diff_file.stem + ".expected.json")
        if not (mock_file.exists() and expected_file.exists()):
            continue
        if args.pattern and args.pattern not in diff_file.name:
            continue
        fixtures.append((diff_file, mock_file, expected_file))

    if len(fixtures) > args.max:
        print(f"⚠️  대상 fixture {len(fixtures)}개 > 한도 {args.max}. "
              f"--max 또는 pattern으로 제한하세요.", file=sys.stderr)
        sys.exit(2)

    print(f"검증 대상: {len(fixtures)}개 fixture (실제 API 호출 발생) · "
          f"LLM_MODEL={os.environ.get('LLM_MODEL') or '(정책 기본값)'}")
    print()

    drift_count = 0
    records = []
    for diff_file, mock_file, expected_file in fixtures:
        name = diff_file.stem
        diff = diff_file.read_text(encoding="utf-8", errors="replace")
        expected = json.loads(expected_file.read_text(encoding="utf-8"))
        det_proc = subprocess.run([find_bash(), str(CHECKER), str(diff_file)],
                                  capture_output=True, text=True, encoding="utf-8")
        if det_proc.returncode != 0:
            print(f"  ERROR {name}: deterministic.sh 실패 — {det_proc.stderr[-300:]}")
            drift_count += 1
            records.append({"fixture": name, "error": "deterministic.sh failed"})
            continue
        det = caller.downgrade_sha_only_workflow_findings(diff, json.loads(det_proc.stdout or "[]"))
        if not caller.should_call_llm(det, diff)[0]:
            print(f"  skip  {name} (게이팅: LLM 미호출)")
            continue

        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False,
                                         encoding="utf-8") as tf:
            json.dump(det, tf)
            det_path = tf.name
        try:
            mock_res = call_adapter(diff_file, det_path, mock_file)
            prod_res = call_adapter(diff_file, det_path)
        except Exception as e:
            print(f"  ERROR {name}: {e}")
            drift_count += 1
            records.append({"fixture": name, "error": str(e)})
            continue
        finally:
            Path(det_path).unlink(missing_ok=True)

        violations = contract_violations(prod_res, expected)
        sig_mock, sig_prod = llm_signature(mock_res), llm_signature(prod_res)
        model = prod_res.get("llm_model", "?")
        status = prod_res.get("summary", "").splitlines()[-1] if prod_res.get("summary") else ""
        records.append({"fixture": name, "model": model, "verdict": prod_res.get("verdict"),
                        "violations": violations, "llm_signature": sig_prod,
                        "mock_signature": sig_mock,
                        "intent": (prod_res.get("assessment") or {}).get("intent")})
        if violations:
            drift_count += 1
            print(f"  DRIFT {name}  [{model}] v={prod_res.get('verdict')}")
            for v in violations:
                print(f"    └ {v}")
            for f in prod_res.get("findings", []):
                if f.get("source") == "llm":
                    print(f"      llm: [{f['severity']}/{f['category']}] {f['description'][:160]}")
        else:
            note = "" if sig_mock == sig_prod else f"  (note: mock {sig_mock} / prod {sig_prod})"
            print(f"  OK    {name}  [{model}] v={prod_res.get('verdict')}{note}")
        if "LLM status" in status or "failed" in status:
            print(f"    {status}")

    if args.json_out:
        Path(args.json_out).write_text(json.dumps(records, ensure_ascii=False, indent=2),
                                       encoding="utf-8")

    print()
    print(f"검증 완료: {len(records) - drift_count} 계약 충족 / {drift_count} DRIFT·오류")
    sys.exit(1 if drift_count > 0 else 0)


if __name__ == "__main__":
    main()
