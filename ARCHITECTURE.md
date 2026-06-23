# TaskCreator v3.4 — 架构与详细设计文档

## 1. 系统概述

TaskCreator 是一个飞书机器人服务，连接三个外部系统实现自动化任务创建流水线：

```
飞书消息 → 妙搭 AI（拆解任务）→ Trinity（创建任务到指定项目）→ 多维表格写入 + 指派通知
```

用户在飞书群聊/私聊发送需求描述（含项目缩写），机器人自动调用妙搭 AI 将需求拆解为结构化子任务，然后在 Trinity 项目管理系统中创建对应任务，最后将结果写入飞书多维表格并向指派人发送通知卡片。

---

## 2. 总体架构

### 2.1 架构分层

```
┌──────────────────────────────────────────────────────────────────────────────────┐
│                           飞书 IM 层 (Feishu Open API)                           │
│  WebSocket Gateway · IM 消息收发 · 联系人查询 · 多维表格 · 卡片消息              │
└──────────────────────────────────────────────────┬───────────────────────────────┘
                                                   │
┌──────────────────────────────────────────────────▼───────────────────────────────┐
│                           接入层 feishu_ws_bot.py                                │
│                                                                                  │
│  ┌─────────────────┐   ┌──────────────────┐   ┌──────────────────────────────┐  │
│  │ WS 线程          │   │ 消息去重层        │   │ 消息队列 Queue               │  │
│  │ 纯接收 + ACK     │ → │ msg_id 持久化    │ → │ FIFO + 5min TTL              │  │
│  │ @提及→姓名+map   │   │ 内容Hash 60s TTL │   │ 去耦 WS 与业务               │  │
│  │ 8s保护期过滤重播  │   │ 10min 定期清理   │   └──────────────────────────────┘  │
│  └─────────────────┘   └──────────────────┘                 │                    │
│                                                             ▼                    │
│  ┌─────────────────┐   ┌──────────────────┐   ┌──────────────────────────────┐  │
│  │ 任务累计计数器    │   │ WS 健康检测       │   │ ThreadPoolExecutor(2)        │  │
│  │ config/          │   │ 2min 无消息检测   │   │ 单条消息 150s 超时保护       │  │
│  │ task_counter.json│   │ WS 线程死→重建    │   │ _process_one_message()      │  │
│  └─────────────────┘   └──────────────────┘   └──────────────────────────────┘  │
└──────────────────────────────────────────────────────────────────────────────────┘
                                                   │
                          ┌────────────────────────┼────────────────────┐
                          │                        │                    │
                          ▼                        ▼                    ▼
              ┌────────────────────┐  ┌────────────────────┐  ┌──────────────────┐
              │ trinity_project_matcher.py │  │  妙搭 OpenAPI      │  │ on_task_created  │
              │ 项目缩写匹配        │  │  AI 任务拆解        │  │ 回调分发          │
              │ 精确+模糊+引导      │  │  /openapi/chat     │  │                   │
              └────────────────────┘  └────────────────────┘  │  ┌──────────────┐ │
                          │                                    │  │feishu_bitable_writer│ │
                          ▼                                    │  │ 多维表格写入  │ │
              ┌────────────────────┐                           │  └──────────────┘ │
              │ trinity_miaoda_task_handler│                           │  ┌──────────────┐ │
              │ 任务创建核心逻辑     │                           │  │notify_assign │ │
              │ · 人名/ID 解析      │                           │  │ 指派通知卡片  │ │
              │ · mention_map 传递  │                           │  └──────────────┘ │
              │ · UID→open_id 回退  │                           └──────────────────┘
              │ · Trinity API 调用  │
              │ · 日期兜底          │
              └────────────────────┘
                        │
                        ▼
              ┌────────────────────┐
              │  Trinity API       │
              │  创建/查询任务      │
              │  成员信息查询       │
              └────────────────────┘
```

### 2.2 核心架构决策

