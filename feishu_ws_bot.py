"""
飞书机器人 — 原始 WebSocket + protobuf + 妙搭 OpenAPI

架构：
  - WS 线程：纯接收+ACK，立即入队，不阻塞
  - 队列 + 单 Worker：逐条顺序处理妙搭和回复
  - ACK 先于处理 + biz_rt 头 + 异步分发
  - 45 秒无消息超时自动重连，绕过服务器单连接事件配额
  - 收到消息立即回复"处理中+队列数"，提升用户体验
  - 连上立即发 PING，消除首次消息延迟
"""
import os
import json
import sys
import time
import signal
import logging
import threading
import queue
import hashlib
from typing import Optional
from threading import Lock
from datetime import datetime

import requests
import urllib3
urllib3.disable_warnings()
from requests.adapters import HTTPAdapter

LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
os.makedirs(LOG_DIR, exist_ok=True)

# 主业务日志
_log_file = os.path.join(LOG_DIR, "bot.log")
_file_handler = logging.FileHandler(_log_file, encoding="utf-8")
_file_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))

# 心跳专用日志
_hb_file = os.path.join(LOG_DIR, "heartbeat.log")
_hb_handler = logging.FileHandler(_hb_file, encoding="utf-8")
_hb_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))

_stream_handler = logging.StreamHandler(sys.stdout)
_stream_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))

logging.basicConfig(level=logging.INFO, handlers=[_file_handler, _stream_handler], force=True)
logger = logging.getLogger(__name__)

_hb_logger = logging.getLogger("heartbeat")
_hb_logger.setLevel(logging.INFO)
_hb_logger.addHandler(_hb_handler)
_hb_logger.addHandler(_stream_handler)
_hb_logger.propagate = False  # 不传到 root，避免重复

# 任务累计计数器日志（独立文件）
_task_log_file = os.path.join(LOG_DIR, "task_counter.log")
_task_handler = logging.FileHandler(_task_log_file, encoding="utf-8")
_task_handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
_task_logger = logging.getLogger("task_counter")
_task_logger.setLevel(logging.INFO)
_task_logger.addHandler(_task_handler)
_task_logger.propagate = False

# 任务累计计数器文件
TASK_COUNTER_FILE = os.path.join(os.path.join(os.path.dirname(os.path.abspath(__file__)), "config"), "task_counter.json")
_task_total = 0

def _load_task_counter() -> int:
    """从文件加载累计任务数"""
    try:
        if os.path.exists(TASK_COUNTER_FILE):
            with open(TASK_COUNTER_FILE, "r", encoding="utf-8") as f:
                return json.load(f).get("total", 0)
    except Exception as e:
        logger.warning("读取任务计数器失败: %s", e)
    return 0

def _save_task_counter(total: int):
    """持久化累计任务数"""
    try:
        os.makedirs(os.path.dirname(TASK_COUNTER_FILE), exist_ok=True)
        with open(TASK_COUNTER_FILE, "w", encoding="utf-8") as f:
            json.dump({"total": total}, f)
    except Exception as e:
        logger.warning("写入任务计数器失败: %s", e)

APP_ID = "cli_a9451285c0b81bc9"
APP_SECRET = "eDgs2IhuO9IW9N7gmU9bBgFF6acx12aN"
MIAODA_BASE = "https://yesv-desaysv.aiforce.cloud/app/app_4kcujad3rhddm"
MIAODA_API_KEY = "bnCIOhfcXSiG7SpW8Ys8_zVQhxdDj6kNB1GWI0aLhN4"

# ========== Trinity 任务创建配置（供 handler 使用）==========
TRINITY_ENABLED = True         # 开关：打开后创建 Trinity 任务
TRINITY_PROJECT_ID = "APP2026032710150663335"  # 默认项目（YE6）
TRINITY_PARENT_TASK = "TASK20260420_20241"     # 默认上级任务
TRINITY_CREATOR_NAME = "孙猛"                   # 默认创建人

