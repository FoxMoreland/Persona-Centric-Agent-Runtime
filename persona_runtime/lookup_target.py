"""Resolve a lookup subject against scoped conversation and acquisition evidence."""
import json
import re
from datetime import datetime, timedelta, timezone

from .archive import encoded, find_turn, fingerprint
from .knowledge_native import NO_QUERY, SENSITIVE_QUERY
from .knowledge_native_memory import eligible
from .knowledge_service import read, turn_usable
from .models import RuntimeErrorBase

VERSION = 'lookup-target-v1'
SHORT_REFS_VERSION = 'lookup-target-v2'
RECENT_CONTEXT_VERSION = 'lookup-target-v3'
QUOTED_TARGET_VERSION = 'lookup-target-v4'
AUTOMATIC_VERSIONS = (RECENT_CONTEXT_VERSION, QUOTED_TARGET_VERSION)
VERSIONS = (VERSION, SHORT_REFS_VERSION, *AUTOMATIC_VERSIONS)
RECENT_TURNS = 25
NODE = 'lookup.target'
RECOVERY_NODE = 'lookup.target.recovery'
PREFIX = 'private.lookup.target.'
CATALOGUE_PREFIX = 'private.lookup.catalogue.'
TITLE_SEPARATOR = re.compile(r'\s+[-–—|｜]\s+')
SUBJECT_REFERENCE = re.compile(
    r'(?:(?:[这這那哪](?:[一二两兩三四五六七八九十幾几\d]+)?(?:[位个個种種项項条條次场場款部对對方些件只隻人]|一个|一個)?'
    r'|[他她它牠][们們]?|彼此|双方|雙方|两者|兩者|二者)'
    r'|(?:this|that|these|those)(?:\s+(?:ones?|both|(?:\d+|two|three)\s+(?:ones?|people)))?'
    r'|(?:he|she|it|they|him|her|them|both)(?:\s+of\s+them)?)', re.I)
SUBJECT_JOIN = re.compile(r'[、+＋]|和|與|与|及|\s+and\s+', re.I)
PAIR_REFERENCE = re.compile(r'^[这這那哪][二两兩2]|^(?:两者|兩者|二者|双方|雙方|both)\b|\b(?:two|2)\b', re.I)


def subject_is_reference(text):
    """A standalone grammatical reference cannot be serialized as a public target."""
    return SUBJECT_REFERENCE.fullmatch(text.strip(' \t\u3000“”‘’「」『』"\'')) is not None


def _omit_subject(field, pin):
    if pin and field['ref']==pin['ref']:
        start,end=pin['start'],pin['end']
        return dict(field,text=field['text'][:start]+' '*(end-start)+field['text'][end:])
    return field


def subject_members(text):
    return [member.strip() for member in SUBJECT_JOIN.split(text)]


def saved(db, tid):
    row = db.execute('SELECT value FROM knowledge_control WHERE key=?', (PREFIX + tid,)).fetchone()
    return json.loads(row[0]) if row else None


def for_query(db, tid):
    proof=saved(db,tid)
    if proof:
        from . import public_lookup
        if public_lookup.active(db,tid):
            manual=db.execute("""SELECT query_json,confirmation_id FROM private_lookup_intents WHERE turn_id=?
                AND origin='confirmed' AND state='ready'""",(tid,)).fetchone()
            if manual and manual[1]:
                return {'status':'confirmed','task':json.loads(manual[0]),'attempt_id':proof.get('attempt_id'),
                        'previous_target_hash':fingerprint(proof)}
    return proof


def frozen_version(db, tid):
    row = db.execute('SELECT source_json FROM private_reply_source_sets WHERE turn_id=?', (tid,)).fetchone()
    return json.loads(row[0]).get('lookup_target_version') if row else None


def enrolled(db, tid, providers):
    version = frozen_version(db,tid)
    if version and (version not in VERSIONS or not getattr(providers, 'target_resolution', False)
                    or version != getattr(providers, 'lookup_target_version', VERSION)):
        raise RuntimeErrorBase('private_chat_review_version_changed')
    return bool(version)


def excluded_sources(db, origin):
    return {r[0] for r in db.execute("""SELECT json_extract(f.spec_json,'$.turn_source[1]')
        FROM local_knowledge_facts f WHERE json_extract(f.spec_json,'$.turn_source[0]')=?
        AND (f.withdrawn=1 OR EXISTS (SELECT 1 FROM knowledge_control k
             WHERE k.key='private.study.withdrawn_document.'||f.document_id))""", (origin,))}


