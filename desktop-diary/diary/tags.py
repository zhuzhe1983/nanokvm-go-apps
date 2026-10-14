"""Small, explicit vocabulary: merge known aliases, never fuzzy-match activities."""
import math
import unicodedata


GROUPS = {
    "桌面壁纸": ("壁纸", "背景壁纸", "桌面背景"),
    "夜景": ("夜间", "夜晚", "夜色", "夜间桌面"),
    "宁静": ("静谧", "安静", "平静"),
    "花卉": ("花朵", "鲜花"),
    "终端": ("终端应用", "命令行", "命令行终端"),
    "编程": ("写代码", "代码编写", "程序开发"),
    "系统设置": ("系统设定",),
}
ALIASES = {alias.casefold(): canonical for canonical, aliases in GROUPS.items()
           for alias in (canonical, *aliases)}
# Only suppress these generic terms when the more specific tag is present.
# Related concepts like 动漫 / 插画, 游戏 / 直播 remain independent.
SPECIFIC = {
    "桌面": {"桌面壁纸", "动漫壁纸", "风景壁纸"},
    "桌面壁纸": {"动漫壁纸", "风景壁纸"},
    "插画": {"动漫插画", "奇幻插画"},
    "动漫": {"动漫插画", "动漫壁纸"},
    "夜景": {"星空", "夜空"},
    "夜空": {"星空"},
}


def canonical_label(value):
    if not isinstance(value, str):
        return ""
    text = unicodedata.normalize("NFKC", value)
    text = " ".join("".join(c for c in text if unicodedata.category(c) != "Cf").split()).strip(" #，,。.")[:10]
    return ALIASES.get(text.casefold(), text)


def number(value, default=0.5):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return default
    return min(1.0, max(0.0, float(value)))


def normalize_tags(raw_tags, palette):
    """Each concept contributes at most once per observation, even across colors."""
    merged = {}
    for raw in raw_tags[:20]:
        if not isinstance(raw, dict):
            continue
        label = canonical_label(raw.get("label"))
        if not label:
            continue
        category = raw.get("category", "neutral")
        if not isinstance(category, str) or category not in palette:
            category = "neutral"
        tag = {"label": label, "category": category, "weight": number(raw.get("weight")),
               "confidence": number(raw.get("confidence"))}
        key = label.casefold()
        if key not in merged or tag["weight"] * tag["confidence"] > merged[key]["weight"] * merged[key]["confidence"]:
            merged[key] = tag
    return list(merged.values())


def diverse_tags(tags, limit=10):
    labels = {tag["label"] for tag in tags}
    return [tag for tag in tags if not SPECIFIC.get(tag["label"], set()) & labels][:limit]
