#!/usr/bin/env python3
"""
test_model_select.py — model_select.py 단위 테스트 (네트워크 없음)

models/v1-models-2026-10-12.json 은 실제 GET /v1/models 응답 덤프 (2026-10-12).
Models API 응답 형식이 바뀌면 이 덤프를 갱신하고 기대값을 다시 확인한다.

사용: python3 test_model_select.py
"""

import io
import json
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "checks"))

import model_select as ms  # noqa: E402

DUMP = HERE / "models" / "v1-models-2026-10-12.json"


def _m(id_, created, line=None, lifecycle="active", deprecated_at=None):
    d = {"id": id_, "created_at": created, "lifecycle": lifecycle, "deprecated_at": deprecated_at}
    if line:
        d["line"] = line
    return d


class LatestSonnet(unittest.TestCase):
    def setUp(self):
        self.models = ms.load_models_json(DUMP)

    def test_dump_latest_sonnet(self):
        self.assertEqual(ms.latest_model_id(self.models), "claude-sonnet-5-5")

    def test_pinned_default_is_listed_and_current(self):
        # 기본값을 바꾸면 이 덤프 기준 최신 Sonnet과 같은지 확인하게 만든다
        self.assertEqual(ms.newer_than(self.models), [])
        ids = {m["id"] for m in self.models}
        self.assertIn(ms.DEFAULT_MODEL, ids)

    def test_newer_than_old_pin(self):
        newer = [m["id"] for m in ms.newer_than(self.models, pinned="claude-sonnet-4-6")]
        self.assertEqual(newer, ["claude-sonnet-5-5", "claude-sonnet-5"])

    def test_only_sonnet_line(self):
        # haiku-5-5 가 더 최근이지만 sonnet line 이 아니므로 제외
        self.assertTrue(all(m["line"] == "sonnet" for m in ms.sonnet_candidates(self.models)))

    def test_deprecated_and_inactive_skipped(self):
        models = [
            _m("claude-sonnet-9", "2027-01-01T00:00:00Z", "sonnet", deprecated_at="2027-02-01T00:00:00Z"),
            _m("claude-sonnet-8", "2026-12-01T00:00:00Z", "sonnet", lifecycle="preview"),
            _m("claude-sonnet-7", "2026-11-01T00:00:00Z", "sonnet"),
        ]
        self.assertEqual(ms.latest_model_id(models), "claude-sonnet-7")

    def test_id_prefix_when_line_missing(self):
        models = [_m("claude-sonnet-7", "2026-11-01T00:00:00Z"),
                  _m("claude-opus-9", "2027-01-01T00:00:00Z")]
        self.assertEqual(ms.latest_model_id(models), "claude-sonnet-7")

    def test_pinned_missing_returns_none(self):
        self.assertIsNone(ms.newer_than([_m("claude-sonnet-7", "2026-11-01T00:00:00Z", "sonnet")],
                                        pinned="claude-sonnet-x"))


class Resolve(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.dict(os.environ, {}, clear=False)
        patcher.start()
        self.addCleanup(patcher.stop)
        for k in ("LLM_MODEL", "LLM_MODELS_JSON"):
            os.environ.pop(k, None)

    def test_default_is_pinned(self):
        self.assertEqual(ms.resolve_model(), (ms.DEFAULT_MODEL, "default (pinned)"))

    def test_explicit_env(self):
        os.environ["LLM_MODEL"] = "claude-opus-5-5"
        self.assertEqual(ms.resolve_model(), ("claude-opus-5-5", "explicit"))

    def test_explicit_arg_beats_env(self):
        os.environ["LLM_MODEL"] = "claude-opus-5-5"
        self.assertEqual(ms.resolve_model("claude-sonnet-5"), ("claude-sonnet-5", "explicit"))

    def test_blank_env_is_default(self):
        os.environ["LLM_MODEL"] = "  "
        self.assertEqual(ms.resolve_model()[1], "default (pinned)")

    def test_auto_uses_models(self):
        os.environ["LLM_MODEL"] = "AUTO"
        self.assertEqual(ms.resolve_model(models_json=str(DUMP)),
                         ("claude-sonnet-5-5", "auto (models api)"))

    def test_auto_lookup_failure_falls_back(self):
        os.environ["LLM_MODEL"] = "auto"
        with mock.patch("sys.stderr", new=io.StringIO()):
            model, source = ms.resolve_model(api_key=None)
        self.assertEqual(model, ms.DEFAULT_MODEL)
        self.assertTrue(source.startswith("auto-fallback"))


class Pagination(unittest.TestCase):
    def test_follows_has_more(self):
        pages = [
            {"data": [_m("claude-sonnet-7", "2026-11-01T00:00:00Z", "sonnet")],
             "has_more": True, "last_id": "claude-sonnet-7"},
            {"data": [_m("claude-sonnet-8", "2026-12-01T00:00:00Z", "sonnet")],
             "has_more": False, "last_id": "claude-sonnet-8"},
        ]
        urls = []

        def fake_urlopen(req, timeout):
            urls.append(req.full_url)
            body = io.BytesIO(json.dumps(pages[len(urls) - 1]).encode())
            body.__enter__ = lambda s=body: s
            body.__exit__ = lambda *a: False
            return body

        with mock.patch.object(ms.urllib.request, "urlopen", side_effect=fake_urlopen):
            models = ms.fetch_models("test-key")
        self.assertEqual([m["id"] for m in models], ["claude-sonnet-7", "claude-sonnet-8"])
        self.assertIn("after_id=claude-sonnet-7", urls[1])
        self.assertEqual(ms.latest_model_id(models), "claude-sonnet-8")


if __name__ == "__main__":
    unittest.main(verbosity=1)
