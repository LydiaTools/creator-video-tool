#!/usr/bin/env python3
"""Small, stateless bridge for the native video tool. Media stays on the external disk."""

import argparse
import fcntl
import hashlib
import json
import os
import signal
import shutil
import subprocess
import sys
import time
import uuid
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
import production


VOLUME = Path("/Volumes/Obsidian知识库")
EXPECTED_UUID = "B3E5063B-1F8A-32A4-9864-FA421DC7C9EC"
LIBRARY = VOLUME / "Codex工作运行/30_工作过程素材/视频出海工具素材库"
WORK_ROOT = VOLUME / "Codex工作运行/10_视频进行中"
RELEASE = VOLUME / "自动化技能备份/2026-08-28_B站视频出海固定工作流/bilibili_auto_dub_stable_2026-08-28_v3"
RUNTIME = VOLUME / "自动化技能备份/2026-08-28_B站视频出海固定工作流/runtime_2026-08-28"
MIN_INTERNAL_FREE = 20 * 1024**3
SUPPORT = Path.home() / "Library/Application Support/出海视频"
ACTIVE_CHILD = None


class StageFailure(RuntimeError):
    def __init__(self, message, workspace, category="process_failure", log_path=""):
        super().__init__(message)
        self.workspace = workspace
        self.category = category
        self.log_path = str(log_path)


def terminate_child(child):
    if child and child.poll() is None:
        try:
            os.killpg(child.pid, signal.SIGTERM)
            child.wait(timeout=8)
        except subprocess.TimeoutExpired:
            os.killpg(child.pid, signal.SIGKILL)
            child.wait()
        except ProcessLookupError:
            child.wait()


def progress(message):
    print(json.dumps({"progress": message}, ensure_ascii=False), file=sys.stderr, flush=True)


def stop_child(*_):
    global ACTIVE_CHILD
    child = ACTIVE_CHILD
    terminate_child(child)
    raise SystemExit(130)


