# TaskCreator v3.5 — 飞书机器人 → 妙搭 → Trinity 任务创建 → 多维表格 + 指派通知

## 概述

连接三个系统实现自动化任务创建：

```
飞书消息 → 妙搭 AI（拆解任务）→ Trinity（创建任务到指定项目）
```

用户在飞书发送需求描述 → 妙搭智能拆解为子任务 → 自动在 Trinity 对应项目中创建任务，创建人为对应项目的 SPM。

## 架构

```
┌─────────────────────────────────────────────────────────────────────────┐
│                        feishu_ws_bot.py                                 │
│  ┌──────────┐   ┌──────────┐   ┌──────────────────────────────────┐    │
│  │ WS 线程   │ → │ 消息去重  │ → │ ThreadPoolExecutor(2)            │    │
│  │ 收消息+ACK │   │ msg_id   │   │ 每条消息最多 150s 超时            │    │
│  │ @提及→姓名 │   │ +内容Hash│   │ ① 提取项目缩写 → trinity_project_matcher  │    │
│  │ +mention_map│  │ +8s保护期│   │ ② 调妙搭拆解任务                    │    │
│  └──────────┘   └──────────┘   │ ③ process_miaoda_tasks 创建任务     │    │
│                                │    ├─ mention_map 传递 @open_id     │    │
│                                │    └─ UID→open_id 飞书API回退       │    │
│                                │ ④ on_task_created 回调              │    │
│                                │    ├─ feishu_bitable_writer  → 多维表格    │    │
│                                │    └─ feishu_notify_assignee → 指派通知    │    │
│                                └──────────────────────────────────┘    │
│  ┌──────────┐   ┌──────────┐                                          │
│  │ 任务计数器 │   │ WS 健康   │                                          │
│  │ 累计统计   │   │ 2min检测  │                                          │
│  │ 持久化JSON │   │ 自动重连   │                                          │
│  └──────────┘   └──────────┘                                          │
└────────────────────────────────────────────────────────────────────────┘
                                           │
              ┌────────────────────────────┼───────────────┐
              │       trinity_miaoda_task_handler.py                │
              │  ┌──────────┐  ┌──────────┐  ┌──────────┐ │
              │  │ NAME_MAP │  │ 拼音转写  │  │ Trinity  │ │
              │  │ 手动覆盖  │→ │ 自动匹配  │→ │ API 创建 │ │
              │  └──────────┘  │ 飞书ID直查 │  │ + 任务链接 │ │
              │                │ ou_ / @   │  └──────────┘ │
              │                ├──────────┤                │
              │                │ mention_map│              │
              │                │ @提及→oid │               │
              │                ├──────────┤                │
              │                │ UID→open_id│              │
              │                │ 飞书API回退│              │
              │                └──────────┘                │
              │                  ┌──────────┐              │
              │                  │ 成员缓存  │              │
              │                  │ 1h TTL   │              │
              │                  └──────────┘              │
              └────────────────────────────────────────────┘
                                           ▲
              ┌────────────────────────────┴───────────────┐
              │            trinity_project_matcher.py               │
              │  projects_config.json → 模糊匹配 → 项目配置  │
              │  "A66T" → "您是否指 A66-T？"                 │
              │  "3DAA" → 精确匹配 → 项目ID / parentTask    │
              └────────────────────────────────────────────┘
```

## 文件说明

| 文件 | 职责 |
|------|------|
| `feishu_ws_bot.py` | 飞书 WS 长连接、消息去重（msg_id + 内容 Hash）、@提及替换、项目缩写提取、任务创建调度、位表写入回调、任务累计计数器、WS 健康检测自动重连 |
| `trinity_miaoda_task_handler.py` | 任务创建核心逻辑：人名/ID解析、mention_map 传递 @open_id、UID→open_id 飞书 API 回退、Trinity API 调用、生成任务链接 |
| `feishu_bitable_writer.py` | 飞书多维表格写入：将已创建的 Trinity 任务写入项目对应 Bitable |
| `feishu_notify_assignee.py` | 指派通知：任务创建后向指派人发送飞书 Interactive Card（含时间戳） |
| `trinity_project_matcher.py` | 项目缩写精确匹配 + 模糊搜索 + 用户引导 |
| `config/projects_config.json` | 项目配置（缩写→项目ID→SPM→父级任务→feishu_url） |
| `config/name_map.json` | 中文名 → 英文名映射（可选，拼音可自动匹配） |
| `config/member_cache.json` | Trinity 项目成员缓存（自动维护） |
| `config/msg_dedup.json` | 消息去重缓存（持久化，进程重启不丢失） |
| `config/task_counter.json` | 累计任务数统计（持久化，进程重启不丢失） |
| `config.py` | Trinity 认证配置、Token 管理 |

