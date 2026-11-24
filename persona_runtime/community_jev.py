"""Optional Jev settings and session grants for the single-Key edition."""

import hmac
import json
import math
from time import monotonic
from urllib.parse import urlsplit
from pathlib import Path

from . import community
from .archive import fingerprint, scope
from .config import atomic_json
from .models import RuntimeErrorBase, identifier
from .scene_policy import read, save
from .security import protect, unprotect


GRANT = 'community.jev.session.v1.'
DEFAULT = {'revision': 0, 'enabled': False, 'base_url': '', 'name': '', 'encrypted_key': ''}
PROBE_SECONDS = 600
PROBE_SHA256 = 'bfa06f07a1af37ccaf89ca9dbe4c79b26e0bbebdbbdc0f1f680bff9df422e5aa'


def endpoint(value):
    if not isinstance(value, str) or not 1 <= len(value) <= 512 or any(c.isspace() or ord(c) < 32 for c in value):
        raise RuntimeErrorBase('community_jev_invalid_url')
    try:
        url = urlsplit(value.rstrip('/'))
        port = url.port
    except ValueError:
        raise RuntimeErrorBase('community_jev_invalid_url') from None
    if (url.scheme != 'https' or not url.hostname or url.username is not None or url.password is not None
            or '?' in value or '#' in value or '\\' in value or (port is not None and not 0 < port <= 65535)):
        raise RuntimeErrorBase('community_jev_invalid_url')
    host = url.netloc.lower()
    path = url.path.removesuffix('/systemone').rstrip('/')
    return 'https://' + host + path


def model_name(value):
    if not isinstance(value, str) or not 1 <= len(value) <= 160 or any(c.isspace() or ord(c) < 32 for c in value):
        raise RuntimeErrorBase('community_jev_invalid_model')
    return value


def config_path(config):
    from .private_personas import safe
    return safe(config.paths.home / 'config' / 'community-jev.json', config.paths.home)


def settings(config):
    try:
        path = config_path(config)
        with path.open('rb') as stream:
            raw = stream.read(16385)
        if len(raw) > 16384:
            raise ValueError
        value = json.loads(raw)
        if (not isinstance(value, dict) or set(value) != set(DEFAULT)
                or type(value['revision']) is not int or not 1 <= value['revision'] < 2**53
                or type(value['enabled']) is not bool or not isinstance(value['encrypted_key'], str)
                or not value['encrypted_key'] or endpoint(value['base_url']) != value['base_url']
                or model_name(value['name']) != value['name']):
            raise ValueError
        return value
    except FileNotFoundError:
        return DEFAULT.copy()
    except (OSError, ValueError, RecursionError):
        raise RuntimeErrorBase('community_jev_settings_unavailable') from None


def recipient(config, saved):
    return fingerprint([config.load()['instance_id'], saved['revision'], saved['base_url'], saved['name'],
                        saved['encrypted_key']])


def session_grant(db, config, person, sid, saved):
    from .sessions import owned
    owned(db, sid, scope(person, 'desktop'))
    old = read(db, GRANT + sid) or {'revision': 0, 'enabled': False}
    binding = recipient(config, saved) if saved['enabled'] else None
    return {'revision': old['revision'], 'available': binding is not None, 'recipient': binding,
            'authorized': bool(binding and old['enabled'] and old.get('recipient') == binding)}


def approved_probe():
    asset = Path(__file__).with_name('release_assets') / 'jev-connection-probe-v1.json'
    try:
        raw = asset.read_bytes()
        if len(raw) > 4096:
            raise ValueError
        payload = json.loads(raw)
        if fingerprint(payload) != PROBE_SHA256:
            raise ValueError
    except (OSError, ValueError, RecursionError):
        raise RuntimeErrorBase('community_jev_probe_review_required') from None
    return payload, {'check_first': 'A', 'check_second': 'B'}


def choice_result(raw, wire):
    from .community_decision import unique
    from .knowledge_web_decision import usage
    try:
        value = json.loads(raw, object_pairs_hook=unique)
        name = wire['model']
        if value['model'] not in (name, name + '.0'):
            raise ValueError
        provided = value['answers']
        if not isinstance(provided, dict) or set(provided) != set(wire['questions']):
            raise ValueError
        answers = {}
        for key, question in wire['questions'].items():
            options = question['criteria']
            answer = provided[key]
            weights = answer['probabilities']
            choice = answer['choice']
            if (answer['type'] != 'choice' or choice not in options or not isinstance(weights, dict)
                    or set(weights) != set(options)
                    or any(type(v) not in (int, float) or not math.isfinite(v) or not 0 <= v <= 1 for v in weights.values())
                    or not math.isclose(sum(weights.values()), 1, abs_tol=.001)):
                raise ValueError
            answers[key] = {'type': 'choice', 'choice': choice, 'probabilities': weights}
            confidence = answer.get('confidence')
            if type(confidence) in (int, float) and math.isfinite(confidence) and 0 <= confidence <= 1:
                answers[key]['confidence'] = confidence
        return {'model': value['model'], 'answers': answers, 'usage': usage(value), 'decision_source': 'jev'}
    except (ValueError, KeyError, AttributeError, TypeError, OverflowError):
        return None


