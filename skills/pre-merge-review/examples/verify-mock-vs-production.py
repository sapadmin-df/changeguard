#!/usr/bin/env python3
"""
verify-mock-vs-production.py — mock 응답이 실제 LLM 동작과 일치하는지 검증

mock 응답은 fixture 작성 시점의 *가정*이지 *현실*이 아니다. 시간이 지나며
모델 업데이트, 정책 변경, 시스템 프롬프트 변경으로 실제 LLM 동작이 mock과
어긋날 수 있다. 이 스크립트는 fixture corpus 전체에 대해 실제 API를 호출하고
mock과 비교하여 *어긋남*을 보고한다.

비용 주의: 모든 fixture를 실제 호출하므로 API 비용 발생. 정책 repo의 주간
스케줄 CI 또는 수동 트리거로 실행 권장.

사용:
  export ANTHROPIC_API_KEY=...
  python verify-mock-vs-production.py [fixture-pattern]

출력:
  fixture별 mock vs production 비교 결과. 다음 차이를 보고:
  - severity 불일치 (mock advisory, production block)
  - category 불일치
  - finding 개수 차이가 큼
  - mock은 빈 findings이지만 production은 추가 finding

종료 코드:
  0 - 모든 mock이 production과 정합 (또는 허용 범위 내)
  1 - 하나 이상 어긋남
  2 - 환경 설정 오류
"""

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
ADAPTER = SCRIPT_DIR.parent / "checks" / "llm-adapter.py"
CHECKER = SCRIPT_DIR.parent / "checks" / "deterministic.sh"

ALLOWED_FIXTURES_PER_RUN = 30  # 안전 가드 — 한 번에 너무 많이 호출하지 않게


def call_adapter(diff_path, det_findings_path, mode):
    """어댑터를 호출하여 결과 반환. mode: 'mock-path' 또는 None (실 API)."""
    cmd = [
        str(ADAPTER),
        "--diff", str(diff_path),
        "--findings", str(det_findings_path),
        "--context", "ci",
    ]
    if mode:  # mock 경로
        cmd.extend(["--mock", mode])
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode not in (0, 1):
        raise RuntimeError(f"adapter failed: {result.stderr}")
    return json.loads(result.stdout)


def compare(mock_result, prod_result, fixture_name):
    """mock과 production 결과를 비교, 어긋남 리스트 반환."""
    diffs = []

    if mock_result["verdict"] != prod_result["verdict"]:
        diffs.append(
            f"verdict mismatch: mock={mock_result['verdict']} "
            f"prod={prod_result['verdict']}"
        )

    mock_llm = [f for f in mock_result["findings"] if f.get("source") == "llm"]
    prod_llm = [f for f in prod_result["findings"] if f.get("source") == "llm"]

    if len(mock_llm) != len(prod_llm):
        diffs.append(
            f"LLM finding count: mock={len(mock_llm)} prod={len(prod_llm)}"
        )

    # 카테고리 집합 비교 (순서 무관)
    mock_cats = sorted([(f["severity"], f["category"]) for f in mock_llm])
    prod_cats = sorted([(f["severity"], f["category"]) for f in prod_llm])
    if mock_cats != prod_cats:
        diffs.append(
            f"finding signature: mock={mock_cats} prod={prod_cats}"
        )

    return diffs


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("pattern", nargs="?", default="",
                   help="fixture 이름 패턴 (지정 시 해당하는 것만)")
    p.add_argument("--max", type=int, default=ALLOWED_FIXTURES_PER_RUN,
                   help="최대 호출 fixture 수 (비용 안전장치)")
    args = p.parse_args()

    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("ANTHROPIC_API_KEY 환경변수가 필요합니다.", file=sys.stderr)
        sys.exit(2)

    # mock이 있는 fixture만 대상
    fixtures = []
    for diff_file in SCRIPT_DIR.rglob("*.diff"):
        mock_file = diff_file.with_suffix(".mock-response.json")
        if mock_file.exists():
            if args.pattern and args.pattern not in diff_file.name:
                continue
            fixtures.append((diff_file, mock_file))

    if len(fixtures) > args.max:
        print(f"⚠️  대상 fixture {len(fixtures)}개 > 한도 {args.max}. "
              f"--max 또는 pattern으로 제한하세요.", file=sys.stderr)
        sys.exit(2)

    print(f"검증 대상: {len(fixtures)}개 fixture (실제 API 호출 발생)")
    print()

    drift_count = 0
    for diff_file, mock_file in fixtures:
        name = diff_file.stem
        # 결정론 결과 생성
        det_proc = subprocess.run([str(CHECKER), str(diff_file)],
                                  capture_output=True, text=True)
        det_path = Path("/tmp") / f"det-{name}.json"
        det_path.write_text(det_proc.stdout)

        try:
            mock_res = call_adapter(diff_file, det_path, str(mock_file))
            prod_res = call_adapter(diff_file, det_path, None)
        except Exception as e:
            print(f"  ERROR {name}: {e}")
            drift_count += 1
            continue
        finally:
            det_path.unlink(missing_ok=True)

        diffs = compare(mock_res, prod_res, name)
        if diffs:
            drift_count += 1
            print(f"  DRIFT {name}:")
            for d in diffs:
                print(f"    └ {d}")
            print(f"    mock summary:    {mock_res.get('summary', '')[:100]}")
            print(f"    prod summary:    {prod_res.get('summary', '')[:100]}")
        else:
            print(f"  OK    {name}")

    print()
    print(f"검증 완료: {len(fixtures) - drift_count} 정합 / {drift_count} 어긋남")
    sys.exit(1 if drift_count > 0 else 0)


if __name__ == "__main__":
    main()
