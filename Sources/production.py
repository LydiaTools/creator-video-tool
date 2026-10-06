"""Small production records and read-only checks around the frozen Stable3 package."""

import importlib
import hashlib
import json
import sys
from datetime import datetime
from pathlib import Path


LEDGER_NAME = "tool_run_ledger.json"


def read_json(path, default=None):
    path = Path(path)
    if not path.is_file():
        return default
    if path.stat().st_size > 4 * 1024**2:
        raise ValueError(f"生产记录过大，停止读取：{path.name}")
    return json.loads(path.read_text(encoding="utf-8"))


def frozen_common(release):
    directory = str(Path(release) / "scripts")
    if directory not in sys.path:
        sys.path.insert(0, directory)
    return importlib.import_module("common")


def approval_state(workspace, release, state, segments):
    if not (workspace / "translation.lock.json").is_file():
        return {"valid": False, "issue": "英文稿尚未批准", "media_plan_matches": None}
    try:
        config = read_json(workspace / "run_config.json", {})
        common = frozen_common(release)
        common.assert_translation_lock(workspace, segments, config, state)
        expected = common.media_plan_digest(segments, config, state)
        cached = workspace / "media.input.sha256"
        recorded = cached.read_text(encoding="utf-8").strip() if cached.is_file() else None
        return {"valid": True, "issue": "", "media_plan_sha256": expected,
                "media_plan_matches": recorded == expected if recorded else None}
    except (ValueError, KeyError, OSError, RuntimeError, ImportError) as error:
        return {"valid": False, "issue": str(error), "media_plan_matches": None}


def failure_kind(detail):
    if "video/audio duration delta" in detail:
        return "timing"
    if "TTS QA failed" in detail:
        return "tts_quality"
    if "stale TTS" in detail or "media plan" in detail:
        return "media_plan"
    if "translation lock" in detail or "approved translation" in detail:
        return "approval"
    if "runtime" in detail or "attestation" in detail:
        return "runtime"
    return "process_failure"


def recovery_message(kind):
    return {
        "resource": "资源不足，本次进程已停止；释放空间或内存后从现有工作区继续。",
        "timeout": "本次进程已超时并停止，日志和分段保留；核对阶段后继续。",
        "cancelled": "本次任务已停止，分段和批准稿保留。",
        "timing": "时长门未通过；工具仅对已证明的帧舍入模式尝试一次恢复。其它情况保留日志待检查。",
        "tts_quality": "有配音分段未通过，先核对对应数字、实体和原音频，再续跑原管线。",
        "media_plan": "媒体计划已变化，保留原件；核对有效批准和当前计划，不强行复用旧校正。",
        "approval": "英文稿或批准锁已变化，重新验证并确认当前全文。",
        "runtime": "固定发布包或运行时未通过，恢复原依赖后继续。",
    }.get(kind, "本次任务未完成，查看阶段日志定位原因；从现有工作区继续。")


def save_run(workspace, run, writer):
    path = workspace / LEDGER_NAME
    ledger = read_json(path, {"schema": 1, "runs": [], "costs": {"model_api_amount": None,
                            "currency": None, "human_minutes": None}})
    runs = [item for item in ledger.get("runs", []) if item.get("id") != run["id"]]
    runs.append(run)
    ledger["runs"] = runs[-200:]
    writer(path, ledger)


def summary(workspace):
    ledger = read_json(workspace / LEDGER_NAME, {})
    runs = ledger.get("runs", [])
    failed = [run for run in runs if run.get("status") in {"failed", "cancelled", "timed_out", "resource_stopped"}]
    active = [run for run in runs if run.get("status") == "running"]
    last_failure = failed[-1] if failed else {}
    landmarks = {}
    state = read_json(workspace / "state.json", {})
    if state.get("created_at"):
        landmarks["workspace_created"] = state["created_at"]
    for label, filename in (("prepared", "source_quality_report.json"),
                            ("approved", "translation.lock.json"),
                            ("narration_ready", "full_en.wav"),
                            ("media_ready", "final_en_softsub.mp4")):
        path = workspace / filename
        if path.is_file():
            landmarks[label] = datetime.fromtimestamp(path.stat().st_mtime).isoformat(timespec="seconds")
    elapsed = sum(run.get("elapsed_seconds", 0) for run in runs if run.get("status") != "running")
    return {"ledger_path": str(workspace / LEDGER_NAME), "run_count": len(runs),
            "recorded_process_seconds": round(elapsed, 1) if runs else None,
            "failed_run_count": len(failed), "last_failure": last_failure, "last_run": runs[-1] if runs else {},
            "interrupted_record": bool(active), "landmarks_local": landmarks,
            "costs": ledger.get("costs", {}),
            "time_basis": "已记录进程墙钟；历史文件节点含等待，均不是 CPU 时间或账单"}


def activate_candidate(workspace, candidate_report, approval_checker, writer):
    """Preserve the original before atomically activating one verified candidate."""
    video = workspace / "video_adjusted.mp4"
    candidate = workspace / "video_adjusted_synced.mp4"
    backup = workspace / "video_adjusted_presync.mp4"
    if (candidate_report.get("candidate_verified") is not True
            or Path(candidate_report.get("candidate", "")).resolve() != candidate.resolve()):
        raise RuntimeError("时长候选没有通过验证，保持原文件")
    if any((workspace / name).exists() for name in ("final_en_softsub.mp4", "final_media_report.json")):
        raise RuntimeError("已有最终媒体文件，停止自动校正")
    if backup.exists() or not candidate.is_file() or not video.is_file():
        raise RuntimeError("时长恢复已有留档或文件不全，请核对后续跑；不做二次缩放")
    approval = approval_checker()
    if not approval.get("valid") or approval.get("media_plan_matches") is not True:
        raise RuntimeError("QA 后媒体计划与批准锁未稳定一致，停止校正")
    def digest(path):
        value = hashlib.sha256()
        with path.open("rb") as source:
            for block in iter(lambda: source.read(1024 * 1024), b""):
                value.update(block)
        return value.hexdigest()
    if (digest(video) != candidate_report.get("original_file_sha256")
            or digest(candidate) != candidate_report.get("candidate_sha256")):
        raise RuntimeError("原视频或候选在验证后发生变化，停止激活")
    import os
    # link creates an exclusive backup and fails if another operator already preserved it.
    os.link(video, backup)
    writer(workspace / "tool_timing_recovery.json", {
        "schema": 1, "status": "candidate_verified_original_preserved",
        "media_plan_sha256": approval["media_plan_sha256"], "report": candidate_report,
        "original": str(backup), "final_pipeline_passed": False})
    os.replace(candidate, video)
    return {"status": "activated_waiting_original_pipeline", "original": str(backup)}
