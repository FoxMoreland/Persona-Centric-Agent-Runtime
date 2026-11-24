"""Restore portable business state without importing execution or consent."""

import json

from . import character_pack_format as fmt
from .archive import binding, encoded, fingerprint, scope
from .import_history import META, PREFIX, group
from .models import LOCAL_USER_ID, PRIVATE_CHAT, PersonaSpec, identifier, timestamp
from .scene_policy import save
from .service import RuntimeService


def restore(db, rows, main):
    from . import character, profile, sessions
    from .recall_runtime import enqueue_imported_session
    from .workbench_timeline import save_session_title

    spec = PersonaSpec.from_dict(rows.character['persona_spec'])
    pid = RuntimeService.insert_persona(db, spec)
    character.append(db, pid, rows.character['character'], 'import')
    if rows.character['relationship']:
        profile.append_version(db, pid, 'relationship',
                               {'description': rows.character['relationship']}, actor='import')
    package_id, now = identifier(), timestamp()
    selected = scope(pid, 'desktop')
    sid_map, turn_map, event_map, event_turn = {}, {}, {}, {}
    for session in rows.items('sessions'):
        sid = sid_map[session['id']] = identifier()
        db.execute("INSERT INTO sessions VALUES(?,?,?,'private',?)", (sid, pid, LOCAL_USER_ID, session['created_at']))
        sessions.register(db, dict(db.execute('SELECT * FROM sessions WHERE id=?', (sid,)).fetchone()),
                          selected, reason='character_package')
        save_session_title(db, pid, sid, session['title'])
        save(db, META + sid, {
            'version': 2, 'revision': 1, 'withdrawn': False, 'organize': False,
            'self_role': 'A', 'role': 'B', 'providers': {}, 'imported_at': now,
            'document': package_id, 'prepare_before_chat': False,
            'portable_package': package_id, 'chat_mode': spec.chat_mode or PRIVATE_CHAT,
        })
    for turn in rows.items('turns'):
        tid = identifier()
        turn_map[turn['id']] = tid
        for index, event in enumerate(rows.items('messages', turn['id']), 1):
            event_map[event['id']] = f'{PREFIX}{tid}.{index}'
            event_turn[event['id']] = turn['id']
    exact = {}
    for turn in rows.items('turns'):
        sid, tid = sid_map[turn['session_id']], turn_map[turn['id']]
        sources, first, last, reply, user = {}, None, None, None, None
        for event in rows.items('messages', turn['id']):
            eid = event_map[event['id']]
            last = db.execute('''INSERT INTO events(id,session_id,persona_id,user_id,role,text,created_at)
                VALUES(?,?,?,?,?,?,?)''', (eid, sid, pid, LOCAL_USER_ID, event['role'], event['text'],
                                          event['time'] or turn['created_at'])).lastrowid
            if first is None:
                first = last
            detail = {key: event[key] for key in ('time', 'raw_time', 'speaker', 'role')}
            quote = event['quote']
            if quote is not None:
                detail['quote'] = {'id': event_map.get(quote['id']), 'text': quote['text']}
            sources[eid] = detail
            if event['excluded']:
                db.execute('INSERT INTO source_exclusions VALUES(?,?,?,?,?)',
                           (eid, pid, LOCAL_USER_ID, event['excluded'], now))
            elif event['role'] == 'user' and user is None:
                user = eid
            elif event['role'] == 'persona':
                reply = eid
        origin = {'first_seq': first, 'last_seq': last, 'sources': sources, 'imported_at': now,
                  'portable_package': package_id}
        ordinal = db.execute('''INSERT INTO turns(id,source_key,persona_id,session_id,channel,user_event_id,
            reply_event_id,status,model_snapshot,created_at,updated_at,origin_json)
            VALUES(?,?,?,?,'desktop',?,?,'completed','{}',?,?,?)''',
                             (tid, PREFIX + tid, pid, sid, user, reply, turn['created_at'], turn['updated_at'],
                              encoded(origin))).lastrowid
        native_turn = {'id': tid, 'session_id': sid, 'persona_id': pid, 'origin_json': encoded(origin)}
        result = group(db, native_turn, selected, {'kind': 'turn', 'id': tid}, purpose='evidence')
        proof = binding(result)
        exact[turn['id']] = (ordinal, proof, result.get('recorded_at'))
        cover = rows.get('coverage', turn['id'])
        db.execute('INSERT INTO memory_coverage VALUES(?,?,?,?,?,NULL,?,?,?)',
                   (pid, LOCAL_USER_ID, tid, encoded(selected), proof.get('hash', ''),
                    cover['disposition'], cover['summary_state'], now))
    for sid in sid_map.values():
        updated = db.execute('SELECT max(updated_at) FROM turns WHERE session_id=?', (sid,)).fetchone()[0]
        db.execute('UPDATE session_lifecycle SET last_activity_at=? WHERE session_id=?', (updated, sid))
        enqueue_imported_session(db, pid, sid)
    db.execute("UPDATE session_heads SET session_id=? WHERE persona_id=? AND channel='desktop'",
               (sid_map[main], pid))
    sessions.select(db, pid, 'desktop', sid_map[main])
    batch = {}
    _memories(db, rows, selected, sid_map, event_map, batch, now)
    _summaries(db, rows, selected, sid_map, event_map, event_turn, turn_map, exact, batch, now)
    return RuntimeService._persona(db, pid) | {'session_id': sid_map[main], 'packaged_history': True}