# 线程安全配置
_token_lock = Lock()
_token_cache = {"token": "", "expire_at": 0}


class InsecureAdapter(HTTPAdapter):
    def send(self, request, **kwargs):
        kwargs["verify"] = False
        return super().send(request, **kwargs)


_insecure_session = requests.Session()
_insecure_session.mount("https://", InsecureAdapter())


def get_token() -> str:
    with _token_lock:
        if time.time() < _token_cache["expire_at"] - 60:
            return _token_cache["token"]
        resp = _insecure_session.post(
            "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
            json={"app_id": APP_ID, "app_secret": APP_SECRET},
            timeout=15,
        )
        data = resp.json()
        _token_cache["token"] = data["tenant_access_token"]
        _token_cache["expire_at"] = time.time() + data["expire"]
        return _token_cache["token"]


# 用户姓名缓存，避免重复调用 API
_name_cache = {}
_name_cache_lock = Lock()


def get_user_name(open_id: str) -> str:
    """根据 open_id 获取用户姓名（缓存 1 小时），失败时返回 open_id 末4位"""
    if not open_id:
        return "unknown"
    with _name_cache_lock:
        if open_id in _name_cache:
            if time.time() < _name_cache[open_id]["expire_at"]:
                return _name_cache[open_id]["name"]
    try:
        token = get_token()
        resp = _insecure_session.get(
            f"https://open.feishu.cn/open-apis/contact/v3/users/{open_id}",
            headers={"Authorization": f"Bearer {token}"},
            timeout=10,
        )
        data = resp.json()
        logger.info("飞书联系人API响应: open_id=%s %s", open_id, json.dumps(data, ensure_ascii=False)[:500])
        if data.get("code") == 0:
            name = data.get("data", {}).get("user", {}).get("name", "")
        else:
            name = ""
            logger.warning("飞书联系人API返回错误: open_id=%s code=%s",
                          open_id, data.get("code"))
    except Exception as e:
        name = ""
        logger.warning("飞书联系人API异常: open_id=%s err=%s", open_id, e)
    if not name:
        # API没返回name时用union_id后缀
        union_id = ""
        try:
            union_id = data.get("data", {}).get("user", {}).get("union_id", "")
        except Exception:
            pass
        if union_id:
            name = f"usr_{union_id[-6:]}"
        else:
            name = f"usr_{open_id[-4:]}"
        logger.info("姓名解析: %s -> %s (API未返回name)", open_id, name)
    with _name_cache_lock:
        _name_cache[open_id] = {"name": name, "expire_at": time.time() + 3600}
    return name


def call_miaoda(message: str, sender_id: str) -> tuple:
    """返回 (reply_text, tasks_list)"""
    import re
    original = message
    # 归一化空格，避免多余空格干扰妙搭 NLP 解析
    message = re.sub(r'\s+', ' ', message).strip()
    # 去掉残留的 @ 符号（@中文 会干扰妙搭 NLP 解析）
    message = re.sub(r'@(\S+)', r'\1', message)
    logger.info("调用妙搭: message=%s sender=%s", message[:200], sender_id[:20])
    try:
        resp = _insecure_session.post(
            f"{MIAODA_BASE}/openapi/chat",
            json={"message": message, "senderId": sender_id},
            headers={"Authorization": f"Bearer {MIAODA_API_KEY}"},
            timeout=60,
        )
        try:
            data = resp.json()
        except Exception:
            logger.error("妙搭返回非 JSON: status=%s body=%s", resp.status_code, resp.text[:200])
            return f"（妙搭接口异常: HTTP {resp.status_code}）", []
        logger.info("妙搭响应: %s", json.dumps(data, ensure_ascii=False)[:500])
        reply = data.get("reply", "（无回复）")
        tasks = data.get("tasks", [])
        return reply, tasks
    except Exception as e:
        logger.exception("调用妙搭异常: %s", e)
        return "（妙搭服务调用超时，请稍后再试）", []


