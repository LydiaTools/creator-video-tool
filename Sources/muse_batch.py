#!/usr/bin/env python3
"""Receive a downloaded MUSE batch on the external disk; never render media."""

import argparse
import hashlib
import json
import math
import re
import shutil
import stat
import subprocess
import uuid
import zipfile
from datetime import datetime
from pathlib import Path, PurePosixPath

import video_tool as tool

MEDIA = {".jpg", ".jpeg", ".png", ".webp", ".mp4", ".mov", ".m4v"}


def archive_name(item):
    # Some Linux ZIP writers store UTF-8 filename bytes without the UTF-8 flag.
    # Recover only those exact bytes; INDEX must still match every media path.
    if not item.flag_bits & 0x800:
        try:
            return item.filename.encode("cp437").decode("utf-8")
        except (UnicodeEncodeError, UnicodeDecodeError):
            pass
    return item.filename


def inspect_package(archive):
    entries = archive.infolist()
    if len(entries) > 32 or sum(item.file_size for item in entries) > 300 * 1024**2:
        raise ValueError("批次超过 32 个条目或 300 MiB 展开上限")
    names = set()
    for item in entries:
        name = archive_name(item)
        path = PurePosixPath(name)
        key = name.casefold()
        if path.is_absolute() or ".." in path.parts or "\\" in name or ":" in name or key in names:
            raise ValueError("批次存在危险路径或重复文件名")
        if stat.S_ISLNK(item.external_attr >> 16):
            raise ValueError("批次不接受符号链接")
        names.add(key)
        if not item.is_dir() and path.suffix.lower() not in MEDIA | {".json", ".md", ".txt"}:
            raise ValueError("批次包含非媒体或非资料文件")
    indices = [item for item in entries if PurePosixPath(archive_name(item)).name == "INDEX.json"]
    if len(indices) != 1 or indices[0].file_size > 1024**2:
        raise ValueError("需要一份不超过 1 MiB 的 INDEX.json")
    index = json.loads(archive.read(indices[0]))
    rows = index.get("assets", index.get("items")) if isinstance(index, dict) else index
    if rows is None and isinstance(index, dict) and isinstance(index.get("file"), dict):
        muse_file = index["file"]
        rows = [{
            "filename": muse_file.get("name"),
            "display_name": index.get("title"),
            "prompt": index.get("prompt"),
            "sha256": muse_file.get("sha256"),
            "tags": index.get("tags", []),
            "shot_roles": index.get("shot_roles", []),
            "review_issues": index.get("review_issues", []),
            "usage": index.get("use_limits", ""),
            "source": index.get("source", "MUSE AI生成"),
            "synthetic_marker": index.get("synthetic_marker", "AI合成概念素材"),
            "needs_revision": index.get("needs_revision", False),
            "source_side_chat_id": index.get("source_side_chat_id"),
        }]
    if not isinstance(rows, list) or not 1 <= len(rows) <= 6:
        raise ValueError("INDEX.json 的 assets 必须包含 1–6 件素材")
    paused = index.get("paused", []) if isinstance(index, dict) else []
    if not isinstance(paused, list) or len(rows) + len(paused) > 6:
        raise ValueError("正常与 paused 素材合计必须为 1–6 件")
    normal = []
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("素材卡必须是对象")
        if "needs_revision" in row and type(row["needs_revision"]) is not bool:
            raise ValueError("needs_revision 必须为布尔值")
        normal.append({**row, "paused_record": False, "blocked_record": row.get("needs_revision", False),
                       "original_revision_record": dict(row) if row.get("needs_revision") else None})
    for row in paused:
        if not isinstance(row, dict) or not isinstance(row.get("reason"), str) or not row["reason"].strip() or not re.fullmatch(r"[a-fA-F0-9]{64}", str(row.get("sha256", ""))):
            raise ValueError("paused 素材必须声明真实 SHA256 与暂停原因")
        # Missing prompt/roles remain explicitly empty, never inferred from filename.
        normal.append({**row, "paused_record": True, "blocked_record": True,
                       "original_paused_record": dict(row)})
    rows = normal
    base = PurePosixPath(archive_name(indices[0])).parent
    available = {archive_name(item) for item in entries if not item.is_dir()}
    info_by_name = {archive_name(item): item for item in entries if not item.is_dir()}
    mapped = []
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("素材卡必须是对象")
        row = dict(row)
        if "needs_revision" in row and type(row["needs_revision"]) is not bool:
            raise ValueError("needs_revision 必须为布尔值")
        if "display_name" not in row and isinstance(row.get("chinese_name"), str):
            row["display_name"] = row["chinese_name"]
        if "review_issues" not in row and isinstance(row.get("review_notes"), str):
            row["review_issues"] = [row["review_notes"]]
        if row["blocked_record"]:
            for key, empty in (("prompt", ""), ("tags", []), ("shot_roles", []), ("usage", "")):
                row.setdefault(key, empty)
            issues = row.get("review_issues", [])
            reason = row.get("reason", "")
            if not isinstance(issues, list) or not all(isinstance(issue, str) for issue in issues) or not isinstance(reason, str):
                raise ValueError("问题素材的具体原因必须是文字")
            issues = list(dict.fromkeys([issue.strip() for issue in issues if issue.strip()] + ([reason.strip()] if reason.strip() else [])))
            if not issues or all(issue in {"待修", "需修正", "needs_revision", "待确认"} for issue in issues) or not re.fullmatch(r"[a-fA-F0-9]{64}", str(row.get("sha256", ""))):
                raise ValueError("问题素材必须声明真实 SHA256 与具体原因")
            row["review_issues"] = issues
            row["reason"] = "；".join(issues)
            if not isinstance(row["prompt"], str):
                raise ValueError("问题素材的 prompt 必须是文字，缺失时保留空值")
        filename = row.get("filename")
        if not isinstance(filename, str):
            raise ValueError("素材卡缺少 filename")
        path = base / filename
        if path.is_absolute() or ".." in path.parts or path.suffix.lower() not in MEDIA or str(path) not in available:
            raise ValueError("素材卡指向不存在或无效的媒体")
        if not isinstance(row.get("display_name"), str) or not row["display_name"].strip() or (not row["blocked_record"] and (not isinstance(row.get("prompt"), str) or not row["prompt"].strip())):
            raise ValueError("每件素材必须有中文名称和生成提示词")
        if "sha256" in row:
            declared = row["sha256"]
            if not isinstance(declared, str) or not re.fullmatch(r"[a-fA-F0-9]{64}", declared):
                raise ValueError("素材声明的 SHA256 无效")
            digest = hashlib.sha256()
            with archive.open(info_by_name[str(path)]) as stream:
                for chunk in iter(lambda: stream.read(1024**2), b""):
                    digest.update(chunk)
            if digest.hexdigest() != declared.lower():
                raise ValueError("素材声明 SHA256 与原包内容不符")
        if not isinstance(row.get("tags"), list) or not all(isinstance(tag, str) for tag in row["tags"]):
            raise ValueError("每件素材必须有字符串标签列表")
        for field in ("shot_roles", "review_issues"):
            if not isinstance(row.get(field, []), list) or not all(isinstance(item, str) for item in row.get(field, [])):
                raise ValueError("镜头用途和待审问题必须为字符串列表")
        mapped.append((str(path), row))
    expected = {name for name, _ in mapped}
    actual = {archive_name(item) for item in entries if not item.is_dir() and PurePosixPath(archive_name(item)).suffix.lower() in MEDIA}
    if expected != actual or len(expected) != len(mapped):
        raise ValueError("实际媒体与素材卡不一致")
    return mapped


