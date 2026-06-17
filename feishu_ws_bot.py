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
from typing import Optional
from threading import Lock

import requests
import urllib3
urllib3.disable_warnings()
from requests.adapters import HTTPAdapter

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    stream=sys.stdout,
    force=True,
)
logger = logging.getLogger(__name__)

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


# ========== 消息队列（WS 线程收 -> worker 按序处理）==========
_message_queue: queue.Queue = queue.Queue()
_stop_worker = threading.Event()
MESSAGE_TTL = 300      # 消息队列最大等待时间（秒），超时丢弃
MESSAGE_TIMEOUT = 150  # 单条消息最大处理时间（秒），超时跳过
_worker_executor = None  # ThreadPoolExecutor，在 main 中初始化


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
    from project_matcher import find_project, load_projects

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
                from project_matcher import find_project
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


def _process_one_message(text: str, chat_id: str, open_id: str):
    """实际处理单条消息：项目匹配 → 调妙搭 → 创建任务 → 回复"""
    logger.info("开始处理消息 from=%s: %s", open_id, text[:80])

    # 解析项目缩写
    miaoda_text, project_cfg, hint = _resolve_project(text)
    if hint and not project_cfg:
        reply_feishu(chat_id, hint)
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

            def on_created(title, hours, start_date, end_date, assignee_oid, task_id, assignee_uid=""):
                # 多维表格写入（如有配置）
                if feishu_url:
                    try:
                        from bitable_writer import write_task_to_bitable
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
                    except Exception as e:
                        logger.warning("多维表格写入异常: %s", e)

                # 指派通知（优先 open_id，回退 uid）
                if assignee_oid or assignee_uid:
                    try:
                        from notify_assignee import notify_assignee
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
                    except Exception as e:
                        logger.warning("指派通知异常: %s", e)

            try:
                from miaoda_task_handler import process_miaoda_tasks
                result = process_miaoda_tasks(
                    tasks,
                    project_id=pid,
                    creator_name=creator,
                    parent_task=ptask,
                    project_name=project_name,
                    on_task_created=on_created,
                )
                full_reply = f"项目: {project_name}\n{reply}\n\n---\n{result}"
            except Exception as e:
                logger.exception("任务创建异常: %s", e)
                full_reply = f"项目: {project_name}\n{reply}\n\n---\n任务创建异常: {e}"
            reply_feishu(chat_id, full_reply)
    else:
        reply_feishu(chat_id, reply)


def queue_worker():
    """单 worker 逐条出队，每条消息独立提交到线程池 + 超时保护。

    妙搭接口偶发卡死时不阻塞整个队列，超时后自动跳过继续处理下一条。
    """
    from concurrent.futures import TimeoutError

    while not _stop_worker.is_set():
        try:
            text, chat_id, open_id, enqueue_time = _message_queue.get(timeout=1)
        except (queue.Empty, ValueError):
            try:
                text, chat_id, open_id, enqueue_time = _message_queue.get(timeout=1)
            except (queue.Empty, ValueError):
                continue

        # 超时检查：消息在队列里等太久 → 丢弃（用户很可能已经重发了）
        age = time.time() - enqueue_time
        if enqueue_time > 0 and age > MESSAGE_TTL:
            logger.warning("消息超时丢弃: age=%.0fs text=%s…", age, text[:40])
            continue

        logger.info("出队消息 age=%.0fs text=%s…", age, text[:40])

        # 提交到线程池执行，带超时保护
        fut = _worker_executor.submit(_process_one_message, text, chat_id, open_id)
        try:
            fut.result(timeout=MESSAGE_TIMEOUT)
        except TimeoutError:
            logger.error("消息处理超时(>%ds): %s…", MESSAGE_TIMEOUT, text[:40])
            # 超时不取消，让它在后台跑完；继续处理下一条


