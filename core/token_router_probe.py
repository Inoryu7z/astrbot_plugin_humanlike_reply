"""探测并调用 token_router 插件，让本插件的 LLM 调用能被正确路由与计数。

背景：
    本插件（humanlike_reply）通过 `provider.text_chat()` 绕过 Pipeline 直接调 LLM，
    导致 token_router 的两个钩子都不生效：
    1. `on_message(priority=9999)` 因本插件 `priority=1 + stop_event` 被跳过
       → `selected_provider` extra 永不被设置
    2. `on_llm_response` 由 Pipeline 触发，本插件绕过 Pipeline → 钩子永不触发
       → 用量永不被记录，路由链永不切换

修复策略：
    - **立即回复路径**：手动调用 `token_router.on_message(event)` 让其设置 extra，
      本插件读取 extra 决定 provider；LLM 响应后由 llm_helper.fire_on_llm_response_event
      手动触发 OnLLMResponseEvent 钩子。
    - **延迟回复路径**：无 event，直接调用 token_router 内部方法（_find_window_config /
      _get_active_model_index / _record_usage），绕过 event 依赖。
"""

from typing import Optional

from astrbot.api import logger

from .dailysharing_probe import find_plugin_instance


class TokenRouterProbe:
    """token_router 插件探测与直接调用。

    所有方法在 token_router 未安装时安全降级（返回 None 或 no-op）。
    """

    def __init__(self, context, plugin_name: str):
        self.context = context
        self.plugin_name = plugin_name
        self._cached_plugin = None
        self._not_found_reported = False

    def _get_plugin(self):
        """获取 token_router 插件实例（带缓存）。"""
        if self._cached_plugin is not None:
            return self._cached_plugin
        if not self.plugin_name:
            return None
        plugin = find_plugin_instance(self.context, self.plugin_name)
        if plugin is None:
            if not self._not_found_reported:
                logger.debug(
                    f"[HumanlikeReply] 未找到 token_router 插件 '{self.plugin_name}'，"
                    f"跳过 token 路由集成"
                )
                self._not_found_reported = True
            return None
        self._cached_plugin = plugin
        return plugin

    async def trigger_on_message(self, event) -> None:
        """手动调用 `token_router.on_message(event)`，让其设置 selected_provider extra。

        用于立即回复路径：本插件 priority=1 先于 token_router(9999) 执行并 stop_event，
        导致 token_router.on_message 永不触发。本方法手动调用以弥补。
        """
        plugin = self._get_plugin()
        if plugin is None:
            return
        try:
            await plugin.on_message(event)
        except Exception as e:
            logger.debug(f"[HumanlikeReply] 触发 token_router.on_message 失败: {e}")

    async def resolve_persona_id(self, umo: str, platform_name: str = "") -> Optional[str]:
        """解析 (umo, platform_name) 对应的人格ID。

        复用 token_router 的 `_get_current_persona_id` 逻辑，但不依赖 event。
        用于延迟回复路径（无 event）。

        Args:
            umo: 统一消息来源
            platform_name: 平台名（延迟路径下从原 event 缓存中获取）
        """
        plugin = self._get_plugin()
        if plugin is None:
            return None
        try:
            conversation_persona_id = None
            curr_cid = await self.context.conversation_manager.get_curr_conversation_id(umo)
            if curr_cid:
                conversation = await self.context.conversation_manager.get_conversation(umo, curr_cid)
                if conversation:
                    conversation_persona_id = conversation.persona_id
            cfg = self.context.get_config(umo)
            provider_settings = cfg.get("provider_settings", {}) if isinstance(cfg, dict) else {}
            persona_id, _, _, _ = await self.context.persona_manager.resolve_selected_persona(
                umo=umo,
                conversation_persona_id=conversation_persona_id,
                platform_name=platform_name,
                provider_settings=provider_settings,
            )
            if persona_id == "[%None]":
                return None
            return persona_id
        except Exception as e:
            logger.debug(f"[HumanlikeReply] 解析 persona_id 失败: {e}")
            return None

    def get_active_provider(self, umo: str, persona_id: Optional[str]) -> Optional[str]:
        """查询 token_router 为 (umo, persona_id) 决定的 active provider_id。

        返回 None 表示：
        - token_router 未启用
        - 无匹配窗口配置
        - 所有模型已耗尽
        - 路由链为空
        """
        plugin = self._get_plugin()
        if plugin is None:
            return None
        try:
            window_config = plugin._find_window_config(umo, persona_id)
            if not window_config:
                return None
            models = window_config.get("models", [])
            if not models:
                return None
            if plugin._is_all_exhausted(umo, persona_id):
                return None
            active_index = plugin._get_active_model_index(umo, persona_id, models)
            if active_index == -1:
                return None
            return models[active_index].get("provider_id", "") or None
        except Exception as e:
            logger.debug(f"[HumanlikeReply] 查询 token_router active provider 失败: {e}")
            return None

    def record_usage(
        self,
        umo: str,
        persona_id: Optional[str],
        provider_id: str,
        usage_tokens: int,
    ) -> None:
        """直接调用 `token_router._record_usage` 记录 token 用量。

        用于延迟回复路径（无 event，无法触发 OnLLMResponseEvent）。
        立即回复路径应使用 `fire_on_llm_response_event` 让钩子自然触发。
        """
        plugin = self._get_plugin()
        if plugin is None:
            return
        if not provider_id or not usage_tokens:
            return
        try:
            plugin._record_usage(umo, persona_id, provider_id, usage_tokens)
            logger.debug(
                f"[HumanlikeReply] token_router 直接记录用量 umo={umo} "
                f"provider={provider_id} tokens=+{usage_tokens}"
            )
        except Exception as e:
            logger.debug(f"[HumanlikeReply] token_router 记录用量失败: {e}")