class Settings:
    def __init__(self, store, config, *, transport=None, probe=None):
        from .scene_jev import JevTransport
        self.store, self.config = store, config
        self.transport = transport or JevTransport
        self.probe = probe or approved_probe
        self.checked = None
        self.check_id = None

    @staticmethod
    def require_edition():
        if not community.enabled():
            raise RuntimeErrorBase('community_jev_unavailable')

    def status(self, persona_id=None, session_id=None):
        self.require_edition()
        value = settings(self.config)
        state = {key: value[key] for key in ('revision', 'enabled', 'base_url', 'name')}
        state['key_configured'] = bool(value['encrypted_key'])
        state['choice_verified'] = bool(value['encrypted_key'])
        state['source'] = 'jev' if value['enabled'] else 'flash'
        if persona_id is not None or session_id is not None:
            with self.store.connection() as db:
                state['session'] = session_grant(db, self.config, persona_id, session_id, value)
        return state

    async def check(self, base_url, name, key, revision):
        self.require_edition()
        saved = settings(self.config)
        self._revision(saved, revision)
        check_id = self.check_id = identifier()
        self.checked = None
        url, name = endpoint(base_url), model_name(name)
        if not isinstance(key, str) or len(key) > 512 or any(c.isspace() or ord(c) < 32 for c in key):
            raise RuntimeErrorBase('community_jev_invalid_key')
        if key:
            secret = protect(key)
        elif url == saved['base_url'] and saved['encrypted_key']:
            secret = saved['encrypted_key']
        else:
            raise RuntimeErrorBase('community_jev_invalid_key')
        contents, expected = self.probe()
        body = {**contents, 'model': name}
        model = community.decision_model(self.config.load()) | {'base_url': url, 'name': name, 'encrypted_key': secret}
        connection = self.transport(model)
        try:
            status, raw = await connection.complete(body, on_dispatch=lambda _: None, on_receipt=lambda _: None,
                                                    diagnostics=lambda _: None)
        finally:
            await connection.close()
        if status != 200:
            raise RuntimeErrorBase('community_jev_http_' + str(status))
        result = choice_result(raw, body)
        if result is None or {k: v['choice'] for k, v in result['answers'].items()} != expected:
            raise RuntimeErrorBase('community_jev_probe_failed')
        self._revision(settings(self.config), revision)
        if check_id != self.check_id:
            raise RuntimeErrorBase('community_jev_check_required')
        token = identifier()
        self.checked = (token, monotonic() + PROBE_SECONDS, revision,
                        {'base_url': url, 'name': name, 'encrypted_key': secret})
        return {'token': token, 'base_url': url, 'name': name, 'connected': True,
                'choice_verified': True, 'usage': result['usage']}

    def activate(self, token, revision):
        self.require_edition()
        with self.store.connection(write=True):
            saved = settings(self.config)
            self._revision(saved, revision)
            checked = self.checked
            if (not isinstance(token, str) or checked is None or checked[2] != revision
                    or not hmac.compare_digest(token, checked[0]) or checked[1] < monotonic()):
                raise RuntimeErrorBase('community_jev_check_required')
            atomic_json(config_path(self.config), dict(checked[3], revision=revision + 1, enabled=True), durable=True)
            self.checked = None
            self.check_id = None
        return self.status()

    def disable(self, revision):
        self.require_edition()
        with self.store.connection(write=True):
            saved = settings(self.config)
            self._revision(saved, revision)
            self.checked = None
            self.check_id = None
            if saved['enabled']:
                atomic_json(config_path(self.config), dict(saved, revision=revision + 1, enabled=False), durable=True)
        return self.status()

    def grant(self, persona_id, session_id, enabled, revision, recipient_hash):
        self.require_edition()
        if type(enabled) is not bool:
            raise RuntimeErrorBase('community_jev_grant_invalid')
        from .private_personas import guard_acceptance_write
        guard_acceptance_write(self.store, persona_id)
        with self.store.connection(write=True) as db:
            saved = settings(self.config)
            current = session_grant(db, self.config, persona_id, session_id, saved)
            if (type(revision) is not int or revision != current['revision']
                    or not current['available'] or recipient_hash != current['recipient']):
                raise RuntimeErrorBase('community_jev_grant_changed')
            save(db, GRANT + session_id,
                 {'revision': revision + 1, 'enabled': enabled, 'recipient': current['recipient']})
            return session_grant(db, self.config, persona_id, session_id, saved)

    @staticmethod
    def _revision(saved, revision):
        if type(revision) is not int or saved['revision'] != revision:
            raise RuntimeErrorBase('community_jev_settings_changed')