def visible_sources(db,turn,sources):
    from . import persona_world
    from hashlib import sha256
    owner=db.execute('SELECT instance_id FROM private_reply_turns WHERE turn_id=?',(turn['id'],)).fetchone()
    if not owner or not persona_world.has_withdrawals(db,owner[0],turn['persona_id']):
        return sources
    return [s for s in sources if not (s.get('url') or '').startswith(('https://','http://')) or not persona_world.withdrawn(
        db,owner[0],turn['persona_id'],s['url'],sha256((s.get('content') or s.get('summary') or '').encode()).hexdigest())]


def _research(db, turn, consent, origin, *, compact=False):
    from .private_lookup import evidence, intent
    from .knowledge_native_memory import _origin_query
    pid, sid = turn['persona_id'], turn['session_id']
    old = intent(db, origin)
    if old and old['origin'] == 'session':
        same = db.execute("""SELECT 1 FROM private_reply_turns a JOIN private_reply_turns b
            ON a.instance_id=b.instance_id WHERE a.turn_id=? AND b.turn_id=?""",(origin,turn['id'])).fetchone()
        if not same or not consent['enabled'] or consent['revision'] != old['consent_revision']:
            return []
    if not turn_usable(db, origin):
        return []
    data = evidence(db, pid, sid, origin)
    if not data:
        return []
    if old:
        question = json.loads(old['query_json'] or '{}').get('question')
        sources = data['sources']
        stored = db.execute('SELECT created_at FROM private_lookup_records WHERE turn_id=?',(origin,)).fetchone()
        if not stored:
            return []
        acquired = stored[0]
    else:
        record = read(db, 'conversation.record.' + origin, {})['data']
        question = _origin_query(db, record)
        fresh = {s['id'] for s in record.get('native', {}).get('sources', []) if s.get('id')}
        sources = [s for s in data['sources'] if s['id'] in fresh]
        acquired = db.execute("""SELECT max(updated_at) FROM organization_tasks
            WHERE id IN (SELECT value FROM json_each(?)) AND status='completed'
            AND json_extract(payload_json,'$.knowledge_processing.root_turn_id')=?
            AND json_extract(payload_json,'$.knowledge_processing.stage') IN
                ('conversation_native','conversation_search','conversation_fetch')""",
            (json.dumps(record['retrieval'].get('task_ids',[])),origin)).fetchone()[0]
    excluded = excluded_sources(db,origin)
    allow_correction = old and old['origin']=='session' and consent['enabled']
    surviving = [s for s in visible_sources(db,turn,sources) if s['id'] not in excluded and (s.get('kind')!='user_correction' or allow_correction)]
    fields = []
    for source in surviving:
        corrected = source.get('kind')=='user_correction'
        label = source.get('summary') if corrected else source.get('title')
        if label:
            if compact and not corrected:
                for separator in TITLE_SEPARATOR.finditer(label):
                    if label[:separator.start()].strip()==label[separator.end():].strip():
                        label=label[:separator.start()].rstrip()
                        break
            fields.append({'ref':'r/'+origin+'/'+source['id'], 'kind':'correction' if corrected else 'title',
                           'text':label[:160], 'date':None if corrected else source.get('date'),
                           'revision':fingerprint(source)})
        if len(fields) == 3:
            break
    if question and surviving:
        fields.insert(0, {'ref':'r/'+origin+'/query', 'kind':'query', 'text':question[:400],
                          'acquired_at':acquired,
                          'revision':fingerprint([question, acquired, [(s['id'],s.get('state_revision')) for s in surviving]])})
    return fields