def reply_feishu(chat_id: str, text: str):
    try:
        token = get_token()
        resp = _insecure_session.post(
            "https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=chat_id",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "receive_id": chat_id,
                "msg_type": "text",
                "content": json.dumps({"text": text}, ensure_ascii=False),
            },
            timeout=15,
        )
        result = resp.json()
        if result.get("code") != 0:
            logger.error("回复消息失败: %s", json.dumps(result, ensure_ascii=False))
        else:
            logger.info("回复成功: %s...", text[:50])
    except Exception as e:
        logger.exception("回复飞书消息异常: %s", e)


import re as _re


def _text_to_post_content(text: str) -> list:
    """将带 **bold** 标记的文本转为飞书 post 格式内容数组"""
    lines = text.split("\n")
    content = []
    for line in lines:
        elements = []
        parts = _re.split(r'(\*\*.*?\*\*)', line)
        for part in parts:
            if part.startswith('**') and part.endswith('**'):
                elements.append({"tag": "text", "text": part[2:-2], "style": ["bold"]})
            else:
                elements.append({"tag": "text", "text": part})
        content.append(elements)
    return content


def reply_feishu_post(chat_id: str, text: str):
    """以富文本消息（msg_type: post）发送，支持 **bold** 标记"""
    try:
        token = get_token()
        content = _text_to_post_content(text)
        resp = _insecure_session.post(
            "https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=chat_id",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "receive_id": chat_id,
                "msg_type": "post",
                "content": json.dumps({"zh_cn": {"content": content}}, ensure_ascii=False),
            },
            timeout=15,
        )
        result = resp.json()
        if result.get("code") != 0:
            logger.error("回复文本失败: %s", json.dumps(result, ensure_ascii=False))
        else:
            logger.info("回复文本成功: %s...", text[:50])
    except Exception as e:
        logger.exception("回复飞书文本异常: %s", e)


# ========== 消息队列（WS 线程收 -> worker 按序处理）==========
_message_queue: queue.Queue = queue.Queue()
_stop_worker = threading.Event()
MESSAGE_TTL = 300      # 消息队列最大等待时间（秒），超时丢弃
MESSAGE_TIMEOUT = 150  # 单条消息最大处理时间（秒），超时跳过
_worker_executor = None  # ThreadPoolExecutor，在 main 中初始化

# 消息去重 + WS 重播防护（持久化到磁盘，进程重启不丢失）
_processed_msg_ids = set()
_processed_msg_ids_lock = Lock()
_ws_ready_time = 0.0  # WS 就绪时间，用于过滤重播旧消息
MSG_ID_DEDUP_TTL = 600  # 10 分钟清理一次

# 内容去重：WS 重播会分配新 msg_id，但内容相同
# {(chat_id, content_hash): timestamp} 60 秒内相同内容跳过
_recent_content = {}
_recent_content_lock = Lock()
CONTENT_DEDUP_TTL = 60

# 持久化去重缓存文件（跨进程重启）
MSG_DEDUP_FILE = os.path.join(os.path.join(os.path.dirname(os.path.abspath(__file__)), "config"), "msg_dedup.json")

def _load_dedup_cache() -> set:
    try:
        if os.path.exists(MSG_DEDUP_FILE):
            with open(MSG_DEDUP_FILE, "r", encoding="utf-8") as f:
                return set(json.load(f))
    except Exception as e:
        logger.warning("读取去重缓存失败: %s", e)
    return set()

def _save_dedup_cache(ids: set):
    try:
        os.makedirs(os.path.dirname(MSG_DEDUP_FILE), exist_ok=True)
        with open(MSG_DEDUP_FILE, "w", encoding="utf-8") as f:
            json.dump(list(ids), f, ensure_ascii=False)
    except Exception as e:
        logger.warning("写入去重缓存失败: %s", e)