| 决策 | 选择 | 理由 |
|------|------|------|
| WS 与业务处理分离 | 消息队列 + 独立 Worker | WS 线程不阻塞，队列缓冲 + TTL 防积压 |
| 消息去重 | msg_id 持久化 + 内容 Hash 双保险 | WS 重播会分配新 msg_id 但内容相同，两重去重覆盖两种场景 |
| 并发模型 | ThreadPoolExecutor(max_workers=2) | 控制并发防止 Trinity 限流，超时保护防卡死 |
| 身份关联 | Trinity UID == 飞书 user_id | 关键假设：两个系统的用户 ID 一致，跳过跨系统映射 |
| @提及 open_id 传递 | mention_map 旁路传递，不嵌入消息文本 | 纯中文名给妙搭不干扰 NLP，open_id 绕道传给 handler |
| UID→open_id 回退 | 飞书用户 API 通过 user_id 查 open_id | 中文名搜索通讯录无结果时的降级路径 |
| 配置驱动 | projects_config.json | 新增/修改项目无需改代码，用户维护 JSON 即可 |
| 缓存策略 | 内存 + 文件 + TTL 三级 | 减少 API 调用，进程重启可恢复 |
| 回调模式 | on_task_created 函数引用 | 主流程与 side-effect（位表/通知）解耦 |
| 日志分层 | 主业务 / 心跳 / 任务计数 独立文件 | 便于监控、排查和统计，互不干扰 |

### 2.3 外部依赖

| 依赖 | 用途 | 协议 |
|------|------|------|
| 飞书 Open API | IM 收发、联系人查询、多维表格操作 | HTTPS REST + WebSocket |
| 妙搭 OpenAPI | AI 对话、任务结构化拆解 | HTTPS REST |
| Trinity API | 任务创建(CreateAndAssignTask)、成员查询(GetProjectMembersById) | HTTPS REST + HTTP Basic Auth |
| Lark-oAPI SDK | 飞书 WS 长连接客户端 | WebSocket + protobuf |
| pypinyin | 中文名转拼音用于成员匹配 | 本地库 |
| trinity.ini | Trinity 认证 Token 持久化 | 本地配置文件 |

---

## 3. 模块详细设计

### 3.1 feishu_ws_bot.py — 主控模块

**职责**：飞书 WebSocket 长连接管理、消息收发、任务调度编排。

#### 3.1.1 数据流

```
飞书 WS Gateway
    │
    ▼
handle_message()          ← WS 线程，纯接收
    │
    ├── 提取 chat_id / open_id / content
    ├── @提及预处理 (_user_X → 姓名(@ou_xxx))
    ├── 入队列 (_message_queue.put)
    └── 异步回复"处理中" (_reply_busy_and_log)
    │
    ▼
_message_queue (Queue)
    │  FIFO + TTL=300s
    ▼
queue_worker()            ← 独立线程
    │
    ├── 出队检查 TTL，超时丢弃
    └── ThreadPoolExecutor.submit(_process_one_message, ...)
        │
        └── future.result(timeout=150)
            │
            ▼
        _process_one_message()  ← 实际处理
            │
            ├── _resolve_project(text) → (cleaned, project_cfg, hint)
            │   ├── 精确前缀匹配（长缩写优先）
            │   ├── 模糊匹配 → 用户引导
            │   └── 未匹配 → 提示可用缩写
            │
            ├── SPM 权限校验（发送者姓名 == project_cfg.spm）
            │
            ├── call_miaoda(text, open_id) → (reply, tasks)
            │
            └── process_miaoda_tasks() + on_task_created 回调
```

#### 3.1.2 关键函数

| 函数 | 触发 | 说明 |
|------|------|------|
| `handle_message(data)` | WS 事件 | 提取消息、替换提及、入队列 |
| `_reply_busy_and_log(chat_id, open_id)` | 异步线程 | 获取发送者姓名，回复"处理中+队列数" |
| `queue_worker()` | 主线程启动 | 循环出队，提交线程池，超时保护 |
| `_process_one_message(text, chat_id, open_id)` | 线程池 | 项目匹配 → SPM 校验 → 妙搭 → 任务创建 |
| `_resolve_project(text)` | 内部 | 项目缩写提取：精确→fuzzy→引导 |
| `call_miaoda(message, sender_id)` | 内部 | 调用妙搭 Chat API |
| `reply_feishu(chat_id, text)` | 内部 | 飞书 IM 文本回复 |
| `get_user_name(open_id)` | 内部 | 飞书联系人查询 + 1h 缓存 |
| `get_token()` | 内部 | 飞书 tenant_access_token + 缓存 |
| `run_ws(stop)` | WS 线程 | 启动 Lark-oAPI WS Client |
| `main()` | 入口 | 初始化线程池、worker、WS 客户端 |

