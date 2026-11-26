"""Same-session private dialogue material. Internal records and unpainted drafts stay outside the dialogue."""
import json
from contextlib import contextmanager
from contextvars import ContextVar

from .archive import fingerprint, read_exact, turn_scope
from .config import ConfigStore
from .context_builders import memory_usable, portable_memory_sessions
from .import_history import chronological, history_allowed
from .knowledge_routing import configured_model
from .models import RuntimeErrorBase
from .session_privacy import current, destination, recipients
from .turn_input import load_turn_input

_access = ContextVar('private_history_access', default=None)


@contextmanager
def history_access(db, session, enabled):
    token = _access.set((db, session) if enabled else None)
    try:
        yield
    finally:
        _access.reset(token)


def session_access(db, sid):
    return _access.get() == (db, sid)


def full_revision(store, db, pid, sid, model, role):
    if not sid or not db.execute("""SELECT 1 FROM session_privacy_events
            WHERE persona_id=? AND session_id=? AND enabled=1 LIMIT 1""",(pid,sid)).fetchone():
        return None
    config = ConfigStore(store.paths)
    cfg = config.load()
    from .private_reply_entry import PRIVATE_ROUTES,retired,selected
    if selected(db,cfg['instance_id'],pid) not in PRIVATE_ROUTES or retired(db,cfg['instance_id']):
        return None
    endpoints = recipients(config,cfg)
    state = current(db,config,cfg,pid,sid,endpoints)
    if state['enabled'] and endpoints.get(role) == destination(config,model):
        return state['revision']
    return None


def memory_in_session(db, mid, pid, sid):
    from .import_memory_scope import memory_source_ids
    ids = memory_source_ids(db,mid)
    return bool(ids) and all(db.execute("""SELECT 1 FROM events WHERE id=? AND persona_id=? AND session_id=?""",
                                        (event,pid,sid)).fetchone() for event in ids)


def main_history_access(store,db,pid,sid):
    full=store and full_revision(store,db,pid,sid,ConfigStore(store.paths).load()['model'],'main')
    return history_access(db,sid,bool(full))


@contextmanager
def memory_access(store,db,payload,model):
    revision=(payload.get('automatic_memory') or {}).get('session_privacy_revision')
    if revision is None:
        yield
        return
    sid=payload['session_id']
    if revision!=full_revision(store,db,payload['persona_id'],sid,model,'organization'):
        raise RuntimeErrorBase('private_cloud_provider_not_authorized')
    with history_access(db,sid,True):
        yield


def _dialogue(db, turn, tid, jev, full):
    from .import_history import group
    source = db.execute("SELECT * FROM turns WHERE id=? AND persona_id=? AND session_id=?",
                        (tid,turn['persona_id'],turn['session_id'])).fetchone()
    if not source or source['status'] != 'completed':
        return []
    source = dict(source)
    target = {'kind':'turn','id':tid}
    scope = turn_scope(db,turn)
    imported = source['source_key'].startswith('imported.')
    if imported:
        if not full and not history_allowed(db,turn['session_id'],'jev',jev):
            return []
        result = group(db,source,scope,target)
    else:
        result = read_exact(db,scope,target)
    if result.get('status') != 'found':
        return []
    rows = []
    for event in chronological(result):
        role=event.get('role','assistant')
        if role != 'user' and not imported:
            if not (turn['channel']=='qq' and event.get('kind')=='part' and event.get('state')=='sent') and not _confirmed(db,tid,event):
                continue
        rows.append({'id':event['id'],'role':role,'text':event['text'],
                     'time':event.get('created_at') or source['created_at']})
    return rows


def _confirmed(db,tid,event):
    from .tk_notice import already_presented
    if event.get('kind')=='search_notice' and already_presented(db,tid):
        return True
    for pubs, visible in (('expression_speech_publications','expression_speech_visible'),
                          ('expression_publications','expression_utterances')):
        pub=db.execute(f"SELECT id FROM {pubs} WHERE canonical_event_id=?",(event['id'],)).fetchone()
        if pub:
            parts=db.execute(f"SELECT ordinal,text FROM {visible} WHERE publication_id=? ORDER BY ordinal",(pub[0],))
            prefix=[]
            for ordinal,text in parts:
                if ordinal!=len(prefix):
                    break
                prefix.append(text)
            return bool(prefix) and ''.join(prefix)==event['text']
    return False


