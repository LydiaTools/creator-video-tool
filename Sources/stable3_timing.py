#!/usr/bin/env python3
"""Inspect Stable3 rounding drift; optionally create a verified, unactivated candidate.

Does not edit the release, workspace JSON, approval locks or existing media.
Default mode is read-only. Final delivery still requires the original pipeline.
"""
import argparse
import hashlib
import json
import math
from pathlib import Path
import subprocess
import sys


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def capture(command):
    return subprocess.check_output(command, text=True)


def file_sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def probe(ffprobe, path):
    data = json.loads(capture([ffprobe, "-v", "error", "-show_streams",
                               "-show_format", "-of", "json", str(path)]))
    duration = float(data["format"]["duration"])
    if not math.isfinite(duration) or duration <= 0:
        raise ValueError("Missing or invalid duration: " + str(path))
    return duration, data["streams"]


def packet_signature(ffprobe, path):
    data = json.loads(capture([ffprobe, "-v", "error", "-select_streams", "v:0",
                              "-show_packets", "-show_data_hash", "sha256",
                              "-show_entries", "packet=dts,data_hash",
                              "-of", "json", str(path)]))["packets"]
    if not data or any("dts" not in p or "data_hash" not in p for p in data):
        raise ValueError("Video packets lack DTS or payload hashes")
    dts = [int(p["dts"]) for p in data]
    if any(b <= a for a, b in zip(dts, dts[1:])):
        raise ValueError("Video packet DTS is not strictly increasing")
    digest = hashlib.sha256("\n".join(p["data_hash"] for p in data).encode()).hexdigest()
    return {"packet_count": len(data), "ordered_packet_hashes_sha256": digest,
            "non_increasing_dts": 0}


def classify(rows, audio_duration, video_duration, tolerance):
    """Conservative candidate bounds from E01/E02, not changed Stable3 gates."""
    if not rows:
        return {"status": "needs_diagnosis", "reasons": ["No segments"]}
    scale = audio_duration / video_duration
    audio_sum = sum(r["audio_seconds"] for r in rows)
    video_sum = sum(r["video_seconds"] for r in rows)
    audio_cursor = video_cursor = 0.0
    boundary_errors = []
    for row in rows:
        audio_cursor += row["audio_seconds"]
        video_cursor += row["video_seconds"]
        boundary_errors.append(abs(video_cursor * scale - audio_cursor))
    checks = {
        "audio_concat_matches_segments": abs(audio_sum - audio_duration) <= 0.05,
        "video_concat_matches_segments": abs(video_sum - video_duration) <= 0.05,
        "every_segment_has_small_positive_drift": all(
            0 <= r["video_seconds"] - r["audio_seconds"] <= 3 / 30 + 0.005
            for r in rows),
        "relative_adjustment_at_most_half_percent": 0 < 1 - scale <= 0.005,
        "projected_segment_boundary_error_at_most_150ms": max(boundary_errors) <= 0.15,
    }
    delta = video_duration - audio_duration
    status = ("already_within_duration_gate" if abs(delta) <= tolerance else
              "rounding_candidate" if delta > tolerance and all(checks.values()) else
              "needs_diagnosis")
    return {"status": status, "audio_seconds": audio_duration,
            "video_seconds": video_duration, "delta_seconds": round(delta, 6),
            "locked_tolerance_seconds": tolerance, "timestamp_scale": f"{scale:.12f}",
            "max_projected_boundary_error_seconds": round(max(boundary_errors), 6),
            "candidate_checks": checks}


