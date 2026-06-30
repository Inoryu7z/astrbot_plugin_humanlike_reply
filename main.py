"""astrbot_plugin_humanlike_reply · 拟人回复节奏

让 AI 像真人一样把控回复节奏：
（v1.0.0）
1. 用户发消息后，判断LLM（人格提示词 + dayflow钩子注入 + 追加任务说明）决定是否
   立即回复 / 延迟X分钟回复 / 不回复，并拟定回复方案。
2. 判断LLM 会参考 Dailysharing 下次主动分享时间，避免延迟回复被抢先。
3. 用户在延迟期间发新消息会触发重新判断（可改为立即回复）。
4. 最终回复由对话LLM 审查判断LLM 拟定的方案后输出，保证人设与事实一致。

作者: Inoryu7z
"""

import asyncio
import json
import re
from typing import Any, Optional

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register

from .core.dailysharing_probe import DailySharingProbe, find_plugin_instance
from .core.llm_helper import (
    apply_on_llm_request_hooks,
    call_llm,
    call_llm_with_response,
    extract_image_urls,
    extract_usage_tokens,
    fire_on_llm_response_event,
    get_begin_dialogs,
    get_history_for_llm,
    get_persona_prompt,
    save_conversation,
)
from .core.prompts import build_judge_prompt_addition, build_reply_prompt_addition
from .core.session import SessionManager, SessionState
from .core.token_router_probe import TokenRouterProbe

__version__ = "1.0.3"

_DEFAULT_DRAFT_FALLBACK = "（系统未生成草稿，请直接回复用户）"

# v1.0.3 起以下配置项不再暴露给用户调节（保留代码默认行为，便于后续需要时恢复）
_DEFAULT_JUDGE_TIMEOUT = 30
_DEFAULT_REPLY_TIMEOUT = 120
_DEFAULT_DAILYSHARING_PROBE_COUNT = 3
_DEFAULT_COMMAND_PREFIXES = ["/"]
_DEFAULT_DAYFLOW_PLUGIN_NAME = "astrbot_plugin_dayflow_life_scheduler"
_DEFAULT_DAILYSHARING_PLUGIN_NAME = "astrbot_plugin_daily_sharing"
_DEFAULT_CHAT_PLUS_PLUGIN_NAME = "astrbot_plugin_group_chat_plus"
_DEFAULT_TOKEN_ROUTER_PLUGIN_NAME = "astrbot_plugin_token_router"


