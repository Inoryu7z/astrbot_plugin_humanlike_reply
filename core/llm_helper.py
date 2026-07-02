"""LLM调用辅助：获取人格提示词、手动触发on_llm_request钩子、调用LLM、保存对话历史。

核心设计：判断LLM和对话LLM都绕过Pipeline直接调用provider.text_chat()，
但在此之前手动触发on_llm_request钩子，让dayflow等插件注入日程/存在感等信息
到system_prompt主体。然后追加本插件的任务说明（一小段），组成完整system_prompt。

v1.0.2 起，对话LLM路径还会手动触发 on_llm_response 钩子，让 token_router 等依赖
该钩子的插件能正确记录 token 用量（本插件绕过 Pipeline，原生钩子不会自动触发）。
"""

import json
from typing import Optional, Tuple

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent
from astrbot.api.message_components import Image


_MAX_CONTEXT_MESSAGES = 20


def extract_image_urls(event: AstrMessageEvent) -> list:
    """从事件中提取图片URL列表。参考 premerger 的实现。"""
    urls: list = []
    try:
        if hasattr(event, "message_obj") and hasattr(event.message_obj, "message"):
            for comp in event.message_obj.message:
                if isinstance(comp, Image):
                    url = getattr(comp, "url", None) or getattr(comp, "file", None)
                    if url:
                        urls.append(url)
    except Exception as e:
        logger.debug(f"[HumanlikeReply] 提取图片URL失败: {e}")
    return urls


async def get_persona_prompt(context, uid: str) -> str:
    """获取人格提示词（system_prompt主体）。"""
    try:
        persona = await context.persona_manager.get_default_persona_v3(uid)
        return persona.get("prompt", "") or ""
    except Exception as e:
        logger.debug(f"[HumanlikeReply] 获取persona失败: {e}")
        return ""


async def get_begin_dialogs(context, uid: str) -> list:
    """获取人格的 begin_dialogs（开场白上下文）。"""
    try:
        persona = await context.persona_manager.get_default_persona_v3(uid)
        return persona.get("_begin_dialogs_processed", []) or []
    except Exception:
        return []


async def apply_on_llm_request_hooks(event: AstrMessageEvent, system_prompt: str, session_id: str) -> str:
    """手动触发on_llm_request钩子，让dayflow等插件注入信息到system_prompt。

    参考proactive_chat的_apply_on_llm_request_hooks实现，但复用原始event，
    无需构造假的AstrBotMessage。dayflow等插件会修改req.system_prompt。

    Args:
        event: 原始消息事件（event_message_type handler中获取的）
        system_prompt: 初始system_prompt（人格提示词）
        session_id: 会话ID

    Returns:
        注入后的system_prompt。失败时返回原始system_prompt。
    """
    try:
        from astrbot.core.provider.entities import ProviderRequest
        from astrbot.core.star.star_handler import EventType, star_handlers_registry
    except ImportError:
        logger.debug("[HumanlikeReply] 无法导入ProviderRequest或star_handler，跳过钩子触发")
        return system_prompt

    try:
        handlers = star_handlers_registry.get_handlers_by_event_type(
            EventType.OnLLMRequestEvent,
        )
    except Exception as e:
        logger.debug(f"[HumanlikeReply] 获取OnLLMRequestEvent处理器失败: {e}")
        return system_prompt

    if not handlers:
        return system_prompt

    try:
        req = ProviderRequest()
        req.session_id = session_id
        req.system_prompt = system_prompt
        # prompt 留空，本插件调用LLM时不需要钩子处理prompt
    except Exception as e:
        logger.warning(f"[HumanlikeReply] 构造ProviderRequest失败: {e}")
        return system_prompt

    hook_count = 0
    for handler in handlers:
        try:
            await handler.handler(event, req)
            hook_count += 1
        except Exception as e:
            handler_name = getattr(handler, "handler_full_name", "unknown")
            logger.debug(f"[HumanlikeReply] on_llm_request钩子执行失败 [{handler_name}]: {e}")

    if hook_count > 0:
        logger.debug(
            f"[HumanlikeReply] 已触发 {hook_count} 个on_llm_request钩子，"
            f"system_prompt长度: {len(system_prompt)} → {len(req.system_prompt or '')}"
        )

    return req.system_prompt or system_prompt