# 格式引导（项目未匹配 / 妙搭无结果时提示用户）
_FORMAT_GUIDE = (
    "**━━━━━━━━请以如下格式给我发送消息🔈━━━━━━━━**\n"
    "**项目缩写**\n"
    "**任务描述** | **指派人:** **姓名/@姓名** | **工时** | **时间**\n\n"
    "**举例如下：**\n"
    "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
    "YE6\n"
    "矩阵更新5----房汉柠 5D 6/23提交\n"
    "白盒测试5----@蔡波  5D 6/20---6/25\n"
    "AVM适配5    @孙猛  5D 6/24完成\n"
    "RSPA横展5    杜雪莲 2天 下周五\n"
    "哨兵横展5    @房汉柠  16h 6/20~6/25\n"
    "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
    "**❗项目缩写必须准确，其他没有严格格式与顺序要求，支持模糊匹配**"
)


def _resolve_project(text: str) -> tuple:
    """
    从消息开头提取项目缩写，返回 (cleaned_text, project_config, hint_text)
      - cleaned_text: 去掉项目缩写前缀后的消息
      - project_config: 匹配到的项目配置 dict，None 表示未匹配
      - hint_text: 引导文字（模糊匹配时用）

    匹配策略：
      1. 遍历已知缩写（按长度降序），检查消息是否以该缩写开头
      2. 若没有精确匹配，用第一个空格分词去模糊匹配
    """
    from trinity_project_matcher import find_project, load_projects

    text = text.strip()
    if not text:
        return text, None, ""

    # 1. 按已知缩写前缀精确匹配（长缩写优先，避免 "A02" 误匹配 "A02Y" 开头）
    all_projects = load_projects()
    sorted_abbrs = sorted(set(p["abbr"] for p in all_projects), key=lambda x: (-len(x), x))
    for abbr in sorted_abbrs:
        if text.startswith(abbr):
            # 检查后面紧跟的是空格、标点或结尾（避免 "A02" 匹配到 "A02Y创建"）
            rest = text[len(abbr):]
            # 后面紧跟非字母数字（中文、空格、标点）或结尾 → 确认匹配
            if not rest or not rest[0].isascii() or not rest[0].isalnum():
                from trinity_project_matcher import find_project
                result = find_project(abbr)
                if result["found"]:
                    cleaned = rest.strip()
                    return cleaned or text, result["found"], ""

    # 2. 精确前缀没匹配到，用第一个词模糊匹配
    first_word = text.split()[0] if text.strip() else ""
    if not first_word:
        return text, None, ""

    result = find_project(first_word)
    if result["found"]:
        cleaned = text[len(first_word):].strip()
        return cleaned or text, result["found"], ""

    if result["suggestions"]:
        # 有建议但不确定
        return text, None, result["text"]

    # 完全没匹配到 → 提示用户指定项目
    all_abbrs = sorted(set(p["abbr"] for p in all_projects), key=lambda x: (len(x), x))
    abbr_list = "、".join(all_abbrs)
    return text, None, f"未识别到项目缩写，请在消息开头加上项目缩写，例如「A66-T 创建任务...」。\n可用缩写：{abbr_list}"