#### 3.1.3 消息去重机制

```
两层去重，覆盖 WS 重播两种场景：

① message_id 去重（持久化到 config/msg_dedup.json）
   · WS 正常投递时 msg_id 相同，直接拒绝
   · 每 10 分钟清理缓存，避免无限增长
   · 进程重启后从文件恢复，跨运行去重

② 内容 Hash 去重（内存，60s TTL）
   · WS 重播可能分配新 msg_id，但内容相同
   · (chat_id, content_hash) → timestamp
   · 60s 内相同内容跳过

③ WS 保护期（启动/重连 8s 内跳过所有消息）
   · 飞书重连后立即推送大量历史消息
   · 保护期内直接 return，等稳定后再接收
```

#### 3.1.4 消息队列安全机制

```
- TTL=300s: 队列等待超过 5 分钟的消息自动丢弃
- 超时=150s: 单条消息处理超时后跳过，不阻塞后续
- max_workers=2: 控制并发，避免 Trinity API 压力
- 5-tuple (text, chat_id, open_id, timestamp, mention_map): 向后兼容 4-tuple
```

#### 3.1.5 @提及预处理逻辑

```
飞书原始格式: "你好 @_user_1 请处理"
               ↓
检测 mentions 数组: key="_user_1", name="张三", id.open_id="ou_xxx"
               ↓
替换: "你好 张三 请处理"           ← 纯中文名，不干扰妙搭 NLP
mention_map: {"张三": "ou_xxx"}    ← open_id 旁路传递给 handler
               ↓
妙搭收到: 可读姓名；handler 收到: mention_map 优先命中 open_id
```

#### 3.1.6 WS 健康检测

```
每 30s 心跳检查：
  · 超过 2 分钟无消息 + WS 线程已死 → 自动重建连接
  · 重建后设 8s 保护期，过滤重播消息
  · 旧进程清理：Windows taskkill /F 强制终止
```

#### 3.1.7 任务累计计数器

```
每次成功创建任务后累加，持久化到 config/task_counter.json
  · 进程重启从文件恢复
  · 独立日志 logs/task_counter.log
  · 启动时打印累计统计
```

---

### 3.2 trinity_miaoda_task_handler.py — 任务创建核心

**职责**：解析妙搭返回的任务列表，解析指派人身份，调用 Trinity API 创建任务。

#### 3.2.1 名称解析流程

```
输入: "房汉柠" / "@ou_xxx" / "ou_xxx" / "房汉柠(@ou_xxx)"
  │
  ├── 飞书ID路径 (@ / ou_ / 组合格式)
  │     │
  │     ├── _resolve_feishu_id(feishu_id)
  │     │    飞书联系人 API → (姓名, user_id)
  │     │
  │     ├── user_id 直搜 member_cache（跳过英文名匹配）
  │     │    找到 → 返回 (uid, eng_name, open_id)
  │     │
  │     └── 姓名走 NAME_MAP / 拼音匹配
  │
  ├── mention_map 路径（@提及直传）
  │     │
  │     ├── 中文名在 mention_map 中 → 已有 open_id
  │     │    匹配成员缓存 → 返回 (uid, eng, open_id)
  │     └── 跳过联系人搜索
  │
  └── 中文名路径
        │
        ├── NAME_MAP 手动映射 (.name_map.json)
        │    有 → 用英文名搜 member_cache
        │
        └── 自动拼音转写 (pypinyin)
              "杜雪莲" → ["du xuelian", "du xue lian"]
              搜索 member_cache
              找到 → open_id 获取:
              │     ├── _find_open_id_by_name(中文名) 通讯录搜索
              │     └── _uid_to_open_id(uid) 飞书API回退
```

#### 3.2.2 成员缓存机制