def probe(path):
    result = subprocess.run([str(tool.RUNTIME / "bin/ffprobe"), "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)],
                            capture_output=True, text=True, timeout=30, check=True)
    data = json.loads(result.stdout)
    streams = data.get("streams", [])
    video = next((stream for stream in streams if stream.get("codec_type") == "video"), {})
    if video.get("width", 0) < 320 or video.get("height", 0) < 320:
        raise ValueError("文件损坏或分辨率不足")
    duration = float(data.get("format", {}).get("duration", video.get("duration", 0)))
    if not math.isfinite(duration):
        raise ValueError("媒体时长无效")
    has_audio = any(stream.get("codec_type") == "audio" for stream in streams)
    if path.suffix.lower() in {".mp4", ".mov", ".m4v"} and (not 3 <= duration <= 12 or has_audio):
        raise ValueError("视频必须为 3–12 秒且没有音轨，不擅自改写 MUSE 原件")
    return {"width": video["width"], "height": video["height"], "duration_seconds": duration,
            "codec": video.get("codec_name"), "frame_rate": video.get("avg_frame_rate"), "has_audio": has_audio}


def register_blocked(path, row, measured, directory, zip_sha, source_thread_id=None):
    digest = tool.sha256(path)
    prior = next((card for card in tool.list_library()["assets"] if card.get("sha256") == digest and Path(card.get("path", "")).is_file()), None)
    card_path = Path(prior["card_path"]) if prior else path.with_suffix(path.suffix + ".json")
    blocked = {key: value for key, value in prior.items() if key != "card_path"} if prior else {
        "material_id": "MAT-" + digest[:16].upper(), "path": str(path), "original_path": str(path),
        "sha256": digest, "origin": "muse", "display_name": row["display_name"],
        "prompt": row.get("prompt", ""), "tags": row.get("tags", []), "shot_roles": row.get("shot_roles", []),
        "usage_notes": row.get("usage", ""), "in_out_points": row.get("in_out_points", ""),
        "generation_source": row.get("source", ""), "synthetic_marker": row.get("synthetic_marker", ""),
        "muse_source_thread": source_thread_id,
        "rights_note": "用户授权MUSE生成并积累为候选；商业条款及本期发布范围待核实，非真实客户案例",
        "imported_at": datetime.now().astimezone().isoformat(),
        "cloud": {"status": "not_uploaded", "provider": None, "object_key": None, "remote_sha256_verified": False}}
    if tool.sha256(Path(blocked["path"])) != digest:
        raise ValueError("问题素材已有索引哈希不符，停止接收")
    blocked.update(needs_revision=True, status="paused_content_mismatch", cleared_for_publication=False,
                   visual_reviewed=False, synthetic=True, media_probe=measured,
                   prompt_missing=not bool(blocked.get("prompt", "").strip()),
                   review_issues=list(dict.fromkeys(blocked.get("review_issues", []) + row["review_issues"])),
                   batch_directory=str(directory), source_zip_sha256=zip_sha)
    if row["paused_record"]: blocked["paused_original_record"] = row["original_paused_record"]
    else: blocked["revision_original_record"] = row["original_revision_record"]
    tool.atomic_json(card_path, blocked)
    return blocked


def receive(value, source_thread_id=None):
    if source_thread_id is not None:
        source_thread_id = str(uuid.UUID(source_thread_id))
    tool.mounted_volume()
    source = Path(value).expanduser()
    if source.is_symlink() or not source.is_file() or source.suffix.lower() != ".zip" or not source.resolve().is_relative_to(tool.VOLUME.resolve()):
        raise ValueError("先把普通 ZIP 附件下载到真实外盘，再执行接收")
    if source.stat().st_size > 100 * 1024**2:
        raise ValueError("批次 ZIP 超过 100 MiB")
    with tool.media_lock(), zipfile.ZipFile(source) as archive:
        rows = inspect_package(archive)
        declared_sources = list(dict.fromkeys(row["source_side_chat_id"] for _, row in rows
                                if isinstance(row.get("source_side_chat_id"), str)))
        if shutil.disk_usage(tool.VOLUME).free < 2 * 1024**3:
            raise RuntimeError("外盘剩余空间不足 2 GiB")
        directory = tool.LIBRARY / "MUSE候选" / datetime.now().strftime("%Y-%m-%d") / ("批次-" + tool.sha256(source)[:12])
        receipt_path = directory / "接收报告.json"
        if receipt_path.is_file():
            receipt = json.loads(receipt_path.read_text())
            if receipt.get("status") == "indexed":
                for item in receipt["assets"]:
                    if tool.sha256(Path(item["path"])) != item["sha256"]:
                        raise RuntimeError("已入库原件哈希发生变化，停止复用")
                for name, row in rows:
                    if row["blocked_record"]:
                        digest = row["sha256"].lower()
                        item = next((item for item in receipt["assets"] if item["sha256"] == digest), None)
                        if not item: raise ValueError("问题件未在已有接收报告中登记")
                        register_blocked(Path(item["path"]), row, item["probe"], directory, receipt["source_zip_sha256"], source_thread_id)
                        item["needs_revision"] = True; item["pause_reason"] = row["reason"]
                tool.atomic_json(receipt_path, receipt)
                return {**receipt, "reused": True}
        directory.mkdir(parents=True, exist_ok=True)
        receipt = {"status": "receiving", "source_zip": str(source), "source_zip_sha256": tool.sha256(source),
                   "muse_source_thread": source_thread_id,
                   "index_declared_source_threads": declared_sources,
                   "directory": str(directory), "assets": [], "visual_reviewed": False,
                   "zip_filename_flag_recovered": any(archive_name(item) != item.filename for item in archive.infolist()),
                   "created_at": datetime.now().astimezone().isoformat()}
        tool.atomic_json(receipt_path, receipt)
        try:
            for info in archive.infolist():
                if info.is_dir():
                    continue
                target = directory / archive_name(info)
                if not target.resolve().is_relative_to((tool.LIBRARY / "MUSE候选").resolve()) or target.is_symlink():
                    raise RuntimeError("目标目录存在指向素材库外部的链接，停止接收")
                target.parent.mkdir(parents=True, exist_ok=True)
                temporary = target.with_name(target.name + "." + uuid.uuid4().hex + ".part")
                with archive.open(info) as src, temporary.open("xb") as dst:
                    shutil.copyfileobj(src, dst, 1024**2)
                if target.exists():
                    if tool.sha256(temporary) != tool.sha256(target):
                        raise RuntimeError("恢复接收时遇到文件冲突，不覆盖原件")
                    temporary.unlink()
                else:
                    temporary.rename(target)
            checked = [(directory / name, row, probe(directory / name)) for name, row in rows]
            for path, row, measured in checked:
                if row["blocked_record"]:
                    register_blocked(path, row, measured, directory, receipt["source_zip_sha256"], source_thread_id)
                result = tool.import_asset(str(path), "muse", "用户授权MUSE生成并积累为候选；商业条款及本期发布范围待核实，非真实客户案例")
                card_path = Path(result["card_path"])
                card = json.loads(card_path.read_text())
                if not result.get("reused"):
                    card.update({"display_name": row["display_name"], "tags": list(dict.fromkeys(row["tags"])),
                                 "prompt": row["prompt"], "media_probe": measured, "synthetic": True,
                                 "shot_roles": row.get("shot_roles", []), "review_issues": row.get("review_issues", []),
                                 "usage_notes": row.get("usage", ""), "in_out_points": row.get("in_out_points", ""),
                                 "generation_source": row.get("source", "Muse AI生成"),
                                 "synthetic_marker": row.get("synthetic_marker", "AI合成示意"),
                                 "muse_source_thread": source_thread_id,
                                 "index_declared_source_thread": row.get("source_side_chat_id"),
                                 "batch_directory": str(directory)})
                    tool.atomic_json(card_path, card)
                receipt["assets"].append({"material_id": card["material_id"], "path": result["asset_path"],
                                          "sha256": result["sha256"], "probe": measured, "reused": result.get("reused", False),
                                          "needs_revision": bool(card.get("needs_revision")), "pause_reason": row.get("reason", "") if row["blocked_record"] else ""})
            receipt["status"] = "indexed"
            tool.atomic_json(receipt_path, receipt)
            return receipt
        except Exception as error:
            receipt.update(status="failed", error=str(error))
            tool.atomic_json(receipt_path, receipt)
            raise


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("zip_path")
    parser.add_argument("--source-thread", help="实际 MUSE 旁聊 UUID；未提供时保留未知，不猜测来源")
    args = parser.parse_args()
    try:
        tool.emit({"ok": True, "data": receive(args.zip_path, args.source_thread)})
    except Exception as error:
        tool.emit({"ok": False, "error": str(error)})
        raise SystemExit(1)