@contextmanager
def media_lock():
    SUPPORT.mkdir(parents=True, exist_ok=True)
    with (SUPPORT / "heavy.lock").open("a") as file:
        try:
            fcntl.flock(file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("已有制作任务在运行，请等待完成或停止后再继续")
        try:
            yield
        finally:
            fcntl.flock(file, fcntl.LOCK_UN)


def atomic_json(path, value):
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with temporary.open("x", encoding="utf-8") as file:
            json.dump(value, file, ensure_ascii=False, indent=2)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def emit(value):
    print(json.dumps(value, ensure_ascii=False))


def mounted_volume():
    if not VOLUME.is_dir() or not os.path.ismount(VOLUME):
        raise RuntimeError("外接盘未挂载：停止，不在 /Volumes 创建同名普通目录")
    try:
        result = subprocess.run([sys.executable, str(Path(__file__).with_name("volume_identity.py")), str(VOLUME)],
                                capture_output=True, timeout=5, check=True)
        info = json.loads(result.stdout)
    except subprocess.TimeoutExpired as error:
        raise RuntimeError("无法在 5 秒内核实外接盘身份，请重新连接外盘") from error
    except subprocess.CalledProcessError as error:
        detail = (error.stderr or b"").decode("utf-8", errors="replace").strip()[:1000]
        raise RuntimeError(f"外接盘身份查询退出 {error.returncode}：{detail or '无错误详情'}") from error
    except (OSError, ValueError) as error:
        raise RuntimeError(f"无法读取外接盘身份：{error}") from error
    if info.get("VolumeUUID") != EXPECTED_UUID or info.get("MountPoint") != str(VOLUME):
        raise RuntimeError("外接盘身份不符，已停止读取和写入")


def resource_snapshot():
    processes = subprocess.run(["/bin/ps", "-axo", "pid=,rss=,command="],
                               capture_output=True, text=True, timeout=5, check=True).stdout
    largest_renderer = 0
    heavy_pids = []
    import shlex
    for line in processes.splitlines():
        fields = line.strip().split(None, 2)
        if len(fields) != 3:
            continue
        pid, rss, command = fields
        if "ClonBrowserCore Helper (Renderer)" in command:
            largest_renderer = max(largest_renderer, int(rss))
        try:
            tokens = shlex.split(command)
        except ValueError:
            tokens = command.split()
        if any(Path(token).name in {"media_pipeline.py", "prepare_workspace.py", "temp_synth_segmented.py"}
               for token in tokens):
            heavy_pids.append(int(pid))
    memory = subprocess.run(["/usr/bin/memory_pressure", "-Q"], capture_output=True,
                            text=True, timeout=5, check=True).stdout
    import re
    match = re.search(r"System-wide memory free percentage:\s*(\d+)%", memory)
    if not match:
        raise RuntimeError("无法核实系统可用内存，停止启动重型任务")
    return {"memory_free_percent": int(match.group(1)),
            "largest_renderer_mib": round(largest_renderer / 1024, 1),
            "heavy_process_pids": heavy_pids}


def disk_status(check_running=True):
    mounted_volume()
    internal_free = shutil.disk_usage(Path.home()).free
    external_free = shutil.disk_usage(VOLUME).free
    release_present = (RELEASE / "SKILL.md").is_file()
    runtime_present = RUNTIME.is_dir()
    resources = resource_snapshot()
    reasons = []
    if internal_free < MIN_INTERNAL_FREE:
        reasons.append("内置盘低于 20 GiB，暂停 ASR/TTS/转码")
    if external_free < 2 * 1024**3:
        reasons.append("外盘剩余空间不足 2 GiB")
    if resources["memory_free_percent"] < 20:
        reasons.append("系统可用内存低于 20%")
    if resources["largest_renderer_mib"] > 1536:
        reasons.append("ClonBrowser 页面超过 1.5 GiB，请先关闭对应页面")
    if check_running and resources["heavy_process_pids"]:
        reasons.append("已有 ASR 或媒体制作进程在运行")
    if not release_present or not runtime_present:
        reasons.append("Stable3 发布包或运行时缺失")
    reason = "；".join(reasons)
    return {
        "internal_free_gib": round(internal_free / 1024**3, 1),
        "external_free_gib": round(external_free / 1024**3, 1),
        "can_run_media": not reason,
        "media_block_reason": reason,
        "release_present": release_present,
        "runtime_present": runtime_present,
        "library": str(LIBRARY),
        **resources,
    }


def ensure_media_resources(check_running=True):
    status = disk_status(check_running=check_running)
    if not status["can_run_media"]:
        raise RuntimeError(status["media_block_reason"])
    return status


def run_process(argv, cwd=None, timeout=None):
    global ACTIVE_CHILD
    child = subprocess.Popen(argv, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             text=True, start_new_session=True,
                             env={**os.environ, "PYTHONUNBUFFERED": "1", "OMP_NUM_THREADS": "2",
                                  "OPENBLAS_NUM_THREADS": "2", "VECLIB_MAXIMUM_THREADS": "2"})
    ACTIVE_CHILD = child
    try:
        stdout, stderr = child.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        os.killpg(child.pid, signal.SIGTERM)
        try:
            child.communicate(timeout=8)
        except subprocess.TimeoutExpired:
            os.killpg(child.pid, signal.SIGKILL)
            child.communicate()
        raise RuntimeError("操作超时，已停止本次子任务；工作区保留，可再次继续")
    finally:
        ACTIVE_CHILD = None
    if child.returncode:
        detail = (stdout + "\n" + stderr).strip()[-3000:]
        raise RuntimeError(f"命令退出 {child.returncode}: {detail}")
    return stdout


def search(platform, query):
    query = query.strip()
    if not query or len(query) > 120:
        raise ValueError("关键词应为 1–120 个字符")
    if platform == "bilibili":
        raw = run_process(["/Users/macmini/.local/bin/bili", "search", query,
                           "--type", "video", "-n", "6", "--json"], timeout=45)
        result = json.loads(raw)
        if not result.get("ok"):
            raise RuntimeError("B站搜索未成功")
        return [{"platform": "bilibili", "title": item.get("title", ""),
                 "url": "https://www.bilibili.com/video/" + item["bvid"] + "/",
                 "creator": item.get("author", ""), "views": item.get("play"),
                 "duration": item.get("duration", "")}
                for item in result.get("data", []) if item.get("bvid")]
    if platform == "youtube":
        raw = run_process(["/Users/macmini/.local/bin/yt-dlp", "--no-update",
                           "--flat-playlist", "--dump-json", "ytsearch6:" + query], timeout=60)
        rows = [json.loads(line) for line in raw.splitlines() if line.startswith("{")]
        return [{"platform": "youtube", "title": item.get("title", ""),
                 "url": item.get("webpage_url") or "https://www.youtube.com/watch?v=" + item["id"],
                 "creator": item.get("uploader") or item.get("channel") or "",
                 "views": item.get("view_count"), "duration": item.get("duration")}
                for item in rows if item.get("id")]
    raise ValueError("目前可在工具中搜索 B站或 YouTube；抖音先保存人工找到的公开链接")


def save_benchmark(platform, url, title, creator="", notes=""):
    mounted_volume()
    from urllib.parse import urlparse
    hosts = {"bilibili": ("bilibili.com", "b23.tv"),
             "youtube": ("youtube.com", "youtu.be"),
             "douyin": ("douyin.com", "v.douyin.com")}
    parsed = urlparse(url)
    if platform not in hosts or parsed.scheme != "https":
        raise ValueError("只接受所选平台的 HTTPS 视频链接")
    if not parsed.hostname or not any(parsed.hostname == host or parsed.hostname.endswith("." + host)
               for host in hosts[platform]):
        raise ValueError("链接域名与所选平台不符")
    if not title.strip():
        raise ValueError("需要填写可识别的标题")
    directory = LIBRARY / "对标线索/用户收藏"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / (datetime.now().strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:8] + ".json")
    card = {"platform": platform, "url": url, "title": title.strip(),
            "creator": creator.strip(), "notes": notes.strip(),
            "saved_at": datetime.now().astimezone().isoformat(),
            "status": "discovery_only", "rights": "unknown", "reviewed": False,
            "media_downloaded": False}
    with path.open("x", encoding="utf-8") as file:
        json.dump(card, file, ensure_ascii=False, indent=2)
    return {"card_path": str(path), "card": card}


def sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for block in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def import_asset(source, origin, rights_note):
    mounted_volume()
    source = Path(source).expanduser()
    if source.is_symlink() or not source.is_file():
        raise ValueError("请选择普通文件，不接受目录或符号链接")
    allowed = {".mp4", ".mov", ".m4v", ".png", ".jpg", ".jpeg", ".webp"}
    if source.suffix.lower() not in allowed:
        raise ValueError("当前仅导入常见图片和视频格式")
    if origin not in {"self_recorded", "muse", "licensed_external"}:
        raise ValueError("素材来源无效")
    if origin == "licensed_external" and not rights_note.strip():
        raise ValueError("第三方素材必须填写可核验的许可来源")
    digest = sha256(source)
    for existing in list_library()["assets"]:
        if existing.get("sha256") == digest and Path(existing.get("path", "")).is_file():
            return {"asset_path": existing["path"],
                    "card_path": str(Path(existing["path"]).with_suffix(Path(existing["path"]).suffix + ".json")),
                    "sha256": digest, "reused": True}
    target_dir = LIBRARY / "MUSE候选" / datetime.now().strftime("%Y-%m-%d") if origin == "muse" else LIBRARY / "其他候选"
    target_dir.mkdir(parents=True, exist_ok=True)
    safe_name = "".join(ch for ch in source.stem if ch.isalnum() or ch in "-_")[:60] or "asset"
    # A Muse export saved directly into today's external candidate directory is
    # already in its archival location. Index it without making another copy.
    in_muse_archive = origin == "muse" and source.resolve().is_relative_to((LIBRARY / "MUSE候选").resolve())
    if in_muse_archive or source.parent.resolve() == target_dir.resolve():
        target = source
    else:
        target = target_dir / f"{digest[:12]}-{safe_name}{source.suffix.lower()}"
    if target.exists():
        if sha256(target) != digest:
            raise RuntimeError("目标文件名冲突，未覆盖已有素材")
    else:
        with source.open("rb") as src, target.open("xb") as dst:
            shutil.copyfileobj(src, dst, 1024 * 1024)
        if sha256(target) != digest:
            raise RuntimeError("复制后哈希不匹配，候选不可用；请人工检查")
    card_path = target.with_suffix(target.suffix + ".json")
    if not card_path.exists():
        card = {"material_id": "MAT-" + digest[:16].upper(),
                "display_name": source.stem, "tags": [], "shot_roles": [],
                "cloud": {"status": "not_uploaded", "provider": None, "object_key": None,
                          "remote_sha256_verified": False},
                "path": str(target), "original_path": str(source), "sha256": digest,
                "origin": origin, "rights_note": rights_note.strip(),
                "imported_at": datetime.now().astimezone().isoformat(),
                "status": "candidate", "visual_reviewed": False, "cleared_for_publication": False}
        with card_path.open("x", encoding="utf-8") as file:
            json.dump(card, file, ensure_ascii=False, indent=2)
    return {"asset_path": str(target), "card_path": str(card_path), "sha256": digest}


def list_library():
    mounted_volume()
    cards = []
    for subdir in (LIBRARY / "MUSE候选", LIBRARY / "其他候选"):
        if subdir.is_dir():
            for path in subdir.rglob("*.json"):
                try:
                    card = json.loads(path.read_text(encoding="utf-8"))
                    if "path" in card and "sha256" in card:
                        card["card_path"] = str(path)
                        cards.append(card)
                except (OSError, json.JSONDecodeError):
                    continue
    return {"assets": sorted(cards, key=lambda x: x.get("imported_at", ""), reverse=True)}