```
三层缓存，降级策略：

① 内存缓存 _member_cache (dict)
   TTL=3600s，进程内最快

② 文件缓存 .member_cache.json
   进程重启后加载，减少 API 调用
   校验 project_id 一致 + TTL

③ Trinity API GetProjectMembersById
   实时查询，两层重试：
     - 首次未命中 → 刷新缓存再查一次
     - 彻底失败 → 报错
```

#### 3.2.3 日期兜底逻辑

```python
if not start_date:
    start_date = today_str      # 当日
    start_ts = today_ts
    date_warnings.append("开始时间")
if not end_date:
    end_date = today_str
    end_ts = today_ts
    date_warnings.append("结束时间")
if date_warnings:
    lines.append(f"⚠️ {'、'.join(date_warnings)}未填写，已默认设为当日")
```

#### 3.2.4 Trinity 任务创建参数

```python
params = {
    "title": title,
    "description": title,
    "assigneeId": assignee_uid,        # Trinity UID
    "reviewerId": creator_uid,         # SPM 作为 reviewer
    "discipline": "sw",
    "planStartDate": start_ts,         # 毫秒时间戳
    "planEndDate": end_ts,
    "initialEstimate": float(hours),
    "parent": parent_task,             # 上级任务 ID
    "taskLevel": 5,
    "apqp": False,
    "projectId": project_id,
    "authorId": creator_uid,
    "authorName": creator_english,
}
```

#### 3.2.5 回调接口

```python
def on_task_created(
    title: str,           # 任务名
    hours: float,         # 预估工时
    start_date: str,      # 计划开始 (YYYY/MM/DD)
    end_date: str,        # 计划结束
    assignee_oid: str,    # 指派人飞书 open_id (ou_xxx)
    task_id: str,         # Trinity 任务 ID
    assignee_uid: str,    # Trinity UID (=飞书 user_id)
) -> None:
```

---

### 3.3 trinity_project_matcher.py — 项目匹配器

**职责**：从 projects_config.json 加载项目列表，支持缩写精确匹配和模糊搜索。

#### 3.3.1 匹配算法

```
输入: "A66T 创建任务..."
  │
  ├── 1. 精确前缀匹配（按长度降序）
  │     遍历已知缩写，消息是否以此开头
  │     检查后续字符：非字母数字/空格/结尾 → 确认
  │     例: "A02创建" → 匹配 A02（不误配 A02Y）
  │
  ├── 2. 精确匹配未命中 → 取第一个词
  │     例: "AT5-T 创建" → find_project("AT5-T")
  │
  ├── 3. 标准化匹配（去分隔符、小写）
  │     例: "A66T" → normalize → "a66t"
  │     处理重复缩写（BZ5 / S20 域控）
  │
  ├── 4. 模糊匹配（包含关系 + 首字符）
  │     例: "3G" → 匹配 "3GE"、"3DAA"
  │     唯一候选 → 自动确认
  │     多候选 → 列表引导
  │
  └── 5. 完全未匹配 → 回复可用缩写列表
```

#### 3.3.2 返回格式

```python
{
    "found": project_dict | None,    # 确定匹配的项目
    "suggestions": [project_dict],   # 模糊匹配候选
    "text": "引导文字"               # 回复给用户
}
```

---

### 3.4 feishu_bitable_writer.py — 多维表格写入器

**职责**：任务创建成功后，将任务信息写入项目对应的飞书多维表格。

#### 3.4.1 列映射

| 字段名 | 类型 | 格式 | 说明 |
|--------|------|------|------|
| `*Title` | 文本 | string | 任务名 |
| `*InitialEstimate(h)` | 数字 | float, 1位小数 | 预估工时 |
| `*PlanStartDate` | 日期 | 毫秒时间戳 int | 计划开始 |
| `*PlanEndDate` | 日期 | 毫秒时间戳 int | 计划结束 |
| `*Assignee` | 人员 | `[{"id": "ou_xxx"}]` | 执行人 open_id |
| `TaskID` | URL | `{"link": "...", "text": "..."}` | Trinity 任务链接 |

#### 3.4.2 API 调用