def _batch(db, selected, sid, batches, now):
    if sid not in batches:
        bid = batches[sid] = identifier()
        db.execute('''INSERT INTO memory_batches(id,persona_id,user_id,session_id,scope_json,work_key,purpose,
            state,data_json,created_at,updated_at) VALUES (?,?,?,?,?,?,'character_package','completed',?,?,?)''',
                   (bid, selected['persona_id'], LOCAL_USER_ID, sid, encoded(selected), bid,
                    encoded({'payload': {'events': []}}), now, now))
    return batches[sid]


def _memories(db, rows, selected, sid_map, event_map, batches, now):
    pid = selected['persona_id']
    ids = {row['id']: identifier() for row in rows.items('memories')}
    # The portable revision chain was validated before the write transaction.
    for (payload,) in db_ordered_memories(rows):
        row = json.loads(payload)
        mid, source = ids[row['id']], event_map[row['event_id']]
        db.execute('INSERT INTO memories VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                   (mid, pid, LOCAL_USER_ID, row['category'], row['text'], source, ids[row['root_id']],
                    ids.get(row['previous_id']), row['revision'], row['status'], row['created_at'], row['updated_at']))
        for entry in row['sources']:
            db.execute('INSERT INTO memory_sources VALUES(?,?,?)', (mid, event_map[entry['event_id']], entry['quote']))
        fact = row['fact'] or {'subject': '', 'attribute': '', 'valid_from': None, 'valid_until': None,
                               'automatic': False, 'manual': False, 'blocked': False}
        origin = rows.get('turns', rows.get('messages', row['event_id'])['turn_id'])
        bid = _batch(db, selected, sid_map[origin['session_id']], batches, now) if fact['automatic'] else None
        detail = {'portable_sessions': [sid_map[key] for key in row['sessions']],
                  'manual_authority': fact['manual'], 'import_review_required': fact['blocked']}
        db.execute('INSERT INTO memory_facts VALUES(?,?,?,?,?,?,?,?,?,?)',
                   (mid, pid, LOCAL_USER_ID, encoded(selected), fact['subject'], fact['attribute'],
                    fact['valid_from'], fact['valid_until'], bid, encoded(detail)))


def db_ordered_memories(rows):
    return rows.db.execute("""SELECT payload FROM rows WHERE section='memories'
        ORDER BY json_extract(payload,'$.root_id'), json_extract(payload,'$.revision')""")


def _summaries(db, rows, selected, sid_map, event_map, event_turn, turn_map, exact, batches, now):
    pid = selected['persona_id']
    tasks = {}
    for row in rows.items('summaries'):
        sid = sid_map[row['session_id']]
        items, events, covered = [], {}, set()
        for item in row['items']:
            sources = []
            for source in item['sources']:
                key = source['event_id']
                eid, turn = event_map[key], event_turn[key]
                if exact[turn][1]['status'] != 'found':
                    break
                event = rows.get('messages', key)
                events[eid] = {'id': eid, 'text': event['text'], 'role': event['role'], 'kind': 'event',
                               'turn_id': turn_map[turn], 'turn': exact[turn][0]}
                covered.add(turn)
                sources.append({'event_id': eid, 'quote': source['quote']})
            else:
                items.append({'text': item['text'], 'sources': sources})
        if not items:
            continue
        used = {source['event_id'] for item in items for source in item['sources']}
        events = {eid: data for eid, data in events.items() if eid in used}
        if row['kind'] == 'fragment':
            translated = [{'text': item['text'], 'sources': [
                {'source_id': source['event_id'], 'quote': source['quote']} for source in item['sources']] }
                          for item in items]
            payload = {'scope': selected, 'portable_package': True, 'events': list(events.values()), 'bindings': [
                {'turn_id': turn_map[key], 'binding': exact[key][1]} for key in sorted(covered)],
                'recorded_times': {turn_map[key]: exact[key][2] for key in covered}}
            db.execute("INSERT INTO memory_fragments VALUES(?,?,?,?,?,NULL,'active',?,?)",
                       (identifier(), pid, LOCAL_USER_ID, sid, _batch(db, selected, sid, batches, now),
                        encoded({'payload': payload, 'items': translated}), row['created_at']))
            continue
        if sid not in tasks:
            task = tasks[sid] = identifier()
            # Legacy summaries require a task foreign key. This local, terminal
            # provenance row has no provider, attempt, payload or dispatch lease.
            db.execute("""INSERT INTO organization_tasks(id,request_id,persona_id,user_id,session_id,channel,
                mode,fingerprint,payload_json,model_snapshot,status,created_at,updated_at)
                VALUES(?,?,?,?,?,'desktop','character_package',?,'{}','{}','completed',?,?)""",
                       (task, 'character-package:' + task, pid, LOCAL_USER_ID, sid, fingerprint(row['id']), now, now))
        ordinals = sorted({event['turn'] for event in events.values()})
        refs = [{'id': eid, 'kind': 'event', 'hash': fingerprint(event['text'])} for eid, event in events.items()]
        reason = 'automatic_memory' if row['kind'] == 'automatic_summary' else 'character_package'
        db.execute("""INSERT INTO summaries(id,persona_id,user_id,session_id,channel,task_id,previous_id,revision,
            start_row,end_row,items_json,sources_json,covered_json,status,reason,created_at,source_refs_json)
            VALUES(?,?,?,?,'desktop',?,NULL,?,?,?,?,?,?,'active',?,?,?)""",
                   (identifier(), pid, LOCAL_USER_ID, sid, tasks[sid], row['revision'], ordinals[0], ordinals[-1],
                    encoded(items), encoded(sorted(events)), encoded(ordinals), reason, row['created_at'], encoded(refs)))