def edit_asset(material_id, name, tags):
    mounted_volume()
    card = next((item for item in list_library()["assets"] if item.get("material_id") == material_id), None)
    if not card or not name.strip() or len(name) > 120:
        raise ValueError("素材不存在，或名称为空/超过 120 字")
    path = Path(card.pop("card_path"))
    card["display_name"] = name.strip()
    card["tags"] = list(dict.fromkeys(part.strip() for part in tags.replace("，", ",").split(",") if part.strip()))[:20]
    atomic_json(path, card)
    return {"material_id": material_id, "card_path": str(path)}


def search_assets(query):
    needle = query.strip().casefold()
    assets = list_library()["assets"]
    if not needle:
        return {"assets": assets}
    terms = needle.split()
    found = []
    for card in assets:
        fields = [card.get("material_id", ""), card.get("display_name", ""),
                  card.get("origin", ""), *card.get("tags", []), *card.get("shot_roles", [])]
        searchable = " ".join(str(value) for value in fields).casefold()
        if needle in searchable or (len(terms) > 1 and all(term in searchable for term in terms)):
            found.append(card)
    return {"assets": found}


def prepare_shot(material_ids, shot_name):
    mounted_volume()
    if not 1 <= len(material_ids) <= 2 or len(set(material_ids)) != len(material_ids):
        raise ValueError("一个镜头请选择 1–2 件不同素材")
    cards = {card.get("material_id"): card for card in list_library()["assets"]}
    selection = []
    for material_id in material_ids:
        card = cards.get(material_id)
        if not card:
            raise ValueError(f"找不到素材 ID：{material_id}")
        if card.get("needs_revision"):
            raise RuntimeError(f"素材 {material_id} 已发现画面问题，等待修正版，不送入制作")
        path = Path(card["path"])
        if not path.is_file():
            cloud = card.get("cloud", {})
            raise RuntimeError(f"素材 {material_id} 当前没有外盘文件；云端状态为 {cloud.get('status', 'unknown')}，尚未配置按需下载")
        if sha256(path) != card["sha256"]:
            raise RuntimeError(f"素材 {material_id} 哈希不匹配，停止取用")
        selection.append({"material_id": material_id,
                          "display_name": card.get("display_name", path.stem),
                          "path": str(path), "sha256": card["sha256"],
                          "tags": card.get("tags", []), "rights_note": card.get("rights_note", "用户声明本批素材可商用"),
                          "status": card.get("status", "candidate"),
                          "cloud_status": card.get("cloud", {}).get("status", "not_uploaded")})
    directory = LIBRARY / "取用清单"
    directory.mkdir(parents=True, exist_ok=True)
    manifest = directory / (datetime.now().strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:8] + ".json")
    payload = {"shot_name": shot_name.strip() or "未命名镜头", "created_at": datetime.now().astimezone().isoformat(),
               "asset_count": len(selection), "assets": selection, "copied_media": False,
               "remote_downloaded": False, "publication_approved": False}
    with manifest.open("x", encoding="utf-8") as file:
        json.dump(payload, file, ensure_ascii=False, indent=2)
    return {"manifest_path": str(manifest), "asset_count": len(selection), "copied_media": False}


def send_shot(material_ids, shot_name):
    result = prepare_shot(material_ids, shot_name)
    directory = WORK_ROOT / "口播制作"
    if not (directory / ".creator-video-volume.json").is_file():
        raise RuntimeError("先在口播制作中连接外盘资料，再发送素材")
    target = directory / "待取用素材.json"
    if target.exists():
        raise RuntimeError("上次选材尚未接收，请先打开口播制作")
    shutil.copyfile(result["manifest_path"], target)
    return result


def workspace_path(value):
    mounted_volume()
    path = Path(value).expanduser().resolve()
    prepared = (path / "state.json").is_file() and (path / "run_config.json").is_file()
    interrupted_prepare = (path / "tool_input.json").is_file()
    if not path.is_relative_to(VOLUME) or not (prepared or interrupted_prepare):
        raise ValueError("请选择外盘上包含 state.json 和 run_config.json 的 Stable3 工作区")
    return path


