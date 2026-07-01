"""
飞书指派通知 — 任务创建后向指派人发送飞书私信通知。

用法（被 feishu_ws_bot.py 调用）：
  from feishu_notify_assignee import notify_assignee
  notify_assignee("ou_xxx", "任务名", 40.0, "2026/06/20", "2026/06/25", "TASK2026...", "PMD...", "项目名")
"""
import json
import logging
import time
from datetime import datetime

import requests

logger = logging.getLogger(__name__)

# 飞书 API 凭据（与 feishu_ws_bot.py 保持一致）
FEISHU_APP_ID = "cli_a9451285c0b81bc9"
FEISHU_APP_SECRET = "eDgs2IhuO9IW9N7gmU9bBgFF6acx12aN"
_token_cache = {"token": "", "expire_at": 0}


def _get_feishu_token() -> str:
    if time.time() < _token_cache["expire_at"] - 60:
        return _token_cache["token"]
    try:
        resp = requests.post(
            "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
            json={"app_id": FEISHU_APP_ID, "app_secret": FEISHU_APP_SECRET},
            timeout=15, verify=False,
        )
        data = resp.json()
        _token_cache["token"] = data["tenant_access_token"]
        _token_cache["expire_at"] = time.time() + data["expire"]
        return _token_cache["token"]
    except Exception as e:
        logger.exception("获取飞书 token 失败: %s", e)
        return ""


def _build_card(title: str, hours: float, start_date: str,
                end_date: str, project_name: str, task_id: str,
                project_id: str) -> dict:
    """构造飞书卡片消息"""
    task_url = (f"https://trinity.desaysv.com/#/task/taskDetail"
                f"?id={task_id}&projectId={project_id}&projectName={project_name}")

    elements = []

    # 任务名
    elements.append({
        "tag": "div",
        "text": {"tag": "lark_md", "content": f"**任务**：{title}"},
    })

    # 项目
    if project_name:
        elements.append({
            "tag": "div",
            "text": {"tag": "lark_md", "content": f"**项目**：{project_name}"},
        })

    # 工时 + 日期 放在同一行两列
    fields = []
    if hours:
        fields.append({"is_short": True, "text": {"tag": "lark_md", "content": f"**工时**\n{hours}h"}})
    if start_date:
        date_text = f"{start_date} ~ {end_date}" if end_date else start_date
        fields.append({"is_short": True, "text": {"tag": "lark_md", "content": f"**计划**\n{date_text}"}})
    if fields:
        elements.append({"tag": "div", "fields": fields})

    # 分隔线
    elements.append({"tag": "hr"})

    # 查看详情按钮
    elements.append({
        "tag": "action",
        "actions": [{
            "tag": "button",
            "text": {"tag": "plain_text", "content": "查看详情"},
            "type": "link",
            "url": task_url,
            "value": {},
        }],
    })

    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": f"新任务已创建：{title}"},
            "template": "blue",
        },
        "elements": elements,
    }


def _to_datestr(s: str) -> str:
    """统一日期格式 YYYY/MM/DD"""
    for sep in ("-", "/"):
        parts = s.split(sep)
        if len(parts) == 3:
            return f"{parts[0]}/{parts[1]:0>2s}/{parts[2]:0>2s}"
    return s


def notify_assignee(open_id: str, title: str, estimated_hours: float,
                    plan_start: str, plan_end: str, task_id: str,
                    project_id: str = "", project_name: str = "",
                    assignee_uid: str = "") -> str:
    """
    向指派人发送飞书私信通知，返回错误信息或空字符串（成功）。

    参数：
      open_id: 指派人飞书 open_id（ou_xxx），为空时用 assignee_uid 发
      title: 任务名
      estimated_hours: 预估工时
      plan_start: 计划开始日期
      plan_end: 计划结束日期
      task_id: Trinity 任务 ID
      project_id: Trinity 项目 ID（用于生成链接）
      project_name: 项目名（用于生成链接）
      assignee_uid: Trinity UID（=飞书 user_id），open_id 为空时回退用
    """
    if not open_id and not assignee_uid:
        return ""

    token = _get_feishu_token()
    if not token:
        return "获取飞书 token 失败"

    card = _build_card(
        title, estimated_hours,
        _to_datestr(plan_start) if plan_start else "",
        _to_datestr(plan_end) if plan_end else "",
        project_name or "",
        task_id or "",
        project_id or "",
    )

    # 优先 open_id，回退 user_id
    receiver_id = open_id or assignee_uid
    id_type = "open_id" if open_id else "user_id"

    try:
        resp = requests.post(
            f"https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type={id_type}",
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            json={
                "receive_id": receiver_id,
                "msg_type": "interactive",
                "content": json.dumps(card, ensure_ascii=False),
            },
            timeout=15, verify=False,
        )
        data = resp.json()
        if data.get("code") == 0:
            logger.info("指派通知发送成功: to=%s title=%s", (open_id or assignee_uid)[:20], title)
            return ""
        else:
            msg = f"指派通知发送失败: code={data.get('code')} msg={data.get('msg', '')}"
            logger.error(msg)
            return msg
    except Exception as e:
        msg = f"指派通知发送异常: {e}"
        logger.exception(msg)
        return msg