```
POST https://open.feishu.cn/open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/records
Authorization: Bearer {token}
Content-Type: application/json

{
    "fields": {
        "*Title": "任务名",
        "*InitialEstimate(h)": 40.0,
        "*PlanStartDate": 1770480000000,
        "*PlanEndDate": 1773072000000,
        "*Assignee": [{"id": "ou_xxx"}],
        "TaskID": {"link": "https://trinity.desaysv.com/...", "text": "TASK2026..."}
    }
}
```

---

### 3.5 feishu_notify_assignee.py — 指派通知

**职责**：任务创建后向指派人发送飞书 Interactive Card 通知。

#### 3.5.1 卡片布局

```
┌──────────────────────────────────────┐
│  🔵 新任务已创建 (蓝色标题栏)          │
├──────────────────────────────────────┤
│  任务：矩阵更新                        │
│  项目：Honda 3DAA                     │
│                                      │
│  工时         计划                    │
│  40h         2026/06/20 ~ 2026/06/25 │
│                                      │
│  ─────────────────────────────────── │
│  [查看详情] → Trinity 任务详情页       │
└──────────────────────────────────────┘
```

#### 3.5.2 双模投递

```python
receiver_id = open_id or assignee_uid   # 优先 open_id
id_type = "open_id" if open_id else "user_id"

POST https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type={id_type}
{
    "receive_id": receiver_id,
    "msg_type": "interactive",
    "content": {card_json}
}
```

当用户未与机器人建立过 P2P 会话时（code=230013），通知发送失败。这是飞书平台限制，非代码问题。

---

### 3.6 config.py — Trinity 认证配置

**职责**：Trinity API 认证管理、Token 获取与持久化。

#### 3.6.1 认证流程

```
1. 读取 trinity.ini → 检查 Token 是否过期（12h TTL）
2. 未过期 → 直接返回 Token
3. 已过期/不存在 → Trinity_GetToken API 刷新
4. 新 Token 写入 trinity.ini 持久化
```

#### 3.6.2 双环境支持

| 环境 | URL | PI 用户 | Platform |
|------|-----|---------|----------|
| dev | http://hzhio133a370v.v01.net:8180 | ALE_HZ_APTDMS_DEV | test |
| prd | https://cop-office.desaysv.com | DQM_PTV_PTV2-CRT29S7J | SZ_BUGER |

当前使用 `ENV = 'prd'`。

---

## 4. 数据流全链路

### 4.1 消息生命周期

```
Step 1: 用户发送消息
  飞书 → WS Gateway → handle_message()
  · 提取 chat_id, open_id, content
  · @提及替换: _user_X → 姓名(@ou_xxx)
  · 入队列 (text, chat_id, open_id, timestamp)
  · 异步回复"处理中+队列数"

Step 2: 队列调度
  queue_worker() 循环出队
  · 检查 TTL (300s)，超时丢弃
  · ThreadPoolExecutor.submit(_process_one_message, ...)
  · future.result(timeout=150)

Step 3: 项目匹配
  _resolve_project(text)
  · 精确前缀 → 模糊 → 引导
  · 无匹配 → 回复可用缩写，终止

Step 4: SPM 校验
  · get_user_name(open_id) 获取发送者姓名
  · 与 project_cfg.spm 对比
  · 不匹配 → 回复"只有 xxx 才能创建任务"，终止

Step 5: 妙搭拆解
  call_miaoda(text, open_id)
  · POST /openapi/chat
  · 返回 (reply_text, tasks_list)

Step 6: 任务创建
  process_miaoda_tasks(tasks, ...)
  循环每个 task:
  · resolve_name_to_uid(assignee)
  · date 兜底（当日）
  · Trinity_CreateAndAssignTask
  · on_task_created 回调
  · 间隔 1.5s 防限流

Step 7: 回调处理
  on_task_created():
  · feishu_bitable_writer.write_task_to_bitable()  ← 非阻塞
  · feishu_notify_assignee.feishu_notify_assignee()       ← 非阻塞

Step 8: 结果回复
  reply_feishu(chat_id, summary)
  · 成功/失败统计
  · 各任务状态 + Trinity 链接
```

---

## 5. 配置模型

### 5.1 projects_config.json

用户维护的项目配置，每个项目一个条目：

