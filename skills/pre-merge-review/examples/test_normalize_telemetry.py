#!/usr/bin/env python3
"""
test_normalize_telemetry.py — 정규화 레코드·telemetry 단위 테스트 (네트워크 없음, v0.18)

사용: python3 test_normalize_telemetry.py
"""

import io
import json
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "checks"))
import normalize as nz  # noqa: E402
import telemetry as tm  # noqa: E402


def result(verdict, findings, **extra):
    r = {"policy_version": "0.18.0", "policy_sha": "a" * 40, "verdict": verdict,
         "findings": findings, "summary": "Verdict: x\nFindings: x\nLLM status: api (claude-sonnet-5-5)",
         "diff_stats": {"files_changed": 1, "lines_added": 1, "lines_removed": 0}}
    r.update(extra)
    return r


DET_PIPE = {"severity": "critical", "category": "pattern", "location": "tests/run.sh:79",
            "description": "Pipe-to-shell 실행 패턴 (curl|sh)", "source": "deterministic"}
LLM_NET = {"severity": "medium", "category": "network", "location": "src/a.py:3",
           "description": "TLS 검증 끔", "source": "llm", "confidence": "high"}


class Normalize(unittest.TestCase):
    def test_legacy_rule_inference_and_ids(self):
        rec = nz.normalize(result("block", [DET_PIPE]), {})
        f = rec["findings"][0]
        self.assertEqual(f["rule"], "det.pattern.pipe_to_shell")
        self.assertEqual(f["path_class"], "test")
        self.assertEqual(f["id"], nz.finding_id("det.pattern.pipe_to_shell", "tests/run.sh", 79))

    def test_path_classes(self):
        cases = {".github/workflows/x.yml": "workflow", "CLAUDE.md": "agent",
                 "ai-commands/plugins/p/commands/c.md": "agent", "src/x.test.ts": "test",
                 "package-lock.json": "lockfile", "docs/a.md": "doc", "src/a.py": "code", "diff": "n/a"}
        for path, cls in cases.items():
            self.assertEqual(nz.classify_path(path), cls, path)

    def test_det_fp_candidate_signal(self):
        rec = nz.normalize(result("block", [DET_PIPE], assessment={"intent": "intentional",
                                                                    "rationale": "r", "reviewer_focus": []}), {})
        self.assertEqual(rec["verdict_driver"], "deterministic")
        self.assertIn("det_fp_candidate", rec["signals"])
        self.assertEqual(rec["disagreement"], "det_only")

    def test_llm_stricter_signal(self):
        rec = nz.normalize(result("advisory", [LLM_NET]), {})
        self.assertEqual(rec["verdict_driver"], "llm")
        self.assertEqual(rec["disagreement"], "llm_stricter")
        self.assertIn("llm_stricter", rec["signals"])

    def test_legacy_billing_failure(self):
        meta = {"severity": "medium", "category": "meta", "location": "n/a", "source": "llm",
                "description": "LLM 호출 실패 (model=x): HTTP 400 invalid_request_error: Your credit balance is too low"}
        r = result("advisory", [meta])
        r["summary"] = "a\nb\nLLM status: failed (HTTP 400 invalid_request_error: Your credit balance is too low)"
        rec = nz.normalize(r, {})
        self.assertEqual(rec["findings"][0]["rule"], "llm.unavailable.billing")
        self.assertEqual(rec["llm"]["failure_class"], "billing")
        self.assertEqual(rec["verdict_driver"], "llm_unavailable")
        self.assertIn("llm_failed", rec["signals"])

    def test_minimal_strips_content(self):
        rec = nz.normalize(result("advisory", [LLM_NET], assessment={"intent": "suspicious",
                                                                     "rationale": "비밀", "reviewer_focus": ["x"]}),
                           {}, level="minimal")
        f = rec["findings"][0]
        self.assertNotIn("path", f)
        self.assertNotIn("description", f)
        self.assertNotIn("rationale", rec["llm"])
        self.assertEqual(rec["llm"]["intent"], "suspicious")

    def test_run_context_pr(self):
        ctx = nz.run_context({"GITHUB_REPOSITORY": "o/r", "GITHUB_RUN_ID": "42",
                              "GITHUB_REF": "refs/pull/7/merge", "GITHUB_EVENT_NAME": "pull_request"})
        self.assertEqual(ctx["pr_number"], 7)
        self.assertEqual(ctx["run_url"], "https://github.com/o/r/actions/runs/42")


class Telemetry(unittest.TestCase):
    def setUp(self):
        self.rec = nz.normalize(result("block", [DET_PIPE], assessment={"intent": "intentional",
                                                                         "rationale": "r", "reviewer_focus": []}),
                                {"repo": "acme/private-app", "pr_number": 9, "run_id": 1,
                                 "run_url": "https://github.com/acme/private-app/actions/runs/1"})

    def run_main(self, argv, env):
        out = io.StringIO()
        with mock.patch.object(sys, "argv", ["telemetry.py", *argv]), \
                mock.patch.dict("os.environ", env, clear=False), redirect_stdout(out):
            rc = tm.main()
        return rc, out.getvalue()

    def test_default_off_sends_nothing(self):
        with mock.patch.object(tm, "api") as api:
            p = Path(self._tmp("rec.json")); p.write_text(json.dumps(self.rec), encoding="utf-8")
            rc, out = self.run_main(["--record", str(p)], {"CG_TELEMETRY": "off"})
        self.assertEqual(rc, 0)
        api.assert_not_called()
        self.assertIn("off", out)

    def test_private_source_to_public_target_forced_minimal(self):
        calls = []

        def fake(method, path, token, body=None):
            calls.append((method, path, body))
            if path == "/repos/sapadmin-df/changeguard":
                return {"private": False}
            if path.startswith("/search/issues"):
                return {"items": []}
            if path.endswith("/issues") and method == "POST":
                return {"html_url": "https://github.com/x/y/issues/1"}
            return {}
        with mock.patch.object(tm, "api", side_effect=fake):
            url, level = tm.post(self.rec, "sapadmin-df/changeguard", "t", "full", "private")
        self.assertEqual(level, "minimal")
        body = next(b for m, p, b in calls if p.endswith("/issues") and m == "POST")["body"]
        self.assertNotIn("tests/run.sh", body)          # 경로 미노출
        self.assertIn("cg-key: acme/private-app#pr-9", body)

    def test_same_pr_appends_comment(self):
        calls = []

        def fake(method, path, token, body=None):
            calls.append((method, path))
            if path.startswith("/search/issues"):
                return {"items": [{"number": 5}]}
            if path == "/repos/o/telemetry":
                return {"private": True}
            return {"html_url": "https://github.com/o/telemetry/issues/5"}
        with mock.patch.object(tm, "api", side_effect=fake):
            url, level = tm.post(self.rec, "o/telemetry", "t", "full", "private")
        self.assertEqual(level, "full")
        self.assertIn(("POST", "/repos/o/telemetry/issues/5/comments"), calls)
        self.assertNotIn(("POST", "/repos/o/telemetry/issues"), calls)

    def _tmp(self, name):
        import tempfile
        d = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(d, ignore_errors=True))
        return str(Path(d) / name)


if __name__ == "__main__":
    unittest.main(verbosity=1)