@register(
    "astrbot_plugin_humanlike_reply",
    "Inoryu7z",
    "让AI像真人一样把控回复节奏：判断LLM决定立即/延迟/不回复，由对话LLM审查后输出",
    __version__,
)
class HumanlikeReplyPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config

        # ---- 暴露给用户的配置项（7项） ----
        self.enable = bool(config.get("enable", True))
        self.judge_provider_id = str(config.get("judge_provider_id", "") or "")
        self.enable_private_chat = bool(config.get("enable_private_chat", True))
        self.enable_group_chat = bool(config.get("enable_group_chat", False))
        self.max_delay_minutes = int(config.get("max_delay_minutes", 30))
        self.no_reply_cooldown_minutes = float(config.get("no_reply_cooldown_minutes", 5.0))
        self.enable_token_router_integration = bool(config.get("enable_token_router_integration", True))

        # ---- 以下配置项不再暴露给用户（使用默认值） ----
        self.judge_timeout_seconds = _DEFAULT_JUDGE_TIMEOUT
        self.reply_timeout_seconds = _DEFAULT_REPLY_TIMEOUT
        self.inject_dayflow_schedule = True
        self.save_conversation_history = True
        self.debug = False
        self.dailysharing_probe_count = _DEFAULT_DAILYSHARING_PROBE_COUNT
        self.chat_plus_plugin_name = _DEFAULT_CHAT_PLUS_PLUGIN_NAME
        self.auto_yield_group_chat = True
        self.token_router_plugin_name = _DEFAULT_TOKEN_ROUTER_PLUGIN_NAME
        self.command_prefixes = list(_DEFAULT_COMMAND_PREFIXES)
        self._dayflow_plugin_name = _DEFAULT_DAYFLOW_PLUGIN_NAME
        dailysharing_name = _DEFAULT_DAILYSHARING_PLUGIN_NAME

        # 会话级开关：umo -> bool。优先级高于全局 self.enable
        # 由 /拟真开 /拟真关 命令动态控制，仅影响当前会话
        self._session_enabled: dict = {}

        self.session_mgr = SessionManager()
        self.session_mgr.set_delay_callback(self._on_delay_fire)

        self._dailysharing_probe = DailySharingProbe(context, dailysharing_name)

        # token_router 集成：让本插件的 LLM 调用能被 token_router 路由与计数
        if self.enable_token_router_integration:
            self._token_router_probe = TokenRouterProbe(context, self.token_router_plugin_name)
        else:
            self._token_router_probe = None

        # 检测ChatPlus冲突：若ChatPlus已加载且本插件开启群聊，自动让位
        self._check_chat_plus_conflict()

        logger.info(
            f"[HumanlikeReply] v{__version__} 加载 | "
            f"启用: {self.enable} | "
            f"私聊: {self.enable_private_chat} | 群聊: {self.enable_group_chat} | "
            f"最大延迟: {self.max_delay_minutes}min | "
            f"判断Provider: {self.judge_provider_id or '默认'} | "
            f"Dailysharing探测: {dailysharing_name or '禁用'} | "
            f"TokenRouter集成: {'启用' if self._token_router_probe else '禁用'}"
        )

    # ------------------------------------------------------------------
    # 命令：会话级开关
    # ------------------------------------------------------------------

    @filter.command("拟真开")
    async def cmd_enable_session(self, event: AstrMessageEvent):
        """临时开启当前会话的拟人回复（不影响其他会话与全局开关）。"""
        umo = event.unified_msg_origin
        self._session_enabled[umo] = True
        logger.info(f"[HumanlikeReply] /拟真开 umo={umo}（全局enable={self.enable}）")
        yield event.plain_result("✅ 拟人回复已开启（本窗口）")

    @filter.command("拟真关")
    async def cmd_disable_session(self, event: AstrMessageEvent):
        """临时关闭当前会话的拟人回复（不影响其他会话与全局开关）。"""
        umo = event.unified_msg_origin
        self._session_enabled[umo] = False
        logger.info(f"[HumanlikeReply] /拟真关 umo={umo}（全局enable={self.enable}）")
        yield event.plain_result("⏸️ 拟人回复已关闭（本窗口）")

    # ------------------------------------------------------------------
    # 工具方法
    # ------------------------------------------------------------------

    def _is_group_event(self, event: AstrMessageEvent) -> bool:
        try:
            return bool(getattr(event.message_obj, "group_id", ""))
        except Exception:
            return False

    def _is_command(self, text: str) -> bool:
        text = text.strip()
        if not text:
            return False
        for prefix in self.command_prefixes:
            if isinstance(prefix, str) and prefix and text.startswith(prefix):
                return True
        return False

    def _get_text(self, event: AstrMessageEvent) -> str:
        try:
            msg_obj = getattr(event, "message_obj", None)
            if msg_obj:
                t = getattr(msg_obj, "message_str", "") or ""
                if t:
                    return t
        except Exception:
            pass
        try:
            return event.message_str or ""
        except Exception:
            return ""

    def _dlog(self, msg: str) -> None:
        if self.debug:
            logger.debug(msg)

    @staticmethod
    def _safe_get_platform_name(event: AstrMessageEvent) -> str:
        """安全读取 event 的 platform_name，失败返回空字符串。"""
        try:
            return event.get_platform_name() or ""
        except Exception:
            return ""

    def _get_selected_provider(self, event: AstrMessageEvent) -> str:
        """读取 token_router 在 event 上设置的 selected_provider extra。无则返回空字符串。"""
        if not self._token_router_probe:
            return ""
        try:
            return str(event.get_extra("selected_provider") or "")
        except Exception:
            return ""

    @staticmethod
    def _parse_judge_response(text: str) -> dict:
        """解析判断LLM的JSON响应。容忍 markdown 代码块包裹。"""
        if not text:
            return {}
        cleaned = text.strip()
        # 去掉 markdown 代码块
        if cleaned.startswith("```"):
            lines = cleaned.split("\n")
            if lines and lines[0].startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].strip().startswith("```"):
                lines = lines[:-1]
            cleaned = "\n".join(lines).strip()
        # 尝试直接解析
        try:
            return json.loads(cleaned)
        except json.JSONDecodeError:
            pass
        # 尝试截取 { ... } 片段
        start = cleaned.find("{")
        end = cleaned.rfind("}")
        if start != -1 and end != -1 and end > start:
            try:
                return json.loads(cleaned[start : end + 1])
            except json.JSONDecodeError:
                pass
        return {}

    def _clamp_delay_seconds(self, delay_seconds: Any) -> int:
        """将判断LLM给出的延迟秒数约束到合法范围。"""
        try:
            delay = int(delay_seconds)
        except (TypeError, ValueError):
            return 0
        max_seconds = self.max_delay_minutes * 60
        if delay < 1:
            return 0
        if delay > max_seconds:
            return max_seconds
        return delay

    def _get_dailysharing_info_text(self) -> str:
        """获取Dailysharing下次任务信息文本。"""
        if not self._dailysharing_probe.plugin_name:
            return ""
        try:
            return self._dailysharing_probe.format_next_tasks_text(self.dailysharing_probe_count)
        except Exception as e:
            self._dlog(f"[HumanlikeReply] 探测Dailysharing任务失败: {e}")
            return ""

    def _check_chat_plus_conflict(self) -> None:
        """检测ChatPlus插件冲突：若ChatPlus已加载且本插件开启群聊，自动让位。

        原因：ChatPlus是群聊专用插件（AI读空气、概率筛选、拟人化等），
        本插件的on_message(priority=1, stop_event=True)会阻断ChatPlus的
        on_group_message(默认priority)执行。两者都有"是否回复"决策逻辑，
        同时启用群聊会导致双重决策、API浪费、行为不可预测。
        默认分工：ChatPlus处理群聊，本插件处理私聊。
        """
        if not self.auto_yield_group_chat:
            return
        if not self.enable_group_chat:
            return
        if not self.chat_plus_plugin_name:
            return

        try:
            plugin = find_plugin_instance(self.context, self.chat_plus_plugin_name)
        except Exception as e:
            self._dlog(f"[HumanlikeReply] 检测ChatPlus失败: {e}")
            return

        if plugin is None:
            return

        # ChatPlus已加载且本插件开了群聊 → 自动让位
        logger.warning(
            f"[HumanlikeReply] 检测到ChatPlus插件('{self.chat_plus_plugin_name}')已加载，"
            f"且本插件enable_group_chat=True。ChatPlus是群聊专用插件，"
            f"与本插件功能重叠（都有是否回复决策）。自动让位群聊处理给ChatPlus，"
            f"本插件仅处理私聊。如需强制共存请关闭auto_yield_group_chat_to_chat_plus（不推荐）。"
        )
        self.enable_group_chat = False

    async def _build_system_prompt(
        self,
        event: AstrMessageEvent,
        uid: str,
        session_id: str,
        addition: str,
    ) -> str:
        """构建完整system_prompt: 人格提示词 + on_llm_request钩子注入 + 追加任务说明。"""
        persona_prompt = await get_persona_prompt(self.context, uid)

        if self.inject_dayflow_schedule:
            injected = await apply_on_llm_request_hooks(event, persona_prompt, session_id)
        else:
            injected = persona_prompt

        if addition:
            if injected and not injected.endswith("\n"):
                return injected + "\n\n" + addition
            return injected + addition
        return injected

    # ------------------------------------------------------------------
    # 主流程：消息拦截
    # ------------------------------------------------------------------

    @filter.event_message_type(filter.EventMessageType.ALL, priority=1)
    async def on_message(self, event: AstrMessageEvent):
        # 会话级开关优先于全局 enable：umo 在 _session_enabled 中时用其值，否则用 self.enable
        uid = event.unified_msg_origin
        if uid in self._session_enabled:
            if not self._session_enabled[uid]:
                return
        elif not self.enable:
            return

        is_group = self._is_group_event(event)
        if is_group and not self.enable_group_chat:
            return
        if not is_group and not self.enable_private_chat:
            return

        text = self._get_text(event)
        image_urls = extract_image_urls(event)

        # 指令直接放行
        if self._is_command(text):
            return
        # 空消息放行（不应发生，但兜底）
        if not text.strip() and not image_urls:
            return

        # token_router 集成：手动触发其 on_message，让其设置 selected_provider extra
        # 原因：本插件 priority=1 先于 token_router(9999) 执行并 stop_event，
        # 导致 token_router.on_message 永不触发。这里手动调用以弥补。
        if self._token_router_probe is not None:
            await self._token_router_probe.trigger_on_message(event)

        state = self.session_mgr.get_state(uid)

        # 持有会话锁进行整个判断→回复流程，保证同一会话串行处理
        async with state.lock:
            # 取消已有延迟任务（用户发新消息，需重新判断）
            was_pending = state.pending is not None
            remaining_seconds = self.session_mgr.get_remaining_seconds(state) if was_pending else 0
            if was_pending:
                self.session_mgr.cancel_pending(state)
                logger.info(
                    f"[HumanlikeReply] uid={uid} 在延迟期间发新消息，"
                    f"取消旧任务（剩余{remaining_seconds}s）重新判断"
                )

            # no_reply 冷却期：静默丢弃（不累积，避免冷却期消息泄漏到下次判断）
            if self.session_mgr.is_in_no_reply_cooldown(state):
                self._dlog(f"uid={uid} 处于no_reply冷却，静默丢弃")
                event.stop_event()
                return

            # 累积本条消息
            state.accumulated_messages.append(
                {
                    "text": text,
                    "image_urls": list(image_urls),
                    "umo": uid,
                }
            )

            # 合并所有累积消息
            snapshot = list(state.accumulated_messages)
            combined_text = "\n".join(m["text"] for m in snapshot if m["text"].strip())
            combined_images: list = []
            for m in snapshot:
                combined_images.extend(m["image_urls"])

            # ---- 调用判断LLM ----
            judge_result = await self._call_judge(
                event=event,
                uid=uid,
                session_id=uid,
                user_text=combined_text,
                image_urls=combined_images,
                waiting_for_reply=was_pending,
                pending_delay_seconds=remaining_seconds,
            )

            if judge_result is None:
                # 判断失败：兜底立即回复（不审查直接用LLM生成）
                logger.warning(f"uid={uid} 判断LLM失败，兜底立即回复")
                state.accumulated_messages.clear()
                event.stop_event()
                await self._fallback_reply(uid, uid, combined_text, combined_images, event)
                return

            decision = str(judge_result.get("decision", "immediate")).lower().strip()
            delay_seconds = self._clamp_delay_seconds(judge_result.get("delay_seconds", 0))
            draft_reply = str(judge_result.get("draft_reply", "") or "").strip()
            reason = str(judge_result.get("reason", "") or "").strip()

            self._dlog(
                f"uid={uid} 判断结果 decision={decision} delay={delay_seconds}s "
                f"reason={reason!r} draft_len={len(draft_reply)}"
            )

            if decision == "no_reply":
                state.accumulated_messages.clear()
                cooldown = self.no_reply_cooldown_minutes * 60.0
                self.session_mgr.set_no_reply_cooldown(state, cooldown)
                logger.info(f"uid={uid} 判断为no_reply，进入冷却{self.no_reply_cooldown_minutes}min")
                event.stop_event()
                return

            if decision == "delay" and delay_seconds > 0:
                # 校验延迟不能超过 dailysharing 最近任务剩余时间（避免被抢先）
                delay_seconds = self._cap_delay_by_dailysharing(delay_seconds)
                reply_context = {
                    "session_id": uid,
                    "umo": uid,
                    "user_text": combined_text,
                    "image_urls": combined_images,
                    "event_uid": uid,
                    # 缓存 token_router 集成所需信息（延迟回复时无 event 可用）
                    "selected_provider": event.get_extra("selected_provider") if self._token_router_probe else None,
                    "platform_name": self._safe_get_platform_name(event) if self._token_router_probe else "",
                }
                self.session_mgr.schedule_delayed_reply(
                    uid=uid,
                    delay_seconds=delay_seconds,
                    draft_reply=draft_reply or _DEFAULT_DRAFT_FALLBACK,
                    decision_reason=reason,
                    reply_context=reply_context,
                )
                logger.info(
                    f"uid={uid} 判断为delay，延迟{delay_seconds}s后回复 "
                    f"(draft_len={len(draft_reply)})"
                )
                event.stop_event()
                return

            # immediate 或 delay 但被夹到 0：立即回复
            state.accumulated_messages.clear()
            event.stop_event()
            await self._do_immediate_reply(
                uid=uid,
                session_id=uid,
                user_text=combined_text,
                image_urls=combined_images,
                draft_reply=draft_reply,
                decision_reason=reason,
                event=event,
            )

    def _cap_delay_by_dailysharing(self, delay_seconds: int) -> int:
        """如果Dailysharing最近任务剩余时间小于延迟，把延迟夹到剩余时间-60s。"""
        try:
            nearest = self._dailysharing_probe.nearest_seconds_until()
        except Exception:
            nearest = None
        if nearest is None or nearest <= 0:
            return delay_seconds
        # 留 60s 缓冲，避免恰好同时触发
        safe_limit = max(1, int(nearest) - 60)
        if delay_seconds >= safe_limit:
            logger.info(
                f"[HumanlikeReply] 延迟{delay_seconds}s超过Dailysharing最近任务剩余"
                f"{int(nearest)}s，夹到{safe_limit}s"
            )
            return safe_limit
        return delay_seconds

    # ------------------------------------------------------------------
    # 判断LLM 调用
    # ------------------------------------------------------------------

    async def _call_judge(
        self,
        event: AstrMessageEvent,
        uid: str,
        session_id: str,
        user_text: str,
        image_urls: list,
        waiting_for_reply: bool,
        pending_delay_seconds: int,
    ) -> Optional[dict]:
        """调用判断LLM，返回解析后的dict，失败返回None。"""
        # 1. dailysharing 任务信息
        dailysharing_text = self._get_dailysharing_info_text()

        # 2. 追加任务说明
        addition = build_judge_prompt_addition(
            dailysharing_info_text=dailysharing_text,
            max_delay_minutes=self.max_delay_minutes,
            waiting_for_reply=waiting_for_reply,
            pending_delay_seconds=pending_delay_seconds,
        )

        # 3. 完整 system_prompt: 人格 + 钩子注入 + 追加
        system_prompt = await self._build_system_prompt(event, uid, session_id, addition)

        # 4. 上下文: begin_dialogs + 历史
        begin_dialogs = await get_begin_dialogs(self.context, uid)
        contexts = await get_history_for_llm(self.context, uid, begin_dialogs)

        # 5. 调用LLM（用户消息作为 prompt）
        raw = await call_llm(
            context=self.context,
            uid=uid,
            system_prompt=system_prompt,
            user_prompt=user_text if user_text.strip() else "（用户发送了图片或空消息）",
            image_urls=image_urls,
            contexts=contexts,
            provider_id=self.judge_provider_id,
            timeout=self.judge_timeout_seconds,
        )
        if not raw:
            return None

        result = self._parse_judge_response(raw)
        if not result or "decision" not in result:
            logger.warning(f"uid={uid} 判断LLM返回无法解析: {raw[:200]}")
            return None
        return result

    # ------------------------------------------------------------------
    # 立即回复流程
    # ------------------------------------------------------------------

    async def _do_immediate_reply(
        self,
        uid: str,
        session_id: str,
        user_text: str,
        image_urls: list,
        draft_reply: str,
        decision_reason: str,
        event: AstrMessageEvent,
    ) -> None:
        """立即回复：对话LLM审查draft后输出最终回复，然后发送+保存。"""
        final_reply = await self._conversation_llm_review(
            event=event,
            uid=uid,
            session_id=session_id,
            user_text=user_text,
            image_urls=image_urls,
            draft_reply=draft_reply or _DEFAULT_DRAFT_FALLBACK,
            decision_reason=decision_reason,
        )
        if not final_reply:
            final_reply = draft_reply or ""
        if not final_reply:
            final_reply = "……"

        # 发送
        await self._send_reply_via_event(event, final_reply)

        # 保存对话历史
        if self.save_conversation_history:
            try:
                await save_conversation(self.context, uid, user_text, final_reply, image_urls)
            except Exception as e:
                logger.warning(f"uid={uid} 保存对话历史失败: {e}")

    async def _conversation_llm_review(
        self,
        event: AstrMessageEvent,
        uid: str,
        session_id: str,
        user_text: str,
        image_urls: list,
        draft_reply: str,
        decision_reason: str,
    ) -> str:
        """对话LLM审查draft并输出最终回复。失败时返回draft_reply。

        v1.0.2 起：
        - 优先使用 token_router 在 event 上设置的 selected_provider 作为对话LLM的 provider
          （让 token_router 的路由链对本插件生效）
        - LLM 响应后手动触发 OnLLMResponseEvent 钩子，让 token_router 记录 token 用量
        """
        addition = build_reply_prompt_addition(
            draft_reply=draft_reply,
            decision_reason=decision_reason,
        )
        system_prompt = await self._build_system_prompt(event, uid, session_id, addition)
        begin_dialogs = await get_begin_dialogs(self.context, uid)
        contexts = await get_history_for_llm(self.context, uid, begin_dialogs)

        # 对话LLM用会话默认provider；若 token_router 设置了 selected_provider 则优先用它
        provider_id = self._get_selected_provider(event)

        result = await call_llm_with_response(
            context=self.context,
            uid=uid,
            system_prompt=system_prompt,
            user_prompt=user_text if user_text.strip() else "（用户发送了图片或空消息）",
            image_urls=image_urls,
            contexts=contexts,
            provider_id=provider_id,
            timeout=self.reply_timeout_seconds,
        )
        if result is None:
            logger.warning(f"uid={uid} 对话LLM审查失败，使用draft作为最终回复")
            return draft_reply

        reply, response = result

        # 手动触发 OnLLMResponseEvent，让 token_router 等插件记录 token 用量
        # （本插件绕过 Pipeline，原生钩子不会自动触发）
        if self._token_router_probe is not None:
            try:
                await fire_on_llm_response_event(event, response)
            except Exception as e:
                self._dlog(f"uid={uid} 触发 OnLLMResponseEvent 失败: {e}")

        if not reply:
            logger.warning(f"uid={uid} 对话LLM审查返回空，使用draft作为最终回复")
            return draft_reply
        return reply

    async def _fallback_reply(
        self,
        uid: str,
        session_id: str,
        user_text: str,
        image_urls: list,
        event: AstrMessageEvent,
    ) -> None:
        """判断LLM失败时的兜底：直接用对话LLM生成回复（无draft）。

        v1.0.2 起同步立即回复路径：优先用 token_router 设置的 selected_provider，
        并在响应后手动触发 OnLLMResponseEvent 让 token_router 记录用量。
        """
        try:
            persona_prompt = await get_persona_prompt(self.context, uid)
            if self.inject_dayflow_schedule:
                system_prompt = await apply_on_llm_request_hooks(event, persona_prompt, session_id)
            else:
                system_prompt = persona_prompt
            begin_dialogs = await get_begin_dialogs(self.context, uid)
            contexts = await get_history_for_llm(self.context, uid, begin_dialogs)

            provider_id = self._get_selected_provider(event)
            result = await call_llm_with_response(
                context=self.context,
                uid=uid,
                system_prompt=system_prompt,
                user_prompt=user_text if user_text.strip() else "（用户发送了图片或空消息）",
                image_urls=image_urls,
                contexts=contexts,
                provider_id=provider_id,
                timeout=self.reply_timeout_seconds,
            )
            if result is None:
                final_reply = "（我暂时无法回复）"
            else:
                reply, response = result
                final_reply = reply or "（我暂时无法回复）"
                if self._token_router_probe is not None:
                    try:
                        await fire_on_llm_response_event(event, response)
                    except Exception as e:
                        self._dlog(f"uid={uid} 兜底路径触发 OnLLMResponseEvent 失败: {e}")
        except Exception as e:
            logger.error(f"uid={uid} 兜底回复失败: {e}")
            final_reply = "……"

        await self._send_reply_via_event(event, final_reply)
        if self.save_conversation_history:
            try:
                await save_conversation(self.context, uid, user_text, final_reply, image_urls)
            except Exception as e:
                logger.warning(f"uid={uid} 保存兜底对话历史失败: {e}")

    # ------------------------------------------------------------------
    # 延迟回复回调
    # ------------------------------------------------------------------

    async def _on_delay_fire(
        self,
        uid: str,
        reply_context: dict,
        draft_reply: str,
        decision_reason: str,
    ) -> None:
        """延迟任务触发时的回调：用对话LLM审查draft并发送。"""
        umo = reply_context.get("umo") or uid
        user_text = reply_context.get("user_text", "")
        image_urls = reply_context.get("image_urls", []) or []
        cached_provider = reply_context.get("selected_provider") if self._token_router_probe else None
        platform_name = reply_context.get("platform_name") if self._token_router_probe else ""

        logger.info(
            f"[HumanlikeReply] 延迟回复触发 uid={uid} draft_len={len(draft_reply)}"
        )

        # 延迟回复无法复用原event（可能已失效），通过 context.send_message 发送
        final_reply = await self._conversation_llm_review_no_event(
            uid=uid,
            session_id=uid,
            umo=umo,
            user_text=user_text,
            image_urls=image_urls,
            draft_reply=draft_reply,
            decision_reason=decision_reason,
            cached_selected_provider=cached_provider,
            platform_name=platform_name,
        )
        if not final_reply:
            final_reply = draft_reply or "……"

        await self._send_reply_via_umo(umo, final_reply)

        if self.save_conversation_history:
            try:
                await save_conversation(self.context, uid, user_text, final_reply, image_urls)
            except Exception as e:
                logger.warning(f"uid={uid} 保存延迟回复对话历史失败: {e}")

    async def _conversation_llm_review_no_event(
        self,
        uid: str,
        session_id: str,
        umo: str,
        user_text: str,
        image_urls: list,
        draft_reply: str,
        decision_reason: str,
        cached_selected_provider: Optional[str] = None,
        platform_name: str = "",
    ) -> str:
        """延迟回调专用的对话LLM审查：无event对象，只读取人格+钩子注入。

        由于on_llm_request钩子需要event参数，这里event传None。多数插件会检查event
        类型，None可能导致跳过，这是可接受的降级。

        v1.0.2 起 token_router 集成（无 event 路径）：
        1. provider 选择优先级：cached_selected_provider > probe.get_active_provider > 默认
           （cached 是延迟前 token_router.on_message 设置的；probe 重新查询以应对延迟
           期间路由链切换的情况）
        2. 响应后用 probe.record_usage 直接调 token_router._record_usage 记录用量
           （无 event 不能用 fire_on_llm_response_event）
        """
        addition = build_reply_prompt_addition(
            draft_reply=draft_reply,
            decision_reason=decision_reason,
        )
        persona_prompt = await get_persona_prompt(self.context, uid)

        if self.inject_dayflow_schedule:
            # 尝试触发钩子；event为None时dayflow等插件可能跳过（取决于其实现）
            # 多数插件会检查event类型，None可能导致跳过，这是可接受的降级
            try:
                injected = await apply_on_llm_request_hooks(None, persona_prompt, session_id)
            except Exception as e:
                self._dlog(f"uid={uid} 延迟回调触发on_llm_request钩子失败: {e}")
                injected = persona_prompt
        else:
            injected = persona_prompt

        if addition:
            if injected and not injected.endswith("\n"):
                system_prompt = injected + "\n\n" + addition
            else:
                system_prompt = injected + addition
        else:
            system_prompt = injected

        begin_dialogs = await get_begin_dialogs(self.context, uid)
        contexts = await get_history_for_llm(self.context, uid, begin_dialogs)

        # ---- token_router 集成：解析 provider_id 和 persona_id ----
        provider_id = ""
        persona_id_for_record = None
        if self._token_router_probe is not None:
            # 1. 解析 persona_id（用于直接调用 _record_usage）
            try:
                persona_id_for_record = await self._token_router_probe.resolve_persona_id(umo, platform_name)
            except Exception as e:
                self._dlog(f"uid={uid} 延迟回调解析 persona_id 失败: {e}")
                persona_id_for_record = None

            # 2. provider 选择：优先用缓存，否则重新查询路由链
            if cached_selected_provider:
                provider_id = cached_selected_provider
            else:
                queried = self._token_router_probe.get_active_provider(umo, persona_id_for_record)
                if queried:
                    provider_id = queried

        result = await call_llm_with_response(
            context=self.context,
            uid=uid,
            system_prompt=system_prompt,
            user_prompt=user_text if user_text.strip() else "（用户发送了图片或空消息）",
            image_urls=image_urls,
            contexts=contexts,
            provider_id=provider_id,
            timeout=self.reply_timeout_seconds,
        )
        if result is None:
            logger.warning(f"uid={uid} 延迟回复对话LLM审查失败，使用draft")
            return draft_reply

        reply, response = result

        # 直接调用 token_router._record_usage 记录用量（无 event，无法触发钩子）
        if self._token_router_probe is not None and provider_id:
            try:
                usage_tokens = extract_usage_tokens(response)
                if usage_tokens > 0:
                    self._token_router_probe.record_usage(
                        umo=umo,
                        persona_id=persona_id_for_record,
                        provider_id=provider_id,
                        usage_tokens=usage_tokens,
                    )
            except Exception as e:
                self._dlog(f"uid={uid} 延迟回调记录 token_router 用量失败: {e}")

        if not reply:
            logger.warning(f"uid={uid} 延迟回复对话LLM审查返回空，使用draft")
            return draft_reply
        return reply

    # ------------------------------------------------------------------
    # 消息发送
    # ------------------------------------------------------------------

    async def _send_reply_via_event(self, event: AstrMessageEvent, text: str) -> None:
        """通过原event发送回复（立即回复场景）。"""
        try:
            await event.send(event.plain_result(text))
        except Exception as e:
            logger.error(f"[HumanlikeReply] 通过event发送失败: {e}")
            # 尝试通过umo兜底
            try:
                umo = event.unified_msg_origin
                await self._send_reply_via_umo(umo, text)
            except Exception as e2:
                logger.error(f"[HumanlikeReply] umo兜底发送也失败: {e2}")

    async def _send_reply_via_umo(self, umo: str, text: str) -> None:
        """通过 context.send_message 发送（延迟回复场景）。参考 daymind proactive_chat。"""
        try:
            from astrbot.core.message.message_event_result import MessageChain
            from astrbot.core.message.components import Plain
        except ImportError:
            logger.error("[HumanlikeReply] 无法导入 MessageChain/Plain，发送失败")
            return

        try:
            chain = [Plain(text=text)]
            message_chain = MessageChain(chain)
            await self.context.send_message(umo, message_chain)
        except Exception as e:
            logger.error(f"[HumanlikeReply] send_message失败 umo={umo}: {e}")

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    async def terminate(self) -> None:
        self.session_mgr.cleanup_all()
        logger.info("[HumanlikeReply] 插件已卸载，所有会话状态已清理")