def inspect(workspace, historical=False):
    config = read_json(workspace / "run_config.json")
    state = read_json(workspace / "state.json")
    segments = read_json(workspace / "segments_translated.json")
    ffprobe = config["paths"]["ffprobe"]
    video = workspace / ("video_adjusted_presync.mp4" if historical else "video_adjusted.mp4")
    audio = workspace / "full_en.wav"
    ad, audio_streams = probe(ffprobe, audio)
    vd, video_streams = probe(ffprobe, video)
    if len(video_streams) != 1 or video_streams[0].get("codec_name") != "h264":
        raise ValueError("Expected exactly one H.264 stream in adjusted video")
    if len(audio_streams) != 1 or audio_streams[0].get("codec_type") != "audio":
        raise ValueError("Expected one audio stream")
    rows = []
    for segment in segments:
        sa, _ = probe(ffprobe, Path(segment["audio_path"]))
        sv, streams = probe(ffprobe, Path(segment["video_path"]))
        if (len(streams) != 1 or streams[0].get("codec_name") != "h264"
                or streams[0].get("avg_frame_rate") != "30/1"):
            raise ValueError("Only the verified 30 fps H.264 per-segment pattern is supported")
        rows.append({"index": segment["index"], "audio_seconds": sa,
                     "video_seconds": sv, "delta_seconds": round(sv - sa, 6)})
    report = classify(rows, ad, vd, float(config["quality"][
        "require_video_audio_duration_delta_seconds"]))
    report.update({"workspace": str(workspace), "inspected_video": str(video),
                   "historical_inspection": historical, "segments": rows,
                   "media_stage": state.get("stages", {}).get("media", {}).get("status"),
                   "candidate_activated": False})
    return report, config, state, video


def create_candidate(workspace, candidate, report, config, state, video):
    if report["historical_inspection"] or report["status"] != "rounding_candidate":
        raise ValueError("Candidate creation requires a current, diagnosed rounding failure")
    if (report["media_stage"] == "done" or
            any((workspace / name).exists() for name in
                ("final_en_softsub.mp4", "final_en_hardsub.mp4", "final_media_report.json"))):
        raise ValueError("Existing final delivery is protected; no candidate is created")
    indices = {r["index"] for r in report["segments"]}
    stages = state.get("stages", {})
    if (stages.get("translation", {}).get("status") != "done"
            or not (workspace / "translation.lock.json").is_file()
            or set(stages.get("media", {}).get("qa_passed_segments", [])) != indices):
        raise ValueError("Requires an approved translation and all segment QA checkpoints")
    if candidate.parent != workspace or candidate.name != "video_adjusted_synced.mp4":
        raise ValueError("Candidate must be workspace/video_adjusted_synced.mp4")
    if candidate.exists():
        raise ValueError("Candidate already exists; inspect it before retrying")
    original_hash = file_sha256(video)
    original_signature = packet_signature(config["paths"]["ffprobe"], video)
    subprocess.run([config["paths"]["ffmpeg"], "-nostdin", "-v", "error", "-n",
                    "-itsscale", report["timestamp_scale"], "-i", str(video),
                    "-map", "0:v:0", "-c", "copy", "-map_metadata", "-1",
                    str(candidate)], check=True)
    new_duration, new_streams = probe(config["paths"]["ffprobe"], candidate)
    _, old_streams = probe(config["paths"]["ffprobe"], video)
    new_signature = packet_signature(config["paths"]["ffprobe"], candidate)
    fields = ("codec_name", "width", "height", "pix_fmt", "nb_frames")
    if (original_signature != new_signature
            or any(old_streams[0].get(k) != new_streams[0].get(k) for k in fields)
            or abs(new_duration - report["audio_seconds"]) > 0.001):
        raise ValueError("Candidate verification failed; original remains untouched")
    report.update({"candidate": str(candidate), "candidate_verified": True,
                   "original_file_sha256": original_hash, "candidate_sha256": file_sha256(candidate),
                   "candidate_duration_seconds": new_duration,
                   "packet_verification": new_signature,
                   "next": "Recheck locks, preserve presync, activate candidate, resume original Stable3"})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--historical-presync", action="store_true", help="Read pre-repair evidence only")
    parser.add_argument("--write-candidate", action="store_true", help="Create candidate only; never activate it")
    args = parser.parse_args()
    workspace = args.workspace.resolve(strict=True)
    report, config, state, video = inspect(workspace, args.historical_presync)
    if args.write_candidate:
        create_candidate(workspace, workspace / "video_adjusted_synced.mp4",
                         report, config, state, video)
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError, KeyError, subprocess.SubprocessError) as exc:
        print("STOP: " + str(exc), file=sys.stderr)
        sys.exit(2)
