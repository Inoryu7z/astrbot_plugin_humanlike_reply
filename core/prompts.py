"""提示词模板：判断LLM与对话LLM的追加任务说明（一小段）。

重要设计：
- dayflow 等插件通过 on_llm_request 钩子自动向 system_prompt 主体注入日程、存在感等信息，
  本模块不重复注入这些。
- 用户消息通过 provider.text_chat 的 prompt 参数传入（作为正常的 user turn）。
- 对话历史通过 contexts 参数传入。
- 本模块的追加提示词只包含"任务说明"这一小段：任务描述 + dailysharing任务时间 +
  决策规则 + 输出格式。

判断LLM的完整system_prompt = [人格提示词 + dayflow等钩子注入的日程] + JUDGE_TASK_ADDITION
对话LLM的完整system_prompt = [人格提示词 + dayflow等钩子注入的日程] + REPLY_TASK_ADDITION
"""

JUDGE_TASK_HEADER = "<HumanlikeReply-Judge-Task>"
JUDGE_TASK_FOOTER = "</HumanlikeReply-Judge-Task>"

REPLY_TASK_HEADER = "<HumanlikeReply-Reply-Task>"
REPLY_TASK_FOOTER = "</HumanlikeReply-Reply-Task>"


def build_judge_prompt_addition(
    dailysharing_info_text: str,
    max_delay_minutes: int,
    waiting_for_reply: bool,
    pending_delay_seconds: int,
) -> str:
    """构建判断LLM的追加任务说明（一小段）。

    用户消息通过 prompt 参数传入，历史通过 contexts 传入，日程由 dayflow 钩子注入。

    Args:
        dailysharing_info_text: Dailysharing下次任务信息文本，空字符串表示无
        max_delay_minutes: 允许的最大延迟分钟数
        waiting_for_reply: 当前是否已有待回复的延迟任务（用户在等待中又发了消息）
        pending_delay_seconds: 如果在等待，原定延迟还剩多少秒
    """
    dailysharing_section = (
        dailysharing_info_text.strip()
        if dailysharing_info_text and dailysharing_info_text.strip()
        else "（未检测到Dailysharing定时任务，无需考虑冲突）"
    )

    waiting_hint = ""
    if waiting_for_reply:
        remain_min = max(0, pending_delay_seconds // 60)
        remain_sec = pending_delay_seconds % 60
        waiting_hint = (
            f"\n⚠️ 用户正在等待：你之前已决定延迟回复，距原定回复还剩约 {remain_min}分{remain_sec}秒，"
            f"用户又发来新消息，可能表示急切。请重新评估是否改为立即回复。\n"
        )

    return f"""{JUDGE_TASK_HEADER}
你有一个额外任务：判断是否需要立刻回复用户的消息，并拟定一个回复方案。

你的日程、当前状态等信息已在上文注入（若有的话），请结合它判断你现在是否有空看消息。

## Dailysharing插件下次主动分享时间
{dailysharing_section}
（Dailysharing会自动向你所在会话推送内容。如果你延迟回复，延迟时间必须短于最近的分享剩余时间，否则你的回复会被抢先，用户消息被跳过无人理睬。）
{waiting_hint}
## 决策规则
1. 根据你的日程/状态判断是否有空：在忙（跳舞、上课、洗澡、睡觉等）则延迟或不回；有空则立即回。
2. 延迟时间必须短于Dailysharing最近的分享剩余时间，避免被抢先。
3. 用户表示紧急（"快"、"急"等）时即使忙碌也应尽快回复。
4. 拟定回复方案要符合人设，并体现当前状态（如"我刚才在跳舞，没看消息"）。
5. 延迟上限 {max_delay_minutes} 分钟。no_reply 仅在消息无需回应时使用。

## 输出格式（严格JSON，不要任何额外文本或代码块标记）
{{
  "decision": "immediate",
  "delay_seconds": 0,
  "reason": "简短决策理由",
  "draft_reply": "拟定的完整回复文案"
}}
- decision: "immediate" | "delay" | "no_reply"
- delay_seconds: 延迟秒数，仅delay有效，范围 1~{max_delay_minutes * 60}，其他填0
- draft_reply: 拟定的回复文案草稿，无论何种决策都必须提供
{JUDGE_TASK_FOOTER}"""


def build_reply_prompt_addition(
    draft_reply: str,
    decision_reason: str,
) -> str:
    """构建对话LLM的追加任务说明（一小段）。

    用户消息通过 prompt 参数传入，历史通过 contexts 传入。

    Args:
        draft_reply: 判断LLM拟定的回复方案
        decision_reason: 判断LLM的决策理由（供对话LLM了解上下文）
    """
    return f"""{REPLY_TASK_HEADER}
你即将回复用户。系统已为你拟定了一个回复方案，请审查并输出最终回复。

## 拟定的回复方案
{draft_reply}

## 决策背景
{decision_reason if decision_reason and decision_reason.strip() else '（无）'}

## 审查要点
1. 是否符合你的人设和性格语气
2. 是否符合事实（当前状态、日程、时间）
3. 语气是否自然像真人
4. 你可以自由判断是否需要优化：方案好就直接采用，有问题就修改。不要刻意改动。

## 输出要求
直接输出最终要发给用户的回复文案，不要包含任何解释、JSON、标记或额外说明。
{REPLY_TASK_FOOTER}"""
