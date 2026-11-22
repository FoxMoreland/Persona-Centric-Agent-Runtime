"""Local, reviewed OpenRouter writer selection for the private desktop route."""
import json
import re

from .archive import fingerprint
from .models import RuntimeErrorBase
from .security import protect

DS = 'ds_pro'
HYDROGEN = 'openrouter_hydrogen'
URL = 'https://openrouter.ai/api/v1'
MODEL = 'google/gemini-4-hydrogen'
DEFAULTS = {'active': DS, 'enabled': False, 'encrypted_key': '', 'review': None}
MARKER = '_main_reply'
CONTRACT = 'openrouter-hydrogen-v1'


def settings(cfg):
    return cfg.get('main_reply', DEFAULTS)


def validate(value):
    review = value.get('review') if isinstance(value, dict) else None
    if (not isinstance(value, dict) or set(value) != set(DEFAULTS)
            or value['active'] not in (DS, HYDROGEN) or type(value['enabled']) is not bool
            or not isinstance(value['encrypted_key'], str)
            or review is not None and not re.fullmatch('[a-f0-9]{64}', review if isinstance(review,str) else '')):
        raise RuntimeErrorBase('main_reply_configuration_invalid')


def registered_bundle(config, cfg):
    path = config.paths.home/'private/ordinary-reply/registration.json'
    if (path.parent/'retired.json').exists() or not path.is_file():
        return None
    try:
        record = json.loads(path.read_bytes())
    except (OSError, ValueError):
        return None
    if not isinstance(record, dict):
        return None
    digest = record.get('reviewed_sha256')
    if (record.get('instance') == cfg['instance_id'] and isinstance(digest,str)
            and re.fullmatch('[a-f0-9]{64}',digest) and path.with_name(digest+'.json').is_file()):
        return digest
    return None


def writer(cfg, *, stream=True):
    return dict(base_url=URL, name=MODEL, encrypted_key=settings(cfg)['encrypted_key'],
                timeout=180, max_tokens=8192, max_input_chars=cfg['model']['max_input_chars'],
                hourly_limit=cfg['model']['hourly_limit'], _openrouter_stream=stream)


def review_binding(cfg, digest):
    limits = {key:value for key,value in writer(cfg).items() if key!='hourly_limit'}
    return fingerprint([cfg['instance_id'], digest, CONTRACT, limits])


def available(config, cfg):
    from .provider_policy import is_official_deepseek
    if cfg['model']['name']!='deepseek-v4-pro' or not is_official_deepseek(cfg['model']):
        return None
    value = settings(cfg)
    digest = registered_bundle(config, cfg) if value['enabled'] and value['encrypted_key'] else None
    return digest if digest and value['review'] == review_binding(cfg, digest) else None


def validate_assembly(config, cfg, digest):
    from .ordinary_providers import _review_bytes, registered
    _review_bytes(config,digest)
    adapter=registered(config)
    if (adapter is None or adapter.version!=digest
            or adapter.bundle.get('main')!={key:cfg['model'][key] for key in ('base_url','name')}
            or adapter.bundle.get('scope',{}).get('instance')!=cfg['instance_id']
            or not adapter.notice_model()):
        raise RuntimeErrorBase('main_reply_assembly_unavailable')


def update(config, cfg, change):
    from .config import same_credential
    keys = {'active', 'enabled', 'api_key', 'reviewed_bundle'}
    if not isinstance(change,dict) or not set(change) <= keys:
        raise RuntimeErrorBase('main_reply_configuration_invalid')
    value = dict(settings(cfg))
    cfg['main_reply'] = value
    if 'api_key' in change:
        secret = change['api_key']
        if not isinstance(secret,str) or any(c.isspace() for c in secret):
            raise RuntimeErrorBase('main_reply_key_invalid')
        new_key = protect(secret) if secret else ''
        if not same_credential(value['encrypted_key'], new_key):
            value.update(encrypted_key=new_key, review=None)
    value.update({k:change[k] for k in ('active','enabled') if k in change})
    if 'reviewed_bundle' in change:
        digest = registered_bundle(config,cfg)
        if not digest or change['reviewed_bundle'] != digest or not value['encrypted_key']:
            raise RuntimeErrorBase('main_reply_assembly_unavailable')
        validate_assembly(config,cfg,digest)
        value['review'] = review_binding(cfg,digest)
    validate(value)
    if change.get('active')==HYDROGEN:
        digest=available(config,cfg)
        if not digest:
            raise RuntimeErrorBase('main_reply_assembly_unavailable')
        if 'reviewed_bundle' not in change:
            validate_assembly(config,cfg,digest)


def admit(config, cfg, route):
    if settings(cfg)['active']==DS:
        return None
    if route not in ('maibot-private-v2','maibot-private-v3'):
        raise RuntimeErrorBase('main_reply_private_route_required')
    return binding(config,cfg)


def binding(config, cfg):
    digest = available(config,cfg)
    if not digest:
        raise RuntimeErrorBase('main_reply_assembly_unavailable')
    return {'bundle':digest,'writer':fingerprint([CONTRACT,writer(cfg)])}


def resolve(config, cfg, accepted):
    if accepted != binding(config,cfg):
        raise RuntimeErrorBase('private_reply_model_changed')
    return writer(cfg)


def status(config, cfg):
    value = settings(cfg)
    return {'active':value['active'], 'enabled':value['enabled'],
            'has_key':bool(value['encrypted_key']), 'ready':bool(available(config,cfg)),
            'bundle':registered_bundle(config,cfg), 'endpoint':URL, 'model':MODEL}


def retry_not_before(value):
    import time
    from email.utils import parsedate_to_datetime
    if not value:
        return None
    try:
        if re.fullmatch('[0-9]{1,12}',value):
            return time.time()+int(value)
        date=parsedate_to_datetime(value)
        return date.timestamp() if date.tzinfo else None
    except (ValueError,TypeError,OverflowError,OSError):
        return None


def cli():
    import argparse
    import getpass
    import warnings
    from pathlib import Path
    from .config import ConfigStore
    from .paths import RuntimePaths
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--home',required=True,type=Path)
    parser.add_argument('--key',action='store_true',help='Read the candidate key from a masked local prompt.')
    parser.add_argument('--reviewed-bundle',help='Approve this exact existing bundle for the candidate writer.')
    parser.add_argument('--select',choices=(DS,HYDROGEN))
    parser.add_argument('--authorize-persona',help='Record the selected protected persona’s approved writer recipient.')
    args = parser.parse_args()
    config = ConfigStore(RuntimePaths(args.home.resolve()))
    changes = {}
    if args.key:
        with warnings.catch_warnings():
            warnings.simplefilter('error',getpass.GetPassWarning)
            secret = getpass.getpass('OpenRouter API Key: ')
        if not secret:
            parser.error('A nonempty key is required.')
        changes.update(api_key=secret,enabled=True)
    if args.reviewed_bundle:
        changes['reviewed_bundle'] = args.reviewed_bundle
    if args.select:
        changes['active'] = args.select
    public = config.update({'main_reply':changes})['main_reply'] if changes else status(config,config.load())
    if args.authorize_persona:
        from .private_personas import PrivatePersonas, OPENROUTER_WRITER_PERMISSION, provider_identity
        from .storage import Store
        from .models import identifier
        cfg=config.load()
        binding(config,cfg)
        PrivatePersonas(config.paths).authorize_model(Store(config.paths),args.authorize_persona,
            OPENROUTER_WRITER_PERMISSION,provider_identity(writer(cfg)),identifier())
    print(json.dumps(public))


if __name__ == '__main__':
    cli()