def _process_one_message(text: str, chat_id: str, open_id: str, mention_map: dict = None):
    """实际处理单条消息：项目匹配 → 调妙搭 → 创建任务 → 回复"""
    logger.info("开始处理消息 from=%s: %s", open_id, text[:80])

    # 解析项目缩写
    miaoda_text, project_cfg, hint = _resolve_project(text)
    if hint and not project_cfg:
        reply_feishu_post(chat_id, f"{hint}\n\n{_FORMAT_GUIDE}")
        return

    # SPM 权限校验：只有项目配置中的 SPM 才能创建任务
    if project_cfg:
        spm_name = (project_cfg.get("spm") or "").strip()
        if spm_name:
            sender_name = get_user_name(open_id).strip()
            # 跳过：获取姓名失败时的 fallback（usr_xxx / unknown），不误拦
            if sender_name.startswith("usr_") or sender_name == "unknown":
                logger.info("SPM 校验跳过（无法获取发送者姓名）: open_id=%s", open_id)
            elif sender_name != spm_name:
                logger.info("SPM 校验不通过: sender=%s spm=%s project=%s",
                            sender_name, spm_name, project_cfg.get("abbr", ""))
                reply_feishu(chat_id, f"只有项目负责人（{spm_name}）才能创建任务，你不是该项目的负责人。")
                return

    reply, tasks = call_miaoda(miaoda_text, open_id)
    logger.info("妙搭回复: %s... tasks=%s", reply[:80], json.dumps(tasks, ensure_ascii=False)[:300])

    if tasks:
        if not TRINITY_ENABLED:
            logger.info("Trinity 任务创建已关闭，仅回复妙搭文本")
            reply_feishu(chat_id, reply)
        elif not project_cfg:
            logger.warning("未匹配到项目，跳过任务创建")
            reply_feishu(chat_id, f"{reply}\n\n---\n⚠️ 未识别到项目缩写，请在消息开头指定项目（如「A66-T 创建任务...」）")
        else:
            pid = project_cfg["projectId"]
            ptask = project_cfg.get("parentTask") or TRINITY_PARENT_TASK
            creator = project_cfg.get("spm") or TRINITY_CREATOR_NAME
            project_name = project_cfg.get("name", f"项目 {pid}")

            logger.info("妙搭返回 %d 个任务 -> 项目: %s spm=%s", len(tasks), project_name, creator)

            # 多维表格回调 + 指派通知
            feishu_url = project_cfg.get("feishu_url", "") or ""
            _task_warnings = []  # 收集位表/通知错误，追加到回复

            def on_created(title, hours, start_date, end_date, assignee_oid, task_id, assignee_uid=""):
                # 多维表格写入（如有配置）
                if feishu_url:
                    try:
                        from feishu_bitable_writer import write_task_to_bitable
                        err = write_task_to_bitable(
                            feishu_url, title, hours,
                            start_date, end_date,
                            assignee_oid, task_id,
                            assignee_uid=assignee_uid,
                            trinity_project_id=pid,
                            trinity_project_name=project_name,
                        )
                        if err:
                            logger.warning("多维表格写入警告: %s", err)
                            _task_warnings.append(f"⚠️ 多维表格写入失败: {err}")
                    except Exception as e:
                        logger.warning("多维表格写入异常: %s", e)
                        _task_warnings.append(f"⚠️ 多维表格写入异常: {e}")

                # 指派通知（优先 open_id，回退 uid）
                if assignee_oid or assignee_uid:
                    try:
                        from feishu_notify_assignee import notify_assignee
                        err = notify_assignee(
                            assignee_oid, title, hours,
                            start_date, end_date,
                            task_id,
                            project_id=pid,
                            project_name=project_name,
                            assignee_uid=assignee_uid,
                        )
                        if err:
                            logger.warning("指派通知异常: %s", err)
                            _task_warnings.append(f"⚠️ 指派通知失败: {err}")
                    except Exception as e:
                        logger.warning("指派通知异常: %s", e)
                        _task_warnings.append(f"⚠️ 指派通知异常: {e}")

            try:
                from trinity_miaoda_task_handler import process_miaoda_tasks
                result = process_miaoda_tasks(
                    tasks,
                    project_id=pid,
                    creator_name=creator,
                    parent_task=ptask,
                    project_name=project_name,
                    on_task_created=on_created,
                    mention_map=mention_map,
                )
                _ts = datetime.now().strftime("%H:%M")
                full_reply = f"项目: {project_name} [{_ts}]\n{reply}\n\n---\n{result}"

                # 追加位表/通知警告
                if _task_warnings:
                    full_reply += "\n\n" + "\n".join(_task_warnings)

                # 累计任务数：从 result 中提取本次成功数
                import re as _re2
                success_m = _re2.search(r'成功 (\d+)', result)
                fail_m = _re2.search(r'失败 (\d+)', result)
                success_count = int(success_m.group(1)) if success_m else 0
                fail_count = int(fail_m.group(1)) if fail_m else 0
                if success_count > 0:
                    global _task_total
                    _task_total += success_count
                    _save_task_counter(_task_total)
                    _task_logger.info("累计创建任务: %s (+%s)", _task_total, success_count)

                if success_count == 0 and fail_count > 0:
                    # 全部失败：不显示妙搭的"已提取并创建"误导文字
                    reply_feishu_post(chat_id, f"项目: {project_name} [{_ts}]\n❌ 任务创建失败:\n{result}")
                else:
                    reply_feishu_post(chat_id, full_reply)
            except Exception as e:
                logger.exception("任务创建异常: %s", e)
                # 异常时也不显示妙搭的"已创建"文字和格式引导
                _ts = datetime.now().strftime("%H:%M")
                reply_feishu_post(chat_id, f"项目: {project_name} [{_ts}]\n❌ 任务创建异常: {e}")
    else:
        reply_feishu_post(chat_id, f"{reply}\n\n{_FORMAT_GUIDE}")


