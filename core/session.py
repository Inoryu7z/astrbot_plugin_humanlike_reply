"""会话状态管理：延迟任务调度、催促重判、no_reply 冷却。

设计要点：
- 每个会话（uid）一份 SessionState，包含 lock / accumulated_messages / pending / no_reply_until
- main.py 持有 session.lock 进行整个流程（判断→回复/延迟），保证同一会话串行处理，
  避免并发消息导致重复回复或丢失消息
- 延迟任务通过 asyncio.create_task + asyncio.sleep 实现，可取消
- 用户在延迟期间发新消息：main.py 取消 pending 任务并重新判断（waiting_for_reply=True）
- no_reply 冷却：判断 LLM 选择不回复后进入冷却期，期间消息被静默丢弃
- 延迟任务触发时，原子地"消费" accumulated_messages（清空并交给回调），避免与新消息竞态
"""

import asyncio
import time
from dataclasses import dataclass, field
from typing import Optional, Callable, Awaitable, Any

from astrbot.api import logger


@dataclass
class PendingState:
    """延迟回复的待处理状态。"""
    fire_at_monotonic: float
    started_at_monotonic: float
    delay_seconds_original: int
    draft_reply: str
    decision_reason: str
    reply_context: dict = field(default_factory=dict)
    task: Optional[asyncio.Task] = None


@dataclass
class SessionState:
    """单会话状态。

    Attributes:
        lock: 会话级锁，main.py 持有此锁进行整个判断→回复流程
        accumulated_messages: 累积的未回复消息列表，每项为 dict(text, image_urls, umo, session_id)
        pending: 当前延迟任务状态，None 表示无待处理延迟
        no_reply_until_monotonic: no_reply 冷却到期时间（monotonic）
    """
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    accumulated_messages: list = field(default_factory=list)
    pending: Optional[PendingState] = None
    no_reply_until_monotonic: float = 0.0


class SessionManager:
    """会话状态管理器。"""

    def __init__(self):
        self._states: dict[str, SessionState] = {}
        self._delay_callback: Optional[Callable[[str, dict, str, str], Awaitable[None]]] = None

    def set_delay_callback(
        self,
        callback: Callable[[str, dict, str, str], Awaitable[None]],
    ) -> None:
        """注册延迟触发时的回调（由 main.py 设置）。

        callback signature:
            async def callback(
                uid: str,
                reply_context: dict,
                draft_reply: str,
                decision_reason: str,
            ) -> None
        """
        self._delay_callback = callback

    def get_state(self, uid: str) -> SessionState:
        if uid not in self._states:
            self._states[uid] = SessionState()
        return self._states[uid]

    def cancel_pending(self, state: SessionState) -> Optional[PendingState]:
        """取消会话的待处理延迟任务。返回被取消的 PendingState（如有）。"""
        if state.pending is None:
            return None
        pending = state.pending
        state.pending = None
        if pending.task is not None and not pending.task.done():
            pending.task.cancel()
        return pending

    def is_in_no_reply_cooldown(self, state: SessionState) -> bool:
        return time.monotonic() < state.no_reply_until_monotonic

    def set_no_reply_cooldown(self, state: SessionState, cooldown_seconds: float) -> None:
        state.no_reply_until_monotonic = time.monotonic() + max(0.0, cooldown_seconds)

    def get_remaining_seconds(self, state: SessionState) -> int:
        """如果当前有 pending 任务，返回距触发还剩多少秒；否则返回 0。"""
        if state.pending is None:
            return 0
        remaining = state.pending.fire_at_monotonic - time.monotonic()
        return max(0, int(remaining))

    def schedule_delayed_reply(
        self,
        uid: str,
        delay_seconds: int,
        draft_reply: str,
        decision_reason: str,
        reply_context: dict,
    ) -> PendingState:
        """调度延迟回复任务。会先取消已有任务。"""
        state = self.get_state(uid)
        self.cancel_pending(state)

        now_mono = time.monotonic()
        pending = PendingState(
            fire_at_monotonic=now_mono + delay_seconds,
            started_at_monotonic=now_mono,
            delay_seconds_original=delay_seconds,
            draft_reply=draft_reply,
            decision_reason=decision_reason,
            reply_context=reply_context,
        )
        task = asyncio.create_task(self._delayed_task(uid, pending))
        pending.task = task
        state.pending = pending
        logger.debug(
            f"[HumanlikeReply] 已调度延迟回复 uid={uid} delay={delay_seconds}s "
            f"draft_len={len(draft_reply)}"
        )
        return pending

    async def _delayed_task(self, uid: str, pending: PendingState) -> None:
        """延迟任务执行体：sleep 后触发回调。"""
        try:
            await asyncio.sleep(pending.delay_seconds_original)
        except asyncio.CancelledError:
            return

        state = self._states.get(uid)
        if state is None:
            return

        consumed_messages: list = []
        async with state.lock:
            # 验证仍是当前 pending（可能已被新消息取消并替换）
            if state.pending is not pending:
                logger.debug(
                    f"[HumanlikeReply] 延迟任务触发但 pending 已被替换 uid={uid}，放弃回调"
                )
                return
            state.pending = None
            consumed_messages = list(state.accumulated_messages)
            state.accumulated_messages.clear()

        pending.reply_context["consumed_messages"] = consumed_messages

        if self._delay_callback is None:
            logger.warning("[HumanlikeReply] 延迟回调未注册，无法完成回复")
            return

        try:
            await self._delay_callback(
                uid,
                pending.reply_context,
                pending.draft_reply,
                pending.decision_reason,
            )
        except Exception as e:
            logger.error(f"[HumanlikeReply] 延迟回复回调失败 uid={uid}: {e}")

    def cleanup_session(self, uid: str) -> None:
        """清理单个会话状态。"""
        state = self._states.pop(uid, None)
        if state and state.pending and state.pending.task:
            state.pending.task.cancel()

    def cleanup_all(self) -> None:
        """清理所有会话状态（插件卸载时调用）。"""
        for uid in list(self._states.keys()):
            self.cleanup_session(uid)
