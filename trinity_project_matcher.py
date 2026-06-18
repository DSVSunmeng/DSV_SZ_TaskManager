"""
项目配置匹配器 — 从 config/projects_config.json 加载项目列表，支持缩写模糊搜索。

用法：
  from trinity_project_matcher import find_project
  result = find_project("A66T")
  # → {"exact": None, "suggestions": [{"name": "...", "abbr": "A66-T", ...}], "text": "您输入的 'A66T' 未找到，是否指 'A66-T'？"}
"""
import os
import json
import re
import logging

logger = logging.getLogger(__name__)

CONFIG_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(CONFIG_DIR, "config", "projects_config.json")

_projects = []
_projects_mtime = 0.0


def load_projects() -> list:
    """加载 projects_config.json，返回项目列表（带文件 mtime 热重载）"""
    global _projects, _projects_mtime
    try:
        mtime = os.path.getmtime(CONFIG_FILE)
        if _projects and mtime <= _projects_mtime:
            return _projects
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            _projects = json.load(f)
        _projects_mtime = mtime
        logger.info("项目配置已加载: %d 个项目", len(_projects))
    except Exception as e:
        logger.error("加载项目配置失败: %s", e)
        if not _projects:
            _projects = []
    return _projects


def _normalize(s: str) -> str:
    """标准化：去分隔符、小写"""
    return re.sub(r"[-_\s]", "", s).lower()


def _build_index(projects: list) -> dict:
    """构建标准化缩写 → 项目索引"""
    idx = {}
    for p in projects:
        norm = _normalize(p["abbr"])
        if norm not in idx:
            idx[norm] = []
        idx[norm].append(p)
    return idx


def find_project(query: str) -> dict:
    """
    根据用户输入的缩写查找项目。

    返回：
      {
        "found": 项目对象 or None,
        "suggestions": [项目对象, ...],  # 模糊匹配的建议
        "text": "用于回复用户的引导文字",  # 找到或未找到时的提示
      }

    匹配逻辑：
      1. 精确匹配（区分大小写）
      2. 标准化匹配（去分隔符、小写）
      3. 模糊匹配（包含关系、编辑距离）
    """
    projects = load_projects()
    if not projects:
        return {"found": None, "suggestions": [], "text": "项目配置为空，请检查 projects_config.json"}

    query = query.strip()
    if not query:
        return {"found": None, "suggestions": [], "text": "请输入项目缩写"}

    # 1. 精确匹配
    for p in projects:
        if p["abbr"] == query:
            return {"found": p, "suggestions": [], "text": f"已匹配项目: {p['name']} (缩写: {p['abbr']})"}

    # 2. 标准化匹配
    norm_query = _normalize(query)
    idx = _build_index(projects)
    if norm_query in idx:
        matches = idx[norm_query]
        if len(matches) == 1:
            p = matches[0]
            return {"found": p, "suggestions": [], "text": f"已匹配项目: {p['name']} (缩写: {p['abbr']})"}
        else:
            # 多个项目共享同一标准化缩写（如 BZ5、S20 域控）
            names = "\n".join(f"  · {m['name']}（{m['abbr']}）" for m in matches)
            return {"found": None, "suggestions": matches,
                    "text": f"缩写「{query}」匹配到多个项目，请指定完整缩写：\n{names}"}

    # 3. 模糊匹配：分两档
    #   强匹配：包含关系（用户输入在缩写中，或缩写包含用户输入）
    #   弱匹配：仅首字母相同
    strong = []
    weak = []
    for p in projects:
        p_norm = _normalize(p["abbr"])
        if norm_query in p_norm or p_norm in norm_query:
            strong.append(p)
        elif query[0].lower() == p["abbr"][0].lower():
            weak.append(p)

    # 强匹配去重
    seen = set()
    unique_strong = []
    for p in strong:
        if p["projectId"] not in seen:
            seen.add(p["projectId"])
            unique_strong.append(p)

    # 唯一强匹配 → 自动确认（如 "AY5" → "AY5-T"）
    if len(unique_strong) == 1:
        p = unique_strong[0]
        return {"found": p, "suggestions": [],
                "text": f"已匹配项目: {p['name']} (缩写: {p['abbr']})"}
    if len(unique_strong) > 1:
        names = "\n".join(f"  · {m['abbr']} — {m['name']}" for m in unique_strong)
        return {"found": None, "suggestions": unique_strong,
                "text": f"未找到「{query}」，您是否想找：\n{names}"}

    # 无强匹配 → 列出弱匹配（首字母）作为候选，但不自动确认
    seen = set()
    unique_weak = []
    for p in weak:
        if p["projectId"] not in seen:
            seen.add(p["projectId"])
            unique_weak.append(p)

    if unique_weak:
        names = "\n".join(f"  · {m['abbr']} — {m['name']}" for m in unique_weak)
        return {"found": None, "suggestions": unique_weak,
                "text": f"未找到「{query}」，您是否想找：\n{names}"}

    # 4. 完全没找到
    all_abbrs = "、".join(sorted(set(p["abbr"] for p in projects), key=lambda x: (len(x), x)))
    return {"found": None, "suggestions": [],
            "text": f"未找到项目「{query}」，可用项目缩写：{all_abbrs}"}
