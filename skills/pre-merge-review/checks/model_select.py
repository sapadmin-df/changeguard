#!/usr/bin/env python3
"""
model_select.py — LLM 모델 선택 (v0.17+)

정책: 검사 모델도 정책의 일부다. 기본값은 *고정*(DEFAULT_MODEL)이며 변경은
PR + fixture 검증을 거친다. 새 Sonnet 출시는 스케줄 job(policy-self-test의
model-watch)이 Models API로 감지해 이슈로 알린다 — 사람은 drift 검증 후 merge.

선택 순서 (resolve_model):
  1. 명시 모델 (--model 인자 또는 LLM_MODEL 환경변수 / vars.LLM_MODEL)
  2. LLM_MODEL=auto  → Models API에서 최신 Sonnet (opt-in 자동 추종)
                        조회 실패 시 DEFAULT_MODEL로 폴백
  3. 그 외(미설정)   → DEFAULT_MODEL

stdlib만 사용 (CI에서 pip install 불필요 — llm-adapter.py와 동일 원칙).

CLI:
  model_select.py latest [--models-json PATH]
      최신 Sonnet id 출력 (Models API 또는 덤프 파일)
  model_select.py check-newer [--models-json PATH]
      DEFAULT_MODEL보다 새로운 Sonnet이 있으면 JSON 출력 + exit 10, 없으면 exit 0
  model_select.py resolve [--model X] [--models-json PATH]
      위 선택 순서로 결정된 모델과 출처를 JSON으로 출력

종료 코드: 0 정상 / 2 조회 실패 / 10 (check-newer) 새 모델 존재
"""

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

# 검토·fixture 검증을 거친 기본 모델. 변경은 PR로만 (model-watch 이슈 참고).
DEFAULT_MODEL = "claude-sonnet-5-5"
MODEL_LINE = "sonnet"
AUTO_TOKEN = "auto"

ANTHROPIC_MODELS_API = "https://api.anthropic.com/v1/models"
ANTHROPIC_VERSION = "2023-06-01"
HTTP_TIMEOUT_SEC = 15
MAX_PAGES = 20


class ModelLookupError(Exception):
    pass


def fetch_models(api_key, timeout=HTTP_TIMEOUT_SEC):
    """GET /v1/models 전 페이지 (has_more/last_id → after_id)."""
    models = []
    after_id = None
    for _ in range(MAX_PAGES):
        params = {"limit": "1000"}
        if after_id:
            params["after_id"] = after_id
        req = urllib.request.Request(
            f"{ANTHROPIC_MODELS_API}?{urllib.parse.urlencode(params)}",
            headers={"x-api-key": api_key, "anthropic-version": ANTHROPIC_VERSION},
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                page = json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, OSError, json.JSONDecodeError) as e:
            raise ModelLookupError(f"{type(e).__name__}: {e}") from e
        models.extend(page.get("data") or [])
        if not page.get("has_more") or not page.get("last_id"):
            return models
        after_id = page["last_id"]
    return models


def load_models_json(path):
    """/v1/models 응답 덤프 파일 로드 (오프라인 테스트용)."""
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f).get("data") or []
    except (OSError, json.JSONDecodeError, AttributeError) as e:
        raise ModelLookupError(f"models json load error: {e}") from e


def _is_line(model, line):
    if model.get("line"):
        return model["line"] == line
    # line 필드가 없는 응답 형식 대비 — id 접두어로 판정
    return str(model.get("id", "")).startswith(f"claude-{line}-")


def _is_usable(model):
    if model.get("deprecated_at"):
        return False
    lifecycle = model.get("lifecycle")
    return lifecycle in (None, "active")


def sonnet_candidates(models, line=MODEL_LINE):
    """해당 line의 사용 가능한 모델을 created_at 내림차순으로."""
    cands = [m for m in models
             if isinstance(m, dict) and m.get("id") and _is_line(m, line) and _is_usable(m)]
    # created_at은 RFC 3339 UTC — 문자열 정렬이 시간 순서와 같다
    return sorted(cands, key=lambda m: m.get("created_at") or "", reverse=True)