def workspace_snapshot(value):
    path = workspace_path(value)
    state_path = path / "state.json"
    state = json.loads(state_path.read_text()) if state_path.is_file() else {}
    if not state and (path / "tool_input.json").is_file():
        state = {"source_video": json.loads((path / "tool_input.json").read_text())["source"]}
    stages = state.get("stages", {})
    report_path = path / "translation_quality_report.json"
    report = json.loads(report_path.read_text()) if report_path.is_file() else {}
    final_report_path = path / "final_media_report.json"
    final_report = json.loads(final_report_path.read_text()) if final_report_path.is_file() else {}
    script_path = path / "full_en_natural.txt"
    segments_path = path / "segments_translated.json"
    segments = json.loads(segments_path.read_text()) if segments_path.is_file() else []
    draft = "\n\n".join(str(segment.get("en_final", "")) for segment in segments)
    displayed = script_path.read_text() if script_path.is_file() else draft
    reviewed_payload = json.dumps({"script": displayed, "language": [{"index": s.get("index"), "zh_fixed": s.get("zh_fixed"), "en_final": s.get("en_final")} for s in segments]}, ensure_ascii=False, sort_keys=True)
    final_path = path / "final_en_softsub.mp4"
    approval = production.approval_state(path, RELEASE, state, segments)
    finished = (final_path.is_file() and final_path.stat().st_size > 0
                and final_report.get("passed") is True and approval["valid"]
                and approval.get("media_plan_matches") is True
                and stages.get("media", {}).get("status") == "done")
    return {"workspace": str(path), "title": Path(state.get("source_video", path.name)).stem,
            "prepare": stages.get("prepare", {}).get("status", "未准备"),
            "translation": stages.get("translation", {}).get("status", "待翻译"),
            "media": stages.get("media", {}).get("status", "未制作"),
            "provenance": state.get("model_provenance", {}),
            "script": displayed,
            "script_matches_segments": displayed.strip() == draft.strip(),
            "review_sha256": hashlib.sha256(reviewed_payload.encode()).hexdigest(),
            "report": report, "final_report": final_report,
            "approved": approval["valid"] and stages.get("translation", {}).get("status") == "done",
            "approval_check": approval, "finished": finished,
            "protected_final": final_path.exists() or final_report_path.exists(),
            "production": production.summary(path),
            "segment_count": len(segments),
            "qa_passed_count": len(stages.get("media", {}).get("qa_passed_segments", [])),
            "final_path": str(final_path), "script_path": str(script_path)}


def run_stage(workspace, title, argv, timeout=600, heavy=False, monitor_interval=10):
    global ACTIVE_CHILD
    mounted_volume()
    initial_resources = ensure_media_resources() if heavy else None
    logs = workspace / "tool_logs"
    logs.mkdir(exist_ok=True)
    temp = workspace / "tool_tmp"
    temp.mkdir(exist_ok=True)
    path = logs / (datetime.now().strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6] + ".log")
    progress(title)
    SUPPORT.mkdir(parents=True, exist_ok=True)
    run = {"id": uuid.uuid4().hex, "stage": title, "started_at": datetime.now().astimezone().isoformat(),
           "status": "running", "log_path": str(path), "elapsed_seconds": 0,
           "resource_start": initial_resources,
           "min_internal_free_gib": initial_resources.get("internal_free_gib") if initial_resources else None,
           "min_memory_free_percent": initial_resources.get("memory_free_percent") if initial_resources else None,
           "max_renderer_mib": initial_resources.get("largest_renderer_mib") if initial_resources else None}
    def persist():
        # Recheck the mounted volume before writing; never recreate an absent external path.
        mounted_volume()
        production.save_run(workspace, run, atomic_json)
        atomic_json(SUPPORT / "last_task.json", {"workspace": str(workspace), "stage": title,
                    "status": run["status"], "log_path": str(path), "run_id": run["id"],
                    "updated_at": datetime.now().timestamp()})
    started = time.monotonic()
    persist()
    with path.open("x", encoding="utf-8") as log:
        try:
            child = subprocess.Popen(["/usr/bin/nice", "-n", "15"] + argv, stdout=log, stderr=subprocess.STDOUT, cwd=RELEASE,
                                     start_new_session=True,
                                     env={**os.environ, "TMPDIR": str(temp), "PYTHONUNBUFFERED": "1",
                                          "OMP_NUM_THREADS": "2", "OPENBLAS_NUM_THREADS": "2",
                                          "MKL_NUM_THREADS": "2", "VECLIB_MAXIMUM_THREADS": "2"})
        except OSError as error:
            run.update(status="failed", finished_at=datetime.now().astimezone().isoformat(),
                       failure={"category": "process_failure", "message": str(error),
                                "next": "程序未能启动，核对本机 Python 与固定运行时。"})
            persist()
            raise StageFailure(f"无法启动{title}：{error}", workspace, log_path=path) from error
        ACTIVE_CHILD = child
        run["pid"] = child.pid
        last_sample = started
        resource_failures = 0
        try:
            while child.poll() is None:
                now = time.monotonic()
                if now - started > timeout:
                    terminate_child(child)
                    run["status"] = "timed_out"
                    raise StageFailure(f"{title}超时，已停止进程并保留工作区", workspace, "timeout", path)
                if heavy and now - last_sample >= monitor_interval:
                    last_sample = now
                    try:
                        resources = disk_status(check_running=False)
                        for target, source, reducer in (("min_internal_free_gib", "internal_free_gib", min),
                                                        ("min_memory_free_percent", "memory_free_percent", min),
                                                        ("max_renderer_mib", "largest_renderer_mib", max)):
                            prior = run[target]
                            run[target] = resources[source] if prior is None else reducer(prior, resources[source])
                        resource_failures = 0 if resources["can_run_media"] else resource_failures + 1
                        if resource_failures >= 2:
                            terminate_child(child)
                            run["status"] = "resource_stopped"
                            raise StageFailure(resources["media_block_reason"] + "；本次已停止，保留分段可继续", workspace, "resource", path)
                        run["elapsed_seconds"] = round(now - started, 1)
                        run["last_activity_bytes"] = path.stat().st_size
                        persist()
                        state = production.read_json(workspace / "state.json", {})
                        qa = len(state.get("stages", {}).get("media", {}).get("qa_passed_segments", []))
                        progress(f"{title} · 已运行 {int((now-started)/60)} 分钟 · QA已通过 {qa} 段 · 内盘 {resources['internal_free_gib']} GiB")
                    except StageFailure:
                        raise
                    except Exception as error:
                        terminate_child(child)
                        run["status"] = "resource_stopped"
                        raise StageFailure(f"资源状态无法核实：{error}；本次已停止", workspace, "resource", path) from error
                time.sleep(0.5)
            if child.returncode:
                with path.open("rb") as details:
                    details.seek(max(0, path.stat().st_size - 5000))
                    detail = details.read().decode("utf-8", errors="replace")
                raise StageFailure(f"{title}失败：{detail}\n运行记录：{path}", workspace,
                                   production.failure_kind(detail), path)
            run["status"] = "succeeded"
        except SystemExit:
            run["status"] = "cancelled"
            run["failure"] = {"category": "cancelled", "message": production.recovery_message("cancelled")}
            raise
        except StageFailure as error:
            if run["status"] == "running":
                run["status"] = "failed"
            run["failure"] = {"category": error.category, "message": str(error)[-5000:],
                              "next": production.recovery_message(error.category)}
            raise
        finally:
            ACTIVE_CHILD = None
            run["finished_at"] = datetime.now().astimezone().isoformat()
            run["elapsed_seconds"] = round(time.monotonic() - started, 1)
            run["exit_code"] = child.returncode
            try:
                persist()
            except Exception:
                # The external drive may have been disconnected; retain only an internal reference.
                atomic_json(SUPPORT / "last_task.json", {"workspace": str(workspace), "stage": title,
                            "status": run["status"], "updated_at": datetime.now().timestamp(),
                            "record_write_failed": True, "log_path": str(path)})
    return {"log_path": str(path), "run": run}


