"""Tests for the partner-managed tier in .github/scripts/validate_skill_evals.py.

The tier tests read SKILL.md files on disk; the mode tests build a throwaway Git
repository with a skill in it, the way the workflow sees a pull request.
"""

from __future__ import annotations

import contextlib
import io
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / ".github" / "scripts"))

import validate_skill_evals  # noqa: E402

GIT_ENV = {
    "GIT_AUTHOR_NAME": "Test",
    "GIT_AUTHOR_EMAIL": "test@example.com",
    "GIT_COMMITTER_NAME": "Test",
    "GIT_COMMITTER_EMAIL": "test@example.com",
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_SYSTEM": os.devnull,
}


def skill_md(name: str, metadata_extra: str = "") -> str:
    return (
        f"---\nname: {name}\ndescription: Use this skill when testing {name}.\n"
        f'metadata:\n  author: octocat\n  version: "1.0.0"\n{metadata_extra}'
        f"---\n\n# {name}\n"
    )


class SkillTierTests(unittest.TestCase):
    def tier(self, text: str):
        directory = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, directory)
        path = directory / "SKILL.md"
        path.write_text(text, encoding="utf-8")
        return validate_skill_evals.skill_tier(path)

    def test_reads_the_tier_in_each_scalar_style(self):
        for value in ("partner-managed", '"partner-managed"', "'partner-managed'", "partner-managed  # who maintains it"):
            with self.subTest(value=value):
                self.assertEqual(self.tier(skill_md("s", f"  tier: {value}\n")), "partner-managed")

    def test_reads_aws_managed(self):
        self.assertEqual(self.tier(skill_md("s", '  tier: "aws-managed"\n')), "aws-managed")

    def test_no_tier(self):
        self.assertIsNone(self.tier(skill_md("s")))

    def test_ignores_a_tier_that_is_not_directly_under_metadata(self):
        cases = {
            "top level": '---\nname: s\ntier: partner-managed\nmetadata:\n  version: "1.0.0"\n---\n',
            "nested deeper": '---\nname: s\nmetadata:\n  version: "1.0.0"\n  extra:\n    tier: partner-managed\n---\n',
            "after metadata ends": '---\nname: s\nmetadata:\n  version: "1.0.0"\nother:\n  tier: partner-managed\n---\n',
            "in the body": '---\nname: s\nmetadata:\n  version: "1.0.0"\n---\n\nmetadata:\n  tier: partner-managed\n',
        }
        for name, text in cases.items():
            with self.subTest(name):
                self.assertIsNone(self.tier(text))

    def test_unreadable_frontmatter_reads_as_no_tier(self):
        cases = {
            "flow mapping": "---\nname: s\nmetadata: {tier: partner-managed}\n---\n",
            "no frontmatter": "# s\n\nmetadata:\n  tier: partner-managed\n",
            "unterminated": "---\nname: s\nmetadata:\n  tier: partner-managed\n",
            "not UTF-8": None,
        }
        for name, text in cases.items():
            with self.subTest(name):
                if text is None:
                    directory = Path(tempfile.mkdtemp())
                    self.addCleanup(shutil.rmtree, directory)
                    path = directory / "SKILL.md"
                    path.write_bytes(b"---\nmetadata:\n  tier: partner-managed\xff\n---\n")
                    self.assertIsNone(validate_skill_evals.skill_tier(path))
                else:
                    self.assertIsNone(self.tier(text))

    def test_missing_file_reads_as_no_tier(self):
        self.assertIsNone(validate_skill_evals.skill_tier(Path(tempfile.gettempdir()) / "no-such-dir" / "SKILL.md"))


class PartnerManagedModeTests(unittest.TestCase):
    """A pull request adding a skill with no evals/ at all."""

    def setUp(self):
        self.repo = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.repo)
        self.git("init", "-q", "-b", "main")
        self.write("README.md", "base\n")
        self.base = self.commit()

    def git(self, *args: str) -> str:
        result = subprocess.run(
            ["git", *args], cwd=self.repo, capture_output=True, text=True, env={**os.environ, **GIT_ENV}
        )
        if result.returncode != 0:
            raise AssertionError(result.stderr)
        return result.stdout.strip()

    def write(self, path: str, text: str) -> None:
        target = self.repo / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")

    def commit(self) -> str:
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "change")
        return self.git("rev-parse", "HEAD")

    def add_skill(self, name: str, tier: str | None) -> None:
        self.write(f"skills/{name}/SKILL.md", skill_md(name, f"  tier: {tier}\n" if tier else ""))
        self.commit()

    def report(self, name: str, **kwargs) -> validate_skill_evals.SkillReport:
        (report,) = validate_skill_evals.build_reports(self.repo, [name], self.base, **kwargs)
        return report

    def test_a_new_skill_without_a_tier_is_enforced(self):
        self.add_skill("plain", None)
        report = self.report("plain")
        self.assertTrue(report.enforced)
        self.assertFalse(report.ok)

    def test_a_new_aws_managed_skill_is_enforced(self):
        self.add_skill("ours", "aws-managed")
        self.assertTrue(self.report("ours").enforced)

    def test_a_new_partner_managed_skill_only_warns(self):
        self.add_skill("theirs", "partner-managed")
        report = self.report("theirs")
        self.assertFalse(report.enforced)
        self.assertEqual(report.mode_note, "partner-managed")
        # The missing results are still reported, so a green check shows them.
        self.assertTrue(report.violations)

    def test_strict_does_not_enforce_a_partner_managed_skill(self):
        self.add_skill("theirs", "partner-managed")
        self.assertFalse(self.report("theirs", force_reason="--strict").enforced)

    def test_the_label_enforces_a_partner_managed_skill(self):
        self.add_skill("theirs", "partner-managed")
        report = self.report(
            "theirs",
            force_reason="enforce-evals label",
            withdraw_exemptions="the `enforce-evals` label",
            enforce_label=True,
        )
        self.assertTrue(report.enforced)
        self.assertFalse(report.ok)

    def run_main(self, *argv: str) -> tuple[int, str]:
        err = io.StringIO()
        with mock.patch.object(validate_skill_evals, "_repo_root", return_value=self.repo), mock.patch.dict(
            os.environ, {k: v for k, v in os.environ.items() if not k.startswith("GITHUB_")}, clear=True
        ), contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            code = validate_skill_evals.main(list(argv))
        return code, err.getvalue()

    def test_main_passes_a_partner_managed_skill_and_says_why(self):
        self.add_skill("theirs", "partner-managed")
        code, err = self.run_main("--base-ref", self.base, "--head-ref", "HEAD")
        self.assertEqual(code, 0)
        self.assertIn(validate_skill_evals.PARTNER_HINT, err)

    def test_main_fails_a_partner_managed_skill_under_the_label(self):
        self.add_skill("theirs", "partner-managed")
        code, _ = self.run_main("--base-ref", self.base, "--head-ref", "HEAD", "--labels", '["enforce-evals"]')
        self.assertEqual(code, 1)

    def test_main_skill_option_honors_the_tier(self):
        self.add_skill("theirs", "partner-managed")
        self.add_skill("ours", "aws-managed")
        self.assertEqual(self.run_main("--skill", "theirs")[0], 0)
        self.assertEqual(self.run_main("--skill", "ours")[0], 1)
        self.assertEqual(self.run_main("--skill", "theirs", "--labels", '["enforce-evals"]')[0], 1)


if __name__ == "__main__":
    unittest.main()
