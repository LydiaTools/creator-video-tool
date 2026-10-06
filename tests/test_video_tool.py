import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import sys
import subprocess
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "Sources"))
import video_tool


class ToolTests(unittest.TestCase):
    def test_revision_candidate_never_enters_shot_manifest(self):
        with patch.object(video_tool, "mounted_volume"), patch.object(video_tool, "list_library", return_value={"assets": [{"material_id": "MAT-test", "needs_revision": True}]}):
            with self.assertRaisesRegex(RuntimeError, "等待修正版"):
                video_tool.prepare_shot(["MAT-test"], "镜头")

    def test_mount_must_match_uuid(self):
        completed = subprocess.CompletedProcess([], 0, json.dumps({"VolumeUUID": "wrong", "MountPoint": str(video_tool.VOLUME)}).encode())
        with patch.object(video_tool.Path, "is_dir", return_value=True), patch.object(video_tool.os.path, "ismount", return_value=True), patch.object(video_tool.subprocess, "run", return_value=completed):
            with self.assertRaisesRegex(RuntimeError, "身份不符"):
                video_tool.mounted_volume()

    def test_mount_rejects_wrong_path_even_with_expected_uuid(self):
        completed = subprocess.CompletedProcess([], 0, json.dumps({"VolumeUUID": video_tool.EXPECTED_UUID, "MountPoint": "/wrong"}).encode())
        with patch.object(video_tool.Path, "is_dir", return_value=True), patch.object(video_tool.os.path, "ismount", return_value=True), patch.object(video_tool.subprocess, "run", return_value=completed):
            with self.assertRaisesRegex(RuntimeError, "身份不符"):
                video_tool.mounted_volume()

    def test_mount_query_timeout_stops_access(self):
        with patch.object(video_tool.Path, "is_dir", return_value=True), patch.object(video_tool.os.path, "ismount", return_value=True), patch.object(video_tool.subprocess, "run", side_effect=subprocess.TimeoutExpired("identity", 5)):
            with self.assertRaisesRegex(RuntimeError, "5 秒"):
                video_tool.mounted_volume()

    def test_global_lock_rejects_second_task(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(video_tool, "SUPPORT", Path(temp)):
            with video_tool.media_lock():
                with self.assertRaisesRegex(RuntimeError, "已有制作任务"):
                    with video_tool.media_lock():
                        self.fail("duplicate job acquired the lock")

    def test_edit_name_tags_preserves_asset(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "still.jpg"
            source.write_bytes(b"asset")
            with patch.object(video_tool, "LIBRARY", root / "lib"), patch.object(video_tool, "mounted_volume"):
                result = video_tool.import_asset(str(source), "muse", "user-attested")
                before = video_tool.sha256(Path(result["asset_path"]))
                card = video_tool.list_library()["assets"][0]
                video_tool.edit_asset(card["material_id"], "开场画面", "窗口，开场,窗口")
                after = video_tool.list_library()["assets"][0]
                self.assertEqual(after["tags"], ["窗口", "开场"])
                self.assertEqual(after["display_name"], "开场画面")
                self.assertEqual(before, video_tool.sha256(Path(result["asset_path"])))

    def test_workspace_resume_reads_without_approval_or_render(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "state.json").write_text(json.dumps({"stages": {"prepare": {"status": "done"}}}))
            (root / "run_config.json").write_text("{}")
            with patch.object(video_tool, "workspace_path", return_value=root), patch.object(video_tool, "SUPPORT", root / "support"):
                response = video_tool.operate_workspace("resume", str(root))
                self.assertFalse(response["approved"])
                self.assertFalse(response["finished"])
                self.assertFalse((root / "translation.lock.json").exists())

    def test_approval_refuses_unconfirmed_or_changed_review(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            snapshot = {"finished": False, "prepare": "done", "review_sha256": "new", "script": "English", "report": {"passed": True}}
            with patch.object(video_tool, "workspace_path", return_value=root), patch.object(video_tool, "workspace_snapshot", return_value=snapshot), patch.object(video_tool, "SUPPORT", root), patch.object(video_tool, "run_stage") as stage:
                with self.assertRaises(ValueError):
                    video_tool.operate_workspace("approve", str(root))
                with self.assertRaisesRegex(RuntimeError, "英文稿已变化"):
                    video_tool.operate_workspace("approve", str(root), "OpenAI", "actual-model", "medium", "old", True)
                stage.assert_not_called()

    def test_media_refuses_unapproved_even_with_disk_available(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            with patch.object(video_tool, "workspace_path", return_value=root), patch.object(video_tool, "workspace_snapshot", return_value={"finished": False, "prepare": "done", "approved": False}), patch.object(video_tool, "SUPPORT", root), patch.object(video_tool, "disk_status", return_value={"can_run_media": True}), patch.object(video_tool, "run_stage") as stage:
                with self.assertRaisesRegex(RuntimeError, "英文稿未批准"):
                    video_tool.operate_workspace("media", str(root))
                stage.assert_not_called()

    def test_finished_workspace_does_not_render_again(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            with patch.object(video_tool, "workspace_path", return_value=root), patch.object(video_tool, "workspace_snapshot", return_value={"finished": True}), patch.object(video_tool, "SUPPORT", root), patch.object(video_tool, "run_stage") as stage:
                with self.assertRaisesRegex(RuntimeError, "不重复制作"):
                    video_tool.operate_workspace("media", str(root))
                stage.assert_not_called()

    def test_same_source_reuses_registered_workspace(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "source.mp4"; source.write_bytes(b"source")
            workspace = root / "workspace"; workspace.mkdir()
            (workspace / "state.json").write_text("{}")
            (root / "jobs.json").write_text(json.dumps([{"workspace": str(workspace), "source_sha256": video_tool.sha256(source)}]))
            with patch.object(video_tool, "SUPPORT", root), patch.object(video_tool, "VOLUME", root), patch.object(video_tool, "mounted_volume"), patch.object(video_tool, "disk_status", return_value={"can_run_media": False, "media_block_reason": "low disk"}), patch.object(video_tool, "prepare_in_workspace") as prepare:
                response = video_tool.prepare_video(str(source))
                self.assertTrue(response["reused"])
                prepare.assert_not_called()

    def test_review_digest_changes_when_segment_changes_without_text_file_change(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "state.json").write_text("{}")
            (root / "full_en_natural.txt").write_text("Hello")
            segment = root / "segments_translated.json"
            segment.write_text(json.dumps([{"index": 1, "en_final": "Hello", "zh_fixed": "你好"}]))
            with patch.object(video_tool, "workspace_path", return_value=root):
                before = video_tool.workspace_snapshot(str(root))
                segment.write_text(json.dumps([{"index": 1, "en_final": "Changed", "zh_fixed": "你好"}]))
                after = video_tool.workspace_snapshot(str(root))
                self.assertNotEqual(before["review_sha256"], after["review_sha256"])
                self.assertFalse(after["script_matches_segments"])

    def test_same_source_reuses_interrupted_workspace_before_state_exists(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "source.mp4"; source.write_bytes(b"source")
            workspace = root / "workspace"; workspace.mkdir()
            (workspace / "tool_input.json").write_text("{}")
            (root / "jobs.json").write_text(json.dumps([{"workspace": str(workspace), "source_sha256": video_tool.sha256(source)}]))
            with patch.object(video_tool, "SUPPORT", root), patch.object(video_tool, "VOLUME", root), patch.object(video_tool, "mounted_volume"), patch.object(video_tool, "disk_status", return_value={"can_run_media": False, "media_block_reason": "low disk"}), patch.object(video_tool, "prepare_in_workspace") as prepare:
                response = video_tool.prepare_video(str(source))
                self.assertEqual(response["workspace"], str(workspace))
                self.assertTrue(response["reused"])
                prepare.assert_not_called()

    def test_cancel_terminates_child_process_group(self):
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(20)"], start_new_session=True)
        try:
            video_tool.ACTIVE_CHILD = child
            with self.assertRaises(SystemExit) as stopped:
                video_tool.stop_child()
            self.assertEqual(stopped.exception.code, 130)
            self.assertIsNotNone(child.poll())
        finally:
            video_tool.ACTIVE_CHILD = None
            if child.poll() is None:
                child.kill(); child.wait()

    def test_interrupted_prepare_can_be_read_without_original_state(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "tool_input.json").write_text(json.dumps({"source": "/sample.mp4", "source_sha256": "original"}))
            with patch.object(video_tool, "workspace_path", return_value=root):
                snapshot = video_tool.workspace_snapshot(str(root))
                self.assertEqual(snapshot["title"], "sample")
                self.assertNotEqual(snapshot["prepare"], "done")
                self.assertFalse(snapshot["finished"])

    def test_bilibili_search_parses_public_metadata_without_download(self):
        payload = {"ok": True, "data": [{"bvid": "BVabc", "title": "test", "author": "A",
                                        "play": 12, "duration": "0:30"}]}
        with patch.object(video_tool, "run_process", return_value=json.dumps(payload)) as command:
            found = video_tool.search("bilibili", "窗口管理")
        self.assertEqual(found[0]["url"], "https://www.bilibili.com/video/BVabc/")
        self.assertNotIn("download", command.call_args.args[0])

    def test_benchmark_saves_link_only_on_external_library(self):
        with tempfile.TemporaryDirectory() as temp:
            with patch.object(video_tool, "LIBRARY", Path(temp)), patch.object(video_tool, "mounted_volume"):
                record = video_tool.save_benchmark("youtube", "https://www.youtube.com/watch?v=abc",
                                                    "A video")
                card = json.loads(Path(record["card_path"]).read_text())
                self.assertEqual(card["status"], "discovery_only")
                self.assertFalse(card["media_downloaded"])
                with self.assertRaises(ValueError):
                    video_tool.save_benchmark("youtube", "https://youtube.com.evil.test/watch?v=abc", "bad")

    def test_asset_copy_is_hashed_and_remains_candidate(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "original.png"
            source.write_bytes(b"test-png-fixture")
            with patch.object(video_tool, "LIBRARY", root / "library"), patch.object(video_tool, "mounted_volume"):
                imported = video_tool.import_asset(str(source), "muse", "user-attested")
                imported_again = video_tool.import_asset(str(source), "muse", "user-attested")
                self.assertEqual(imported["asset_path"], imported_again["asset_path"])
                self.assertEqual(Path(imported["asset_path"]).read_bytes(), source.read_bytes())
                card = json.loads(Path(imported["card_path"]).read_text())
                self.assertFalse(card["cleared_for_publication"])
                self.assertEqual(len(video_tool.list_library()["assets"]), 1)

    def test_muse_export_already_in_candidate_directory_is_indexed_in_place(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            day = video_tool.datetime.now().strftime("%Y-%m-%d")
            directory = root / "MUSE候选" / day
            directory.mkdir(parents=True)
            source = directory / "still.jpg"
            source.write_bytes(b"muse-fixture")
            with patch.object(video_tool, "LIBRARY", root), patch.object(video_tool, "mounted_volume"):
                imported = video_tool.import_asset(str(source), "muse", "user-attested")
                self.assertEqual(Path(imported["asset_path"]), source)
                self.assertEqual(list(directory.glob("*.jpg")), [source])

    def test_search_and_two_asset_shot_do_not_copy_media(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            with patch.object(video_tool, "LIBRARY", root), patch.object(video_tool, "mounted_volume"):
                ids = []
                directory = root / "MUSE候选" / video_tool.datetime.now().strftime("%Y-%m-%d")
                directory.mkdir(parents=True)
                for name in ("网格背景", "窗口痛点"):
                    source = directory / f"{name}.jpg"
                    source.write_bytes(name.encode())
                    imported = video_tool.import_asset(str(source), "muse", "user-attested")
                    card_path = Path(imported["card_path"])
                    card = json.loads(card_path.read_text())
                    card["display_name"] = name
                    card["tags"] = ["窗口管理"]
                    card_path.write_text(json.dumps(card, ensure_ascii=False))
                    ids.append(card["material_id"])
                self.assertEqual(len(video_tool.search_assets("窗口管理")["assets"]), 2)
                self.assertEqual(len(video_tool.search_assets("窗口 管理")["assets"]), 2)
                self.assertEqual([item["display_name"] for item in video_tool.search_assets("窗口 背景")["assets"]], ["网格背景"])
                manifest = video_tool.prepare_shot(ids, "开场")
                data = json.loads(Path(manifest["manifest_path"]).read_text())
                self.assertEqual(data["asset_count"], 2)
                self.assertFalse(data["copied_media"])
                self.assertFalse(data["remote_downloaded"])
                self.assertEqual(len(list(root.rglob("*.jpg"))), 2)
                with self.assertRaisesRegex(ValueError, "1–2"):
                    video_tool.prepare_shot(ids + ids[:1], "too many")

    def test_media_refuses_low_internal_disk_before_mutation(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "source.mp4"
            source.write_bytes(b"source")
            with patch.object(video_tool, "mounted_volume"), patch.object(video_tool, "SUPPORT", root), patch.object(video_tool, "WORK_ROOT", root / "work"), patch.object(video_tool, "disk_status", return_value={"can_run_media": False, "media_block_reason": "low disk"}):
                with self.assertRaisesRegex(RuntimeError, "low disk"):
                    video_tool.prepare_video(str(source))
                self.assertFalse((root / "work").exists())


if __name__ == "__main__":
    unittest.main()
