"""
飞书多维表格写入器 — 将已创建的 Trinity 任务写入项目对应多维表格。

用法：
  from bitable_writer import write_task_to_bitable
  err = write_task_to_bitable(feishu_url, "任务名", 40.0, "2026/06/20", "2026/06/25", "ou_xxx", "TASK2026...", "uid02619")
"""
import json
import logging
import re
import time

import requests

logger = logging.getLogger(__name__)

# 飞书 API 凭据（与 feishu_ws_bot.py 保持一致）
FEISHU_APP_ID = "cli_a9451285c0b81bc9"
FEISHU_APP_SECRET = "eDgs2IhuO9IW9N7gmU9bBgFF6acx12aN"
_token_cache = {"token": "", "expire_at": 0}


def _get_feishu_token() -> str:
    """获取飞书 tenant_access_token（带本地缓存）"""
    if time.time() < _token_cache["expire_at"] - 60:
        return _token_cache["token"]
    try:
        resp = requests.post(
            "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
            json={"app_id": FEISHU_APP_ID, "app_secret": FEISHU_APP_SECRET},
            timeout=15,
        )
        data = resp.json()
        _token_cache["token"] = data["tenant_access_token"]
        _token_cache["expire_at"] = time.time() + data["expire"]
        return _token_cache["token"]
    except Exception as e:
        logger.exception("获取飞书 token 失败: %s", e)
        return ""


def _parse_bitable_url(url: str) -> tuple:
    """从飞书多维表格 URL 提取 (app_token, table_id)，失败返回 (None, None)"""
    m = re.search(r'/base/([^/?]+)', url)
    t = re.search(r'table=([^&]+)', url)
    if m and t:
        return m.group(1), t.group(1)
    logger.warning("无法解析多维表格 URL: %s", url)
    return None, None


def _to_datestr(s: str) -> str:
    """将 YYYY-MM-DD 或 YYYY/MM/DD 统一转为 YYYY/MM/DD 格式"""
    for sep in ("-", "/"):
        parts = s.split(sep)
        if len(parts) == 3:
            return f"{parts[0]}/{parts[1]:0>2s}/{parts[2]:0>2s}"
    return s


def _to_timestamp(date_str: str) -> int:
    """将 YYYY/MM/DD 转为毫秒时间戳（飞书日期字段需要）"""
    parts = date_str.split("/")
    if len(parts) == 3:
        from datetime import datetime
        dt = datetime(int(parts[0]), int(parts[1]), int(parts[2]))
        return int(dt.timestamp() * 1000)
    return 0


def write_task_to_bitable(
    feishu_url: str,
    title: str,
    estimated_hours: float,
    plan_start: str,
    plan_end: str,
    assignee_open_id: str,
    task_id: str,
    assignee_uid: str = "",
    trinity_project_id: str = "",
    trinity_project_name: str = "",
) -> str:
    """
    将已创建的 Trinity 任务写入飞书多维表格，返回错误信息或空字符串（成功）。

    列映射（实际字段名带 * 前缀）：
      *Title              → 任务名
      *InitialEstimate(h) → 预估工时（一位小数）
      *PlanStartDate      → 计划开始（日期类型，毫秒时间戳）
      *PlanEndDate        → 计划结束（日期类型，毫秒时间戳）
      *Assignee           → 人员（优先 open_id，回退 user_id）
      TaskID              → Trinity 任务链接（URL 类型）
    """
    if not feishu_url:
        return ""

    app_token, table_id = _parse_bitable_url(feishu_url)
    if not app_token or not table_id:
        return f"feishu_url 格式错误: {feishu_url}"

    token = _get_feishu_token()
    if not token:
        return "获取飞书 token 失败"

    # Assignee：使用 open_id（id 字段格式）
    assignee_field = []
    if assignee_open_id:
        assignee_field = [{"id": assignee_open_id}]

    # TaskID 是 URL 类型字段，带上 projectId 和 projectName 确保链接触达
    task_url = ""
    if task_id and trinity_project_id:
        task_url = {
            "link": f"https://trinity.desaysv.com/#/task/taskDetail?id={task_id}&projectId={trinity_project_id}&projectName={trinity_project_name}",
            "text": task_id,
        }

    # 日期转为毫秒时间戳
    start_ts = _to_timestamp(_to_datestr(plan_start)) if plan_start else 0
    end_ts = _to_timestamp(_to_datestr(plan_end)) if plan_end else 0

    fields = {
        "*Title": title,
        "*InitialEstimate(h)": round(float(estimated_hours), 1) if estimated_hours else 0.0,
        "*PlanStartDate": start_ts,
        "*PlanEndDate": end_ts,
        "*Assignee": assignee_field,
        "TaskID": task_url,
    }

    # 移除空值字段
    fields = {k: v for k, v in fields.items() if v or k in ("*InitialEstimate(h)",) or v == 0}

    try:
        resp = requests.post(
            f"https://open.feishu.cn/open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/records",
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            json={"fields": fields},
            timeout=15,
        )
        data = resp.json()
        if data.get("code") == 0:
            logger.info("多维表格写入成功: title=%s task_id=%s", title, task_id)
            return ""
        else:
            msg = f"多维表格写入失败: code={data.get('code')} msg={data.get('msg', '')}"
            logger.error("%s fields=%s", msg, json.dumps(fields, ensure_ascii=False)[:500])
            return msg
    except Exception as e:
        msg = f"多维表格写入异常: {e}"
        logger.exception(msg)
        return msg
