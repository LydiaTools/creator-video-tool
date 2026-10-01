import io
import json
import tempfile
import unittest
import zipfile
import sys
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
            with patch.object(video_tool, "VOLUME", root), patch.object(video_tool, "LIBRARY", root / "library"), patch.object(video_tool, "SUPPORT", root / "support"), patch.object(video_tool, "mounted_volume"), patch.object(muse_batch, "probe", return_value={"width": 1920, "height": 1080, "has_audio": False}):
                result = muse_batch.receive(str(source))
                self.assertEqual(result["status"], "indexed")
                cards = video_tool.list_library()["assets"]
                self.assertEqual(cards[0]["tags"], ["外贸", "细节"])
                self.assertFalse(cards[0]["visual_reviewed"])
                self.assertEqual(len(list(root.rglob("*.jpg"))), 1)
                self.assertTrue(muse_batch.receive(str(source))["reused"])
                self.assertEqual(len(list(root.rglob("*.jpg"))), 1)
