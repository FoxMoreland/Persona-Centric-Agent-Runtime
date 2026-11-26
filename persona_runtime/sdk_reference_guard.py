"""Project-process-only SDK adapter guard; never edit an installed package."""
import functools
import inspect
import json
from collections import Counter

from .config import atomic_json
from .models import RuntimeErrorBase


def install(adapter_class=None, *, admit_event=None):
    if adapter_class is None:
        from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_platform_adapter import AiocqhttpAdapter
        adapter_class = AiocqhttpAdapter
    original = adapter_class._convert_handle_message_event
    if getattr(original, '_pcar_local_references', False): return
    parameters = inspect.signature(original).parameters
    if tuple(parameters) != ('self', 'event', 'get_reply') or not inspect.iscoroutinefunction(original):
        raise RuntimeErrorBase('sdk_reference_guard_incompatible')

    @functools.wraps(original)
    async def guarded(self, event, get_reply=True):
        if admit_event is not None and not admit_event(event):
            return None
        return await original(self, event, get_reply=False)
    guarded._pcar_local_references = True
    adapter_class._convert_handle_message_event = guarded


def observe_ingress(paths, cfg):
    """Count bound noise before SDK conversion or its private-chat whitelist."""
    profile = paths.home/'private/qq-ingress-observation.json'
    if cfg.get('qq_backend') != 'snowluma' or not profile.is_file():
        return None
    watched = json.loads(profile.read_text(encoding='utf-8'))
    target = paths.home/'runtime/qq-sdk-ingress-counts.json'
    counts = Counter(json.loads(target.read_text(encoding='utf-8'))) if target.is_file() else Counter()
    if not target.is_file():
        atomic_json(target,{})

    def admit(event):
        from .qq_bridge import raw_event_type
        category = raw_event_type(event,cfg)
        counts[category] += 1
        field = {'group':'group_id','other_private':'user_id'}.get(category)
        if field and str(event.get(field,'')) == watched.get(category):
            counts['observed_'+category] += 1
        atomic_json(target,dict(counts))
        return category == 'authorized_private' and not watched.get('observe_only',False)

    return admit


def install_context_capture(journal, client_class=None):
    if journal is None:
        return
    if client_class is None:
        from aiocqhttp import CQHttp
        client_class = CQHttp
    original = client_class._handle_event
    if getattr(original,'_pcar_context_capture',False):
        return
    if tuple(inspect.signature(original).parameters) != ('self','payload') or not inspect.iscoroutinefunction(original):
        raise RuntimeErrorBase('sdk_context_capture_incompatible')

    @functools.wraps(original)
    async def captured(self, payload):
        saved = journal.capture(payload)
        if journal.core_incoming(payload) and journal.current_message(payload) and saved in ('incoming','duplicate'):
            return await original(self,payload)
        if saved is not None or payload.get('post_type') == 'message_sent':
            return None
        return await original(self,payload)

    captured._pcar_context_capture = True
    client_class._handle_event = captured
