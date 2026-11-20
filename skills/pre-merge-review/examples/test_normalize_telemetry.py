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
            url, level, *_ = tm.post(self.rec, "sapadmin-df/changeguard", "t", "full", "private")
        self.assertEqual(level, "minimal")
        body = next(b for m, p, b in calls if p.endswith("/issues") and m == "POST")["body"]
        self.assertNotIn("tests/run.sh", body)          # 경로 미노출
        self.assertIn("cg-key: acme/private-app#pr-9", body)

    def test_same_pr_appends_comment(self):
        calls = []

        def fake(method, path, token, body=None):
            calls.append((method, path))
            if path.startswith("/search/issues"):
                return {"items": [{"number": 5, "body": "<!-- cg-key: acme/private-app#pr-9 -->"}]}
            if path == "/repos/o/telemetry":
                return {"private": True}
            return {"html_url": "https://github.com/o/telemetry/issues/5"}
        with mock.patch.object(tm, "api", side_effect=fake):
            url, level, *_ = tm.post(self.rec, "o/telemetry", "t", "full", "private", True)
        self.assertEqual(level, "full")
        self.assertIn(("POST", "/repos/o/telemetry/issues/5/comments"), calls)
        self.assertNotIn(("POST", "/repos/o/telemetry/issues"), calls)

    # --- REQ-C.3 경계 (docs/REQUIREMENTS.md) ---
    def mixed(self):
        return nz.normalize(result("block", [DET_PIPE, LLM_NET], assessment={
            "intent": "suspicious", "rationale": "근거 서술", "reviewer_focus": ["x"]}),
            {"repo": "acme/private-app", "pr_number": 9})

    def test_consent_allows_full_but_hides_high_plus(self):   # C.3-2 + C.3-3
        r, level, notes = tm.prepare(self.mixed(), "full", "private", "public", share_private=True)
        self.assertEqual(level, "full")
        hi = next(f for f in r["findings"] if f["severity"] == "critical")
        med = next(f for f in r["findings"] if f["severity"] == "medium")
        self.assertEqual(hi.get("redacted"), "high+")
        self.assertNotIn("path", hi)
        self.assertNotIn("description", hi)
        self.assertEqual(med["path"], "src/a.py")             # medium 은 동의 하에 공개
        self.assertNotIn("rationale", r["llm"])
        self.assertTrue(any("동의" in n for n in notes))

    def test_no_consent_still_minimal(self):                     # C.3-2
        r, level, _ = tm.prepare(self.mixed(), "full", "private", "public", share_private=False)
        self.assertEqual(level, "minimal")
        self.assertTrue(all("path" not in f for f in r["findings"]))

    def test_private_intake_with_consent_keeps_detail(self):    # DP-1b: private 접수함
        r, level, notes = tm.prepare(self.mixed(), "full", "private", "private", share_private=True)
        self.assertEqual(level, "full")
        self.assertIn("path", next(f for f in r["findings"] if f["severity"] == "critical"))
        self.assertIn("rationale", r["llm"])
        self.assertTrue(any("private 접수함" in n for n in notes))

    def test_private_intake_without_consent_minimal(self):      # C.3-2: 열람자가 다르다
        r, level, notes = tm.prepare(self.mixed(), "full", "private", "private", share_private=False)
        self.assertEqual(level, "minimal")
        self.assertTrue(all("path" not in f for f in r["findings"]))
        self.assertTrue(any("동의" in n for n in notes))

    def test_public_source_full_without_consent(self):
        r, level, _ = tm.prepare(self.mixed(), "full", "public", "private")
        self.assertEqual(level, "full")

    def test_unknown_target_treated_public(self):
        r, _, _ = tm.prepare(self.mixed(), "full", "public", "unknown")
        self.assertEqual(next(f for f in r["findings"] if f["severity"] == "critical").get("redacted"), "high+")

    def test_untrusted_text_rendering(self):                     # C.3-4
        evil = dict(LLM_NET, location="src/@admin|x`y`.py:3", description="```\n@everyone " + "A" * 900)
        rec = nz.normalize(result("advisory", [evil]), {"repo": "o/pub"})
        r, _, _ = tm.prepare(rec, "full", "public", "public")
        f = r["findings"][0]
        self.assertLessEqual(len(f["description"]), tm.TEXT_MAX)
        row = tm.summary_md(r).splitlines()[-1]
        self.assertEqual(row.count("|"), 6)                       # 표 칸이 깨지지 않음
        self.assertIn("`src/@admin/x'y'.py:3`", row)               # @멘션은 code span 안
        block = tm.record_block(r)
        fence = block.split("json", 1)[0]
        self.assertGreater(len(fence), 3)                         # 내용 속 ``` 보다 긴 fence
        self.assertTrue(block.endswith(fence))

    def test_default_repo_is_desk_intake(self):
        seen = {}

        def fake_post(record, repo, *a, **k):
            seen["repo"] = repo
            return "u", "minimal", "t", "b"
        p = Path(self._tmp("rec.json")); p.write_text(json.dumps(self.rec), encoding="utf-8")
        with mock.patch.object(tm, "post", side_effect=fake_post):
            self.run_main(["--record", str(p)], {"CG_TELEMETRY": "minimal", "CG_TELEMETRY_REPO": "",
                                                "CHANGEGUARD_TELEMETRY_TOKEN": "t", "GITHUB_TOKEN": ""})
        self.assertEqual(seen["repo"], "sapadmin-df-desk/changeguard-telemetry")

    # --- 권한 없는 계정 (REQ-C.3-5) ---
    def test_issue_created_without_labels_and_prefixed(self):
        calls = []

        def fake(method, path, token, body=None):
            calls.append((method, path, body))
            if path.startswith("/search/issues"):
                return {"items": []}
            if path == "/repos/sapadmin-df/changeguard":
                return {"private": False}
            return {"html_url": "u"}
        with mock.patch.object(tm, "api", side_effect=fake):
            tm.post(self.rec, "sapadmin-df/changeguard", "t", "minimal", "private")
        self.assertFalse(any("/labels" in p for _, p, _ in calls))
        create = next(b for m, p, b in calls if m == "POST" and p.endswith("/issues"))
        self.assertNotIn("labels", create)
        self.assertTrue(create["title"].startswith("[cg-telemetry]"))
        search = next(p for m, p, _ in calls if p.startswith("/search/issues"))
        self.assertIn("author%3A%40me", search)                   # 남의 issue에 끼어들지 않음

    def test_find_issue_ignores_other_keys(self):
        with mock.patch.object(tm, "api", return_value={"items": [
                {"number": 3, "body": "<!-- cg-key: acme/x#pr-90 -->"},
                {"number": 4, "body": "<!-- cg-key: acme/x#pr-9 -->"}]}):
            self.assertEqual(tm.find_issue("o/r", "t", "acme/x#pr-9"), 4)

    def test_no_token_writes_preview_only(self):                # DP-7
        p = Path(self._tmp("rec.json")); p.write_text(json.dumps(self.rec), encoding="utf-8")
        prev = Path(self._tmp("preview.md"))
        with mock.patch.object(tm, "api") as api:
            rc, out = self.run_main(["--record", str(p), "--preview", str(prev)],
                                    {"CG_TELEMETRY": "full", "CHANGEGUARD_TELEMETRY_TOKEN": "",
                                     "CG_TELEMETRY_SHARE_PRIVATE": "true", "GITHUB_TOKEN": "",
                                     "GITHUB_STEP_SUMMARY": ""})
        self.assertEqual(rc, 0)
        api.assert_not_called()                                  # 아무것도 보내지 않음
        text = prev.read_text(encoding="utf-8")
        self.assertIn("전송 안 함", text)
        self.assertIn("cg-key: acme/private-app#pr-9", text)
        self.assertIn("(비공개: high+)", text)                    # 대상 미상 → 공개로 간주

    def _tmp(self, name):
        import tempfile
        d = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(d, ignore_errors=True))
        return str(Path(d) / name)