async def fire_on_llm_response_event(event, response) -> None:
    """手动触发 OnLLMResponseEvent 钩子，让 token_router 等插件记录 token 用量。

    本插件绕过 Pipeline 直接调 `provider.text_chat()`，导致 OnLLMResponseEvent
    不会自动触发。本方法手动调用所有注册了 OnLLMResponseEvent 的处理器。

    与 `apply_on_llm_request_hooks` 对称：前者补 on_llm_request，本方法补 on_llm_response。

    Args:
        event: 原始消息事件（延迟回复路径下可能为 None，此时跳过触发）
        response: `provider.text_chat()` 返回的 LLMResponse 对象
    """
    if event is None:
        return
    try:
        from astrbot.core.star.star_handler import EventType, star_handlers_registry
    except ImportError:
        logger.debug("[HumanlikeReply] 无法导入 star_handler，跳过 OnLLMResponse 钩子触发")
        return

    try:
        handlers = star_handlers_registry.get_handlers_by_event_type(
            EventType.OnLLMResponseEvent,
        )
    except Exception as e:
        logger.debug(f"[HumanlikeReply] 获取 OnLLMResponseEvent 处理器失败: {e}")
        return

    if not handlers:
        return

    hook_count = 0
    for handler in handlers:
        try:
            await handler.handler(event, response)
            hook_count += 1
        except Exception as e:
            handler_name = getattr(handler, "handler_full_name", "unknown")
            logger.debug(f"[HumanlikeReply] on_llm_response 钩子执行失败 [{handler_name}]: {e}")

    if hook_count > 0:
        logger.debug(f"[HumanlikeReply] 已触发 {hook_count} 个 on_llm_response 钩子")


def extract_usage_tokens(response) -> int:
    """从 LLMResponse 对象提取 token 用量。失败返回 0。"""
    if response is None:
        return 0
    usage = getattr(response, "usage", None)
    if usage is None:
        return 0
    total = getattr(usage, "total", None)
    if isinstance(total, int):
        return total
    return 0


async def get_history_for_llm(context, uid: str, begin_dialogs: list) -> list:
    """获取对话历史列表（供provider.text_chat的contexts参数）。参考premerger。"""
    contexts: list = []
    try:
        if begin_dialogs:
            contexts.extend(begin_dialogs)
        conv_mgr = getattr(context, "conversation_manager", None)
        if not conv_mgr:
            return contexts
        curr_cid = await conv_mgr.get_curr_conversation_id(uid)
        if not curr_cid:
            return contexts
        conversation = await conv_mgr.get_conversation(uid, curr_cid)
        if not conversation or not hasattr(conversation, "history"):
            return contexts
        history = conversation.history
        if isinstance(history, str):
            try:
                history = json.loads(history)
            except Exception:
                return contexts
        if isinstance(history, list):
            history = history[-_MAX_CONTEXT_MESSAGES:]
            for msg in history:
                if isinstance(msg, dict):
                    role = msg.get("role", "")
                    content = msg.get("content", "")
                    if role and content:
                        contexts.append({"role": role, "content": content})
    except Exception as e:
        logger.debug(f"[HumanlikeReply] 读取对话历史失败: {e}")
    return contexts


def _resolve_provider(context, uid: str, provider_id: str = ""):
    """解析LLM provider。优先用指定的provider_id，否则用会话默认provider。"""
    if provider_id:
        try:
            provider = context.get_provider_by_id(provider_id)
            if provider:
                return provider
            logger.warning(f"[HumanlikeReply] 指定的provider '{provider_id}' 不存在，回退到默认provider")
        except Exception as e:
            logger.warning(f"[HumanlikeReply] 获取provider '{provider_id}' 失败: {e}")
    return context.get_using_provider(uid)


