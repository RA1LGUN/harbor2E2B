"""Offline regression checks for evidence, credential and ownership boundaries."""
import json
import os
import sys
import tempfile
import shutil
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import e2b_template as helper
import inspect_trial


class Response:
    def __init__(self, data):
        self.data = data

    def raise_for_status(self):
        pass

    def json(self):
        return self.data


class HelpersTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.task = self.root / "task"
        (self.task / "environment").mkdir(parents=True)
        (self.task / "tests").mkdir()
        (self.task / "tests/test.sh").write_text("#!/bin/bash\nexit 0\n")
        (self.task / "instruction.md").write_text("Write the requested output.\n")
        self.image = "registry.example.invalid/tasks/demo@sha256:" + "a" * 64
        (self.task / "environment/Dockerfile").write_text("FROM " + self.image + "\n")
        (self.task / "task.toml").write_text(
            '[task]\nname="test/task"\n[environment]\n'
            f'docker_image="{self.image}"\ncpus=1\nmemory_mb=2048\nstorage_mb=15360\n')
        self.conn = {"api_url": "https://api.example.invalid", "domain": "example.invalid", "api_key": "example-key", "request_timeout": 45}
        self.plan = helper.make_plan(self.task)
        self.path = self.root / "receipt.json"
        self.receipt = {"schema": helper.SCHEMA, "endpoint": self.conn["api_url"],
                        "domain": self.conn["domain"], "plan": self.plan, "owned": True,
                        "run_id": "local-test", "template_id": "owned-id", "build_id": "owned-build", "status": "READY"}
        self.remote = {"templateID": "owned-id", "buildID": "owned-build", "aliases": [self.plan["alias"]],
                       "buildStatus": "ready", "cpuCount": 1, "memoryMB": 2048, "diskSizeMB": 15360}

    def write_receipt(self):
        self.path.write_text(json.dumps(self.receipt))

    def test_credentials_cannot_follow_an_arbitrary_task_host(self):
        with patch.dict(os.environ, {"REGISTRY_USERNAME": "account", "REGISTRY_PASSWORD": "secret-password", "REGISTRY_HOST": "other.example.invalid"}, clear=True):
            with self.assertRaisesRegex(ValueError, "REGISTRY_HOST"):
                helper.registry_auth(self.image, "default")
            self.assertEqual(helper.registry_auth(self.image, "micro"), {})

    def test_micro_rejects_non_acr_source_before_build(self):
        with self.assertRaisesRegex(ValueError, "ACR"):
            helper.make_plan(self.task, mode="micro")

    def test_existing_alias_never_triggers_a_build_or_claims_ownership(self):
        client = Mock()
        client.get.return_value = Response([self.remote])
        args = SimpleNamespace(task=self.task, image=self.image, mode="default", receipt=self.path)
        with patch.dict(os.environ, {}, clear=True), patch("e2b.Template.build_in_background") as create:
            with self.assertRaisesRegex(ValueError, "already exists"):
                helper.build(args, self.conn, client)
            create.assert_not_called()
        self.assertFalse(json.loads(self.path.read_text())["owned"])

    def test_build_failure_preserves_first_remote_reason_without_retry(self):
        client = Mock()
        client.get.side_effect = [Response([]), Response({"status": "error", "reason": {"message": "push denied"}})]
        args = SimpleNamespace(task=self.task, image=self.image, mode="default", receipt=self.path)
        with patch.dict(os.environ, {}, clear=True), patch("e2b.Template.build_in_background", return_value=SimpleNamespace(template_id="owned-id", build_id="owned-build")) as create:
            self.assertEqual(helper.build(args, self.conn, client), 1)
            self.assertEqual(create.call_count, 1)
        result = json.loads(self.path.read_text())
        self.assertEqual(result["status"], "BUILD_FAILED")
        self.assertEqual(result["build_status"]["reason"]["message"], "push denied")
        self.assertTrue(result["owned"])

    def test_resource_change_can_keep_alias_but_invalidates_binding(self):
        self.write_receipt()
        file = self.task / "task.toml"
        file.write_text(file.read_text().replace("cpus=1", "cpus=2"))
        self.assertEqual(helper.make_plan(self.task)["alias"], self.plan["alias"])
        with patch("e2b.Sandbox.create") as create:
            with self.assertRaisesRegex(ValueError, "binding changed"):
                helper.probe(SimpleNamespace(task=self.task, receipt=self.path, accept_disk_mb=None), self.conn, Mock())
            create.assert_not_called()

    def test_disk_acceptance_cannot_hide_a_cpu_mismatch(self):
        other = {**self.remote, "cpuCount": 2, "diskSizeMB": 20480}
        self.assertEqual(set(helper.resource_differences(self.plan, other, 20480)), {"cpuCount"})

    def test_identical_task_can_move_without_changing_template_binding(self):
        self.write_receipt()
        moved = self.root / "moved/task"
        shutil.copytree(self.task, moved)
        client = Mock()
        client.get.side_effect = [Response([self.remote]), Response([])]
        sandbox = Mock(sandbox_id="owned-sandbox")
        sandbox.commands.run.return_value = SimpleNamespace(exit_code=0, stdout="HARBOR_E2B_READY", stderr="")
        sandbox.kill.return_value = True
        with patch("e2b.Sandbox.create", return_value=sandbox):
            self.assertEqual(helper.probe(SimpleNamespace(task=moved, receipt=self.path, accept_disk_mb=None), self.conn, client), 0)

    def test_failed_probe_still_kills_its_sandbox_and_redacts_secret(self):
        self.write_receipt()
        client = Mock()
        client.get.side_effect = [Response([self.remote]), Response([])]
        sandbox = Mock(sandbox_id="owned-sandbox")
        sandbox.commands.run.side_effect = RuntimeError("failure with secret-password")
        sandbox.kill.return_value = True
        with patch.dict(os.environ, {"REGISTRY_PASSWORD": "secret-password"}), patch("e2b.Sandbox.create", return_value=sandbox):
            with self.assertRaises(RuntimeError):
                helper.probe(SimpleNamespace(task=self.task, receipt=self.path, accept_disk_mb=None), self.conn, client)
        sandbox.kill.assert_called_once()
        self.assertNotIn("secret-password", self.path.read_text())
        self.assertTrue(json.loads(self.path.read_text())["probes"][0]["cleanup_confirmed"])

    def test_unknown_sandbox_list_shape_cannot_confirm_cleanup(self):
        self.write_receipt()
        client = Mock()
        client.get.side_effect = [Response([self.remote]), Response([{"unexpected_id": "owned-sandbox"}])]
        sandbox = Mock(sandbox_id="owned-sandbox")
        sandbox.commands.run.return_value = SimpleNamespace(exit_code=0, stdout="HARBOR_E2B_READY", stderr="")
        sandbox.kill.return_value = True
        with patch("e2b.Sandbox.create", return_value=sandbox):
            code = helper.probe(SimpleNamespace(task=self.task, receipt=self.path, accept_disk_mb=None), self.conn, client)
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(self.path.read_text())["status"], "CLEANUP_UNCONFIRMED")

    def test_delete_refuses_unowned_or_rebound_templates(self):
        self.receipt["owned"] = False
        self.write_receipt()
        client = Mock()
        with self.assertRaisesRegex(ValueError, "unowned"):
            helper.delete_template(SimpleNamespace(receipt=self.path), self.conn, client)
        self.receipt["owned"] = True
        self.write_receipt()
        client.get.return_value = Response([{**self.remote, "buildID": "someone-elses-build"}])
        with self.assertRaisesRegex(ValueError, "identity"):
            helper.delete_template(SimpleNamespace(receipt=self.path), self.conn, client)
        client.delete.assert_not_called()

    def test_delete_targets_only_recorded_template_and_checks_absence(self):
        self.write_receipt()
        client = Mock()
        other = {"templateID": "unrelated"}
        client.get.side_effect = [Response([self.remote, other]), Response([other])]
        client.delete.return_value = Response(None)
        self.assertEqual(helper.delete_template(SimpleNamespace(receipt=self.path), self.conn, client), 0)
        client.delete.assert_called_once_with("/templates/owned-id")

    def test_receipt_endpoint_must_match_current_credentials(self):
        self.write_receipt()
        with self.assertRaisesRegex(ValueError, "endpoint"):
            helper.read_receipt(self.path, {**self.conn, "api_url": "https://api.other.invalid"})

    def test_scores_require_completed_verifier_and_matching_raw_reward(self):
        trial = self.root / "trial"
        (trial / "verifier").mkdir(parents=True)
        doc = {"verifier": {"started_at": "2026-01-01T00:00:00Z", "finished_at": "2026-01-01T00:00:01Z"},
               "verifier_result": {"rewards": {"reward": 0}}, "exception_info": None}
        (trial / "result.json").write_text(json.dumps(doc))
        self.assertIsNone(inspect_trial.inspect(trial)["reward"])
        (trial / "verifier/reward.txt").write_text("0\n")
        self.assertTrue(inspect_trial.inspect(trial, expected=0)["expected_reward_matches"])
        doc["exception_info"] = {"exception_type": "BuildException"}
        doc["verifier_result"]["rewards"]["reward"] = 1
        (trial / "result.json").write_text(json.dumps(doc))
        (trial / "verifier/reward.txt").write_text("1\n")
        self.assertEqual(inspect_trial.inspect(trial)["status"], "ENVIRONMENT_ERROR")
        self.assertIsNone(inspect_trial.inspect(trial)["reward"])

    def test_boolean_and_nonfinite_rewards_are_not_scores(self):
        for value in [True, float("nan"), float("inf"), -1, 2, "1"]:
            self.assertFalse(inspect_trial.finite_reward(value))

    def test_successful_grading_after_agent_timeout_keeps_score_and_error(self):
        trial = self.root / "interrupted-agent"
        (trial / "verifier").mkdir(parents=True)
        result = {"exception_info": {"exception_type": "AgentTimeoutError"},
                  "verifier": {"started_at": "2026-01-01T00:00:00Z", "finished_at": "2026-01-01T00:00:01Z"},
                  "verifier_result": {"rewards": {"reward": 0.5}}}
        (trial / "result.json").write_text(json.dumps(result))
        (trial / "verifier/reward.txt").write_text("0.5\n")
        summary = inspect_trial.inspect(trial)
        self.assertEqual(summary["status"], "SCORED_WITH_AGENT_ERROR")
        self.assertEqual(summary["reward"], 0.5)
        self.assertEqual(summary["exception_info"], result["exception_info"])

    def test_invalid_verifier_order_cannot_support_a_score(self):
        trial = self.root / "invalid-timing"
        (trial / "verifier").mkdir(parents=True)
        (trial / "result.json").write_text(json.dumps({
            "verifier": {"started_at": "2026-01-01T00:00:01Z", "finished_at": "2026-01-01T00:00:00Z"},
            "verifier_result": {"rewards": {"reward": 1}}}))
        (trial / "verifier/reward.txt").write_text("1\n")
        self.assertIsNone(inspect_trial.inspect(trial)["reward"])


if __name__ == "__main__":
    unittest.main()
