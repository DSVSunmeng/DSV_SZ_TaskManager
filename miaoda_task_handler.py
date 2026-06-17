"""
妙搭任务处理器 — 解析妙搭返回的任务列表，创建 Trinity 任务

用法（被 feishu_ws_bot.py 调用）：
  from miaoda_task_handler import process_miaoda_tasks
  result = process_miaoda_tasks(tasks, project_id, creator_name, parent_task)

缓存说明：
  - .name_map.json: 中文名 → 英文名映射（用户维护）
  - .member_cache.json: 英文名 → UID 缓存（从 Trinity 自动获取）
"""
import os
import json
import time
import logging
import requests
from requests.auth import HTTPBasicAuth
from datetime import datetime
from typing import Optional, Tuple
from pypinyin import lazy_pinyin

logger = logging.getLogger(__name__)

# ========== 配置 ==========
CONFIG_DIR = os.path.dirname(os.path.abspath(__file__))
NAME_MAP_FILE = os.path.join(CONFIG_DIR, ".name_map.json")
MEMBER_CACHE_FILE = os.path.join(CONFIG_DIR, ".member_cache.json")

_DEFAULT_NAME_MAP = {
    "孙猛": "Sun Meng",
    "宋学郊": "Song Xuejiao",
}

# 飞书 API 凭据（与 feishu_ws_bot.py 保持一致）
FEISHU_APP_ID = "cli_a9451285c0b81bc9"
FEISHU_APP_SECRET = "eDgs2IhuO9IW9N7gmU9bBgFF6acx12aN"
_feishu_token_cache = {"token": "", "expire_at": 0}

MEMBER_CACHE_TTL = 3600  # 缓存有效期 1 小时

# 运行时缓存
_name_map = {}
_member_cache: dict = {}
_member_cache_time = 0.0


# ========== NAME_MAP 管理 ==========

def load_name_map():
    global _name_map
    if os.path.exists(NAME_MAP_FILE):
        try:
            with open(NAME_MAP_FILE, "r", encoding="utf-8") as f:
                _name_map = json.load(f)
            logger.info("NAME_MAP 已加载: %d 条", len(_name_map))
            return
        except Exception as e:
            logger.warning("读取 NAME_MAP 文件失败: %s", e)
    _name_map = dict(_DEFAULT_NAME_MAP)
    try:
        with open(NAME_MAP_FILE, "w", encoding="utf-8") as f:
            json.dump(_name_map, f, ensure_ascii=False, indent=2)
        logger.info("NAME_MAP 默认文件已创建: %s", NAME_MAP_FILE)
    except Exception as e:
        logger.warning("写入 NAME_MAP 文件失败: %s", e)


# ========== 成员缓存管理 ==========