async def call_llm(
    context,
    uid: str,
    system_prompt: str,
    user_prompt: str,
    image_urls: list = None,
    contexts: list = None,
    provider_id: str = "",
    timeout: int = 120,
) -> Optional[str]:
    """调用LLM（绕过Pipeline，直接provider.text_chat）。

    Args:
        system_prompt: 完整system_prompt（人格+钩子注入+任务说明）
        user_prompt: 用户消息
        image_urls: 图片URL列表
        contexts: 对话历史上下文
        provider_id: 指定provider，空则用会话默认
        timeout: 超时秒数

    Returns:
        LLM回复文本，失败返回None
    """
    result = await call_llm_with_response(
        context=context,
        uid=uid,
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        image_urls=image_urls,
        contexts=contexts,
        provider_id=provider_id,
        timeout=timeout,
    )
    if result is None:
        return None
    return result[0]


async def call_llm_with_response(
    context,
    uid: str,
    system_prompt: str,
    user_prompt: str,
    image_urls: list = None,
    contexts: list = None,
    provider_id: str = "",
    timeout: int = 120,
) -> Optional[Tuple[str, object]]:
    """调用LLM并返回完整响应对象（含 usage 字段，供 token_router 记录用）。

    与 `call_llm` 的区别：本方法返回 `(reply_text, response)` 元组，
    调用方可通过 `extract_usage_tokens(response)` 提取 token 用量，
    或通过 `fire_on_llm_response_event(event, response)` 手动触发 OnLLMResponseEvent 钩子。

    Returns:
        (reply_text, response) 元组，失败返回 None。
    """
    provider = _resolve_provider(context, uid, provider_id)
    if not provider:
        logger.error("[HumanlikeReply] 无法获取LLM provider")
        return None

    import asyncio
    try:
        kwargs = {
            "prompt": user_prompt,
            "context": contexts or [],
            "system_prompt": system_prompt,
        }
        if image_urls:
            kwargs["image_urls"] = image_urls

        response = await asyncio.wait_for(
            provider.text_chat(**kwargs),
            timeout=timeout,
        )
        reply_text = getattr(response, "completion_text", "") or ""
        return (reply_text.strip(), response)
    except asyncio.TimeoutError:
        logger.warning(f"[HumanlikeReply] LLM调用超时 ({timeout}s)")
        return None
    except Exception as e:
        logger.error(f"[HumanlikeReply] LLM调用失败: {e}")
        return None


