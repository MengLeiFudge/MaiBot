"""以框架实际 Hook 规格和注册器检查聊天回复插件，拦截部署时才出现的声明错误。"""
from unittest import IsolatedAsyncioTestCase



from plugins.qqbot_reply_timing.plugin import QQBotReplyTimingPlugin
from src.chat.message_receive.bot import register_chat_hook_specs
from src.maisaka.chat_loop_service import register_maisaka_hook_specs
from src.plugin_runtime.host.component_registry import ComponentRegistry
from src.plugin_runtime.host.hook_spec_registry import HookSpecRegistry
from src.plugin_runtime.runner.plugin_loader import PluginLoader
from src.services.send_service import register_send_service_hook_specs


class ReplyRegistrationTests(IsolatedAsyncioTestCase):
    async def test_reply_plugins_register_against_actual_host_specs(self):
        """校验全部组件的模式、错误策略、名称和原生 Hook 兼容性。"""
        specs = HookSpecRegistry()
        register_chat_hook_specs(specs)
        register_maisaka_hook_specs(specs)
        register_send_service_hook_specs(specs)
        registry = ComponentRegistry(hook_spec_registry=specs)
        for plugin_id, plugin in (

            ("mlj.qqbot-reply-timing", QQBotReplyTimingPlugin()),
        ):
            PluginLoader._validate_sdk_plugin_contract(plugin_id, plugin)
            components = [{**item, "component_type": item["type"]} for item in plugin.get_components()]
            self.assertEqual(registry.register_plugin_components(plugin_id, components), len(components))

    async def test_partially_loaded_plugins_can_be_unloaded(self):
        """注册失败时还没执行 on_load，也必须允许框架卸载实例。"""
        for plugin in (QQBotReplyTimingPlugin(),):
            await plugin.on_unload()
            await plugin.on_config_update("self", {}, "0.1.0")
