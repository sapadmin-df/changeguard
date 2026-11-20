#!/usr/bin/env python3
"""
test_policy_bump.py — 정책 SHA bump 감지 단위 테스트 (네트워크 없음)

policy-bump-verify.py extract_bump 는 자동 검증(v0.14)과 POLICY_UPDATED 알림(v0.11)이
공유하는 판정이다. v0.16 reusable workflow 전환 후 이 판정이 v0.15 형태만 알아
두 기능이 소비자 전부에서 죽어 있었다 (v0.17.1 수정).

사용: python3 test_policy_bump.py
"""

import importlib.util
import sys
import unittest
from unittest import mock
from pathlib import Path

CHECKS = Path(__file__).resolve().parent.parent / "checks"


def _load(name, filename):
    spec = importlib.util.spec_from_file_location(name, CHECKS / filename)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


pbv = _load("policy_bump_verify", "policy-bump-verify.py")
caller = _load("ci_caller", "ci-caller.py")

OLD = "c8f0e2ca1aa390ad4aeed18f42745358e420e380"
NEW = "b6ace3edfb8e8c39f4ea95a22bbd7bd9b5c5cd5e"
OTHER = "de0fac2e4500dabe0009e67214ff5f5447ce83dd"

REUSABLE = f"""diff --git a/.github/workflows/pre-merge-review.yml b/.github/workflows/pre-merge-review.yml
--- a/.github/workflows/pre-merge-review.yml
+++ b/.github/workflows/pre-merge-review.yml
@@ -20,7 +20,7 @@ jobs:
   review:
-    uses: sapadmin-df/changeguard/.github/workflows/pre-merge-review.yml@{OLD}
+    uses: sapadmin-df/changeguard/.github/workflows/pre-merge-review.yml@{NEW}
     with:
-      policy_sha: {OLD}
+      policy_sha: {NEW}
"""

TEMPLATE_V015 = f"""diff --git a/.github/workflows/pre-merge-review.yml b/.github/workflows/pre-merge-review.yml
@@ -10,3 +10,3 @@ env:
-  POLICY_REPO_SHA: "{OLD}"
+  POLICY_REPO_SHA: "{NEW}"
"""

UNRELATED_ACTION = f"""diff --git a/.github/workflows/ci.yml b/.github/workflows/ci.yml
@@ -5,2 +5,2 @@ steps:
-      - uses: actions/checkout@{OLD}
+      - uses: actions/checkout@{OTHER}
"""


class ExtractBump(unittest.TestCase):
    def test_reusable_uses_and_input(self):
        self.assertEqual(pbv.extract_bump(REUSABLE), (OLD, NEW))

    def test_policy_sha_only(self):
        diff = f"@@\n-      policy_sha: '{OLD}'\n+      policy_sha: '{NEW}'\n"
        self.assertEqual(pbv.extract_bump(diff), (OLD, NEW))

    def test_v015_template(self):
        self.assertEqual(pbv.extract_bump(TEMPLATE_V015), (OLD, NEW))

    def test_other_action_pin_is_not_policy_bump(self):
        self.assertIsNone(pbv.extract_bump(UNRELATED_ACTION))

    def test_custom_policy_repo(self):
        diff = REUSABLE.replace("sapadmin-df/changeguard", "acme/guard")
        self.assertEqual(pbv.extract_bump(diff, "acme/guard")[1], NEW)

    def test_no_change(self):
        same = f"@@\n-      policy_sha: {OLD}\n+      policy_sha: {OLD}\n"
        self.assertIsNone(pbv.extract_bump(same))


# v0.19.1: 워크플로우마다 다른 새 SHA (oh-my-bas #96 실측 — watcher 는 검증, 스캐너는 미검증)
MISMATCH = REUSABLE + f"""diff --git a/.github/workflows/policy-bump-watcher.yml b/.github/workflows/policy-bump-watcher.yml
--- a/.github/workflows/policy-bump-watcher.yml
+++ b/.github/workflows/policy-bump-watcher.yml
@@ -30,7 +30,7 @@ jobs:
-    uses: sapadmin-df/changeguard/.github/workflows/policy-bump-watcher.yml@{OLD}
+    uses: sapadmin-df/changeguard/.github/workflows/policy-bump-watcher.yml@{OTHER}
-      policy_sha: {OLD}
+      policy_sha: {OTHER}
"""


def fake_verify(bad=()):
    def v(repo, old, new, token):
        overall = "suspicious" if new in bad else "trusted"
        return {"bump_detected": True, "policy_repo": repo, "old_sha": old, "new_sha": new,
                "new_sha_short": new[:12], "exists": new not in bad, "verified": True,
                "verification_reason": "valid", "reachable_from_main": new not in bad,
                "ancestor_status": "behind", "author_login": "a", "committer_login": "web-flow",
                "message_first_line": "m", "commit_url": "u", "compare_url": "c",
                "overall": overall, "verdict_reasons": ["main에서 도달 불가"] if new in bad else []}
    return v


class AllShasVerified(unittest.TestCase):
    def test_extract_all(self):
        self.assertEqual(pbv.extract_bumps(MISMATCH), [(OLD, NEW), (OLD, OTHER)])
        self.assertEqual(pbv.extract_bumps(REUSABLE), [(OLD, NEW)])
        self.assertEqual(pbv.extract_bump(MISMATCH), (OLD, NEW))   # 하위 호환 (POLICY_UPDATED)

    def test_single_sha_unchanged(self):
        with mock.patch.object(pbv, "verify", side_effect=fake_verify()):
            r = pbv.verify_all("o/r", pbv.extract_bumps(REUSABLE), "")
        self.assertEqual(r["overall"], "trusted")
        self.assertFalse(r["pin_mismatch"])
        self.assertNotIn("targets", r)

    def test_mismatch_never_trusted(self):
        with mock.patch.object(pbv, "verify", side_effect=fake_verify()) as m:
            r = pbv.verify_all("o/r", pbv.extract_bumps(MISMATCH), "")
        self.assertEqual(m.call_count, 2)                            # 두 SHA 모두 검증
        self.assertEqual(r["overall"], "mismatch")
        self.assertTrue(r["pin_mismatch"])
        self.assertEqual([t["new_sha"] for t in r["targets"]], [NEW, OTHER])
        self.assertIn("불일치", r["verdict_reasons"][0])

    def test_second_sha_suspicious_wins(self):
        with mock.patch.object(pbv, "verify", side_effect=fake_verify(bad={OTHER})):
            r = pbv.verify_all("o/r", pbv.extract_bumps(MISMATCH), "")
        self.assertEqual(r["overall"], "suspicious")                 # 첫 SHA 가 정상이어도
        self.assertTrue(any(OTHER[:12] in w for w in r["verdict_reasons"]))

    def test_new_pin_equal_to_removed_is_not_bump(self):
        moved = (f"@@\n-      policy_sha: {OLD}\n+      policy_sha: {OLD}\n"
                 f"+    uses: sapadmin-df/changeguard/x.yml@{OLD}\n")
        self.assertEqual(pbv.extract_bumps(moved), [])


class CallerUsesSameRule(unittest.TestCase):
    def test_policy_updated_detection(self):
        self.assertEqual(caller.diff_contains_policy_bump(REUSABLE), (OLD, NEW))
        self.assertIsNone(caller.diff_contains_policy_bump(UNRELATED_ACTION))


if __name__ == "__main__":
    unittest.main(verbosity=1)