async def send_reply_with_hooks(event, text: str) -> None:
    """设置 result + 手动触发 on_decorating_result + 发送 + 手动触发 after_message_sent。

    v1.0.4 起替代 ``event.send(event.plain_result(text))`` 的直接发送方式。

    问题背景：本插件 ``stop_event()`` 后用 ``event.send()`` 直接发送，
    绕过了框架的 ``ResultDecorateStage`` 和 ``RespondStage``，导致：
    1. ``on_decorating_result`` 钩子不触发 → postsplitter 等分段插件不生效
    2. ``after_message_sent`` 钩子不触发 → ttsplus 等语音插件不生效

    修复策略（洋葱模型的等价手动实现）：
    1. ``set_result`` 设置回复内容
    2. ``continue_event`` 临时恢复传播（让 ``call_event_hook`` 中的 ``is_stopped`` 检查通过）
    3. 手动调 ``call_event_hook(OnDecoratingResultEvent)`` → postsplitter 处理分段
    4. ``event.send(result)`` 发送处理后的 result（postsplitter 会把最后一段留在 result.chain）
    5. 手动调 ``call_event_hook(OnAfterMessageSentEvent)`` → ttsplus/thinkview 等处理
    6. ``stop_event`` 重新终止，防止调度器继续执行后续 Stage

    前置条件：``fire_on_llm_response_event`` 必须在本方法之前调用，
    以便 postsplitter 的 ``on_llm_response`` 设置 ``__post_splitter_is_llm_reply`` 标记，
    否则 postsplitter 的 ``on_decorating_result`` 会认为不是 LLM 回复而跳过。
    """
    if event is None:
        logger.warning("[HumanlikeReply] send_reply_with_hooks: event 为 None，跳过")
        return

    try:
        from astrbot.core.pipeline.context_utils import call_event_hook
        from astrbot.core.star.star_handler import EventType
    except ImportError:
        # 框架版本过旧，回退到直接发送
        logger.debug("[HumanlikeReply] 无法导入 call_event_hook，回退到直接 event.send")
        try:
            await event.send(event.plain_result(text))
        except Exception as e:
            logger.error(f"[HumanlikeReply] 回退发送失败: {e}")
        return

    # 1. 设置 result
    result = event.plain_result(text)
    event.set_result(result)

    # 2. 临时恢复传播（call_event_hook 内部会检查 is_stopped）
    event.continue_event()

    try:
        # 3. 手动触发 on_decorating_result（让 postsplitter 等处理 result.chain）
        try:
            await call_event_hook(event, EventType.OnDecoratingResultEvent)
        except Exception as e:
            logger.debug(f"[HumanlikeReply] on_decorating_result 钩子链异常: {e}")

        # 如果装饰钩子中又被 stop_event，恢复一下以便继续发送
        if event.is_stopped():
            event.continue_event()

        # 4. 发送处理后的 result
        #    postsplitter 会把最后一段留在 result.chain，前置分段自己发送
        current_result = event.get_result()
        if current_result and current_result.chain:
            try:
                await event.send(current_result)
            except Exception as e:
                logger.error(f"[HumanlikeReply] 发送 result 失败: {e}")
        else:
            logger.debug("[HumanlikeReply] result.chain 为空，跳过发送（可能已被钩子完全处理）")

        # 5. 手动触发 after_message_sent（让 ttsplus/thinkview 等处理）
        if event.is_stopped():
            event.continue_event()
        try:
            await call_event_hook(event, EventType.OnAfterMessageSentEvent)
        except Exception as e:
            logger.debug(f"[HumanlikeReply] after_message_sent 钩子链异常: {e}")
    finally:
        # 6. 重新终止事件传播，防止调度器继续执行后续 Stage
        event.stop_event()


async def save_conversation(context, uid: str, user_text: str, assistant_text: str, image_urls: list = None) -> None:
    """保存用户消息和bot回复到对话历史。参考premerger的_save_conversation。"""
    try:
        from astrbot.core.agent.message import (
            AssistantMessageSegment,
            ImageURLPart,
            TextPart,
            UserMessageSegment,
        )
    except ImportError as e:
        logger.debug(f"[HumanlikeReply] 无法导入消息类型，跳过对话历史保存: {e}")
        return

    try:
        conv_mgr = getattr(context, "conversation_manager", None)
        if not conv_mgr:
            logger.debug("[HumanlikeReply] conversation_manager不可用，跳过对话历史保存")
            return
        curr_cid = await conv_mgr.get_curr_conversation_id(uid)
        if not curr_cid:
            logger.debug("[HumanlikeReply] 无当前会话ID，跳过对话历史保存")
            return

        user_content: list = []
        if user_text:
            user_content.append(TextPart(text=user_text))
        if image_urls:
            for url in image_urls:
                try:
                    user_content.append(ImageURLPart(image_url=ImageURLPart.ImageURL(url=url)))
                except Exception:
                    pass
        if not user_content:
            user_content.append(TextPart(text=""))
        user_msg = UserMessageSegment(content=user_content)
        assistant_msg = AssistantMessageSegment(content=[TextPart(text=assistant_text)])

        await conv_mgr.add_message_pair(
            cid=curr_cid,
            user_message=user_msg,
            assistant_message=assistant_msg,
        )
        logger.debug(f"[HumanlikeReply] 对话历史已保存 uid={uid}")
    except Exception as e:
        logger.warning(f"[HumanlikeReply] 保存对话历史失败: {e}")
