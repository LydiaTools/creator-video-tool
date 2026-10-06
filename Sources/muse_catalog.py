"""Refresh a small selection index without moving or editing original assets."""
from collections import Counter
from datetime import datetime, timezone, timedelta
from pathlib import Path
import json
import re

import video_tool


CATEGORIES = ["中文口播与工具演示", "网站园艺", "产品与服务说明", "片尾与转场"]


def classify(asset, previous):
    # Existing classifications may have been adjusted by the user.
    if previous.get("category"):
        return previous["category"], previous.get("subcategory", "未细分")
    if asset.get("category"):
        return asset["category"], asset.get("subcategory") or "未细分"
    words = " ".join([asset.get("display_name", ""), *asset.get("tags", []),
                      *asset.get("shot_roles", []), asset.get("usage_notes", "")])
    subject = " ".join([asset.get("display_name", ""), *asset.get("tags", [])])
    if "覆盖物补铺" in subject or re.search(r"\bmulch\s+top[-\s]+up\b", subject, flags=re.IGNORECASE):
        return CATEGORIES[1], "覆盖物补铺"
    if "草皮" in subject or re.search(r"\bsod\b", subject, flags=re.IGNORECASE):
        return CATEGORIES[1], "草皮采购与铺设"
    if "盆栽土" in subject or re.search(r"\bpotting\s+soil\b", subject, flags=re.IGNORECASE):
        return CATEGORIES[1], "盆栽用土与容器"
    if "松针" in subject or re.search(r"\bpine\s+straw\b", subject, flags=re.IGNORECASE):
        return CATEGORIES[1], "松针覆盖物采购与铺设"
    if "碎石" in subject or re.search(r"\bgravel\b", subject, flags=re.IGNORECASE):
        return CATEGORIES[1], "碎石采购与铺设"
    if "种植床" in subject or re.search(r"\braised\s+(?:garden\s+)?bed\b", subject, flags=re.IGNORECASE):
        return CATEGORIES[1], "种植床用土"
    if any(word in words for word in ["中文口播", "信息加工", "创作者", "口播", "电脑", "键鼠", "文件夹"]):
        if any(word in words for word in ["审听", "试听", "耳机", "改稿"]):
            return CATEGORIES[0], "审稿与试听"
        if any(word in words.lower() for word in ["录音", "话筒", "麦克风", "配音", "旁白", "音色", "voiceover", "voice-over", "microphone", "narration"]):
            return CATEGORIES[0], "声音与口播录制"
        if any(word in words for word in ["资料", "信息", "阅读", "筛选", "重点", "笔记"]):
            return CATEGORIES[0], "找资料与加工文案"
        return CATEGORIES[0], "创作与工具演示"
    if any(word in words for word in ["CoverCalc", "园艺", "庭院", "花坛", "覆盖物", "树皮"]):
        return CATEGORIES[1], "园艺情境"
    if any(word in words for word in ["片尾", "留白", "转场", "行动提示"]):
        return CATEGORIES[3], "片尾与转场"
    if any(word in words for word in ["外贸", "样件", "产品细节", "包装", "服务", "协作", "会议"]):
        return CATEGORIES[2], "产品与服务过程"
    return "待确认用途", "先核对画面与本期台词"