def latest_model_id(models, line=MODEL_LINE):
    cands = sonnet_candidates(models, line)
    return cands[0]["id"] if cands else None


def newer_than(models, pinned=DEFAULT_MODEL, line=MODEL_LINE):
    """pinned보다 created_at이 늦은 같은 line 모델 목록 (pinned가 목록에 없으면 None)."""
    cands = sonnet_candidates(models, line)
    pinned_entry = next((m for m in models if isinstance(m, dict) and m.get("id") == pinned), None)
    if pinned_entry is None:
        return None
    pinned_at = pinned_entry.get("created_at") or ""
    return [m for m in cands if (m.get("created_at") or "") > pinned_at]


def _get_models(api_key=None, models_json=None):
    models_json = models_json or os.environ.get("LLM_MODELS_JSON")
    if models_json:
        return load_models_json(models_json)
    if not api_key:
        raise ModelLookupError("ANTHROPIC_API_KEY 없음 — Models API 조회 불가")
    return fetch_models(api_key)


def resolve_model(explicit=None, api_key=None, models_json=None):
    """(model_id, source) 반환. source는 결과 JSON의 llm_model_source에 기록된다."""
    requested = (explicit or os.environ.get("LLM_MODEL") or "").strip()
    if requested and requested.lower() != AUTO_TOKEN:
        return requested, "explicit"
    if requested.lower() == AUTO_TOKEN:
        try:
            latest = latest_model_id(_get_models(api_key, models_json))
            if latest:
                return latest, "auto (models api)"
            return DEFAULT_MODEL, "auto-fallback (sonnet 후보 없음)"
        except ModelLookupError as e:
            print(f"[model-select] auto 조회 실패 — 기본값 사용: {e}", file=sys.stderr)
            return DEFAULT_MODEL, "auto-fallback (models api 조회 실패)"
    return DEFAULT_MODEL, "default (pinned)"


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("command", choices=["latest", "check-newer", "resolve"])
    p.add_argument("--models-json", help="/v1/models 응답 덤프 파일 (없으면 API 조회)")
    p.add_argument("--model", help="resolve: 명시 모델")
    args = p.parse_args()
    api_key = os.environ.get("ANTHROPIC_API_KEY")

    if args.command == "resolve":
        model, source = resolve_model(args.model, api_key, args.models_json)
        print(json.dumps({"model": model, "source": source}))
        return 0

    try:
        models = _get_models(api_key, args.models_json)
    except ModelLookupError as e:
        print(f"models lookup failed: {e}", file=sys.stderr)
        return 2

    if args.command == "latest":
        latest = latest_model_id(models)
        if not latest:
            print("sonnet 후보 없음", file=sys.stderr)
            return 2
        print(latest)
        return 0

    newer = newer_than(models)
    pinned_entry = next((m for m in models
                         if isinstance(m, dict) and m.get("id") == DEFAULT_MODEL), None)
    pinned_usable = pinned_entry is not None and _is_usable(pinned_entry)
    out = {
        "pinned": DEFAULT_MODEL,
        "latest": latest_model_id(models),
        "pinned_usable": pinned_usable,
        "pinned_deprecated_at": (pinned_entry or {}).get("deprecated_at"),
        "pinned_retires_at": (pinned_entry or {}).get("retires_at"),
        "newer": [{"id": m["id"], "display_name": m.get("display_name"),
                   "created_at": m.get("created_at")} for m in (newer or [])],
    }
    print(json.dumps(out, ensure_ascii=False, indent=2))
    # 기본 모델이 사용 불가(폐기·미등록)이거나 더 새 Sonnet이 있으면 사람 개입 필요
    return 10 if (not pinned_usable or newer) else 0


if __name__ == "__main__":
    sys.exit(main())