def queue_worker():
    """单 worker 逐条出队，每条消息独立提交到线程池 + 超时保护。

    妙搭接口偶发卡死时不阻塞整个队列，超时后自动跳过继续处理下一条。
    """
    from concurrent.futures import TimeoutError

    while not _stop_worker.is_set():
        mention_map = {}
        try:
            item = _message_queue.get(timeout=1)
            if len(item) == 5:
                text, chat_id, open_id, enqueue_time, mention_map = item
            else:
                text, chat_id, open_id, enqueue_time = item
        except (queue.Empty, ValueError):
            continue

        # 超时检查：消息在队列里等太久 → 丢弃（用户很可能已经重发了）
        age = time.time() - enqueue_time
        if enqueue_time > 0 and age > MESSAGE_TTL:
            logger.warning("消息超时丢弃: age=%.0fs text=%s…", age, text[:40])
            continue

        logger.info("出队消息 age=%.0fs text=%s… mention_map=%s", age, text[:40], mention_map)

        # 提交到线程池执行，带超时保护
        fut = _worker_executor.submit(_process_one_message, text, chat_id, open_id, mention_map)
        try:
            fut.result(timeout=MESSAGE_TIMEOUT)
        except TimeoutError:
            logger.error("消息处理超时(>%ds): %s…", MESSAGE_TIMEOUT, text[:40])
            # 超时不取消，让它在后台跑完；继续处理下一条