## 使用方式

### 发送消息格式

在消息开头加上项目缩写，支持以下指派人格式：

```
A66-T 帮我创建以下任务：
1. 矩阵更新 | 指派人: 蔡波 | 40h            ← 中文名
2. 白盒测试 | 指派人: @孙猛 | 40h             ← 飞书@提及
```

支持无空格：
```
3DAA创建几个测试任务...
A19白盒测试 指派人: 房汉柠 | 20h
```

### 指派人解析规则（按优先级）

| 输入格式 | 解析方式 |
|---------|---------|
| `房汉柠` | NAME_MAP 手动映射 → 拼音自动匹配 → 成员缓存 |
| `@孙猛` 或 `@ou_xxx` | 飞书联系人 API 查 user_id + 姓名 |
| `ou_xxx`（裸 open_id）| 自动识别 ou_ 前缀，走飞书 API |
| `房汉柠(@ou_xxx)` | 提取姓名 + ID 双路解析 |
| 飞书 `@`提及 | 自动替换 `_user_X` → `姓名(@open_id)` 后送妙搭 |

飞书 ID 路径优先通过 `user_id` 直搜成员缓存，跳过英文名匹配。

### 项目匹配规则

1. **精确匹配** — 消息开头匹配已知缩写（长缩写优先）
   - `A66-T创建...` → A66-T 项目
   - `3DAA创建...` → 3DAA 项目
   - `A02创建...` → A02 项目（不会误匹配 A02Y）
2. **模糊匹配** — 近似缩写会提示用户确认
   - `AT5-T创建...` → "您是否指 AY5-T？"
3. **未匹配** — 不创建任务，回复可用缩写列表

### 安全机制

- 未识别到项目缩写 → **不创建任务**，提示用户指定
- 模糊匹配多个项目 → **不创建任务**，列出候选让用户选择
- reviewer = SPM（创建人），非执行人

## 配置说明

### 项目配置 (projects_config.json)

```json
{
    "name": "Honda 3DAA",
    "abbr": "3DAA",
    "projectId": "APP2026042119304398954",
    "parentTask": "TASK20260422_21287",
    "spm": "杜雪莲",
    "feishu_url": "https://yesv-desaysv.feishu.cn/base/UE0ubzRvxau17ZsiYApcV9Zoncd?table=tblImisAZ2X6K0ZU"
}
```

用户只需维护此 JSON 文件，添加/修改项目配置。
设置 `feishu_url` 后，任务创建成功会自动写入对应多维表格。

### 飞书机器人

- `APP_ID` / `APP_SECRET` — 飞书应用凭证（需开通 IM + 联系人权限）
- `MIAODA_BASE` / `MIAODA_API_KEY` — 妙搭 OpenAPI 凭据

### 名称解析流程

```
中文名（如"杜雪莲"）
  │
  ├─→ 飞书ID路径（@ / ou_ / _user_）
  │     飞书联系人API查姓名 + user_id
  │     ├─→ user_id 直搜成员缓存（跳过拼音）
  │     └─→ 姓名走 NAME_MAP / 拼音匹配
  │
  ├─→ mention_map 路径（@提及直传）
  │     @提及 → 中文名 + open_id 旁路传递
  │     ├─→ 中文名匹配成员缓存 → UID + open_id
  │     └─→ 跳过联系人搜索（open_id 已知）
  │
  └─→ NAME_MAP 手动匹配（.name_map.json）
  │    有 → 使用映射的英文名
  │
  └─→ 自动拼音转写（pypinyin）
        "杜雪莲" → ["du xuelian", "du xue lian"]
        │
        └─→ 与 Trinity 成员缓存匹配
              找到 → 搜索联系人 open_id
              │      ├─→ 中文名搜索通讯录
              │      └─→ UID→open_id 飞书API回退
              未找到 → 刷新缓存再试
              仍未找到 → 报告错误
```

### 成员缓存机制