def verify_package(workspace):
    run_stage(workspace, "检查 Stable3 发布文件", [sys.executable, str(RELEASE / "scripts/verify_release.py")])


def recover_timing(workspace):
    snapshot = workspace_snapshot(str(workspace))
    approval = snapshot["approval_check"]
    if not approval["valid"] or approval.get("media_plan_matches") is not True:
        raise StageFailure("QA 后媒体计划未稳定一致，保持原视频；请先核对媒体计划", workspace, "media_plan")
    if snapshot["protected_final"] or (workspace / "video_adjusted_presync.mp4").exists():
        raise StageFailure("已有最终文件或预校正留档，不重复时长修复", workspace, "timing")
    helper = Path(__file__).with_name("stable3_timing.py")
    result = run_stage(workspace, "核对帧舍入误差并验证时长候选",
                       [sys.executable, str(helper), "--workspace", str(workspace), "--write-candidate"],
                       timeout=600, heavy=True)
    candidate = production.read_json(Path(result["log_path"]))
    production.activate_candidate(workspace, candidate,
        lambda: workspace_snapshot(str(workspace))["approval_check"], atomic_json)
    progress("时长候选验证通过，已保留原文件；继续原版终检")


def operate_workspace(action, value, provider="", model="", effort="", reviewed_sha="", confirmed=False):
    path = workspace_path(value)
    if action == "resume":
        return workspace_snapshot(value)
    with media_lock():
        snapshot = workspace_snapshot(value)
        if snapshot["finished"]:
            raise RuntimeError("已有成片和通过报告，不重复制作；可直接打开交付文件")
        if action == "media" and snapshot.get("protected_final"):
            raise RuntimeError("已有最终媒体文件但当前核对未通过；保留成片，请核对批准锁和报告，不自动覆盖")
        if snapshot["prepare"] != "done":
            raise RuntimeError("先继续完成源片准备，再验证英文稿或制作媒体")
        if action == "approve":
            if not confirmed or not all(item.strip() for item in (provider, model, effort)):
                raise ValueError("需明确批准全文并填写实际 provider、模型 ID 和推理档位")
            if not reviewed_sha or reviewed_sha != snapshot["review_sha256"] or not snapshot["script"]:
                raise RuntimeError("英文稿已变化或尚未显示，请重新读取全文并确认")
            if snapshot["report"].get("passed") is not True:
                raise RuntimeError("英文稿尚未通过自动验证，不得批准")
            if not snapshot["script_matches_segments"]:
                raise RuntimeError("全文和分段译稿不一致，请先验证并重新审阅全文")
        if action == "media":
            status = disk_status()
            if not status["can_run_media"]:
                raise RuntimeError(status["media_block_reason"] or "Stable3 运行时不完整")
            if not snapshot["approved"]:
                raise RuntimeError("英文稿未批准，不启动配音或成片")
        verify_package(path)
        if action == "validate":
            run_stage(path, "验证英文稿、数字和实体", [sys.executable, str(RELEASE / "scripts/validate_translation.py"), "--workspace", str(path)])
            # This is an unapproved text view; do not alter the source or approved wording.
            segments = json.loads((path / "segments_translated.json").read_text())
            if not snapshot["approved"]:
                (path / "full_en_natural.txt").write_text("\n\n".join(s["en_final"] for s in segments), encoding="utf-8")
        elif action == "approve":
            # Frozen approval normalizes wording. Refuse a changed review instead of approving unseen text.
            sys.path.insert(0, str(RELEASE / "scripts"))
            from common import cleanup_spoken_english
            from translation_contract import translation_content_digest
            segments = json.loads((path / "segments_translated.json").read_text())
            if snapshot["report"].get("translation_sha256") != translation_content_digest(segments):
                raise RuntimeError("分段译稿在验证后发生变化，请重新验证和审阅")
            if any(cleanup_spoken_english(seg.get("en_final", "")) != seg.get("en_final", "") for seg in segments):
                raise RuntimeError("稿件仍需原版规范化，请先按 Stable3 完成规范化，再重新审阅全文")
            run_stage(path, "锁定你已批准的英文稿", [sys.executable, str(RELEASE / "scripts/mark_translation.py"), "--workspace", str(path),
                      "--provider", provider, "--model", model, "--reasoning-effort", effort, "--approve"])
        elif action == "media":
            argv = [sys.executable, str(RELEASE / "scripts/media_pipeline.py"), "--workspace", str(path)]
            try:
                run_stage(path, "原管线制作英文配音、字幕和视频", argv, timeout=14400, heavy=True)
            except StageFailure as error:
                if error.category != "timing":
                    raise
                recover_timing(path)
                # One bounded recovery, followed by all original quality gates; never loop.
                run_stage(path, "帧时长恢复后继续原版质量门", argv, timeout=14400, heavy=True)
        else:
            raise ValueError("未知工作区操作")
        return workspace_snapshot(value)


