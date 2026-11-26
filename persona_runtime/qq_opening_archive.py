"""Archive the first confirmed QQ speech without inventing an incoming message."""
import json

from .archive import encoded,scope
from .models import LOCAL_USER_ID,RuntimeErrorBase,identifier,require_text,timestamp
from .qq_native_reply import configured
from .sessions import owned


def _key(session_id):
    return 'qq.first.confirmed.'+session_id


def record_confirmed(store, config, persona_id, session_id, body, platform_message_id):
    require_text(body,'body')
    require_text(platform_message_id,'platform_message_id')
    cfg = config.load()
    if not configured(store.paths,cfg,persona_id):
        raise RuntimeErrorBase('qq_native_binding_changed')
    origin = scope(persona_id,'qq',cfg['bot_qq'],cfg['owner_qq'])
    key = _key(session_id)
    with store.connection(write=True) as db:
        owned(db,session_id,origin)
        existing = db.execute('SELECT value FROM knowledge_control WHERE key=?',(key,)).fetchone()
        if existing:
            entry = json.loads(existing[0])
            event = db.execute('SELECT text FROM events WHERE id=?',(entry['event_id'],)).fetchone()
            if entry['platform_message_id'] != platform_message_id or not event or event[0] != body:
                raise RuntimeErrorBase('qq_opening_receipt_conflict')
            return entry['event_id']
        if db.execute('SELECT 1 FROM events WHERE session_id=?',(session_id,)).fetchone():
            raise RuntimeErrorBase('qq_opening_session_not_empty')
        event_id = identifier()
        db.execute("""INSERT INTO events(id,session_id,persona_id,user_id,role,text,created_at)
                      VALUES(?,?,?,?, 'persona',?,?)""",(event_id,session_id,persona_id,LOCAL_USER_ID,body,timestamp()))
        db.execute('INSERT INTO knowledge_control VALUES (?,?)',
                   (key,encoded({'origin':origin,'event_id':event_id,'platform_message_id':platform_message_id})))
        return event_id


def opening_material(db, turn):
    from .archive import turn_scope
    if turn['channel'] != 'qq':
        return None
    row = db.execute('SELECT value FROM knowledge_control WHERE key=?',(_key(turn['session_id']),)).fetchone()
    if not row:
        return None
    entry = json.loads(row[0])
    if entry['origin'] != turn_scope(db,turn):
        return None
    row = db.execute("""SELECT id,text,created_at FROM events WHERE id=? AND session_id=? AND persona_id=?
            AND role='persona' AND NOT EXISTS(SELECT 1 FROM source_exclusions WHERE event_id=events.id)""",
                     (entry['event_id'],turn['session_id'],turn['persona_id'])).fetchone()
    return {'id':row['id'],'role':'assistant','text':row['text'],'time':row['created_at']} if row else None
