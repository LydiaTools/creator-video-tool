import io
import json
import tempfile
import unittest
import zipfile
import sys
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "Sources"))
import muse_batch
import video_tool


def package(extra=None):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("INDEX.json", json.dumps({"assets": [{"filename": "still.jpg", "display_name": "产品微距", "prompt": "原创产品微距", "tags": ["外贸", "细节"]}]}))
        archive.writestr("still.jpg", b"sample")
        if extra:
            archive.writestr(*extra)
    buffer.seek(0)
    return buffer


class BatchTests(unittest.TestCase):
    def test_utf8_bytes_without_flag_are_recovered_not_guessed(self):
        item = zipfile.ZipInfo("产品.jpg".encode("utf-8").decode("cp437"))
        item.flag_bits = 0
        self.assertEqual(muse_batch.archive_name(item), "产品.jpg")
        item.flag_bits = 0x800
        self.assertEqual(muse_batch.archive_name(item), item.filename)

    def test_muse_items_schema_preserves_original_name_and_review_notes(self):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("still.jpg", b"sample")
            archive.writestr("INDEX.json", json.dumps({"items": [{"filename": "still.jpg", "chinese_name": "原创微距", "prompt": "完整提示词", "tags": ["横屏"], "review_notes": "检查几何一致性"}]}))
        buffer.seek(0)
        with zipfile.ZipFile(buffer) as archive:
            rows = muse_batch.inspect_package(archive)
            self.assertEqual(rows[0][1]["display_name"], "原创微距")
            self.assertEqual(rows[0][1]["review_issues"], ["检查几何一致性"])

    def test_muse_single_asset_index_schema_is_normalized(self):
        image = b"sample"
        import hashlib
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("still.jpg", image)
            archive.writestr("INDEX.json", json.dumps({
                "batch": "B69",
                "title": "素材库归档与复用",
                "kind": "concept_still",
                "needs_revision": False,
                "file": {"name": "still.jpg", "sha256": hashlib.sha256(image).hexdigest()},
                "prompt": "完整生成提示词",
                "use_limits": "仅作AI概念B-roll",
                "source_side_chat_id": "a106bd5c-3bdf-47d6-9d3f-a823e262fecb",
            }))
        buffer.seek(0)
        with zipfile.ZipFile(buffer) as archive:
            rows = muse_batch.inspect_package(archive)
        row = rows[0][1]
        self.assertEqual(row["filename"], "still.jpg")
        self.assertEqual(row["display_name"], "素材库归档与复用")
        self.assertEqual(row["sha256"], hashlib.sha256(image).hexdigest())
        self.assertEqual(row["prompt"], "完整生成提示词")
        self.assertEqual(row["usage"], "仅作AI概念B-roll")
        self.assertEqual(row["tags"], [])
        self.assertEqual(row["source_side_chat_id"], "a106bd5c-3bdf-47d6-9d3f-a823e262fecb")

    def test_receive_keeps_index_source_declared_not_verified_when_option_is_omitted(self):
        import hashlib
        image = b"sample"
        source_id = "a106bd5c-3bdf-47d6-9d3f-a823e262fecb"
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "muse.zip"
            with zipfile.ZipFile(source, "w") as archive:
                archive.writestr("still.jpg", image)
                archive.writestr("INDEX.json", json.dumps({
                    "title": "素材归档",
                    "file": {"name": "still.jpg", "sha256": hashlib.sha256(image).hexdigest()},
                    "prompt": "完整提示词",
                    "source_side_chat_id": source_id,
                }))
            test_disk_usage = SimpleNamespace(free=9 * 1024**3)
            with patch.object(video_tool, "VOLUME", root), patch.object(video_tool, "LIBRARY", root / "library"), patch.object(video_tool, "SUPPORT", root / "support"), patch.object(video_tool, "mounted_volume"), patch.object(muse_batch, "probe", return_value={"width": 1920, "height": 1080, "has_audio": False}), patch.object(muse_batch.shutil, "disk_usage", return_value=test_disk_usage):
                result = muse_batch.receive(str(source))
            self.assertIsNone(result["muse_source_thread"])
            self.assertEqual(result["index_declared_source_threads"], [source_id])

    def test_path_traversal_refused(self):
        with zipfile.ZipFile(package(("../outside.txt", "bad"))) as archive:
            with self.assertRaisesRegex(ValueError, "危险路径"):
                muse_batch.inspect_package(archive)

    def test_unlisted_media_refused(self):
        with zipfile.ZipFile(package(("unlisted.png", "bad"))) as archive:
            with self.assertRaisesRegex(ValueError, "不一致"):
                muse_batch.inspect_package(archive)

    def test_batch_is_indexed_in_place_and_reused_without_copy(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "batch.zip"; source.write_bytes(package().getvalue())
            test_disk_usage = SimpleNamespace(free=9 * 1024**3)
            with patch.object(video_tool, "VOLUME", root), patch.object(video_tool, "LIBRARY", root / "library"), patch.object(video_tool, "SUPPORT", root / "support"), patch.object(video_tool, "mounted_volume"), patch.object(muse_batch, "probe", return_value={"width": 1920, "height": 1080, "has_audio": False}), patch.object(muse_batch.shutil, "disk_usage", return_value=test_disk_usage):
                result = muse_batch.receive(str(source))
                self.assertEqual(result["status"], "indexed")
                cards = video_tool.list_library()["assets"]
                self.assertEqual(cards[0]["tags"], ["外贸", "细节"])
                self.assertFalse(cards[0]["visual_reviewed"])
                self.assertEqual(len(list(root.rglob("*.jpg"))), 1)
                self.assertTrue(muse_batch.receive(str(source))["reused"])
                self.assertEqual(len(list(root.rglob("*.jpg"))), 1)