def prepare_video(source):
    mounted_volume()
    source = Path(source).expanduser().resolve(strict=True)
    if not source.is_file():
        raise ValueError("源视频必须是普通文件")
    scripts = RELEASE / "scripts"
    with media_lock():
        progress("核对源片，查找已存在工作区")
        digest = sha256(source)
        jobs_path = SUPPORT / "jobs.json"
        jobs = json.loads(jobs_path.read_text()) if jobs_path.is_file() else []
        for job in jobs:
            registered = Path(job["workspace"])
            if (job.get("source_sha256") == digest and registered.resolve().is_relative_to(VOLUME.resolve())
                    and ((registered / "state.json").is_file() or (registered / "tool_input.json").is_file())):
                return {"workspace": job["workspace"], "source_sha256": digest, "stage": "existing_workspace", "reused": True}
        ensure_media_resources()
        workspace = WORK_ROOT / (datetime.now().strftime("%Y-%m-%d_%H-%M-%S-") + uuid.uuid4().hex[:6] + "-出海视频工具") / "stable3_workspace"
        workspace.mkdir(parents=True, exist_ok=False)
        atomic_json(workspace / "tool_input.json", {"source": str(source.resolve()), "source_sha256": digest})
        jobs.insert(0, {"workspace": str(workspace), "source_sha256": digest, "source": str(source)})
        atomic_json(jobs_path, jobs[:100])
        prepare_in_workspace(source, workspace, scripts)
    return {"workspace": str(workspace), "source_sha256": digest,
            "stage": "prepared_only", "next": "翻译起草、验证与人工批准；未生成媒体"}