def handle_message(data) -> None:
    """WS 线程中调用：提取消息入队列，不阻塞"""
    global _last_msg_time
    try:
        event = getattr(data, "event", None)
        if not event or not event.message:
            return

        chat_id = event.message.chat_id
        sender_id = event.sender.sender_id
        open_id = sender_id.open_id or sender_id.user_id or ""

        content = event.message.content or "{}"
        try:
            content_json = json.loads(content)
            user_text = content_json.get("text", "")
        except json.JSONDecodeError:
            user_text = content

        # 解析 @ 提及（独立 try/except，不影响消息入队列）
        try:
            mentions = getattr(event.message, "mentions", None)
            if mentions:
                for m in mentions:
                    # Lark-oAPI MentionEvent 对象，用属性访问而非 dict
                    key = getattr(m, 'key', '')
                    name = getattr(m, 'name', '')
                    mid = getattr(m, 'id', None)
                    oid = getattr(mid, 'open_id', '') if mid else ''
                    if key and (name or oid):
                        # 替换格式 "姓名(@open_id)"，妙搭能读姓名，handler 也能解析 @open_id
                        replacement = f"{name}(@{oid})" if name else f"@{oid}"
                        user_text = user_text.replace(f"@{key}", replacement)
                        user_text = user_text.replace(key, replacement)
                        logger.info("提及替换: %s -> %s (oid=%s)", key, replacement, oid)
        except Exception as e:
            logger.warning("提及替换异常（不影响消息处理）: %s", e)

        # 先打日志（不阻塞事件循环）
        logger.info("收到消息 from=%s chat=%s text=%s", open_id, chat_id, user_text[:100])

        # 记录最后消息时间（用于 WS 超时重连判断）
        _last_msg_time = time.time()

        # 入队列（带时间戳，worker 超时检查用）
        _message_queue.put((user_text, chat_id, open_id, time.time()))

        # 在线程里：取姓名 → 打日志 → 回复"处理中"
        threading.Thread(target=_reply_busy_and_log, args=(chat_id, open_id), daemon=True).start()
    except Exception as e:
        logger.exception("handle_message 异常: %s", e)


def _reply_busy_and_log(chat_id: str, open_id: str):
    """在线程中获取姓名 → 打日志 → 回复"处理中"（含姓名）"""
    try:
        name = get_user_name(open_id)
        logger.info("发送者身份: open_id=%s display=%s", open_id, name)
        qsize = _message_queue.qsize()
        busy_text = f"收到{name}的消息，当前队列还有 {qsize} 条消息待处理..."
        reply_feishu(chat_id, busy_text)
    except Exception as e:
        logger.exception("回复处理中异常: %s", e)


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
                try:
                    os.kill(old_pid, signal.SIGTERM)
                    logger.info("已结束旧进程 PID=%s", old_pid)
                    time.sleep(2)
                except (OSError, ProcessLookupError):
                    pass
        except Exception:
            pass


# ========== 主函数 ==========
def main():
    global _ws_thread, _worker_executor

    _cleanup_old_process()
    with open(PID_FILE, "w") as f:
        f.write(str(os.getpid()))

    logger.info("飞书机器人启动中... PID=%s", os.getpid())

    from concurrent.futures import ThreadPoolExecutor
    _worker_executor = ThreadPoolExecutor(max_workers=2)

    # 启动队列 worker
    worker = threading.Thread(target=queue_worker, daemon=True)
    worker.start()

    # 启动 WS 客户端（官方 SDK，自动重连+心跳）
    stop = threading.Event()
    _ws_thread = threading.Thread(target=run_ws, args=(stop,), daemon=True)
    _ws_thread.start()

    start_time = time.time()
    _hb_count = 0
    try:
        while not stop.is_set():
            time.sleep(1)
            _hb_count += 1
            if _hb_count % 30 == 0:
                logger.info("心跳: 运行中... (%.0fs) 队列:%d", time.time() - start_time, _message_queue.qsize())
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