def handle_message(data) -> None:
    """WS 线程中调用：提取消息入队列，不阻塞"""
    global _last_msg_time, _ws_ready_time
    # WS 保护期：刚启动/重连时跳过旧消息重播
    if time.time() < _ws_ready_time:
        logger.info("WS 保护期(%.1fs)，跳过消息", _ws_ready_time - time.time())
        return
    try:
        event = getattr(data, "event", None)
        if not event or not event.message:
            return

        chat_id = event.message.chat_id
        sender_id = event.sender.sender_id
        open_id = sender_id.open_id or sender_id.user_id or ""

        content = event.message.content or "{}"
        logger.info("原始消息内容: %s", content[:300])

        # 消息去重：message_id 相同说明是 WS 重复投递
        msg_id = getattr(event.message, "message_id", "") or ""
        if msg_id:
            with _processed_msg_ids_lock:
                if msg_id in _processed_msg_ids:
                    logger.info("跳过重复消息: msg_id=%s text=%s", msg_id, content[:50])
                    return
                _processed_msg_ids.add(msg_id)
                if len(_processed_msg_ids) % 20 == 0:
                    _save_dedup_cache(_processed_msg_ids)
                logger.info("消息 msg_id=%s (去重缓存大小:%d)", msg_id, len(_processed_msg_ids))
        try:
            content_json = json.loads(content)
            user_text = content_json.get("text", "")
        except json.JSONDecodeError:
            user_text = content

        # 解析 @ 提及（独立 try/except，不影响消息入队列）
        _mention_map = {}  # {中文名: open_id}，传给 handler 优先使用
        try:
            mentions = getattr(event.message, "mentions", None)
            if mentions:
                for m in mentions:
                    key = getattr(m, 'key', '')
                    name = getattr(m, 'name', '')
                    mid = getattr(m, 'id', None)
                    oid = getattr(mid, 'open_id', '') if mid else ''
                    if key and (name or oid):
                        # 只替换为中文名（不加 open_id 后缀干扰妙搭 NLP），
                        # open_id 走 _mention_map 传递
                        replacement = name if name else f"@{oid}"
                        if name and oid:
                            _mention_map[name] = oid
                        user_text = user_text.replace(f"@{key}", replacement)
                        user_text = user_text.replace(key, replacement)
                        logger.info("提及替换: %s -> %s (oid=%s)", key, replacement, oid)
        except Exception as e:
            logger.warning("提及替换异常（不影响消息处理）: %s", e)

        # 先打日志（不阻塞事件循环）
        logger.info("收到消息 from=%s chat=%s text=%s", open_id, chat_id, user_text[:100])

        # 记录最后消息时间（用于 WS 超时重连判断）
        _last_msg_time = time.time()

        # 入队列（带时间戳 + mention 映射，worker 超时检查用）
        _message_queue.put((user_text, chat_id, open_id, time.time(), _mention_map))

        # 在线程里：取姓名 → 打日志 → 回复"处理中"
        threading.Thread(target=_reply_busy_and_log, args=(chat_id, open_id), daemon=True).start()
    except Exception as e:
        logger.exception("handle_message 异常: %s", e)


def _reply_busy_and_log(chat_id: str, open_id: str):
    """立即回复"处理中"，不等待飞书联系人 API（异步获取姓名打日志）"""
    try:
        qsize = _message_queue.qsize()
        _ts = datetime.now().strftime("%H:%M")
        busy_text = f"[{_ts}] 收到消息，当前队列还有 {qsize} 条消息待处理..."
        reply_feishu(chat_id, busy_text)
        # 异步获取姓名只用于日志，不阻塞回复
        name = get_user_name(open_id)
        logger.info("发送者身份: open_id=%s display=%s", open_id, name)
    except Exception as e:
        logger.warning("回复处理中异常: %s", e)


# ========== WS 线程（使用官方 SDK Client）==========
_ws_client: Optional["Client"] = None
_ws_thread: Optional[threading.Thread] = None


def run_ws(stop: threading.Event):
    """在独立线程中运行官方 WS Client（阻塞直到连接断开或进程退出）"""
    global _ws_client

    from lark_oapi import EventDispatcherHandler
    from lark_oapi.ws import Client

    handler = (EventDispatcherHandler.builder("", "")
               .register_p2_im_message_receive_v1(handle_message)
               .register_p2_im_chat_access_event_bot_p2p_chat_entered_v1(lambda d: None)
               .register_p2_im_chat_member_bot_added_v1(lambda d: None)
               .register_p2_im_chat_member_bot_deleted_v1(lambda d: None)
               .register_p2_im_message_message_read_v1(lambda d: None)
               .register_p2_im_message_reaction_created_v1(lambda d: None)
               .register_p2_im_message_reaction_deleted_v1(lambda d: None)
               .build())

    client = Client(APP_ID, APP_SECRET, event_handler=handler)
    _ws_client = client

    logger.info("WS 客户端线程启动...")
    try:
        client.start()  # 阻塞，内部处理重连和心跳
    except Exception as e:
        logger.exception("WS 客户端异常: %s", e)
    finally:
        logger.info("WS 客户端线程已停止")
        stop.set()