def prepare_in_workspace(source, workspace, scripts):
    ensure_media_resources()
    for args, title in ((["verify_release.py"], "检查发布文件"),
                        (["verify_release.py", "--environment"], "检查冻结运行时"),
                        (["runtime_smoke.py"], "运行固定语音回归")):
        run_stage(workspace, title, [sys.executable, str(scripts / args[0])] + args[1:],
                  timeout=1800, heavy=args[0] == "runtime_smoke.py")
    run_stage(workspace, "检查源片并识别中文", [sys.executable, str(scripts / "prepare_workspace.py"), str(source), "--workspace", str(workspace)], timeout=14400, heavy=True)


def resume_prepare(value):
    path = workspace_path(value)
    state_path = path / "state.json"
    state = json.loads(state_path.read_text()) if state_path.is_file() else {}
    if state.get("stages", {}).get("prepare", {}).get("status") == "done":
        return workspace_snapshot(value)
    with media_lock():
        ensure_media_resources()
        source = Path(state.get("source_video") or json.loads((path / "tool_input.json").read_text())["source"])
        if (path / "tool_input.json").is_file():
            original = json.loads((path / "tool_input.json").read_text())
            if not source.is_file() or sha256(source) != original["source_sha256"]:
                raise RuntimeError("源片与最初指纹不同，停止续跑")
        prepare_in_workspace(source, path, RELEASE / "scripts")
    return workspace_snapshot(value)


def main():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("doctor")
    lookup = sub.add_parser("search")
    lookup.add_argument("platform", choices=["bilibili", "youtube"])
    lookup.add_argument("query")
    save = sub.add_parser("save-benchmark")
    for field in ("platform", "url", "title"):
        save.add_argument(field)
    save.add_argument("--creator", default="")
    save.add_argument("--notes", default="")
    add = sub.add_parser("import-asset")
    add.add_argument("source")
    add.add_argument("origin", choices=["self_recorded", "muse", "licensed_external"])
    add.add_argument("--rights-note", default="")
    batch = sub.add_parser("import-muse-batch")
    batch.add_argument("source")
    sub.add_parser("list-assets")
    lookup_assets = sub.add_parser("search-assets")
    lookup_assets.add_argument("query")
    shot = sub.add_parser("prepare-shot")
    shot.add_argument("material_ids", nargs="+")
    shot.add_argument("--name", default="")
    video = sub.add_parser("prepare-video")
    video.add_argument("source")
    edit = sub.add_parser("edit-asset")
    edit.add_argument("material_id")
    edit.add_argument("name")
    edit.add_argument("tags")
    send = sub.add_parser("send-shot")
    send.add_argument("material_ids", nargs="+")
    send.add_argument("--name", default="")
    operate = sub.add_parser("workspace")
    operate.add_argument("action", choices=["resume", "validate", "approve", "media", "prepare"])
    operate.add_argument("path")
    operate.add_argument("--provider", default="")
    operate.add_argument("--model", default="")
    operate.add_argument("--effort", default="")
    operate.add_argument("--reviewed-sha", default="")
    operate.add_argument("--confirm", action="store_true")
    args = parser.parse_args()
    try:
        if args.command == "doctor":
            result = disk_status()
        elif args.command == "search":
            result = {"results": search(args.platform, args.query)}
        elif args.command == "save-benchmark":
            result = save_benchmark(args.platform, args.url, args.title, args.creator, args.notes)
        elif args.command == "import-asset":
            result = import_asset(args.source, args.origin, args.rights_note)
        elif args.command == "import-muse-batch":
            from muse_batch import receive
            result = receive(args.source)
        elif args.command == "list-assets":
            result = list_library()
        elif args.command == "search-assets":
            result = search_assets(args.query)
        elif args.command == "prepare-shot":
            result = prepare_shot(args.material_ids, args.name)
        elif args.command == "send-shot":
            result = send_shot(args.material_ids, args.name)
        elif args.command == "edit-asset":
            result = edit_asset(args.material_id, args.name, args.tags)
        elif args.command == "workspace":
            result = resume_prepare(args.path) if args.action == "prepare" else operate_workspace(args.action, args.path, args.provider, args.model, args.effort, args.reviewed_sha, args.confirm)
        else:
            result = prepare_video(args.source)
        emit({"ok": True, "data": result})
    except Exception as error:
        response = {"ok": False, "error": str(error)}
        if isinstance(error, StageFailure):
            response["failure_category"] = error.category
            response["recovery_hint"] = production.recovery_message(error.category)
            try:
                response["recovery"] = workspace_snapshot(str(error.workspace))
            except Exception:
                pass
        emit(response)
        return 1
    return 0


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, stop_child)
    signal.signal(signal.SIGINT, stop_child)
    sys.exit(main())