def _build_member_cache(project_id: str) -> bool:
    global _member_cache, _member_cache_time
    import config as trinity_config

    try:
        request_info = trinity_config.get_request_info()
        token = trinity_config.get_token()
        url = rf'{request_info.host}/hzsv/Trinity/Trinity_GetProjectMembersById'

        data = {
            "params": {"projectIds": [project_id]},
            "platform": "yunfeng",
        }
        headers = {
            "Content-Type": "application/json",
            "X-Token": f"Bearer {token}",
        }

        resp = requests.post(url, json=data, headers=headers,
                             auth=HTTPBasicAuth(request_info.pi_user, request_info.pi_pw),
                             timeout=60)
        j = resp.json()
        if j.get("code") != 200:
            logger.error("获取项目成员失败: %s", j.get("msg"))
            return False

        members = j.get("data", [])
        cache = {}
        for mem in members:
            full_name = mem.get("name", "")
            uid = mem.get("userId", "")
            name_part = full_name.split("(")[0].strip()
            if name_part and uid:
                cache[name_part.lower()] = {"uid": uid, "full_name": full_name}

        _member_cache = cache
        _member_cache_time = time.time()

        cache_data = {
            "time": _member_cache_time,
            "project_id": project_id,
            "cache": cache,
        }
        try:
            with open(MEMBER_CACHE_FILE, "w", encoding="utf-8") as f:
                json.dump(cache_data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.warning("写入成员缓存文件失败: %s", e)

        logger.info("成员缓存已更新: %d 人 (项目: %s)", len(cache), project_id)
        return True
    except Exception as e:
        logger.exception("构建成员缓存异常: %s", e)
        return False


def _ensure_member_cache(project_id: str) -> bool:
    global _member_cache, _member_cache_time

    if _member_cache and time.time() - _member_cache_time < MEMBER_CACHE_TTL:
        return True

    try:
        if os.path.exists(MEMBER_CACHE_FILE):
            with open(MEMBER_CACHE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            if (data.get("project_id") == project_id
                    and time.time() - data.get("time", 0) < MEMBER_CACHE_TTL):
                _member_cache = data["cache"]
                _member_cache_time = data["time"]
                logger.info("成员缓存已从文件加载: %d 人", len(_member_cache))
                return True
    except Exception as e:
        logger.warning("读取成员缓存文件失败: %s", e)

    return _build_member_cache(project_id)


# ========== 飞书 API 辅助 ==========

def _get_feishu_token() -> str:
    """获取飞书 tenant_access_token（带本地缓存，避免重复获取）"""
    if time.time() < _feishu_token_cache["expire_at"] - 60:
        return _feishu_token_cache["token"]
    try:
        resp = requests.post(
            "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
            json={"app_id": FEISHU_APP_ID, "app_secret": FEISHU_APP_SECRET},
            timeout=15,
        )
        data = resp.json()
        _feishu_token_cache["token"] = data["tenant_access_token"]
        _feishu_token_cache["expire_at"] = time.time() + data["expire"]
        return _feishu_token_cache["token"]
    except Exception as e:
        logger.exception("获取飞书 token 失败: %s", e)
        return ""


def _resolve_feishu_id(feishu_id: str) -> tuple:
    """
    通过飞书联系人 API 查询用户 ID（open_id / user_id）。
    返回 (name, user_id)，失败时 (None, None)。
    name=中文姓名，user_id=飞书用户 ID（可能和 Trinity UID 一致）。
    """
    token = _get_feishu_token()
    if not token:
        return None, None
    try:
        resp = requests.get(
            f"https://open.feishu.cn/open-apis/contact/v3/users/{feishu_id}",
            headers={"Authorization": f"Bearer {token}"},
            timeout=10,
        )
        data = resp.json()
        logger.info("飞书联系人查询: id=%s result=%s", feishu_id,
                     json.dumps(data.get("data", {}), ensure_ascii=False)[:300])
        if data.get("code") == 0:
            user = data.get("data", {}).get("user", {})
            name = user.get("name", "")
            uid = user.get("user_id", "")
            if name or uid:
                logger.info("飞书 ID 解析成功: %s -> name=%s user_id=%s", feishu_id, name, uid)
                return name, uid
        logger.warning("飞书 ID 查询未返回数据: id=%s code=%s", feishu_id, data.get("code"))
        return None, None
    except Exception as e:
        logger.exception("飞书联系人 API 异常: id=%s err=%s", feishu_id, e)
        return None, None


# 姓名 → open_id 缓存
_name_to_openid_cache: dict = {}
_name_to_openid_cache_time = 0.0
_NAME_TO_OID_CACHE_TTL = 3600  # 1 小时


def _find_open_id_by_name(chinese_name: str) -> str:
    """
    通过搜索飞书联系人列表，按中文名查找用户的 open_id。
    返回 open_id（ou_xxx），未找到返回空字符串。
    结果缓存在内存中避免重复搜索。
    """
    global _name_to_openid_cache, _name_to_openid_cache_time

    # 缓存命中
    if (_name_to_openid_cache
            and time.time() - _name_to_openid_cache_time < _NAME_TO_OID_CACHE_TTL):
        return _name_to_openid_cache.get(chinese_name, "")

    token = _get_feishu_token()
    if not token:
        return ""

    try:
        # 分页搜索飞书联系人
        page_token = ""
        all_users = {}
        while True:
            params = {"page_size": 50}
            if page_token:
                params["page_token"] = page_token
            resp = requests.get(
                "https://open.feishu.cn/open-apis/contact/v3/users",
                headers={"Authorization": f"Bearer {token}"},
                params=params,
                timeout=15,
            )
            data = resp.json()
            if data.get("code") != 0:
                logger.warning("飞书联系人列表查询失败: code=%s", data.get("code"))
                break

            items = data.get("data", {}).get("items", [])
            for user in items:
                name = user.get("name", "")
                oid = user.get("open_id", "")
                if name and oid:
                    all_users[name] = oid

            if not data.get("data", {}).get("has_more"):
                break
            page_token = data.get("data", {}).get("page_token", "")

        _name_to_openid_cache = all_users
        _name_to_openid_cache_time = time.time()
        logger.info("飞书联系人缓存已更新: %d 人", len(all_users))

        return all_users.get(chinese_name, "")
    except Exception as e:
        logger.exception("搜索飞书联系人异常: %s", e)
        return ""


# ========== 名称解析 ==========

def _chinese_to_pinyin_candidates(chinese_name: str) -> list:
    """
    将中文名转为可能的英文拼音格式列表。
    例: "房汉柠" → ["fang hanning", "fang han ning"]
         "孙猛" → ["sun meng"]
         "蔡波" → ["cai bo"]
    """
    parts = [p.lower().capitalize() for p in lazy_pinyin(chinese_name)]
    if len(parts) <= 2:
        return [" ".join(parts).lower()]
    # 3+ 字: 尝试姓氏+名(连写) 和 全分开
    candidates = [
        f"{parts[0]} {''.join(parts[1:])}".lower(),
        " ".join(parts).lower(),
    ]
    return list(set(candidates))


def _resolve_by_name(chinese_name: str, project_id: str) -> tuple:
    """
    通过中文名查找 Trinity UID。
    流程：NAME_MAP → 拼音 → member_cache
    返回 (uid, english_name, None)，第三个值为飞书 open_id（仅 @/ou_ 路径才有）
    """
    # 1. NAME_MAP 手动覆盖
    english_name = _name_map.get(chinese_name)
    if english_name:
        _ensure_member_cache(project_id)
        entry = _member_cache.get(english_name.lower())
        if entry:
            return entry["uid"], english_name, None
        # 缓存未命中，刷新后重试
        if _build_member_cache(project_id):
            entry = _member_cache.get(english_name.lower())
            if entry:
                return entry["uid"], english_name, None
        logger.error("未在项目 %s 中找到成员: %s (%s)", project_id, english_name, chinese_name)
        return None, None, None

    # 2. 自动拼音转换
    _ensure_member_cache(project_id)
    candidates = _chinese_to_pinyin_candidates(chinese_name)
    logger.info("自动拼音转换 %s -> %s", chinese_name, candidates)

    for cand in candidates:
        entry = _member_cache.get(cand)
        if entry:
            # 转回标准首字母大写格式
            display_name = cand.split()
            if len(display_name) >= 2:
                eng_name = display_name[0].capitalize() + " " + "".join(p.capitalize() for p in display_name[1:])
            else:
                eng_name = cand.capitalize()
            logger.info("拼音匹配成功: %s -> %s (%s)", chinese_name, eng_name, entry["uid"])
            return entry["uid"], eng_name, None

    # 3. 刷新缓存再试一次
    if _build_member_cache(project_id):
        for cand in candidates:
            entry = _member_cache.get(cand)
            if entry:
                display_name = cand.split()
                if len(display_name) >= 2:
                    eng_name = display_name[0].capitalize() + " " + "".join(p.capitalize() for p in display_name[1:])
                else:
                    eng_name = cand.capitalize()
                return entry["uid"], eng_name, None

    logger.error("无法解析负责人「%s」的 UID（项目 %s 中未找到该成员）", chinese_name, project_id)
    return None, None, None


def resolve_name_to_uid(name_or_id: str, project_id: str) -> tuple:
    """
    解析指派人 → (UID, 英文名, 飞书 open_id)

    支持三种输入：
      1. `@feishu_id` 或裸 `ou_xxx` — 飞书 open_id，通过飞书 API 直接获取 user_id + 姓名
      2. 中文名 — 走原流程：NAME_MAP → 拼音 → member_cache

    返回 (uid, english_name, feishu_open_id)，失败时 (None, None, None)
    """
    raw = name_or_id.strip()

    # ---- 预处理：提取 "姓名(@ou_xxx)" 格式中的姓名或 open_id ----
    import re
    m = re.match(r'^(.+?)\(@(ou_\w+)\)$', raw)
    if m:
        candidate_name = m.group(1).strip()
        candidate_oid = m.group(2)
        feishu_id = candidate_oid
        logger.info("检测到 姓名+ID 格式: name=%s oid=%s", candidate_name, candidate_oid)
    else:
        feishu_id = ""

    if not feishu_id:
        if raw.startswith("@"):
            feishu_id = raw[1:].strip()
        elif raw.startswith("ou_"):
            feishu_id = raw

    if feishu_id:
        logger.info("检测到飞书 ID: %s", feishu_id)
        feishu_name, feishu_uid = _resolve_feishu_id(feishu_id)

        # _resolve_feishu_id 也返回 open_id 信息，但这里 feishu_id 本身就是 open_id
        feishu_open_id = feishu_id  # feishu_id 是 ou_xxx 格式，可直接当 open_id 用

        if not feishu_name and not feishu_uid:
            logger.error("飞书 ID %s 查询失败", feishu_id)
            return None, None, None

        # 1) 优先：用飞书 user_id 直接搜 member_cache（不经过英文名）
        if feishu_uid:
            _ensure_member_cache(project_id)
            for eng_key, entry in _member_cache.items():
                if entry.get("uid") == feishu_uid:
                    eng_name = eng_key.split()
                    display = eng_key.capitalize() if len(eng_name) == 1 else \
                        eng_name[0].capitalize() + " " + "".join(p.capitalize() for p in eng_name[1:])
                    logger.info("飞书 user_id 直接匹配: %s -> %s (%s)", feishu_id, display, feishu_uid)
                    return feishu_uid, display, feishu_open_id

        # 2) 用姓名走 NAME_MAP → 拼音 → member_cache
        if feishu_name:
            uid, eng, _ = _resolve_by_name(feishu_name, project_id)
            if uid:
                return uid, eng, feishu_open_id

        logger.error("飞书用户 %s(%s) 未在项目 %s 成员中找到", feishu_name, feishu_id, project_id)
        return None, None, None

    # ---- 中文名路径 ----
    uid, eng, _ = _resolve_by_name(raw, project_id)
    if uid:
        # 尝试搜索飞书联系人获取 open_id（给位表写入用）
        oid = _find_open_id_by_name(raw)
        if oid:
            return uid, eng, oid
    return uid, eng, None


# ========== 日期工具 ==========

def date_to_timestamp(date_str: str) -> int:
    """将 YYYY-MM-DD 或 YYYY/MM/DD 转为毫秒时间戳"""
    for sep in ("-", "/"):
        parts = date_str.split(sep)
        if len(parts) == 3:
            try:
                dt = datetime(int(parts[0]), int(parts[1]), int(parts[2]))
                return int(dt.timestamp() * 1000)
            except ValueError:
                continue
    raise ValueError(f"无法解析日期: {date_str}")


# ========== Trinity API ==========

def create_trinity_task(params: dict) -> dict:
    """调用 Trinity_CreateAndAssignTask 创建任务"""
    import config as trinity_config

    request_info = trinity_config.get_request_info()
    token = trinity_config.get_token()
    url = rf'{request_info.host}/hzsv/Trinity/Trinity_CreateAndAssignTask'

    data = {
        "params": params,
        "platform": "yunfeng",
    }
    headers = {
        "Content-Type": "application/json",
        "X-Token": f"Bearer {token}",
    }

    logger.info("创建任务: title=%s assignee=%s", params.get("title"), params.get("assigneeId"))

    try:
        resp = requests.post(url, json=data, headers=headers,
                             auth=HTTPBasicAuth(request_info.pi_user, request_info.pi_pw),
                             timeout=60)
    except requests.RequestException as e:
        logger.exception("创建任务请求异常: %s", e)
        return {"error": str(e)}

    try:
        resp_json = resp.json()
    except Exception:
        logger.error("创建任务返回非 JSON: status=%s body=%s", resp.status_code, resp.text[:200])
        return {"error": f"HTTP {resp.status_code}"}

    if resp_json.get("code") != 200:
        logger.error("创建任务失败: HTTP=%s code=%s msg=%s 完整响应=%s",
                     resp.status_code,
                     resp_json.get("code"), resp_json.get("msg"),
                     json.dumps(resp_json, ensure_ascii=False)[:500])
        err_msg = resp_json.get("msg") or resp_json.get("message") or f"HTTP {resp.status_code} " + json.dumps(resp_json, ensure_ascii=False)[:200]
        return {"code": resp_json.get("code"), "error": err_msg}

    logger.info("任务创建成功: %s", resp_json.get("msg"))
    return resp_json


# ========== 主入口 ==========

def process_miaoda_tasks(tasks: list, project_id: str, creator_name: str,
                         parent_task: str = "", project_name: str = "",
                         on_task_created=None) -> str:
    """
    处理妙搭返回的任务列表，逐个创建 Trinity 任务。
    返回格式化的结果文本用于飞书回复。

    参数：
      tasks: 妙搭返回的任务列表 [{taskDescription, assignee, estimatedHours, planStartAt, planEndAt}, ...]
      project_id: Trinity 项目 ID
      creator_name: 创建人中文名
      parent_task: 上级任务 ID
      on_task_created: 可选回调，每条任务创建成功后调用
                       on_task_created(title, hours, start_date, end_date, assignee_open_id, task_id, assignee_uid="")
    """
    if not tasks:
        return ""

    # 确保缓存加载
    load_name_map()
    _ensure_member_cache(project_id)

    lines = []
    success_count = 0
    fail_count = 0

    creator_uid, creator_english, _ = resolve_name_to_uid(creator_name, project_id)
    if not creator_uid:
        logger.error("无法解析创建人 %s 的 UID，使用指派人作为创建人", creator_name)

    for i, task in enumerate(tasks, 1):
        title = task.get("taskDescription", "").strip()
        assignee_cn = task.get("assignee", "").strip()
        hours = task.get("estimatedHours", 0)
        start_date = task.get("planStartAt", "")
        end_date = task.get("planEndAt", "")

        if not title or not assignee_cn:
            lines.append(f"{i}. 缺少任务名或指派人")
            fail_count += 1
            continue

        assignee_uid, assignee_en, assignee_oid = resolve_name_to_uid(assignee_cn, project_id)
        if not assignee_uid:
            lines.append(f"{i}. 「{title}」无法解析负责人「{assignee_cn}」的 UID（项目中未找到该成员）")
            fail_count += 1
            continue

        try:
            start_ts = date_to_timestamp(start_date) if start_date else 0
            end_ts = date_to_timestamp(end_date) if end_date else 0
        except ValueError as e:
            lines.append(f"{i}. 「{title}」日期解析失败: {e}")
            fail_count += 1
            continue

        params = {
            "title": title,
            "description": title,
            "assigneeId": assignee_uid,
            "reviewerId": creator_uid or assignee_uid,
            "discipline": "sw",
            "planStartDate": start_ts,
            "planEndDate": end_ts,
            "initialEstimate": float(hours) if hours else 0,
            "parent": parent_task,
            "taskLevel": 5,
            "apqp": False,
            "projectId": project_id,
            "authorId": creator_uid or assignee_uid,
            "authorName": creator_english or assignee_en,
        }

        result = create_trinity_task(params)
        if "error" in result or result.get("code") != 200:
            code = result.get("code", "?")
            msg = result.get("msg") or result.get("error") or "无错误信息"
            lines.append(f"{i}. 「{title}」创建失败(code={code}): {msg}")
            fail_count += 1
        else:
            task_id = result.get("data", {}).get("id", "")
            task_url = f"https://trinity.desaysv.com/#/task/taskDetail?id={task_id}&projectId={project_id}&projectName={project_name}&type=wbsWatch"
            lines.append(f"{i}. 「{title}」创建成功 {task_url}")
            success_count += 1

            # 回调：通知外部（如多维表格写入），传入 assignee_uid 供位表使用
            if on_task_created:
                try:
                    on_task_created(title, hours, start_date, end_date, assignee_oid, task_id, assignee_uid)
                except TypeError:
                    # 兼容旧回调（不含 assignee_uid 参数）
                    try:
                        on_task_created(title, hours, start_date, end_date, assignee_oid, task_id)
                    except Exception as cb_err:
                        logger.warning("任务创建回调异常: %s", cb_err)
                except Exception as cb_err:
                    logger.warning("任务创建回调异常: %s", cb_err)

        # 每个任务间等待 1.5 秒，避免 Trinity API 限流
        if i < len(tasks):
            time.sleep(1.5)

    summary = f"任务创建完成：成功 {success_count}，失败 {fail_count}\n\n"
    summary += "\n".join(lines)
    if parent_task:
        summary += f"\n\n上级任务: {parent_task}"
    summary += f"\n项目: {project_id}"
    summary += f"\n创建人: {creator_name}"
    return summary
