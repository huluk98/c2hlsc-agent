from __future__ import annotations

import importlib.util
from pathlib import Path
import unittest

try:
    import tomllib
except ImportError:  # Python 3.9 and 3.10 validate the files textually.
    tomllib = None


ROOT = Path(__file__).resolve().parents[1]


def _load_guardrail_module():
    path = ROOT / "scripts" / "verify_github_guardrails.py"
    spec = importlib.util.spec_from_file_location("verify_github_guardrails", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class CollaborationAssetTests(unittest.TestCase):
    def test_skill_is_complete_and_copy_prompt_invokes_it(self) -> None:
        skill = (
            ROOT / ".agents" / "skills" / "coordinate-team-work" / "SKILL.md"
        ).read_text(encoding="utf-8")
        metadata = (
            ROOT
            / ".agents"
            / "skills"
            / "coordinate-team-work"
            / "agents"
            / "openai.yaml"
        ).read_text(encoding="utf-8")
        self.assertTrue(skill.startswith("---\nname: coordinate-team-work\n"))
        self.assertNotIn("TODO", skill)
        self.assertIn("$coordinate-team-work", metadata)

    def test_ci_has_one_pr_run_and_stable_gate(self) -> None:
        workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn("branches: [main]", workflow)
        self.assertNotIn('branches: ["**"]', workflow)
        self.assertIn("concurrency:", workflow)
        self.assertIn("cancel-in-progress: true", workflow)
        self.assertIn("name: ci", workflow)

    @unittest.skipIf(tomllib is None, "tomllib is built into Python 3.11+")
    def test_project_agents_are_bounded_and_model_portable(self) -> None:
        config = tomllib.loads((ROOT / ".codex" / "config.toml").read_text("utf-8"))
        self.assertEqual(config["agents"]["max_concurrent_threads_per_session"], 3)

        expected_modes = {
            "coordination_explorer": "read-only",
            "bounded_implementer": "workspace-write",
            "verification_reviewer": "read-only",
        }
        found = {}
        for path in (ROOT / ".codex" / "agents").glob("*.toml"):
            agent = tomllib.loads(path.read_text("utf-8"))
            self.assertIn("description", agent)
            self.assertIn("developer_instructions", agent)
            self.assertNotIn("model", agent)
            self.assertIn("Do not spawn subagents", agent["developer_instructions"])
            found[agent["name"]] = agent["sandbox_mode"]
        self.assertEqual(found, expected_modes)

    def test_guardrail_validator_accepts_required_policy(self) -> None:
        module = _load_guardrail_module()
        protection = {
            "required_status_checks": {"strict": True, "contexts": ["ci"]},
            "required_pull_request_reviews": {
                "required_approving_review_count": 1,
                "require_last_push_approval": True,
                "dismiss_stale_reviews": True,
            },
            "required_conversation_resolution": {"enabled": True},
            "enforce_admins": {"enabled": True},
            "allow_force_pushes": {"enabled": False},
            "allow_deletions": {"enabled": False},
        }
        self.assertEqual(
            module.validate_guardrails(protection, {"allow_auto_merge": False}), []
        )

    def test_guardrail_validator_reports_unsafe_defaults(self) -> None:
        module = _load_guardrail_module()
        errors = module.validate_guardrails({}, {"allow_auto_merge": True})
        self.assertGreaterEqual(len(errors), 8)
        self.assertIn("required status check 'ci' is missing", errors)
        self.assertIn("administrators can bypass branch protection", errors)
        self.assertIn("repository auto-merge is enabled", errors)


if __name__ == "__main__":
    unittest.main()