# ========== PID 管理 ==========
PID_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".bot_pid")


def _cleanup_old_process():
    if os.path.exists(PID_FILE):
        try:
            with open(PID_FILE) as f:
                old_pid = int(f.read().strip())
            if old_pid != os.getpid():
                import subprocess
                try:
                    # Windows 用 taskkill /F 确保进程真正终止
                    subprocess.run(["taskkill", "/F", "/PID", str(old_pid)],
                                   capture_output=True, timeout=5)
                    logger.info("已强制终止旧进程 PID=%s", old_pid)
                    time.sleep(2)
                except Exception:
                    pass
        except Exception:
            pass


# ========== 主函数 ==========
def main():
    global _ws_thread, _worker_executor, _last_msg_time, _ws_ready_time

    _cleanup_old_process()
    with open(PID_FILE, "w") as f:
        f.write(str(os.getpid()))

    global _task_total, _processed_msg_ids
    _task_total = _load_task_counter()
    with _processed_msg_ids_lock:
        _processed_msg_ids = _load_dedup_cache()
    logger.info("飞书机器人启动中... PID=%s | 累计创建任务: %s | 加载去重缓存: %d",
                os.getpid(), _task_total, len(_processed_msg_ids))
    _task_logger.info("飞书机器人启动中... PID=%s | 累计创建任务: %s", os.getpid(), _task_total)

    from concurrent.futures import ThreadPoolExecutor
    _worker_executor = ThreadPoolExecutor(max_workers=2)

    # 启动队列 worker
    worker = threading.Thread(target=queue_worker, daemon=True)
    worker.start()

    # 启动 WS 客户端（官方 SDK，自动重连+心跳）
    stop = threading.Event()
    _ws_thread = threading.Thread(target=run_ws, args=(stop,), daemon=True)
    _ws_thread.start()
    _ws_ready_time = time.time() + 8  # WS 启动 8 秒后才接受消息（过滤重播）

    start_time = time.time()
    _hb_count = 0
    _last_msg_time = time.time()  # 初始化，避免刚启动就触发检测
    try:
        while not stop.is_set():
            time.sleep(1)
            _hb_count += 1

            # WS 健康检测：超过 2 分钟无消息 → 杀死旧 WS 线程重建
            if _hb_count % 30 == 0:
                _hb_logger.info("心跳: 运行中... (%.0fs) 队列:%d", time.time() - start_time, _message_queue.qsize())
                if time.time() - _last_msg_time > 120 and _ws_thread and not _ws_thread.is_alive():
                    _hb_logger.warning("WS 线程已死，重启中...")
                    _ws_ready_time = time.time() + 8  # 重启保护期，过滤重播
                    stop = threading.Event()
                    _ws_thread = threading.Thread(target=run_ws, args=(stop,), daemon=True)
                    _ws_thread.start()

            # 每 10 分钟清理一次消息去重缓存
            if _hb_count % 600 == 0:
                with _processed_msg_ids_lock:
                    _processed_msg_ids.clear()
                    _save_dedup_cache(set())
                    _hb_logger.info("消息去重缓存已清理")
    except KeyboardInterrupt:
        logger.info("收到 Ctrl+C, 正在停止...")
    except SystemExit:
        logger.info("收到退出信号, 正在停止...")

    logger.info("正在停止...")
    _stop_worker.set()
    worker.join(timeout=5)
    _worker_executor.shutdown(wait=False)
    logger.info("机器人进程退出")
    try:
        if os.path.exists(PID_FILE):
            os.remove(PID_FILE)
    except Exception:
        pass


if __name__ == "__main__":
    main()
