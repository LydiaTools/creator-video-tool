import json
import hashlib
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "Sources"))
import production
import stable3_timing
import video_tool


class ProductionTests(unittest.TestCase):
    def test_resource_gate_blocks_memory_renderer_and_other_media(self):
        safe = {"memory_free_percent": 60, "largest_renderer_mib": 100, "heavy_process_pids": []}
        cases = [(dict(safe, memory_free_percent=10), "内存"),
                 (dict(safe, largest_renderer_mib=1600), "ClonBrowser"),
                 (dict(safe, heavy_process_pids=[123]), "已有")]
        with patch.object(video_tool, "mounted_volume"), patch.object(video_tool.shutil, "disk_usage", return_value=SimpleNamespace(free=30*1024**3)), patch.object(video_tool.Path, "is_file", return_value=True), patch.object(video_tool.Path, "is_dir", return_value=True):
            for resources, reason in cases:
                with patch.object(video_tool, "resource_snapshot", return_value=resources):
                    result = video_tool.disk_status()
                    self.assertFalse(result["can_run_media"])
                    self.assertIn(reason, result["media_block_reason"])

    def test_resume_is_readable_while_production_lock_is_held(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            with patch.object(video_tool, "SUPPORT", root), patch.object(video_tool, "workspace_path", return_value=root), patch.object(video_tool, "workspace_snapshot", return_value={"finished": True}):
                with video_tool.media_lock():
                    self.assertTrue(video_tool.operate_workspace("resume", str(root))["finished"])

    def test_passed_report_with_missing_lock_is_protected_not_finished(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "state.json").write_text(json.dumps({"stages": {"media": {"status": "done"}}}))
            (root / "final_media_report.json").write_text('{"passed": true}')
            (root / "final_en_softsub.mp4").write_bytes(b"old-final")
            with patch.object(video_tool, "workspace_path", return_value=root), patch.object(video_tool, "SUPPORT", root / "support"), patch.object(video_tool, "run_stage") as stage:
                snapshot = video_tool.workspace_snapshot(str(root))
                self.assertFalse(snapshot["finished"])
                self.assertTrue(snapshot["protected_final"])
                with self.assertRaisesRegex(RuntimeError, "不自动覆盖"):
                    video_tool.operate_workspace("media", str(root))
                stage.assert_not_called()
                self.assertEqual((root / "final_en_softsub.mp4").read_bytes(), b"old-final")

    def test_real_child_success_and_failure_are_durable(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            with patch.object(video_tool, "mounted_volume"), patch.object(video_tool, "RELEASE", root), patch.object(video_tool, "SUPPORT", root / "support"):
                video_tool.run_stage(root, "isolated success", [sys.executable, "-c", "print('done')"])
                with self.assertRaises(video_tool.StageFailure):
                    video_tool.run_stage(root, "isolated failure", [sys.executable, "-c", "raise SystemExit(9)"])
                ledger = json.loads((root / production.LEDGER_NAME).read_text())
                self.assertEqual([r["status"] for r in ledger["runs"]], ["succeeded", "failed"])
                self.assertEqual(ledger["runs"][-1]["exit_code"], 9)
                self.assertGreater(ledger["runs"][0]["elapsed_seconds"], 0)
                self.assertIsNone(video_tool.ACTIVE_CHILD)
                self.assertIsNone(ledger["costs"]["model_api_amount"])

    def test_real_child_timeout_stops_and_records_checkpoint(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            with patch.object(video_tool, "mounted_volume"), patch.object(video_tool, "RELEASE", root), patch.object(video_tool, "SUPPORT", root / "support"):
                with self.assertRaises(video_tool.StageFailure) as failed:
                    video_tool.run_stage(root, "timeout sample", [sys.executable, "-c", "import time; time.sleep(20)"], timeout=0.1)
                self.assertEqual(failed.exception.category, "timeout")
                last = json.loads((root / production.LEDGER_NAME).read_text())["runs"][-1]
                self.assertEqual(last["status"], "timed_out")
                self.assertIsNotNone(last["exit_code"])
                self.assertIsNone(video_tool.ACTIVE_CHILD)

    def test_running_resource_loss_stops_actual_child(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            safe = {"can_run_media": True, "internal_free_gib": 30, "memory_free_percent": 60, "largest_renderer_mib": 100}
            low = dict(safe, can_run_media=False, internal_free_gib=18, media_block_reason="low disk")
            with patch.object(video_tool, "mounted_volume"), patch.object(video_tool, "RELEASE", root), patch.object(video_tool, "SUPPORT", root / "support"), patch.object(video_tool, "ensure_media_resources", return_value=safe), patch.object(video_tool, "disk_status", return_value=low):
                with self.assertRaises(video_tool.StageFailure) as failed:
                    video_tool.run_stage(root, "resource sample", [sys.executable, "-c", "import time; time.sleep(20)"], heavy=True, monitor_interval=0.1)
                self.assertEqual(failed.exception.category, "resource")
                last = json.loads((root / production.LEDGER_NAME).read_text())["runs"][-1]
                self.assertEqual(last["status"], "resource_stopped")
                self.assertEqual(last["min_internal_free_gib"], 18)
                self.assertIsNotNone(last["exit_code"])

    def test_media_recovers_timing_only_once(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            snapshot = {"finished": False, "prepare": "done", "approved": True}
            failure = video_tool.StageFailure("video/audio duration delta", root, "timing")
            with patch.object(video_tool, "workspace_path", return_value=root), patch.object(video_tool, "workspace_snapshot", return_value=snapshot), patch.object(video_tool, "SUPPORT", root), patch.object(video_tool, "disk_status", return_value={"can_run_media": True}), patch.object(video_tool, "verify_package"), patch.object(video_tool, "run_stage", side_effect=failure) as stage, patch.object(video_tool, "recover_timing") as recovery:
                with self.assertRaises(video_tool.StageFailure):
                    video_tool.operate_workspace("media", str(root))
                self.assertEqual(stage.call_count, 2)
                recovery.assert_called_once_with(root)

    def test_tts_quality_failure_does_not_activate_timing(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            snapshot = {"finished": False, "prepare": "done", "approved": True}
            failure = video_tool.StageFailure("TTS QA failed", root, "tts_quality")
            with patch.object(video_tool, "workspace_path", return_value=root), patch.object(video_tool, "workspace_snapshot", return_value=snapshot), patch.object(video_tool, "SUPPORT", root), patch.object(video_tool, "disk_status", return_value={"can_run_media": True}), patch.object(video_tool, "verify_package"), patch.object(video_tool, "run_stage", side_effect=failure), patch.object(video_tool, "recover_timing") as recovery:
                with self.assertRaises(video_tool.StageFailure):
                    video_tool.operate_workspace("media", str(root))
                recovery.assert_not_called()

    def test_candidate_activation_preserves_original_and_refuses_repeat(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            original = root / "video_adjusted.mp4"
            candidate = root / "video_adjusted_synced.mp4"
            original.write_bytes(b"original-packets")
            candidate.write_bytes(b"verified-candidate")
            report = {"candidate_verified": True, "candidate": str(candidate),
                      "original_file_sha256": hashlib.sha256(original.read_bytes()).hexdigest(),
                      "candidate_sha256": hashlib.sha256(candidate.read_bytes()).hexdigest()}
            stable = lambda: {"valid": True, "media_plan_matches": True, "media_plan_sha256": "same-plan"}
            production.activate_candidate(root, report, stable, video_tool.atomic_json)
            self.assertEqual((root / "video_adjusted_presync.mp4").read_bytes(), b"original-packets")
            self.assertEqual(original.read_bytes(), b"verified-candidate")
            with self.assertRaisesRegex(RuntimeError, "已有留档"):
                production.activate_candidate(root, report, stable, video_tool.atomic_json)

    def test_plan_change_blocks_candidate_before_media_mutation(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            original = root / "video_adjusted.mp4"
            candidate = root / "video_adjusted_synced.mp4"
            original.write_bytes(b"original")
            candidate.write_bytes(b"candidate")
            with self.assertRaisesRegex(RuntimeError, "未稳定一致"):
                production.activate_candidate(root, {"candidate_verified": True, "candidate": str(candidate)},
                    lambda: {"valid": True, "media_plan_matches": False}, video_tool.atomic_json)
            self.assertEqual(original.read_bytes(), b"original")
            self.assertFalse((root / "video_adjusted_presync.mp4").exists())

    def test_in_gate_duration_is_not_selected_for_repair(self):
        rows = [{"index": i, "audio_seconds": 20, "video_seconds": 20.04} for i in range(14)]
        self.assertEqual(stable3_timing.classify(rows, 280, 280.56, 1)["status"], "already_within_duration_gate")

    def test_candidate_change_after_verification_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            original = root / "video_adjusted.mp4"
            candidate = root / "video_adjusted_synced.mp4"
            original.write_bytes(b"original")
            candidate.write_bytes(b"changed-after-verification")
            report = {"candidate_verified": True, "candidate": str(candidate),
                      "original_file_sha256": hashlib.sha256(b"original").hexdigest(),
                      "candidate_sha256": hashlib.sha256(b"verified").hexdigest()}
            with self.assertRaisesRegex(RuntimeError, "验证后发生变化"):
                production.activate_candidate(root, report,
                    lambda: {"valid": True, "media_plan_matches": True, "media_plan_sha256": "same"}, video_tool.atomic_json)
            self.assertEqual(original.read_bytes(), b"original")


if __name__ == "__main__":
    unittest.main()
