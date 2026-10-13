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


class CallerUsesSameRule(unittest.TestCase):
    def test_policy_updated_detection(self):
        self.assertEqual(caller.diff_contains_policy_bump(REUSABLE), (OLD, NEW))
        self.assertIsNone(caller.diff_contains_policy_bump(UNRELATED_ACTION))


if __name__ == "__main__":
    unittest.main(verbosity=1)
