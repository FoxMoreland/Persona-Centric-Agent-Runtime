"""Cloud generation is official DeepSeek; Jev has its independent transport."""
from urllib.parse import urlsplit
from .provider_compat import CURRENT_DEEPSEEK


PRIVATE_COMPATIBLE_WRITER = ('https://e-flowcode.cc/v1', 'gemini-4-hydrogen')
DEEPSEEK_TEXT_RELAY = 'https://e-flowcode.cc/v1'
TYPESAFE_JEV_URL = 'https://api.typesafe.ai/v1'
TYPESAFE_JEV_MODEL = 'jev-1.14.0'


def is_official_typesafe_jev(model):
    return isinstance(model, dict) and (model.get('base_url'), model.get('name')) == (
        TYPESAFE_JEV_URL, TYPESAFE_JEV_MODEL)


def is_deepseek_text_relay(model):
    return (model.get('base_url') == DEEPSEEK_TEXT_RELAY
            and model.get('name') in ('deepseek-v4-pro', 'deepseek-flash'))


def is_private_compatible_writer(model):
    return (model.get('base_url'), model.get('name')) == PRIVATE_COMPATIBLE_WRITER


def is_openrouter_writer(model):
    from .main_reply import URL, MODEL
    return (model.get('base_url'), model.get('name')) == (URL, MODEL)


def is_official_deepseek(model):
    try:
        raw = model['base_url']
        url = urlsplit(raw)
        return (isinstance(raw,str) and not any(character.isspace() for character in raw)
                and url.scheme=='https' and url.hostname=='api.deepseek.com'
                and url.port in (None,443) and not url.username and not url.password and not url.query and not url.fragment
                and url.path.rstrip('/') in ('','/v1','/chat/completions','/v1/chat/completions')
                and model['name'] in CURRENT_DEEPSEEK)
    except (ValueError,KeyError,TypeError,AttributeError):
        return False


def require_generation_provider(model, *, allow_loopback_fixture=False, allow_reply_decision_tool=False,
                                allow_private_compatible_writer=False, allow_openrouter_writer=False):
    try:
        if '_reply_decision_tool' in model:
            if not allow_reply_decision_tool:raise ValueError()
            from .deepseek_reply_decision import model_profile
            model_profile(model)
            return
        if allow_private_compatible_writer and is_private_compatible_writer(model):
            return
        if is_openrouter_writer(model):
            if allow_openrouter_writer and '_native_web_profile' not in model:
                return
            raise ValueError()
        if is_deepseek_text_relay(model) and '_native_web_profile' not in model:
            return
        raw=model['base_url'];url=urlsplit(raw)
        if (allow_loopback_fixture and url.hostname in ('127.0.0.1','localhost','::1')
                and url.scheme in ('http','https') and not url.username and not url.password):
            return
        if not is_official_deepseek(model):
            raise ValueError()
    except (ValueError,KeyError,TypeError,AttributeError):
        raise ValueError('provider_official_deepseek_required') from None
