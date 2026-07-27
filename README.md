[![HumanlikeReply Counter](https://count.getloli.com/get/@Inoryu7z.humanlikereply?theme=miku)](https://github.com/Inoryu7z/astrbot_plugin_humanlike_reply)

# 🎭 拟人回复 · HumanlikeReply

让 AI 不再秒回，像真人一样把控回复节奏。

**拟人回复** 是一个对话节奏控制插件，专注于让 Bot 在收到消息后能像真人一样：
**判断自己现在是否有空、是否应该立刻回复、还是先忙完手头的事再回。**

它通过一个"判断LLM"来决定回复时机，并拟定回复方案；再由"对话LLM"审查方案后输出最终回复。
整个过程对用户透明，只会感觉到 Bot 的回复节奏变得更自然、更拟真。

---

## ✨ 它能做什么

### 🎭 拟人回复节奏

收到用户消息后，Bot 不会立刻秒回，而是先判断自己当前是否有空：

- **立即回复**：当前空闲，直接回复
- **延迟回复**：当前在忙（跳舞、上课、洗澡、睡觉等），延迟若干分钟后回复
- **不回复**：消息无需回应（如纯表情、无意义内容），静默丢弃

判断依据综合参考：
- 当前日程（由 DayFlow 等插件通过 `on_llm_request` 钩子注入）
- 用户消息内容与紧急程度
- Dailysharing 下次主动分享时间

### 📝 二阶段审查

无论判断结果是立即、延迟还是不回复，判断LLM 都会拟定一个回复方案。
最终回复时，对话LLM 会审查这个方案：

- 是否符合人设和性格语气
- 是否符合事实（当前状态、日程、时间）
- 语气是否自然像真人
- 方案好就直接采用，有问题就修改

这样既保证了回复时机的拟真性，也保证了回复内容的质量。

### ⏰ Dailysharing 冲突避免

如果安装了 `astrbot_plugin_daily_sharing`，本插件会探测其下次主动分享时间。
当判断LLM 决定延迟回复时，延迟时间会自动夹到不与 Dailysharing 冲突的范围：

- 如果 Dailysharing 将在 20 分钟后主动分享
- 判断LLM 设定的延迟不能超过 19 分钟（留 1 分钟缓冲）
- 避免延迟回复被 Dailysharing 抢先，导致用户消息被跳过

### 🚨 催促重判

如果判断LLM 决定延迟回复，用户在等待期间又发了新消息（可能表示急切），
本插件会取消原延迟任务并重新判断：

- 用户表示"快"、"急"等紧急词汇时，即使忙碌也会尽快回复
- 重新判断可以选择立即回复（覆盖原延迟）、设定新的延迟、或不回复

---

## 🌼 适配场景

如果你希望 Bot：
- 不再像机器一样秒回，而是有真实的生活节奏
- 忙的时候会先忙完再回消息，像真人一样
- 用户着急时会调整优先级，尽快回复
- 回复内容会体现当前状态（如"我刚才在跳舞，没看消息"）
- 与 Dailysharing、DayFlow、DayMind 等插件协同，形成完整的拟人生活感

那拟人回复会很适合你。

---

## 🧩 推荐搭配插件

本插件可以独立运行，但若想获得更完整体验，推荐搭配：

| 插件 | 作用 |
|------|------|
| `astrbot_plugin_dayflow_life_scheduler` | 通过 `on_llm_request` 钩子注入日程、存在感、当前活动等信息，让判断LLM 能看到"我现在在忙什么" |
| `astrbot_plugin_daily_sharing` | 提供下次主动分享时间，让延迟回复不会与主动分享冲突 |
| `astrbot_plugin_daymind` | 通过 `on_llm_request` 钩子注入心情、思考状态，让回复更贴合当前心理状态 |
| `astrbot_plugin_token_router` | 多 Provider 按窗口路由 + 用量限制（v1.0.2 起联动）。本插件会手动触发其钩子，让路由链与用量统计对本插件的 LLM 调用生效 |
| `astrbot_plugin_postsplitter` | 长消息分段发送（v1.0.4 起联动）。本插件立即回复路径走完整钩子链，postsplitter 的 `on_decorating_result` 分段对最终回复生效 |
| `astrbot_plugin_tts_plus` | TTS 语音合成（v1.0.4 起联动）。本插件立即回复路径触发 `after_message_sent` 钩子，ttsplus 的语音合成对最终回复生效 |
| `astrbot_plugin_thinkview` | 思考记录捕获（v1.0.4 起联动）。本插件立即回复路径触发 `OnLLMResponseEvent` 与 `after_message_sent` 钩子，thinkview 能正常记录本插件的 LLM 推理过程 |

### 🔗 DayFlow 协同

DayFlow 通过 `on_llm_request` 钩子向 system_prompt 注入：
- `<DayFlow-Schedule>` 日程安排
- `<DayFlow-Presence>` 存在感
- `<DayFlow-Current-Activity>` 当前活动

本插件手动触发该钩子，让判断LLM 和对话LLM 都能看到这些信息，
无需 DayFlow 做任何适配。

### 🌊 Dailysharing 协同

本插件通过访问 Dailysharing 的 `scheduler.get_jobs()` 探测下次主动分享时间。
如果延迟回复的时间会超过最近一次分享，会自动夹到安全范围。

### 🔗 ChatPlus 协同（自动让位）

[`astrbot_plugin_group_chat_plus`](https://github.com/Him666233/astrbot_plugin_group_chat_plus)（chat_plus）是一个以"AI读空气"为核心的群聊专用插件。
本插件与 chat_plus 在群聊场景下功能重叠（都有"是否回复"决策），同时启用会导致：

- 双重决策消耗 API（本插件判断LLM + chat_plus 决策AI 各跑一遍）
- 本插件的 `stop_event()` 会阻断 chat_plus 的概率筛选与拟人化处理
- 行为不可预测

**默认分工**：chat_plus 处理群聊，本插件处理私聊。

本插件在加载时会自动检测 chat_plus：
- 若 chat_plus 已加载且本插件 `enable_group_chat=True`，会**自动覆盖为 `False`** 并告警
- 私聊场景不受影响，两者可共存
- 如需强制共存（不推荐），可关闭 `auto_yield_group_chat_to_chat_plus`

### 🔗 TokenRouter 联动（v1.0.2）

[`astrbot_plugin_token_router`](https://github.com/Inoryu7z/astrbot_plugin_token_router) 是按时间窗口在多个 Provider 间路由 + 单窗口用量限制的插件。

**联动问题**：本插件通过 `provider.text_chat()` 绕过 Pipeline 直接调 LLM，导致 token_router 的两个钩子均失效：
1. `on_message(priority=9999)` 因本插件 `priority=1 + stop_event` 被跳过 → `selected_provider` 永不被设置，路由链对本插件不生效
2. `on_llm_response` 由 Pipeline 触发，本插件绕过 Pipeline → 钩子永不触发 → 用量永不被记录

**联动方案**（v1.0.2 起，默认开启）：
- **立即回复路径**：`on_message` 入口手动调 `token_router.on_message(event)` 让其设置 extra，本插件读取 extra 作为 provider_id；LLM 响应后手动触发 `OnLLMResponseEvent` 钩子让 token_router 记录用量
- **延迟回复路径**（无 event）：延迟前缓存 `selected_provider` 和 `platform_name` 到 `reply_context`；响应后直接调 `token_router._record_usage(...)` 记录用量（绕过钩子）

**降级行为**：token_router 未安装时所有探测方法静默 no-op，不影响主流程。

**关闭联动**：将 `enable_token_router_integration` 设为 `false`，本插件 LLM 调用将完全绕过 token_router（不推荐，会导致用量统计缺失）。

### 🔗 钩子链完整性（v1.0.4）

本插件需要绕过框架的消息发送流程来控制回复时机，这导致 v1.0.4 之前 postsplitter / ttsplus / thinkview 等依赖框架消息发送流程的插件在本插件下**完全不生效**——长消息不会被分段、回复不会被转语音、思考过程也不会被记录。

**v1.0.4 修复**：立即回复路径重新接入框架的完整消息发送流程，上述插件对本插件的最终回复**全部生效**。

> ⚠️ **延迟回复路径**仍受限制：延迟回复通过另一条通道发送（原消息事件可能已失效），无法触发上述插件。也就是说，如果判断LLM 决定延迟回复，那条回复**不会**被 postsplitter 分段、不会被 ttsplus 转语音、也不会被 thinkview 记录。如需这些插件生效，可适当调小 `max_delay_minutes` 减少延迟回复的发生。

---

## ⚙️ 主要配置项

v1.0.3 起精简为 7 项核心配置，其他配置项回退到代码默认值。

### 基础开关
- `enable`：是否启用拟人回复（默认 `true`）。也可改用 `/拟真开` `/拟真关` 命令按会话临时控制
- `enable_private_chat`：是否在私聊中启用（默认 `true`）
- `enable_group_chat`：是否在群聊中启用（默认 `false`，按会话隔离。若安装了 chat_plus 会自动让位）

### LLM 相关
- `judge_provider_id`：判断LLM 的 Provider ID（留空则用会话默认对话LLM）

### 延迟与冷却
- `max_delay_minutes`：最大延迟分钟数（默认 `30`）
- `no_reply_cooldown_minutes`：判断为"不回复"后的冷却分钟数（默认 `5.0`，期间消息静默丢弃，避免连续判断浪费 token）

### 插件联动
- `enable_token_router_integration`：是否启用 TokenRouter 联动（v1.0.2 新增，默认 `true`）。开启后手动触发 token_router 的 `on_message` 与 `on_llm_response` 钩子，让路由链与用量统计对本插件生效

> **被移除的配置项**（v1.0.3）：`command_prefixes`、`dayflow_plugin_name`、`dailysharing_plugin_name`、`dailysharing_probe_count`、`chat_plus_plugin_name`、`auto_yield_group_chat_to_chat_plus`、`inject_dayflow_schedule`、`save_conversation_history`、`debug`、`token_router_plugin_name`、`judge_timeout_seconds`、`reply_timeout_seconds`。如需调整这些参数，可编辑 `main.py` 顶部的 `_DEFAULT_*` 常量。

---

## 📝 使用说明

1. 首次使用需在 AstrBot 配置界面中启用插件（默认启用）。
2. 默认只在私聊中启用，群聊需手动开启 `enable_group_chat`。
3. 判断LLM 和对话LLM 都使用当前会话的人格提示词作为 system_prompt 主体，只是追加的任务说明不同。
4. 如需省 token 或加快判断速度，可为 `judge_provider_id` 配置一个轻量模型。
5. 插件会拦截所有非指令消息（以 `/` 开头的消息直接放行）。
6. 延迟回复期间，Bot 的消息发送通过 `context.send_message` 完成，不依赖原始事件。
7. 若 DayFlow 未安装或未启用，判断LLM 将看不到日程信息，但仍可根据用户消息内容判断。

### 🎛️ 会话级命令（v1.0.3）

- `/拟真开`：临时开启当前会话（窗口）的拟人回复，不影响其他会话与全局 `enable`
- `/拟真关`：临时关闭当前会话（窗口）的拟人回复，不影响其他会话与全局 `enable`

会话级开关优先级高于全局 `enable`。重启插件后会话级状态清空，回到全局配置。