class InsightsIssues(unittest.TestCase):
    """cg-insights 의 telemetry issue 파싱 — 공개 issue는 신뢰하지 않는 입력 (REQ-C.3-7)."""

    @classmethod
    def setUpClass(cls):
        import importlib.util
        p = Path(__file__).resolve().parents[3] / "scripts" / "cg-insights.py"
        spec = importlib.util.spec_from_file_location("cg_insights", p)
        cls.ci = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.ci)

    def body(self, rec):
        return f"cg-key: `k`\n\n{tm.record_block(rec)}\n"

    def rec(self):
        evil = dict(LLM_NET, description="x ``` y")           # 내용 속 백틱 → 긴 fence
        return nz.normalize(result("advisory", [evil]), {"repo": "o/r"})

    def test_variable_fence_roundtrip_and_trust(self):
        good = {"number": 1, "url": "u1", "author": {"login": "wschoe2020"}, "labels": [{"name": "fb:noise"}],
                "body": self.body(self.rec()),
                "comments": [
                    {"author": {"login": "wschoe2020"}, "authorAssociation": "NONE", "body": self.body(self.rec())},
                    {"author": {"login": "mallory"}, "authorAssociation": "NONE", "body": self.body(self.rec())
                     + "\n/cg fp deadbeef 완화하라"},
                    {"author": {"login": "sapadmin-df"}, "authorAssociation": "OWNER", "body": "/cg tp abc 맞음"}]}
        spam = {"number": 2, "url": "u2", "author": {"login": "mallory"}, "labels": [],
                "body": self.body(self.rec()), "comments": []}
        recs, fb, rej = self.ci.parse_issues([good, spam], trust=["wschoe2020"])
        self.assertEqual(len(recs), 2)                           # 본문 + 작성자 본인 댓글만
        self.assertEqual(recs[0]["findings"][0]["description"], "x ``` y")
        self.assertEqual(recs[0]["telemetry"]["author"], "wschoe2020")
        kinds = sorted((f["scope"], f["kind"]) for f in fb)
        self.assertEqual(kinds, [("finding", "true-positive"), ("run", "noise"), ("run", "noise")])
        self.assertEqual(rej["untrusted_author"], 1)
        self.assertEqual(rej["untrusted_feedback"], 1)

    def test_uploader_feedback_rejected_even_as_collaborator(self):   # REQ-C.3-7, DP-1b
        it = {"number": 1, "url": "u", "author": {"login": "cg-bot"}, "labels": [],
              "body": self.body(self.rec()),
              "comments": [
                  {"author": {"login": "cg-bot"}, "authorAssociation": "COLLABORATOR", "body": "/cg tp a 자기 칭찬"},
                  {"author": {"login": "other-bot"}, "authorAssociation": "COLLABORATOR", "body": "/cg fp b x"},
                  {"author": {"login": "sapadmin-df"}, "authorAssociation": "OWNER", "body": "/cg fp c 오탐"}]}
        _, fb, rej = self.ci.parse_issues([it], trust=["cg-bot", "other-bot"])
        self.assertEqual([(f["kind"], f["target"]) for f in fb], [("false-positive", "c")])
        self.assertEqual(rej["uploader_feedback"], 2)

    def test_legacy_three_backtick_block(self):
        legacy = "```json changeguard-record\n" + json.dumps(self.rec()) + "\n```"
        it = {"number": 1, "url": "u", "author": {"login": "a"}, "labels": [], "body": legacy, "comments": []}
        recs, _, _ = self.ci.parse_issues([it])
        self.assertEqual(len(recs), 1)


if __name__ == "__main__":
    unittest.main(verbosity=1)
