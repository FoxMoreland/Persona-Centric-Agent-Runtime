"""Minimal owner-only adapter; all model calls belong to Persona Runtime."""
import asyncio
import time
from contextlib import suppress

from astrbot.api import logger
from astrbot.api.event import filter
from astrbot.api.star import Star, register

from persona_runtime.config import ConfigStore
from persona_runtime.logging_safe import setup
from persona_runtime.paths import RuntimePaths
from persona_runtime.qq_bridge import QQBridge, allowed_message, event_type
from persona_runtime.qq_client import require_snowluma, send_private_text
from persona_runtime.qq_native_reply import configured as native_qq_configured
from persona_runtime.qq_native_reply import pro_test
from persona_runtime.qq_local_actions import SnowLumaLocalActions, enabled as local_actions_enabled
from persona_runtime.qq_recall_notice import notice as recall_notice


@register("astrbot_plugin_persona_runtime", "Persona Runtime", "本人好友私聊人格桥接", "0.7.4")
class PersonaRuntimePlugin(Star):
    def __init__(self, context):
        super().__init__(context)
        self.paths = RuntimePaths.from_environment()
        self.config = ConfigStore(self.paths)
        self.bridge = QQBridge(self.paths)
        self.health_task = None
        self.delivery_task = None
        self.delivery_wakeup = asyncio.Event()
        self.active_until = 0
        self.published_ingress_counts = None

    async def initialize(self):
        setup(self.paths, "astrbot")
        if self.bridge.observed_noise.get('observe_only',False):
            return
        # AstrBot's default log broker must not persist raw chat or third-party errors.
        self.health_task = asyncio.create_task(self.health())
        self.delivery_task = asyncio.create_task(self.deliver_pending())

    def platform_client(self, platform, cfg):
        if local_actions_enabled(self.paths,cfg):
            return SnowLumaLocalActions(self.paths,cfg)
        return platform.get_client()

    async def deliver_native(self, cfg):
        await self.bridge.flush_native_ack()
        if self.bridge.delivery_halted():
            return False
        for platform in self.context.platform_manager.platform_insts:
            if platform.meta().name != "aiocqhttp" or platform.meta().id != "persona-onebot":
                continue
            client = self.platform_client(platform,cfg)
            await require_snowluma(client, cfg)
            return await self.bridge.deliver_native(lambda text: send_private_text(client,cfg,text),cfg)
        return False

    async def deliver_pending(self):
        delay=.5
        while True:
            try:
                cfg = self.config.load()
                enabled = cfg.get('qq_backend', 'napcat') != 'snowluma' or cfg['runtime']['components']['napcat']
                if enabled and cfg["owner_qq"] and cfg["bot_qq"]:
                    await self.bridge.flush_receipts()
                    await self.bridge.reconcile_inbox()
                    health = await self.bridge.request("GET", "/v1/health")
                    m63 = "reply_delivery_parts_v1" in health.get("capabilities", [])
                    native = (native_qq_configured(self.paths,cfg,cfg['runtime']['qq_persona_id'])
                              and not pro_test(self.paths,cfg,cfg['runtime']['qq_persona_id']))
                    if native:
                        active = await self.deliver_native(cfg)
                        pending = []
                    else:
                        pending = [] if self.bridge.delivery_halted() else await self.bridge.request(
                            "GET", "/v2/delivery/streams" if m63 else "/v1/qq-pending")
                        active = bool(pending)
                    delay=.5 if active or time.monotonic()<self.active_until else min(5,delay*1.5)
                    for item in pending:
                        if self.bridge.delivery_halted():
                            break
                        for platform in self.context.platform_manager.platform_insts:
                            if platform.meta().name != "aiocqhttp" or platform.meta().id != "persona-onebot":
                                continue
                            client = self.platform_client(platform,cfg)
                            if cfg.get('qq_backend', 'napcat') == 'snowluma':
                                await require_snowluma(client, cfg)
                            else:
                                login = await asyncio.wait_for(client.get_login_info(), 3)
                                if str(login.get("user_id", "")) != cfg["bot_qq"]:
                                    continue
                            async def send(text):
                                return await self.bridge.send_if_open(lambda body: send_private_text(client, cfg, body), text)
                            if m63:
                                await self.bridge.deliver_stream(item, send)
                            else:
                                await self.bridge.deliver(item, send)
                            break
            except Exception:
                delay=min(5,delay*1.5)
            current_counts = dict(self.bridge.ingress_counts)
            if self.published_ingress_counts != current_counts:
                try:
                    self.bridge.publish_ingress_counts()
                    self.published_ingress_counts = current_counts
                except OSError:
                    logger.error('qq_ingress_counter_write_failed')
            try:await asyncio.wait_for(self.delivery_wakeup.wait(),delay)
            except asyncio.TimeoutError:pass
            self.delivery_wakeup.clear()

    async def health(self):
        from persona_runtime.bridge_health import heartbeat_loop
        await heartbeat_loop(self.paths,self.config,lambda:self.context.platform_manager.platform_insts,
                             client_for=self.platform_client)

    @filter.event_message_type(filter.EventMessageType.ALL, priority=10000)
    async def receive(self, event):
        if event.platform_meta.name != "aiocqhttp":
            return
        event.stop_event()
        if self.bridge.observed_noise.get('observe_only',False):
            return
        try:
            cfg = self.config.load()
            client = event.bot
            if cfg.get('qq_backend', 'napcat') == 'snowluma':
                if local_actions_enabled(self.paths,cfg):
                    client = SnowLumaLocalActions(self.paths,cfg)
                recall = recall_notice(event,cfg)
                if recall and event.platform_meta.id == 'persona-onebot':
                    if native_qq_configured(self.paths,cfg,cfg['runtime']['qq_persona_id']):
                        await require_snowluma(client,cfg)
                        await self.bridge.request('POST','/v2/qq-native/recall',recall)
                    return
                category = event_type(event, cfg)
                if not allowed_message(event, cfg):
                    self.bridge.note_ingress(category if category != 'authorized_private' else 'malformed_private', event)
                    return
                self.bridge.note_ingress(category)
                await require_snowluma(client, cfg)
            async def send(text):
                # Await actual platform completion; no generator-yield success assumptions.
                if cfg.get('qq_backend', 'napcat') == 'snowluma':
                    return await self.bridge.send_if_open(lambda body: send_private_text(client, cfg, body), text)
                return await event.send(event.plain_result(text))
            await self.bridge.handle(event, send, background=True)
            self.active_until=time.monotonic()+60
            self.delivery_wakeup.set()
        except Exception:
            logger.error("persona_bridge_failure")

    async def terminate(self):
        await self.bridge.close()
        if self.delivery_task:
            self.delivery_task.cancel()
            with suppress(asyncio.CancelledError):
                await self.delivery_task
        if self.health_task:
            self.health_task.cancel()
            with suppress(asyncio.CancelledError):
                await self.health_task