def collect(entry, db, turn, contract, frozen=None):
    from .chat import terms
    from .references import prepare, ReferenceError
    from .profile import model_persona_data, source_blocked
    from . import desktop_messages, quote_context
    pid,sid = turn['persona_id'],turn['session_id']
    full = contract['consent']['enabled']
    jev = configured_model(entry.store.paths)
    if not full:
        from .private_personas import guard_cloud_dispatch
        guard_cloud_dispatch(entry.store,pid,'knowledge_router',jev,db=db)
    profile = json.loads(turn['profile_snapshot'])
    if any(source_blocked(db,item['source_ids']) for item in profile.values()):
        raise RuntimeErrorBase('private_reply_source_changed')
    current_rows = load_turn_input(db,turn)
    current_ids = [e['id'] for e in current_rows]
    catalog = {e['id']:{'id':e['id'],'text':e['text'],'role':'user','time':e['created_at']} for e in current_rows}
    recent_ids, refs, memory_ids, quoted_memory_ids = [], [], [], []
    previous_exchange = []
    writer_history = []
    quote = None
    with history_access(db,sid,full):
        quote = quote_context.read(db, turn)
        history = frozen['history'] if frozen else [] if quote else [r[0] for r in db.execute("""SELECT id FROM turns WHERE session_id=? AND persona_id=? AND channel=?
            AND status='completed' AND rowid<(SELECT rowid FROM turns WHERE id=?) ORDER BY rowid DESC LIMIT 4""",
            (sid,pid,turn['channel'],turn['id']))]
        if turn['channel']=='qq' and not quote and len(history)<4:
            from .qq_opening_archive import opening_material
            opening = opening_material(db,turn)
            if opening and (not frozen or opening['id'] in frozen['admitted_ids']):
                catalog[opening['id']] = opening
                recent_ids.append(opening['id'])
                if not history:
                    previous_exchange.append(opening['id'])
        for previous in reversed(history):
            for item in _dialogue(db,turn,previous,jev,full):
                catalog[item['id']] = item
                recent_ids.append(item['id'])
                if previous==history[0]:
                    previous_exchange.append(item['id'])
        legacy_refs = []
        if quote:
            anchor = quote['anchor']
            refs = [anchor['id']]
            recent_ids = [row['id'] for row in quote['latest']]
            catalog.update({row['id']: quote_context.projection(row) for row in
                            [anchor, *quote_context.neighborhood(quote), *quote['latest']]})
        else:
            try:
                _, manifest = prepare(db,turn)
            except ReferenceError as exc:
                raise RuntimeErrorBase('private_reply_reference_unavailable') from exc
            legacy_refs = manifest['references']
        for ref in legacy_refs:
            target = ref['target']
            rows = _dialogue(db,turn,ref['turn_id'],jev,full)
            accepted=json.loads(db.execute("SELECT binding_json FROM input_references WHERE event_id=?",
                                (ref['input_event_id'],)).fetchone()[0])
            retained=set(accepted['member_ids']+accepted['reply_ids'])
            scoped = [e for e in rows if e['id'] in retained and (target['kind']=='turn' or e['id']==target['id'])]
            if not scoped:
                raise RuntimeErrorBase('private_reply_reference_unavailable')
            for e in scoped:
                catalog[e['id']] = e
                if e['id'] not in refs:
                    refs.append(e['id'])
        query = terms(' '.join(e['text'] for e in current_rows))
        from .context_memory import quote_order, quote_priorities, recency
        from time import time
        now=time()
        pinned=quote_priorities(db,turn,quote) if not frozen or frozen.get('quoted_memories') else {}
        if frozen and quote:
            pinned={mid:distance for mid,distance in pinned.items() if mid in frozen.get('quoted_memories',())}
        frozen_order={mid:i for i,mid in enumerate(frozen['memories'])} if frozen else {}
        memories = []
        selection = (" AND m.id IN (SELECT value FROM json_each(?))" if frozen else "")
        parameters = (pid,sid,sid,sid,json.dumps(frozen['memories'])) if frozen else (pid,sid,sid,sid)
        memory_query="""SELECT m.* FROM memories m JOIN events e ON e.id=m.source_event_id
                LEFT JOIN memory_facts f ON f.memory_id=m.id
                WHERE m.persona_id=? AND (e.session_id=? OR EXISTS(
                  SELECT 1 FROM json_each(f.data_json,'$.portable_sessions') WHERE value=?) OR EXISTS(
                  SELECT 1 FROM knowledge_control k WHERE k.key='qq.native.memory.'||?||'.'||m.id))
                AND m.status='active'""" + selection
        if pinned:
            memory_query+=" ORDER BY (m.id IN (SELECT value FROM json_each(?))) DESC,m.rowid DESC LIMIT 128"
            parameters=(*parameters,json.dumps(list(pinned)))
        else:
            memory_query+=" ORDER BY m.rowid DESC LIMIT 128"
        for row in db.execute(memory_query,parameters):
            item = dict(row)
            from .qq_shared_memory import readable
            if not (full and readable(db,entry,turn,item)):
                if not portable_memory_sessions(db,item['id']) and not memory_in_session(db,item['id'],pid,sid):
                    continue
                if not memory_usable(db,turn,item):
                    continue
                if not full and not memory_usable(db,turn,item,purpose='jev',providers=jev):
                    continue
            if not quote or item["id"] in pinned:
                score=len(query & terms(item['text']))
                memories.append(((*quote_order(pinned,item['id']),score,recency(db,item,now)),item))
        key=(lambda pair:-frozen_order[pair[1]['id']]) if frozen else (lambda pair:pair[0])
        for _,item in sorted(memories,key=key,reverse=True)[:8]:
            mid=item['id']
            catalog[mid]={'id':mid,'role':'memory','text':item['text'],'revision':item['revision']}
            memory_ids.append(mid)
            if mid in pinned:
                quoted_memory_ids.append(mid)
        from .import_history import META
        from .scene_policy import read as read_state
        if not quote and read_state(db, META + sid):
            ids = frozen.get('writer_turns', []) if frozen else [row[0] for row in db.execute('''SELECT id FROM turns
                WHERE session_id=? AND persona_id=? AND channel='desktop' AND status='completed'
                  AND cancelled_at IS NULL AND (user_event_id IS NOT NULL OR reply_event_id IS NOT NULL)
                  AND rowid < (SELECT rowid FROM turns WHERE id=?) ORDER BY rowid DESC LIMIT 50''', (sid, pid, turn['id']))]
            for previous in reversed(ids):
                for item in _dialogue(db, turn, previous, jev, full):
                    catalog[item['id']] = item
                    writer_history.append(item['id'])
        else:
            ids = []
        if not quote and desktop_messages.branch(db, sid):
            prefix = desktop_messages.recent(db, pid, desktop_messages.segments(db, pid, sid, include_own=False), 20)
            catalog.update({row['id']: quote_context.projection(row) for row in prefix})
            writer_history = [row['id'] for row in prefix] + recent_ids
            if not recent_ids:
                recent_ids = writer_history[-10:]
    from .character_context import select_context, unset_fact
    from .import_card_publish import uses_sparse_card
    chosen=select_context(json.loads(turn['character_snapshot']), ' '.join(row['text'] for row in current_rows),
                          example_limit=0)
    character=[]
    omit_unset = uses_sparse_card(db, pid)
    for part in chosen['parts']:
        if omit_unset and unset_fact(part):
            continue
        ident='character/'+part['id']
        catalog[ident]={'id':ident,'role':'character','text':part['text'],'version':chosen['version_id']}
        character.append(ident)
    if frozen and 'admitted_ids' in frozen:
        admitted=set(frozen['admitted_ids'])
        recent_ids=[i for i in recent_ids if i in admitted]
        previous_exchange=[i for i in previous_exchange if i in admitted]
        writer_history=[i for i in writer_history if i in admitted]
        catalog={k:v for k,v in catalog.items() if k in admitted}
    from . import message_time
    time_proof = {}
    if message_time.enabled(turn) and (frozen is None or frozen.get(message_time.KEY)==message_time.VERSION):
        dialogue_ids = set(current_ids + recent_ids + refs + writer_history).intersection(catalog)
        times = message_time.event_times(db, dialogue_ids)
        time_proof = {'message_time_bindings':times}
        for eid in dialogue_ids:
            item = catalog[eid]
            entries = message_time.wire_times(times.get(eid, []))
            item.pop('time', None)
            if entries:
                item['message_times'] = entries
    return {**time_proof, **({'quote_window': quote} if quote else {}),
            **({'quoted_memories':quoted_memory_ids} if quoted_memory_ids else {}),
            **({'writer_history': writer_history, 'writer_turns': ids} if writer_history else {}),
            'current':current_ids,'recent':recent_ids,'references':refs,'memories':memory_ids,'character':character,
            'history':history,'previous_exchange':previous_exchange,'jev_model':jev['name'],
            'catalog':catalog,'persona':{kind:model_persona_data(item['data']) for kind,item in profile.items()},
            'profile_versions':{kind:item['id'] for kind,item in profile.items()}}


def selected_proof(material, ids):
    return {i:fingerprint(material['catalog'][i]) for i in ids if i in material['catalog']}