1. 内存缓存（本进程内）
2. 文件缓存（`.member_cache.json`，1 小时 TTL）
3. Trinity API 实时查询（`Trinity_GetProjectMembersById`）

## 多维表格（Bitable）集成

项目配置中设置 `feishu_url` 后，每次 Trinity 任务创建成功会自动写入飞书多维表格。

### 列映射

| 字段名 | 类型 | 说明 |
|--------|------|------|
| `*Title` | 文本 | 任务名 |
| `*InitialEstimate(h)` | 数字（一位小数）| 预估工时 |
| `*PlanStartDate` | 日期 YYYY/MM/DD | 计划开始 |
| `*PlanEndDate` | 日期 YYYY/MM/DD | 计划结束 |
| `*Assignee` | 人员 | 执行人（优先通过 @提及的 open_id，中文名自动搜索飞书联系人） |
| `TaskID` | URL | Trinity 任务链接（含 projectId / projectName 参数） |

### 指派人 open_id 解析

当用户使用中文名（非 @提及）时，机器人自动搜索飞书通讯录匹配姓名获取 open_id：
1. 内存缓存（进程内，1 小时 TTL）
2. 飞书联系人 API 分页拉取全量通讯录匹配

## 指派通知

每个任务创建成功后，自动向指派人发送飞书卡片消息通知。通知使用飞书 Interactive Card 格式，包含：

- **蓝色标题栏**：显示"新任务已创建"
- **任务名** + **项目名**
- **工时** + **计划日期**（左右分栏）
- **查看详情按钮**：点击跳转 Trinity 任务页（含 projectId / projectName 参数）

通过在 `feishu_ws_bot.py` 的 `on_task_created` 回调中调用 `feishu_notify_assignee.feishu_notify_assignee()` 实现。

## 运行

```bash
cd D:/Tools/TaskCreator
python feishu_ws_bot.py
```

启动后自动连接飞书 WS 网关，心跳每 30s 打印一次。

## v3.5 更新内容

### v3.5（当前）
- **任务名项目缩写兜底**：妙搭偶尔吞掉任务名中的项目缩写（如 `"AY5-TM GNSS/IMU 驱动"` → `"GNSS/IMU 驱动"`），`process_miaoda_tasks` 创建前自动检测并在 title 最前面补上项目缩写
- **mention_map 优先路径**：`_resolve_by_name` 函数新增 mention_map(open_id) 优先路径，有 open_id 时直接通过飞书 API 获取 Trinity UID，跳过拼音匹配。解决多音字（如"覃"读 qín 但 pypinyin 拼成 tán）导致的人名解析失败
- **通知卡片标题优化**：从 `新任务已创建 [HH:MM]` 改为 `新任务已创建：任务名`，去掉时间戳，信息更直观
- **妙搭超时加长**：妙搭调用超时从 60s → 120s，减少因 LLM 推理慢导致的超时失败

### v3.4
- **消息去重**：新增 `message_id` 去重（持久化 `config/msg_dedup.json`，跨进程重启）+ 内容 Hash 去重（60s TTL），彻底解决 WS 重播导致重复处理
- **WS 保护期**：启动/重连后 8 秒内跳过所有消息，过滤飞书重播旧消息
- **WS 健康检测**：每 30s 检查一次，超过 2 分钟无消息且 WS 线程已死 → 自动重建连接
- **心跳日志独立**：心跳单独写 `logs/heartbeat.log`，不再混在主业务日志 `logs/bot.log`
- **任务累计计数器**：统计历史累计创建任务数，持久化到 `config/task_counter.json`，进程重启不丢失
- **@提及优化**：替换格式从 `姓名(@open_id)` → 纯中文名（不干扰妙搭 NLP 解析），`open_id` 通过 `mention_map` 旁路传递给 handler 优先使用
- **UID→open_id 回退**：中文名搜索通讯录无结果时，用 Trinity UID（=飞书 user_id）直查飞书 API 获取 open_id
- **位表/通知告警**：多维表格写入失败 + 通知发送失败的错误信息追加到飞书回复末尾，不再静默吞掉
- **失败场景优化**：全部失败时不显示妙搭的"已提取并创建"误导文字；异常场景也不追加格式引导
- **回复时间戳**：所有回复消息 header 加 `[HH:MM]` 时间戳；通知卡片标题也加上时间
- **回复响应速度**："处理中"先发送再异步获取姓名，不等待飞书联系人 API
- **旧进程清理**：Windows 下用 `taskkill /F` 替代 `os.kill` 确保旧进程真正终止