```json
{
    "name": "Honda 3DAA",           // 项目全称
    "abbr": "3DAA",                 // 消息前缀缩写
    "projectId": "APP2026...",      // Trinity 项目 ID
    "parentTask": "TASK2026...",    // 上级任务 ID（可选）
    "spm": "杜雪莲",                // 项目负责人
    "feishu_url": "https://..."     // 多维表格链接（可选）
}
```

### 5.2 .name_map.json

中文名到英文名的手动映射，自动拼音匹配失败时的补充：

```json
{
    "孙猛": "Sun Meng",
    "宋学郊": "Song Xuejiao"
}
```

### 5.3 .member_cache.json

自动维护，从 Trinity API 获取的项目成员缓存：

```json
{
    "time": 1718000000.0,
    "project_id": "APP2026...",
    "cache": {
        "sun meng": {"uid": "uid03519", "full_name": "Sun Meng(uid03519)"},
        "du xuelian": {"uid": "uid02619", "full_name": "Du Xuelian(uid02619)"}
    }
}
```

### 5.4 trinity.ini

Trinity Token 持久化（config.py 维护）：

```ini
[TOKEN]
expire_time = 202606170000
token = eyJhbGci...
```

### 5.5 msg_dedup.json

消息去重缓存（feishu_ws_bot.py 自动维护，持久化防进程重启）：

```json
["msg_abc123", "msg_def456"]
```

- 每 10 分钟清空一次避免无限增长
- 跨进程重启恢复，防止重启后重播消息被重复处理

### 5.6 task_counter.json

累计任务数统计（feishu_ws_bot.py 自动维护）：

```json
{"total": 1234}
```

- 每次成功创建任务后累加
- 进程重启从文件恢复，统计不丢失

---

## 6. 错误处理策略

| 错误场景 | 处理方式 | 用户体验 |
|---------|---------|---------|
| 妙搭超时(>60s) | 线程池 150s 超时，跳过继续处理 | 回复超时提示 |
| 妙搭返回非 JSON | 捕获异常，回复错误码 | "妙搭接口异常: HTTP xxx" |
| 项目缩写未识别 | _resolve_project 返回提示 | 回复可用缩写列表 |
| SPM 不匹配 | 回复"只有 xxx 才能创建任务" | 明确拒绝 |
| 指派人未找到 | 跳到下一个任务 | "无法解析负责人 xxx 的 UID" |
| Trinity API 失败 | 捕获异常，记录日志 | "xxx 创建失败(code=xxx)" |
| 多维表格写入失败 | 记录 + 追加告警到回复末尾 | 回复末尾显示 ⚠️ 告警 |
| 通知发送失败 | 记录 + 追加告警到回复末尾 | 回复末尾显示 ⚠️ 告警 |
| WS 连接断开 | Lark-oAPI SDK 自动重连 | 短暂不可用后自动恢复 |
| WS 线程僵死 | 2min 检测 + 自动重建 WS 线程 | 透明恢复 |
| WS 重播旧消息 | msg_id 去重 + 内容 Hash + 8s 保护期 | 三重防护，消息只处理一次 |
| 消息队列积压 (>300s) | TTL 检查丢弃 | 无回复（用户可重发） |

---

## 7. 安全机制

| 机制 | 说明 |
|------|------|
| SPM 权限校验 | 只有项目配置中的 SPM 能创建任务 |
| 未匹配项目不创建 | 无项目缩写时终止流程，回复提示 |
| 模糊匹配不自动确认 | 多候选时列出选项让用户选择 |
| reviewer = SPM | 创建人为 reviewer，非执行人 |
| 消息去重三重防护 | msg_id 持久化 + 内容 Hash + WS 保护期，防止重播 |
| Token 安全 | Trinity Token 文件持久化，12h 自动刷新 |
| 飞书 Token 缓存 | 内存缓存避免频繁获取 |
| 请求超时 | 所有外部 API 调用设 timeout（10-60s） |
| 日志脱敏 | URL 参数等敏感信息在日志中截断 |
| 旧进程清理 | 启动时 taskkill /F 强制终止旧 PID，防止多实例 |
| WS 健康自愈 | WS 线程僵死自动检测重建，避免静默失败 |
