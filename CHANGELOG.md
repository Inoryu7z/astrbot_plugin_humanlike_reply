### v1.0.2

**🔗 TokenRouter 联动：让路由与用量统计对本插件生效**

本插件绕过 Pipeline 直接调 `provider.text_chat()`，导致 token_router 的 `on_message` 与 `on_llm_response` 钩子均失效。本版本通过手动触发钩子弥补。

* 新增 `core/token_router_probe.py`：封装 token_router 探测与直接调用（trigger_on_message / resolve_persona_id / get_active_provider / record_usage）
* `core/llm_helper.py` 新增 `fire_on_llm_response_event` 与 `call_llm_with_response`，并删除死代码
* `main.py` 三条路径（立即回复 / 兜底 / 延迟回复）全部接入 token_router：
  - 立即路径：手动调 `on_message` 设置 extra → 读 `selected_provider` 选 provider → 响应后触发 `OnLLMResponseEvent`
  - 延迟路径（无 event）：缓存 `selected_provider` → 响应后直接调 `_record_usage` 记录用量
* 新增配置：`enable_token_router_integration`（默认 true）、`token_router_plugin_name`
* token_router 未安装时所有 probe 方法安全降级，不影响主流程

---

### v1.0.1

**🔗 ChatPlus 适配：自动让位群聊**

**1. 🔗 ChatPlus 冲突检测与自动让位**

* 检测到 `astrbot_plugin_group_chat_plus`（chat_plus）已加载且本插件 `enable_group_chat=True` 时，自动覆盖为 `False` 并告警
* 原因：chat_plus 是群聊专用插件（AI读空气、概率筛选、拟人化），与本插件在群聊场景下功能重叠（都有"是否回复"决策）
* 本插件的 `on_message(priority=1, stop_event=True)` 会阻断 chat_plus 的 `on_group_message`（默认 priority）执行
* 同时启用群聊会导致双重决策、API 浪费、行为不可预测
* **默认分工**：chat_plus 处理群聊，本插件处理私聊，两者天然不冲突

**2. ⚙️ 新增配置项**

* `chat_plus_plugin_name`：ChatPlus 插件名（默认 `astrbot_plugin_group_chat_plus`），用于检测是否加载
* `auto_yield_group_chat_to_chat_plus`：是否在检测到 ChatPlus 时自动让位群聊（默认开启，关闭可强制共存但不推荐）

**3. 📚 文档更新**

* README 新增"ChatPlus 协同（自动让位）"章节，说明冲突原因与默认分工
* TODO 列表中"与 chat_plus 的兼容性支持"标记为已完成

---

### v1.0.0

**🎭 首次发布：让AI像真人一样把控回复节奏**

**1. 🎭 拟人回复节奏上线**

* 新增判断LLM 机制：用户发消息后，由判断LLM 决定立即回复 / 延迟X分钟回复 / 不回复
* 判断依据综合参考当前日程（由 DayFlow 通过 `on_llm_request` 钩子注入）、用户消息内容与紧急程度
* 判断LLM 与对话LLM 使用同一人格提示词作为 system_prompt 主体，仅追加的任务说明不同
* 判断LLM 超时时兜底立即回复，避免用户久等
* 判断为"不回复"后进入冷却期（默认 5 分钟），期间消息静默丢弃，避免连续判断浪费 token

**2. 📝 二阶段审查：判断+审查**

* 判断LLM 无论决策如何，都会拟定一个回复方案草稿
* 最终回复时，对话LLM 审查该方案：是否符合人设、是否符合事实、语气是否自然
* 对话LLM 可自由判断是否需要优化：方案好就直接采用，有问题就修改
* 保证回复内容质量的同时，让回复时机也具有拟真性

**3. ⏰ Dailysharing 冲突避免**

* 探测 `astrbot_plugin_daily_sharing` 的下次主动分享时间（通过 `scheduler.get_jobs()`）
* 延迟回复时间自动夹到不与 Dailysharing 冲突的范围（留 60 秒缓冲）
* 避免延迟回复被 Dailysharing 抢先，导致用户消息被跳过无人理睬
* 向判断LLM 报告最近 3 个未来任务时间，让其决策时考虑冲突

**4. 🚨 催促重判**

* 用户在延迟期间发新消息时，取消原延迟任务并重新判断
* 重新判断可感知"用户正在等待"状态，包含距原定回复的剩余时间
* 用户表示紧急（"快"、"急"等）时，即使忙碌也会改为立即回复
* 同一会话串行处理，避免并发消息导致重复回复或丢失消息

**5. 🔗 手动触发 on_llm_request 钩子**

* `provider.text_chat()` 绕过 Pipeline，不会自动触发 `on_llm_request` 事件
* 实现手动触发机制，让 DayFlow、DayMind 等插件能正常注入日程、心情等信息
* 不重复注入日程/时间等信息——该注入的都会由对应插件注入，本插件只追加一小段任务说明

**6. 📨 延迟回复发送**

* 延迟回复时原事件可能已失效，通过 `context.send_message(umo, MessageChain)` 发送
* 参考_daymind proactive_chat 的发送实现
* 延迟任务触发时原子地"消费"累积消息，避免与新消息竞态

**7. ⚙️ 配置项**

* 基础开关：`enable`、`enable_private_chat`、`enable_group_chat`、`debug`
* LLM 相关：`judge_provider_id`、`judge_timeout_seconds`、`reply_timeout_seconds`
* 延迟与冷却：`max_delay_minutes`、`no_reply_cooldown_minutes`
* 插件联动：`dayflow_plugin_name`、`dailysharing_plugin_name`、`dailysharing_probe_count`、`inject_dayflow_schedule`
* 其他：`command_prefixes`、`save_conversation_history`

---
