"""Explicit sharing of a published memory between a desktop and its bound QQ session."""
import json

from . import session_privacy
from .archive import encoded, find_turn
from .context_builders import memory_usable
from .models import LOCAL_USER_ID, RuntimeErrorBase
from .qq_native_reply import assert_bound


PREFIX = 'qq.native.memory.'


def _key(target, memory):
    return PREFIX + target + '.' + memory


def _authority(db, store, config, memory, target):
    source = find_turn(db, 'event', memory['source_event_id'])
    if not source or source['status'] != 'completed' or source['persona_id'] != memory['persona_id']:
        return None
    if target['persona_id'] != source['persona_id'] or {source['channel'], target['channel']} != {'desktop','qq'}:
        return None
    if memory['status'] != 'active' or not memory_usable(db,source,memory):
        return None
    from .ordinary_materials import memory_in_session
    pid = source['persona_id']
    if not memory_in_session(db,memory['id'],pid,source['session_id']):
        return None
    cfg = config.load()
    try:
        assert_bound(db,store.paths,cfg,source if source['channel']=='qq' else target)
        destinations = session_privacy.recipients(config,cfg)
        grants = [session_privacy.current(db,config,cfg,pid,turn['session_id'],destinations)
                  for turn in (source,target)]
    except RuntimeErrorBase:
        return None
    if not all(grant['enabled'] for grant in grants):
        return None
    return {'source':source['session_id'], 'target':target['session_id'],
            'revision':memory['revision'], 'consent_revisions':[g['revision'] for g in grants],
            'recipient_hash':grants[0]['recipient_hash']}


def readable(db, entry, turn, memory):
    row = db.execute('SELECT value FROM knowledge_control WHERE key=?',
                     (_key(turn['session_id'],memory['id']),)).fetchone()
    if not row:
        return False
    authority = _authority(db,entry.store,entry.config,memory,turn)
    return authority is not None and json.loads(row[0]) == authority


def update(store, config, persona_id, memory_id, target_session_id, enabled):
    if type(enabled) is not bool:
        raise RuntimeErrorBase('qq_memory_invalid_operation')
    key = _key(target_session_id,memory_id)
    with store.connection(write=True) as db:
        memory = db.execute('SELECT * FROM memories WHERE id=? AND persona_id=? AND user_id=?',
                            (memory_id,persona_id,LOCAL_USER_ID)).fetchone()
        session = db.execute("""SELECT l.channel,l.bot,l.peer FROM session_lifecycle l
            JOIN sessions s ON s.id=l.session_id WHERE s.id=? AND s.persona_id=? AND s.user_id=?""",
                            (target_session_id,persona_id,LOCAL_USER_ID)).fetchone()
        if not memory or not session:
            raise RuntimeErrorBase('qq_memory_scope_unavailable')
        if not enabled:
            db.execute('DELETE FROM knowledge_control WHERE key=?',(key,))
            return {'enabled':False}
        target = {'persona_id':persona_id, 'session_id':target_session_id, 'channel':session['channel'],
                  'origin_json':encoded({'bot':session['bot'], 'sender':session['peer']})}
        authority = _authority(db,store,config,dict(memory),target)
        if not authority:
            raise RuntimeErrorBase('qq_memory_share_not_authorized')
        db.execute('INSERT OR REPLACE INTO knowledge_control VALUES (?,?)',(key,encoded(authority)))
        return {'enabled':True}
