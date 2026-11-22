"""Model configuration for the separately distributed Community edition."""

import os
from pathlib import Path

from .models import RuntimeErrorBase


BASE = 'https://api.deepseek.com'
PRO = 'deepseek-v4-pro'
FLASH = 'deepseek-flash'
DECISION_BASE = BASE + '/beta'
ROUTE = 'deepseek-official-community-v1'


def enabled():
    return (os.environ.get('PCAR_EDITION') == 'community'
            or Path(__file__).with_name('community-edition').is_file())


def field_test_enabled():
    return Path(__file__).with_name('community-field-test').is_file()


def apply_models(cfg):
    from .provider_policy import is_official_deepseek

    main = cfg['model']
    key = main['encrypted_key'] if is_official_deepseek(main) else ''
    main.update(base_url=BASE, name=PRO, encrypted_key=key)
    flash = dict(main, name=FLASH)
    cfg['organization']['reuse_chat'] = False
    cfg['organization']['model'].update(base_url=BASE, name=FLASH, encrypted_key=key)
    cfg['character_selector'].update(base_url=BASE, name=FLASH, encrypted_key=key)
    cfg['native_web_model'] = flash.copy()
    cfg['lookup_target_model'] = flash.copy()
    cfg['private_research_account'] = 'native_web'
    cfg.pop('main_reply', None)


def validate_changes(changes):
    if set(changes) & {'main_reply', 'native_web_model', 'lookup_target_model', 'private_research_account'}:
        raise RuntimeErrorBase('community_official_key_required')
    main = changes.get('model', {})
    organization = changes.get('organization', {})
    selector = changes.get('character_selector', {})
    if (not all(isinstance(value, dict) for value in (main, organization, selector))
            or main.get('base_url', BASE) != BASE or main.get('name', PRO) != PRO
            or organization.get('reuse_chat', False)):
        raise RuntimeErrorBase('community_official_key_required')
    for section in (selector, organization.get('model', {})):
        if not isinstance(section, dict) or set(section) & {'base_url', 'name', 'api_key'}:
            raise RuntimeErrorBase('community_official_key_required')


def remove_derived_secrets(cfg):
    for model in (cfg['organization']['model'], cfg['character_selector']):
        model['encrypted_key'] = ''
    for key in ('native_web_model', 'lookup_target_model', 'private_research_account'):
        cfg.pop(key, None)


def decision_model(cfg):
    from .config import DEFAULTS

    main = cfg['model']
    if main['base_url'] != BASE or not main['encrypted_key']:
        raise RuntimeErrorBase('jev_configuration_required')
    return dict(DEFAULTS['model'], base_url=DECISION_BASE, name=FLASH,
                encrypted_key=main['encrypted_key'], timeout=min(main['timeout'], 30),
                max_input_chars=main['max_input_chars'])