### v3.3
- **配置热重载**：`projects_config.json` 和 `.name_map.json` 更新后自动加载，无需重启进程
- **消息格式引导**：项目未匹配 / 妙搭无法解析 / 任务创建失败时，自动回复格式示例引导用户
- **富文本加粗支持**：改用 `msg_type: "post"` 发送，`**加粗**` 标记真正生效
- **项目匹配修复**：区分强匹配（包含关系可自动确认）和弱匹配（仅首字母相同只列候选不自动确认）
- **失败场景引导覆盖**：任务创建部分失败时也附带格式引导，全部成功时不追加

### v3.2
- **SPM 权限校验**：只有项目配置中的 SPM 才能创建任务，其他人回复无权操作提示
- **日志独立目录**：日志写入 `logs/bot.log`，不再混在项目根目录
- **原始内容日志**：新增 WS 消息原始内容日志，便于排查飞书消息截断问题
- **通知优先 uid**：无 open_id 时直接用 Trinity UID（=飞书 user_id）发通知，不依赖联系人搜索
- **日期兜底**：妙搭未解析开始/结束日期时自动设为当日，并在结果中给出 ⚠️ 提醒

### v3.1
- **通知支持 user_id 回退**：没有 open_id 时直接用 Trinity UID（=飞书 user_id）发送通知，无需搜索飞书通讯录
- **feishu_notify_assignee 优化**：新增 `assignee_uid` 参数，自动选择 `receive_id_type`
- **简化依赖**：指派通知不依赖飞书联系人搜索，有 uid 就能发

### v3.0
- **指派通知**：新增 `feishu_notify_assignee.py`，每个任务创建后向指派人发送飞书卡片消息通知
- **卡片消息格式**：使用 Interactive Card，含蓝色标题栏、任务/项目/工时/日期分栏、"查看详情"按钮
- **完整回调链路**：`on_task_created` 回调统一处理多维表格写入 + 指派通知，两种互不阻塞
- **文档架构图更新**：反映完整流程（WS → 队列 → 任务创建 → 位表写入 + 通知）

### v2.3
- **多维表格写入**：新增 `feishu_bitable_writer.py`，任务创建成功后自动写入项目对应飞书多维表格
- **字段名修正**：Bitable 字段名带 `*` 前缀（`*Title`, `*InitialEstimate(h)` 等），日期字段发毫秒时间戳
- **人员字段修复**：Bitable User 字段使用 `{"id": "ou_xxx"}` 格式，中文名指派人自动搜索飞书通讯录获取 open_id
- **TaskID 链接修正**：URL 类型字段带 `projectId` / `projectName` 参数，确保链接触达 Trinity
- **联系人搜索**：飞书通讯录全量搜索（分页 + 缓存），中文名 → open_id 自动解析
- **Queue worker 稳定性修复**：竞态条件下 3→4 元组解包崩溃导致 worker 线程退出的 bug 修复
- **回调增强**：`process_miaoda_tasks` 的 `on_task_created` 回调新增 `assignee_uid` 参数

### v2.2
- 消息队列超时保护：MQ 中等待超过 5 分钟的消息自动丢弃，避免旧消息延迟响应
- 单消息处理超时：`ThreadPoolExecutor(max_workers=2)` + 150s 超时，妙搭卡死不阻塞后续消息
- Lark-oAPI MentionEvent 兼容：属性访问替代 dict 访问，修复 SDK 对象解析崩溃
- 飞书 ID 多格式解析：支持 `@ou_xxx`、裸 `ou_xxx`、`姓名(@ou_xxx)` 三种输入
- 飞书 ID 路径跳过拼音匹配：user_id 直搜成员缓存，飞书和 Trinity 同 ID 时零跳转
- 文档统一为单 README.md 维护，清除多版本冗余文件

### v2.1
- 飞书 open_id 直接解析
- `@`提及自动替换：`_user_X` → `姓名(@open_id)`
- 飞书联系人 API 查询 user_id + 姓名

## 待完善

- [ ] 项目 ID / 上级任务 / 创建人 从飞书消息动态解析
- [ ] WebSocket 稳定性改进