def refresh():
    video_tool.mounted_volume()
    root = video_tool.LIBRARY
    target = root / "MUSE补库/分类目录.json"
    previous = json.loads(target.read_text()) if target.is_file() else {}
    old = {item["material_id"]: item for item in previous.get("items", [])}
    assets = video_tool.list_library()["assets"]
    items = []
    for asset in assets:
        ident = asset["material_id"]
        path = Path(asset["path"])
        card = Path(asset["card_path"])
        if not path.is_file() or not card.is_file():
            raise ValueError(f"原件或素材卡缺失：{ident}")
        category, subcategory = classify(asset, old.get(ident, {}))
        blocked = bool(asset.get("needs_revision", False))
        items.append(dict(material_id=ident, name=asset.get("display_name", path.stem),
                          category=category, subcategory=subcategory,
                          media_type="视频" if path.suffix.lower() == ".mp4" else "图片",
                          source=asset.get("origin", "未知"), status="禁选" if blocked else "内部候选",
                          path=str(path), card_path=str(card), tags=asset.get("tags", []),
                          shot_roles=asset.get("shot_roles", []), usage=asset.get("usage_notes", ""),
                          needs_revision=blocked, synthetic_marker=asset.get("synthetic_marker", ""),
                          publication_approved=bool(asset.get("cleared_for_publication", False))))
        if asset.get("derivation"):
            items[-1]["derivation"] = asset["derivation"]
    items.sort(key=lambda item: item["material_id"])
    existing = sorted(previous.get("items", []), key=lambda item: item["material_id"])
    entry = root / "从这里选素材.md"
    def selection_entry_matches():
        if not entry.is_file():
            return False
        expected = {}
        for item in items:
            if item["needs_revision"]:
                continue
            name = item["name"].replace("[", "（").replace("]", "）")
            line = f"- [{name}](<{item['path']}>)｜{item['media_type']}｜{item['material_id']}"
            expected[line] = item
        current_category = current_subcategory = None
        found = set()
        for line in entry.read_text().splitlines():
            if line.startswith("## "):
                current_category = line[3:]
                current_subcategory = None
            elif line.startswith("### "):
                current_subcategory = line[4:]
            elif line in expected:
                item = expected[line]
                if (current_category, current_subcategory) != (item["category"], item["subcategory"]):
                    return False
                found.add(item["material_id"])
        return found == {item["material_id"] for item in items if not item["needs_revision"]}

    if existing == items and selection_entry_matches():
        return {"changed": False, "cards": len(items)}
    timestamp = datetime.now(timezone(timedelta(hours=8))).isoformat()
    data = dict(previous, schema=1, updated_at=timestamp, items=items)
    data.setdefault("source", "按实际素材卡刷新取用目录；原件、名称、标签和素材卡不改动")
    video_tool.atomic_json(target, data)
    muse = [item for item in items if item["source"] == "muse"]
    derived = sum(bool(item.get("derivation")) for item in muse)
    source_summary = (f"MUSE素材{len(muse)}件（生成交付原件{len(muse) - derived}件、派生版{derived}件）"
                      if derived else f"MUSE原件{len(muse)}件")
    lines = ["# 从这里选素材", "", "在“视频制作”应用的“素材库”按名称或标签找。下面直接链接原件。", "",
             f"更新：{timestamp}。共享素材卡{len(items)}张；{source_summary}，其中内部候选"
             f"{sum(not item['needs_revision'] for item in muse)}件、禁选{sum(item['needs_revision'] for item in muse)}件。", "",
             "合成画面作情境与解释；真实软件功能、账号数据和效果证据使用自己的录屏或实拍。", ""]
    for category in CATEGORIES + ["待确认用途"]:
        subset = [item for item in items if item["category"] == category and not item["needs_revision"]]
        if not subset:
            continue
        lines += [f"## {category}", ""]
        for subcategory in sorted({item["subcategory"] for item in subset}):
            lines += [f"### {subcategory}", ""]
            for item in subset:
                if item["subcategory"] == subcategory:
                    name = item["name"].replace("[", "（").replace("]", "）")
                    lines.append(f"- [{name}](<{item['path']}>)｜{item['media_type']}｜{item['material_id']}")
            lines += [""]
    lines += ["## 问题素材：保留原件，禁选", ""]
    for item in items:
        if item["needs_revision"]:
            lines.append(f"- [{item['name']}](<{item['card_path']}>)｜{item['material_id']}｜具体原因见素材卡")
    entry = root / "从这里选素材.md"
    temporary = entry.with_suffix(".md.tmp")
    temporary.write_text("\n".join(lines) + "\n")
    temporary.replace(entry)
    return {"changed": True, "cards": len(items), "muse": len(muse), "muse_originals": len(muse) - derived,
            "muse_derivatives": derived,
            "categories": dict(Counter(item["category"] for item in items)), "entry": str(entry)}


if __name__ == "__main__":
    print(json.dumps(refresh(), ensure_ascii=False))
