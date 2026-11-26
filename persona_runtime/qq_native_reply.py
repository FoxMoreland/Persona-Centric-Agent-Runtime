"""Bind native private replies to one SnowLuma conversation and its delivery receipts."""
import json
from datetime import datetime, timezone

from .archive import fingerprint,scope
from .models import RuntimeErrorBase
from .sessions import owned


FIXED_WINDOW_PREFIX = 'qq.native.fixed_window.'
FULL_LOCAL_FILE = 'private/qq-full-local-required.json'


def full_local_required(paths):
    return (paths.home/FULL_LOCAL_FILE).is_file()


def full_local_ready(paths, cfg, persona_id, session_id=None):
    if not full_local_required(paths):
        return False
    saved = json.loads((paths.home/FULL_LOCAL_FILE).read_bytes())
    if saved.get('state') != 'ready':
        return False
    if session_id is None:
        session_id = saved.get('session')
    if (not session_id or not configured(paths,cfg,persona_id) or (paths.home/'private/qq-pro-test.json').exists()
            or saved != {'state':'ready','scope':'bound_private_only','web_lookup':False,
                         'knowledge_sync':False,'allow_pro_only':False,'instance':cfg['instance_id'],
                         'persona':persona_id,'bot':cfg['bot_qq'],'peer':cfg['owner_qq'],
                         'session':session_id}):
        raise RuntimeErrorBase('qq_full_local_binding_changed')
    return True


def fixed_window(db, session_id):
    row = db.execute('SELECT value FROM knowledge_control WHERE key=?',
                     (FIXED_WINDOW_PREFIX+session_id,)).fetchone()
    if not row:
        return None
    milliseconds = json.loads(row[0])
    if type(milliseconds) is not int or not 1000 <= milliseconds <= 60000:
        raise RuntimeErrorBase('qq_fixed_window_invalid')
    return milliseconds


def configured(paths, cfg, persona_id):
    path = paths.home / 'private/qq-native-reply.json'
    if not path.is_file():
        return False
    value = json.loads(path.read_text(encoding='utf-8'))
    matches = (cfg.get('qq_backend') == 'snowluma'
            and cfg['runtime']['qq_persona_id'] == persona_id
            and bool(cfg['bot_qq']) and bool(cfg['owner_qq'])
            and cfg['bot_qq'] != cfg['owner_qq']
            and value == {'instance': cfg['instance_id'], 'persona': persona_id,
                          'bot': cfg['bot_qq'], 'peer': cfg['owner_qq']})
    if not matches:
        raise RuntimeErrorBase('qq_native_binding_changed')
    return True


def assert_bound(db, paths, cfg, turn):
    persona_id = turn['persona_id']
    origin = {'bot': cfg['bot_qq'], 'sender': cfg['owner_qq']}
    if (turn['channel'] != 'qq' or not configured(paths, cfg, persona_id)
            or json.loads(turn['origin_json'] or 'null') != origin):
        raise RuntimeErrorBase('qq_native_binding_changed')
    owned(db, turn['session_id'], scope(persona_id, 'qq', origin['bot'], origin['sender']))
    return origin


def pro_test(paths, cfg, persona_id, session_id=None):
    path = paths.home/'private/qq-pro-test.json'
    if not path.is_file():
        return False
    from .provider_policy import is_official_deepseek
    saved = json.loads(path.read_text(encoding='utf-8'))
    if (not configured(paths,cfg,persona_id) or cfg['model']['name']!='deepseek-v4-pro'
            or not is_official_deepseek(cfg['model']) or not saved.get('session')
            or saved != {'instance':cfg['instance_id'], 'persona':persona_id,
                         'bot':cfg['bot_qq'], 'peer':cfg['owner_qq'],
                         'session':saved['session'], 'model':fingerprint(cfg['model'])}
            or (session_id is not None and saved['session']!=session_id)):
        raise RuntimeErrorBase('qq_pro_test_scope_changed')
    return True


class QQNativeDelivery:
    def __init__(self, store, config, publication):
        self.store = store
        self.config = config
        self.publication = publication

    def next(self, presenter):
        cfg = self.config.load()
        persona_id = cfg['runtime']['qq_persona_id']
        if not configured(self.store.paths, cfg, persona_id):
            return {'state': 'disabled'}
        with self.store.connection() as db:
            row = db.execute("""
                SELECT t.id AS turn_id, t.session_id, t.persona_id, t.channel, t.origin_json,
                       u.id AS unit_id, p.id AS publication_id, p.revision, p.reason, i.ordinal, i.state,
                       i.delay_ms
                FROM expression_speech_units u
                JOIN turns t ON t.id=u.turn_id
                JOIN expression_speech_publications p ON p.utterance_id=u.id
                JOIN expression_speech_items i ON i.publication_id=p.id AND i.revision=p.revision
                WHERE t.persona_id=? AND t.channel='qq' AND u.source_kind='maibot_private_reply'
                  AND json_extract(t.origin_json,'$.bot')=? AND json_extract(t.origin_json,'$.sender')=?
                  AND (p.reason='qq_delivery_unknown'
                       OR (p.state NOT IN ('closed','complete') AND i.state IN ('offered','unknown','pending')))
                ORDER BY (p.reason='qq_delivery_unknown' OR i.state IN ('offered','unknown')) DESC,
                         t.rowid, u.rowid, i.ordinal
                LIMIT 1""", (persona_id, cfg['bot_qq'], cfg['owner_qq'])).fetchone()
            if row is None:
                return {'state': 'empty'}
            row = dict(row)
            origin = assert_bound(db, self.store.paths, cfg, row)
            if row['state'] == 'pending' and row['ordinal'] and row['delay_ms']:
                previous = db.execute('''SELECT presented_at FROM expression_speech_visible
                    WHERE publication_id=? AND ordinal=?''', (row['publication_id'], row['ordinal']-1)).fetchone()
                if previous:
                    elapsed = (datetime.now(timezone.utc) - datetime.fromisoformat(previous[0])).total_seconds()
                    if elapsed * 1000 < row['delay_ms']:
                        return {'state': 'waiting'}
        if row['reason'] == 'qq_delivery_unknown' or row['state'] in ('offered', 'unknown'):
            return {'state': 'unsettled'}
        offer = self.publication.offer(persona_id, row['session_id'], row['unit_id'], row['publication_id'],
                                       row['revision'], row['ordinal'], presenter, qq_origin=origin)
        if offer['state'] == 'offered' and 'body' not in offer:
            return {'state': 'unsettled'}
        if offer['state'] != 'offered':
            return {'state': offer['state']}
        return {**offer, 'utterance_id': row['unit_id'], 'session_id': row['session_id'],
                'persona_id': persona_id, 'origin': origin}

    def result(self, value):
        cfg = self.config.load()
        persona_id = cfg['runtime']['qq_persona_id']
        with self.store.connection() as db:
            row = db.execute("""SELECT t.* FROM expression_speech_units u JOIN turns t ON t.id=u.turn_id
                                WHERE u.id=? AND u.source_kind='maibot_private_reply'""",
                             (value['utterance_id'],)).fetchone()
            if not row or row['persona_id'] != persona_id:
                raise RuntimeErrorBase('qq_native_binding_changed')
            origin = assert_bound(db, self.store.paths, cfg, dict(row))
        return self.publication.acknowledge(persona_id, row['session_id'], value['utterance_id'],
                                            value['id'], value['lease_id'], value['presenter'],
                                            'qq-delivery.'+value['lease_id'], value['body_hash'],
                                            value['state'], qq_origin=origin,
                                            platform_message_id=value.get('platform_message_id'))