def packet(db, turn, material, target, consent, *, compact=False, recent_window=False, complete_batch=False):
    fields = []
    seen = set()
    current = [target] if not complete_batch and target in material['current'] else material['current']
    history = material['catalog']
    recent = material['recent'][-8:]
    if recent_window and consent['enabled']:
        from .ordinary_materials import _dialogue, history_access
        from .message_time import annotate_events, enabled, event_times
        previous = db.execute("""SELECT id FROM turns WHERE session_id=? AND persona_id=? AND channel='desktop'
            AND status='completed' AND cancelled_at IS NULL AND rowid<(SELECT rowid FROM turns WHERE id=?)
            ORDER BY rowid DESC LIMIT ?""", (turn['session_id'],turn['persona_id'],turn['id'],RECENT_TURNS)).fetchall()
        with history_access(db,turn['session_id'],True):
            rows = []
            for row in reversed(previous):
                if not turn_usable(db,row[0]):
                    continue
                speakers = {}
                for item in _dialogue(db,turn,row[0],{},True):
                    speakers['user' if item['role']=='user' else 'assistant'] = item
                rows.extend(speakers.values())
        if enabled(turn) and rows:
            annotate_events(rows,event_times(db,[e['id'] for e in rows]))
        history = {**{e['id']:e for e in rows},**history}
        recent = [e['id'] for e in rows]
    for kind, ids, limit in (('current',current,min(800,1600//max(1,len(current)))),
                             ('reference',material['references'][:4],320), ('recent',recent,240)):
        for eid in ids:
            if eid in seen:
                continue
            seen.add(eid)
            row = history[eid]
            entry = {'ref':eid, 'kind':kind, 'role':row['role'], 'text':row['text'][:limit],
                     'revision':fingerprint(row['text'])}
            times = [part for part in row.get('message_times',[]) if part.get('start',0)<len(entry['text'])]
            if recent_window and kind=='recent':
                times=times[:1]
            if times:
                entry['message_times'] = [dict(t,end=min(t['end'],len(entry['text']))) if 'end' in t else t
                                          for t in times]
            fields.append(entry)
    # Explicit references lead the catalogue even when several later topics intervened.
    origins = []
    for eid in material['references'][:4]:
        prior = find_turn(db, 'event', eid)
        if prior and prior['id'] not in origins:
            origins.append(prior['id'])
    recent = db.execute("""SELECT t.id FROM turns t WHERE t.persona_id=? AND t.session_id=?
        AND t.channel='desktop' AND t.status='completed' AND t.rowid<(SELECT rowid FROM turns WHERE id=?)
        AND (EXISTS(SELECT 1 FROM private_lookup_records r WHERE r.turn_id=t.id AND r.outcome='acquired')
        OR EXISTS(SELECT 1 FROM knowledge_control k WHERE k.key='knowledge.conversation.record.'||t.id
            AND json_extract(k.value,'$.data.retrieval.material_acquired')=1))
        ORDER BY t.rowid DESC LIMIT 12""", (turn['persona_id'],turn['session_id'],turn['id']))
    topics = 0
    for origin in dict.fromkeys([*origins, *(row[0] for row in recent)]):
        entries = _research(db, turn, consent, origin, compact=compact)
        if entries:
            fields.extend(entries)
            topics += 1
            if topics == 4:
                break
    if recent_window and complete_batch:
        from .persona_world_topics import fields as world_fields
        fields.extend(world_fields(db,turn,consent))
    return {'fields':fields}


def wire(packet, aliases=None):
    fields = [{k:v for k,v in row.items() if k!='revision'} for row in packet['fields']]
    if aliases is not None:
        reverse = {ref:alias for alias,ref in aliases.items()}
        for field in fields:
            field['ref'] = reverse[field['ref']]
    return {'fields':fields}


def check_packet(db, turn, material, target, consent, previous, *, compact=False):
    if packet(db,turn,material,target,consent,compact=compact) != previous:
        raise RuntimeErrorBase('private_reply_source_changed')


def selection(response, packet, aliases=None):
    """Only bounded verbatim spans can enter the locally assembled public query."""
    if not isinstance(response,dict) or set(response) not in ({'parts'},{'parts','after'}):
        raise RuntimeErrorBase('lookup_target_invalid')
    parts = response['parts']
    if not isinstance(parts,list) or len(parts)>6:
        raise RuntimeErrorBase('lookup_target_invalid')
    fields = {row['ref']:row for row in packet['fields']}
    pins, terms = [], []
    for part in parts:
        if not isinstance(part,dict) or set(part) != {'ref','start','end'} or not isinstance(part['ref'],str):
            raise RuntimeErrorBase('lookup_target_invalid')
        ref = aliases.get(part['ref']) if aliases is not None else part['ref']
        field = fields.get(ref)
        start,end = part['start'],part['end']
        if (field is None or type(start) is not int or type(end) is not int or
                not 0<=start<end<=len(field['text']) or end-start>128):
            raise RuntimeErrorBase('lookup_target_invalid')
        term = field['text'][start:end].strip()
        if not term or '\n' in term or '\r' in term or term in terms:
            raise RuntimeErrorBase('lookup_target_invalid')
        terms.append(term)
        pins.append(dict(part, ref=ref, revision=field['revision']))
    if 'after' in response:
        ref = response['after']
        if isinstance(ref,str) and aliases is not None:
            ref = aliases.get(ref)
        field = fields.get(ref) if isinstance(ref,str) else None
        if not terms or not field or field.get('kind')!='query' or not field.get('acquired_at'):
            raise RuntimeErrorBase('lookup_target_invalid')
        origins = {p['ref'].split('/',2)[1] for p in pins if p['ref'].startswith('r/')}
        if origins and ref.split('/',2)[1] not in origins:
            raise RuntimeErrorBase('lookup_target_invalid')
        try:
            at = datetime.fromisoformat(field['acquired_at'])
        except (TypeError,ValueError):
            raise RuntimeErrorBase('lookup_target_invalid') from None
        if at.tzinfo is None:
            raise RuntimeErrorBase('lookup_target_invalid')
        terms.append('after:'+at.astimezone(timezone(timedelta(hours=8))).date().isoformat())
        pins.append({'ref':ref,'after':field['acquired_at'],'revision':field['revision']})
    query = ' '.join(terms)
    if len(query)>128 or SENSITIVE_QUERY.search(query) or NO_QUERY.search(query):
        raise RuntimeErrorBase('lookup_target_invalid')
    return query, pins


def quoted_selection(response, catalogue, aliases, current_ids):
    """Translate exact model-selected quotes into revalidatable offsets in the original source."""
    invalid = 'lookup_target_invalid'
    fields = {f['ref']:f for f in catalogue['fields']}
    if not isinstance(response,dict):
        raise RuntimeErrorBase(invalid)
    if set(response)=={'repeat'}:
        ref=response['repeat']
        field=fields.get(aliases.get(ref)) if isinstance(ref,str) else None
        if not field or field.get('kind')!='query' or not field['ref'].endswith('/query'):
            raise RuntimeErrorBase(invalid)
        return selection({'parts':[{'ref':ref,'start':0,'end':len(field['text'])}]},catalogue,aliases)
    if set(response) not in ({'parts'},{'parts','after'}) or not isinstance(response['parts'],list) or len(response['parts'])>6:
        raise RuntimeErrorBase(invalid)
    translated=[]
    roles=[]
    for part in response['parts']:
        if not isinstance(part,dict) or set(part)!={'ref','quote','role'} or not isinstance(part['ref'],str):
            raise RuntimeErrorBase(invalid)
        role=part['role']
        if (not roles and role!='subject') or (roles and role not in ('attribute','qualifier')):
            raise RuntimeErrorBase(invalid)
        field=fields.get(aliases.get(part['ref']))
        quote=part['quote']
        if not field or not isinstance(quote,str) or not quote.strip() or quote!=quote.strip() or len(quote)>128:
            raise RuntimeErrorBase(invalid)
        start=field['text'].find(quote)
        if start<0 or field['text'].find(quote,start+1)>=0:
            raise RuntimeErrorBase(invalid)
        roles.append(role)
        translated.append({'ref':part['ref'],'start':start,'end':start+len(quote)})
    refs=[aliases[p['ref']] for p in translated]
    if any(role=='attribute' and ref not in current_ids for role,ref in zip(roles,refs)):
        # A title sometimes contributes a second span mislabeled as the new attribute.
        # Keep its public subject and the sole span selected from the current input.
        from_current=[i for i,ref in enumerate(refs) if ref in current_ids]
        if (not refs[0].startswith('r/') or len(from_current)!=1
                or roles[from_current[0]]!='qualifier'
                or any(ref!=refs[0] or roles[i]!='attribute'
                       for i,ref in enumerate(refs[1:],1) if i not in from_current)):
            raise RuntimeErrorBase(invalid)
        translated=[translated[0],translated[from_current[0]]]
        roles=['subject','attribute']
    elif roles and not any(ref in current_ids and role in ('subject','attribute') for role,ref in zip(roles,refs)):
        raise RuntimeErrorBase(invalid)
    question,pins=selection(dict(response,parts=translated),catalogue,aliases)
    for pin,role in zip(pins,roles):
        pin['role']=role
    return question,pins


def direct_task(db, turn, target, full):
    """Use a bound selection or link; a button applied to the entire input still needs a subject."""
    from .private_lookup import projection
    from .turn_input import load_turn_input
    members = load_turn_input(db, turn)
    ids = [m['id'] for m in members if m['id']==target] or [m['id'] for m in members]
    task, origin = projection(db,turn,ids,full,grounded=True)
    if not task:
        return None, None
    q = task['question']
    if q.startswith(('https://','http://')) and not any(c.isspace() for c in q) and not SENSITIVE_QUERY.search(q):
        from .knowledge_general import public_url
        return task | {'question':q,'url':public_url(q),'intent':'read_link'}, origin
    bound = []
    narrowed = False
    for m in members:
        if m['id'] not in ids:
            continue
        span = db.execute('SELECT value FROM knowledge_control WHERE key=?',
                          ('private.lookup.span.'+m['id'],)).fetchone()
        explicit = read(db,'native.public_event.'+m['id'],{})['data'].get('task',{})
        selected = json.loads(span[0])['text'] if span else explicit.get('question')
        if selected:
            bound.append(selected)
            narrowed |= selected != m['text'].strip() and selected in m['text']
    if narrowed and q=='\n'.join(bound) and not SENSITIVE_QUERY.search(q):
        return task, origin
    if task.get('url') and not SENSITIVE_QUERY.search(task['url']):
        return task | {'question':task['url']}, origin
    return None, None


def focused_acquisition(db, turn, catalogue):
    """Tie an ambiguous retry to the one public acquisition from the immediately preceding turn."""
    origins={ref.split('/',2)[1] for ref in catalogue.get('refs',{}).values() if ref.startswith('r/')}
    if len(origins)!=1:
        return None
    earlier=list(db.execute("""SELECT * FROM turns WHERE persona_id=? AND session_id=?
        AND channel='desktop' AND rowid<(SELECT rowid FROM turns WHERE id=?) ORDER BY rowid DESC LIMIT 2""",
        (turn['persona_id'],turn['session_id'],turn['id'])))
    if not earlier:
        return None
    from . import public_lookup
    previous=earlier[0]
    if (len(earlier)==2 and previous['status']=='interrupted' and not previous['cancelled_at']
            and previous['error_code']=='private_reply_review_rejected' and not previous['reply_event_id']
            and not public_lookup.saved(db,public_lookup.key(previous['id'],'order'))
            and not db.execute("SELECT 1 FROM private_lookup_calls WHERE turn_id=?",(previous['id'],)).fetchone()):
        from .turn_input import load_turn_input
        if [m['text'] for m in load_turn_input(db,dict(previous))]==[m['text'] for m in load_turn_input(db,dict(turn))]:
            previous=earlier[1]
    if previous['status']!='completed' or previous['cancelled_at'] or previous['id'] not in origins:
        return None
    return previous['id'] if public_lookup.saved(db,public_lookup.key(previous['id'],'order')) else None


async def resolve(flow, turn, base, material, target, catalogue):
    consent = base['consent']
    with flow.store.connection() as db:
        task, origin = direct_task(db,turn,target,consent['enabled'])
    if task:
        return {'status':'direct','task':task,'origin':origin,'target':target}
    if not consent['enabled']:
        return {'status':'needs_consent'}
    version = getattr(flow.providers,'lookup_target_version',VERSION)
    if version==QUOTED_TARGET_VERSION:
        from .persona_world_topics import prepare
        await prepare(flow,turn,base,material,target)
    proof = await _resolve_once(flow,turn,base,material,target,catalogue,version)
    needs_binding=proof.get('unbound_subject') is True
    retry=version in AUTOMATIC_VERSIONS and proof['status']=='invalid'
    focus=None
    if version==QUOTED_TARGET_VERSION and proof['status'] in ('resolved','ambiguous','invalid'):
        from . import public_lookup,session_privacy
        with flow.store.connection() as db:
            scope=session_privacy.public_scope(db,flow.entry.config,base['instance'],turn['persona_id'],turn['session_id'],consent)
            requested=public_lookup.request(db,turn)
            if scope['enabled'] and proof['status']=='resolved' and proof['task']['question'] not in requested['selection'].values():
                retry=not public_lookup.context_authority(db,flow.entry.config,turn,base,proof,requested)
            elif (scope['enabled'] and scope['protocol'] in (session_privacy.PUBLIC_ATTRIBUTE_SCOPE,session_privacy.PUBLIC_QUESTION_SCOPE)
                    and proof['status'] in ('ambiguous','invalid') and not material['references'] and not needs_binding):
                first=public_lookup.saved(db,CATALOGUE_PREFIX+turn['id']) or {}
                focus=focused_acquisition(db,turn,first)
                retry=retry or bool(focus)
    if retry:
        locked = proof if proof['status']=='resolved' and proof.get('pins') else None
        recovered = await _resolve_once(flow,turn,base,material,target,catalogue,version,
                                        recovery=True,locked=locked,focus=focus,
                                        omission=proof['reference_pin'] if needs_binding else None)
        if needs_binding:
            if recovered['status']!='resolved':
                return proof | {'recovery_result':recovered}
            if ((recovered['pins'][1:],recovered.get('precision'))!=
                    (proof['requested_pins'],proof.get('precision'))
                    or (proof.get('pair_reference') and not recovered.get('subject_pair'))):
                return proof | {'recovery_result':{'status':'different_query','attempt_id':recovered['attempt_id']}}
        if focus:
            pins=recovered.get('pins') or []
            if (recovered['status']=='resolved' and pins and pins[0].get('role')=='subject'
                    and pins[0]['ref'].startswith('r/'+focus+'/') and any(p.get('role')=='attribute' for p in pins)):
                with flow.store.connection() as db:
                    authority=public_lookup.context_authority(db,flow.entry.config,turn,base,recovered,public_lookup.request(db,turn))
                if (authority and authority['topics']==[focus]
                        and (not proof.get('precision') or recovered.get('precision')==proof['precision'])):
                    return recovered | {'previous_attempt_id':proof['attempt_id']}
            return proof | {'recovery_result':recovered}
        if locked and recovered['status']=='resolved':
            def shape(result):
                return [(pin.get('role'),pin['end']-pin['start'],pin.get('after'),
                         pin['ref'] if pin.get('role')=='attribute' else None) for pin in result['pins']]
            if (recovered['task']!=locked['task'] or shape(recovered)!=shape(locked)
                    or recovered.get('precision')!=locked.get('precision')):
                return proof | {'recovery_result':{'status':'different_query','attempt_id':recovered['attempt_id']}}
            with flow.store.connection() as db:
                authorized=public_lookup.context_authority(db,flow.entry.config,turn,base,recovered,public_lookup.request(db,turn))
            if not authorized:
                return proof | {'recovery_result':{'status':'no_public_scope','attempt_id':recovered['attempt_id']}}
            recovered['initial_resolution']=locked
        if proof['status']=='resolved' and recovered['status']!='resolved':
            return proof | {'recovery_result':recovered}
        return recovered | {'previous_attempt_id':proof['attempt_id']}
    return proof


async def _resolve_once(flow, turn, base, material, target, catalogue, version, *,
                        recovery=False, locked=None, focus=None, omission=None):
    consent = base['consent']
    compact = version!=VERSION
    recent_window = version in AUTOMATIC_VERSIONS
    quoted = version==QUOTED_TARGET_VERSION
    node = RECOVERY_NODE if recovery else NODE
    catalogue_key = CATALOGUE_PREFIX+turn['id']+('.recovery' if recovery else '')
    budget = getattr(flow.providers,'lookup_target_budget',None)
    limit = budget() if recent_window and budget is not None else None

    def references(selected):
        prefix = 'R' if recovery else 'S'
        refs = dict.fromkeys(field['ref'] for field in selected['fields'])
        return {f'{prefix}{i}':ref for i,ref in enumerate(refs,1)}

    def build(db):
        selected = packet(db,turn,material,target,consent,compact=compact,recent_window=recent_window,complete_batch=quoted)
        if locked:
            # Recover public witnesses for the same query. A new turn may select a different subject.
            pin=locked['pins'][0]
            source=next((f for f in selected['fields'] if f['ref']==pin['ref'] and f['revision']==pin['revision']),None)
            if source is None:
                raise RuntimeErrorBase('private_reply_source_changed')
            subject=source['text'][pin['start']:pin['end']]
            used={p['ref'] for p in locked['pins']}
            selected['fields']=[f for f in selected['fields'] if f['kind']=='current' or f['ref'] in used
                                or f['kind'] in ('query','title') and subject in f['text']]
        elif focus:
            prefix='r/'+focus+'/'
            selected['fields']=[f for f in selected['fields'] if f['kind']=='current'
                                or (f['ref'].startswith(prefix) and f['kind'] in ('query','title'))]
        elif omission:
            selected['fields']=[_omit_subject(f,omission) for f in selected['fields']]
        elif recovery:
            selected['fields'] = [f for f in selected['fields'] if f['kind'] not in ('title','correction')]
            if quoted and any(f['kind']=='query' for f in selected['fields']):
                selected['fields']=[f for f in selected['fields'] if f['kind'] in ('query','current','reference')]
        if limit is not None:
            for field in list(selected['fields']):
                if len(encoded(wire(selected,references(selected))))<=limit:
                    break
                if field['kind']=='recent':
                    selected['fields'].remove(field)
        return selected

    aliases = None
    if compact:
        with flow.store.connection() as db:
            catalogue = build(db)
        aliases = references(catalogue)
    request = wire(catalogue,aliases)
    if limit is not None and len(encoded(request))>limit:
        return {'status':'input_budget'}

    def check(db):
        from .private_lookup import enabled, intent
        if not enabled(db,turn['persona_id']):
            raise RuntimeErrorBase('knowledge_permission_changed')
        existing = intent(db,turn['id'])
        if existing and existing['state']!='waiting':
            raise RuntimeErrorBase('private_lookup_confirmation_closed')
        if not db.execute("""SELECT 1 FROM private_reply_nodes WHERE turn_id=? AND draft=0
                AND node='J4' AND state='received' AND json_extract(result_json,'$')='retrieve'""",
                (turn['id'],)).fetchone():
            raise RuntimeErrorBase('private_lookup_scope_changed')
        if build(db)!=catalogue:
            raise RuntimeErrorBase('private_reply_source_changed')
        if compact:
            from .public_lookup import freeze
            mapping={'refs':aliases,'wire_hash':fingerprint(request)}
            if quoted:
                mapping['lengths']={f['ref']:len(f['text']) for f in catalogue['fields']}
            if omission:
                mapping['omission']=omission
            freeze(db,catalogue_key,mapping)

    response = await flow.resolve_target(turn['id'],request,base,material,check,node=node)
    with flow.store.connection() as db:
        receipt = db.execute("""SELECT attempt_id FROM private_reply_nodes
            WHERE turn_id=? AND node=? AND state='received' AND draft=0""",(turn['id'],node)).fetchone()
    proof = {'attempt_id':receipt[0], 'consent_revision':consent['revision'],
             'recipient_hash':consent['recipient_hash'], 'version':flow.providers.version}
    if compact:
        proof['catalogue_hash']=fingerprint(request)
    if recovery:
        proof['recovery']=True
    if quoted:
        proof['quoted']=True
    try:
        if getattr(flow.providers,'research_contract_version',None):
            response,precision=research_selection(response)
            proof['precision']=precision
        question, pins = (quoted_selection(response,catalogue,aliases,material['current']) if quoted else
                          selection(response,catalogue,aliases))
    except RuntimeErrorBase as exc:
        if str(exc)!='lookup_target_invalid':
            raise
        return proof | {'status':'invalid'}
    if not question:
        return proof | {'status':'ambiguous'}
    if quoted and pins[0].get('role')=='subject':
        pin=pins[0]
        field=next(f for f in catalogue['fields'] if f['ref']==pin['ref'])
        subject=field['text'][pin['start']:pin['end']]
        if subject_is_reference(subject):
            return proof | {'status':'invalid','unbound_subject':True,'reference_pin':pin,'requested_pins':pins[1:],
                            'pair_reference':bool(PAIR_REFERENCE.search(subject))}
        if omission:
            members=subject_members(subject)
            proof['subject_pair']=len(members)==2 and len(set(members))==2 and all(members)
    return proof | {'status':'resolved', 'task':{'question':question}, 'origin':'session','pins':pins}


def research_selection(response):
    from .research_contract import PRECISIONS
    if not isinstance(response,dict) or response.get('precision') not in PRECISIONS:
        raise RuntimeErrorBase('lookup_target_invalid')
    return {k:v for k,v in response.items() if k!='precision'},response['precision']


def is_world_title(ref):
    return ref.startswith('r/') and ref.split('/',2)[-1].startswith('world.')


def validate(db, turn, consent, proof):
    if proof.get('status') == 'direct':
        task,_ = direct_task(db,turn,proof['target'],consent['enabled'])
        if task != proof['task']:
            raise RuntimeErrorBase('private_lookup_scope_changed')
    if proof.get('status') != 'resolved':
        return
    if not consent['enabled'] or (consent['revision'],consent['recipient_hash']) != (
            proof['consent_revision'],proof['recipient_hash']):
        raise RuntimeErrorBase('private_lookup_scope_changed')
    if proof.get('initial_resolution'):
        validate(db,turn,consent,proof['initial_resolution'])
    node = RECOVERY_NODE if proof.get('recovery') else NODE
    received = db.execute("""SELECT result_json,usage_json FROM private_reply_nodes WHERE turn_id=?
        AND draft=0 AND node=? AND attempt_id=? AND state='received'""",
        (turn['id'],node,proof['attempt_id'])).fetchone()
    if not received:
        raise RuntimeErrorBase('private_lookup_scope_changed')
    audit = json.loads(received['usage_json'])
    if audit.get('preset_version')!=proof['version'] or audit.get('recipient_hash')!=proof['recipient_hash']:
        raise RuntimeErrorBase('private_lookup_scope_changed')
    fields = []
    research = {}
    aliases = None
    compact = 'catalogue_hash' in proof
    if compact:
        key = CATALOGUE_PREFIX+turn['id']+('.recovery' if proof.get('recovery') else '')
        row = db.execute('SELECT value FROM knowledge_control WHERE key=?',(key,)).fetchone()
        catalogue = json.loads(row[0]) if row else None
        if not catalogue or catalogue['wire_hash']!=proof['catalogue_hash']:
            raise RuntimeErrorBase('private_reply_source_changed')
        aliases=catalogue['refs']
    for ref in dict.fromkeys(pin['ref'] for pin in proof['pins']):
        if is_world_title(ref):
            from .persona_world_topics import fields as world_fields
            matched=next((f for f in world_fields(db,turn,consent) if f['ref']==ref),None)
            if matched:
                fields.append(matched)
                continue
        elif ref.startswith('r/'):
            origin = ref.split('/',2)[1]
            if origin not in research:
                research[origin] = {f['ref']:f for f in _research(db,turn,consent,origin,compact=compact)}
            field = research[origin].get(ref)
            if field:
                fields.append(field)
                continue
        else:
            origin = find_turn(db, 'event', ref)
            if (origin and not origin['cancelled_at'] and origin['session_id']==turn['session_id'] and origin['persona_id']==turn['persona_id']
                    and (origin['id']==turn['id'] or eligible(db,origin['id']) and turn_usable(db,origin['id']))):
                if origin['source_key'].startswith('imported.'):
                    from .ordinary_materials import history_access
                    from .import_history import history_allowed
                    with history_access(db,turn['session_id'],consent['enabled']):
                        if not history_allowed(db,turn['session_id'],'dialogue'):
                            raise RuntimeErrorBase('private_reply_source_changed')
                row = db.execute("""SELECT text FROM events WHERE id=? AND NOT EXISTS
                    (SELECT 1 FROM source_exclusions WHERE event_id=?)""", (ref,ref)).fetchone()
                if row:
                    fields.append({'ref':ref,'text':row[0],'revision':fingerprint(row[0])})
                    continue
        raise RuntimeErrorBase('private_reply_source_changed')
    revisions = {f['ref']:f['revision'] for f in fields}
    if any(revisions[p['ref']] != p['revision'] for p in proof['pins']):
        raise RuntimeErrorBase('private_reply_source_changed')
    raw=json.loads(received['result_json'])
    if 'precision' in proof:
        raw,precision=research_selection(raw)
        if precision!=proof['precision']:
            raise RuntimeErrorBase('private_reply_source_changed')
    if proof.get('quoted'):
        from .turn_input import load_turn_input
        current=[m['id'] for m in load_turn_input(db,dict(turn))]
        fields=[_omit_subject(dict(f,text=f['text'][:catalogue['lengths'][f['ref']]]),catalogue.get('omission'))
                for f in fields]
        question,pins=quoted_selection(raw,{'fields':fields},aliases,current)
    else:
        question,pins=selection(raw,{'fields':fields},aliases)
    if pins!=proof['pins'] or {'question':question}!=proof['task']:
        raise RuntimeErrorBase('private_reply_source_changed')


def current_enough(rows, proof):
    after = next((p['after'] for p in (proof or {}).get('pins',[]) if 'after' in p),None)
    if not after:
        return rows
    cutoff = datetime.fromisoformat(after)
    return [row for row in rows if row.get('verified_at') and datetime.fromisoformat(row['verified_at'])>cutoff]
