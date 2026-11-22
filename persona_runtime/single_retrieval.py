"""One reviewed text model for query translation and batch evidence assessment."""
import asyncio
from hashlib import sha256
import json
import re
import unicodedata

from . import ordinary_materials, persona_world, public_lookup, semantic_knowledge as semantic, session_privacy
from .archive import encoded, fingerprint
from .knowledge_native import SENSITIVE_QUERY, NO_QUERY
from .knowledge_service import turn_usable
from .models import RuntimeErrorBase
from .turn_input import load_turn_input

VERSION = 'single-retrieval-v1'
DIGEST = '6588961f19efd03c7925fa10354ad31d012b13f7f5fae224bb1c4fef7b73bb5b'
TRANSLATION_V2_DIGEST = '8f9dc9fc024e3944017582e6cf3f7ff681acb3a131a4ec5ac965b9db22876de7'
TRANSLATION_RANGE_V3_DIGEST = 'ca816b19a9a82a951e24784ea816eebbcf1bc1a7cd8f573613bbf03f4e7513c3'
SOURCE_ASSESSMENT_V2_DIGEST = 'b0f8bb1f7a7b470de55639b956da0ae044fd8206e7ed85f5c34c873873929dca'


def load_manifest(bundle,config):
    raw=bundle['retrieval_manifest']
    if not isinstance(raw,str) or sha256(raw.encode()).hexdigest()!=DIGEST:
        raise RuntimeErrorBase('private_chat_review_version_changed')
    spec=json.loads(raw)
    baseline=config.paths.home/'private/ordinary-reply'/(spec['baseline_bundle']+'.json')
    original=baseline.read_bytes()
    if (sha256(original).hexdigest()!=spec['baseline_bundle'] or
            json.loads(original)!={k:v for k,v in bundle.items() if k not in ('retrieval_manifest','egress_manifest','translation_manifest')} or
            bundle['scope']!={'instance':spec['instance_id'],'personas':spec['persona_ids']}):
        raise RuntimeErrorBase('private_chat_review_scope_changed')
    return spec


def apply_translation_manifest(bundle,config,primary):
    raw=bundle['translation_manifest']
    if not primary or not isinstance(raw,str) or sha256(raw.encode()).hexdigest()!=TRANSLATION_V2_DIGEST:
        raise RuntimeErrorBase('private_chat_review_version_changed')
    spec=json.loads(raw)
    parent=(config.paths.home/'private/ordinary-reply'/(spec['baseline_bundle']+'.json')).read_bytes()
    previous=primary['phases']['translate']
    fields=('instance_id','persona_ids','recipient','context','user_serialization')
    if (sha256(parent).hexdigest()!=spec['baseline_bundle']
            or json.loads(parent)!={k:v for k,v in bundle.items() if k!='translation_manifest'}
            or spec['previous_manifest']!=DIGEST or spec['phase']!='translate'
            or any(spec[k]!=primary[k] for k in fields)
            or any(spec[k]!=previous[k] for k in ('user_fields','returns'))):
        raise RuntimeErrorBase('private_chat_review_scope_changed')
    phases=primary['phases']|{'translate':previous|{'system':spec['system']}}
    return primary|{'phases':phases}


def apply_translation_range(review, base_bundle, config, primary):
    if not isinstance(review,dict) or set(review)!={'manifest','system'} or not primary:
        raise RuntimeErrorBase('private_chat_review_scope_changed')
    manifest,system=review['manifest'],review['system']
    if (not isinstance(manifest,str) or not isinstance(system,str)
            or sha256(manifest.encode()).hexdigest()!=TRANSLATION_RANGE_V3_DIGEST):
        raise RuntimeErrorBase('private_chat_review_version_changed')
    spec=json.loads(manifest)
    parent=spec['baseline_registered_bundle']
    original=(config.paths.home/'private/ordinary-reply'/(parent+'.json')).read_bytes()
    scope=spec['scope']
    recipient=primary['recipient']
    previous=primary['phases']['translate']
    if (sha256(original).hexdigest()!=parent or json.loads(original)!=base_bundle
            or spec['previous_manifest_sha256']!=TRANSLATION_V2_DIGEST
            or scope['phase']!='translate' or scope['instance_id']!=primary['instance_id']
            or scope['persona_id'] not in primary['persona_ids']
            or scope['recipient']!={'model':recipient['model'],'endpoint':recipient['base_url']}
            or scope['recipient_hash']!=recipient['recipient_set_at_review']
            or sha256(previous['system'].encode()).hexdigest()!=spec['files']['v2已实测-System.txt']
            or sha256(system.encode()).hexdigest()!=spec['files']['v3候选-System.txt']):
        raise RuntimeErrorBase('private_chat_review_scope_changed')
    return primary|{'phases':primary['phases']|{'translate':previous|{'system':system}}}


def apply_source_assessment(review, base_bundle, config, primary):
    if not isinstance(review,dict) or set(review)!={'manifest','system'} or not primary:
        raise RuntimeErrorBase('private_chat_review_scope_changed')
    manifest,system=review['manifest'],review['system']
    if (not isinstance(manifest,str) or not isinstance(system,str)
            or sha256(manifest.encode()).hexdigest()!=SOURCE_ASSESSMENT_V2_DIGEST):
        raise RuntimeErrorBase('private_chat_review_version_changed')
    spec=json.loads(manifest)
    parent=spec['baseline_bundle']
    original=(config.paths.home/'private/ordinary-reply'/(parent+'.json')).read_bytes()
    scope=spec['scope']
    recipient=primary['recipient']
    previous=primary['phases']['assess']
    if (sha256(original).hexdigest()!=parent or json.loads(original)!=base_bundle
            or spec['phase']!='assess' or scope['instance']!=primary['instance_id']
            or scope['persona'] not in primary['persona_ids']
            or scope['recipient']!={'model':recipient['model'],'endpoint':recipient['base_url']}
            or scope['recipient_hash']!=recipient['recipient_set_at_review']
            or sha256(previous['system'].encode()).hexdigest()!=spec['files']['现行-System.txt']
            or sha256(system.encode()).hexdigest()!=spec['files']['候选-System.txt']):
        raise RuntimeErrorBase('private_chat_review_scope_changed')
    return primary|{'phases':primary['phases']|{'assess':previous|{'system':system}}}


def activate_formal_scope(config, reviewed, activation):
    """Bind unchanged reviewed phases to an explicitly selected local instance."""
    from .provider_policy import is_official_deepseek
    from .private_personas import PrivatePersonas

    cfg = config.load()
    personas = activation['personas']
    if (activation['instance'] != cfg['instance_id'] or not isinstance(personas, dict)
            or set(personas) != set(reviewed['persona_ids'])
            or any(not isinstance(pid, str) or not pid for pid in personas.values())
            or len(set(personas.values())) != len(personas)):
        raise RuntimeErrorBase('private_chat_review_scope_changed')
    registry = PrivatePersonas(config.paths)
    for pid in personas.values():
        if registry.protected(pid):
            continue
        # An owned visible-only acceptance copy can reuse the same signed
        # phases without importing the protected original's registry or data.
        copy = registry.acceptance_copy_policy(pid)
        if not copy or copy.get('copy_scope')!='visible_only_v1':
            raise RuntimeErrorBase('private_chat_review_scope_changed')

    models = session_privacy.configured_models(config, cfg)
    destinations = session_privacy.recipients(config, cfg)
    account = destinations.get('organization', {}).get('account')
    model = models.get('organization', {})
    recipient = reviewed['recipient']
    if (not account or cfg['organization']['reuse_chat'] or cfg.get('private_research_account') != 'native_web'
            or fingerprint(destinations) != activation['recipients_sha256']
            or any(not is_official_deepseek(m) for role, m in models.items() if role != 'jev')
            or (model.get('base_url'), model.get('name'), model.get('max_input_chars'), model.get('max_tokens'))
            != (recipient['base_url'], recipient['model'], recipient['max_input_chars_at_review'],
                recipient['max_output_tokens_at_review'])):
        raise RuntimeErrorBase('private_chat_review_recipient_changed')
    return reviewed | {
        'instance_id': activation['instance'],
        'persona_ids': [personas[pid] for pid in reviewed['persona_ids']],
        'recipient': recipient | {
            'receipt_account_fingerprint': account,
            'recipient_set_at_review': activation['recipients_sha256'],
        },
    }


def check_recipient(config,spec,base):
    cfg=config.load()
    destinations=session_privacy.recipients(config,cfg)
    recipient=spec['recipient']
    model=cfg['organization']['model']
    if (base['instance']!=spec['instance_id'] or base['persona'] not in spec['persona_ids']
            or not base['consent']['enabled'] or cfg['organization']['reuse_chat']
            or fingerprint(destinations)!=base['consent']['recipient_hash']
            or fingerprint(destinations)!=recipient['recipient_set_at_review']
            or destinations['organization']['account']!=recipient['receipt_account_fingerprint']
            or (model['base_url'],model['name'])!=(recipient['base_url'],recipient['model'])
            or model['max_input_chars']!=recipient['max_input_chars_at_review']
            or model['max_tokens']!=recipient['max_output_tokens_at_review']):
        raise RuntimeErrorBase('private_cloud_provider_not_authorized')
    return model


def key(tid,part):
    return 'private.single_retrieval.'+tid+'.'+part


def saved(db,tid,part):
    return public_lookup.saved(db,key(tid,part))


def freeze(db,tid,part,value):
    return public_lookup.freeze(db,key(tid,part),value)


def enrolled(db,tid):
    return bool(db.execute("""SELECT 1 FROM private_reply_source_sets WHERE turn_id=?
        AND json_type(source_json,'$.single_retrieval_bundle')='text'""",(tid,)).fetchone())


def budget(flow,phase):
    spec=flow.providers.retrieval_spec
    return spec['recipient']['max_input_chars_at_review']-len(spec['phases'][phase]['system'])


def validate_packet(spec,phase,packet):
    fields=dict(spec['phases'][phase]['user_fields'])
    fields['current']=spec['phases']['translate']['user_fields']['current']
    validate_fields(fields,packet)


def validate_fields(fields,packet):
    def matches(value,shape):
        if isinstance(shape,dict):
            return isinstance(value,dict) and set(shape)==set(value) and all(matches(value[k],s) for k,s in shape.items())
        if isinstance(shape,list):
            return isinstance(value,list) and all(matches(entry,shape[0]) for entry in value)
        return value is None or type(value) in (str,int)
    if not matches(packet,fields):
        raise RuntimeErrorBase('private_chat_review_scope_changed')


def reference_history(db,turn,material):
    """Use accepted reference links to keep a clarification on its subject."""
    from .references import ReferenceError, prepare

    source=turn
    if not material['references']:
        previous=db.execute("""SELECT * FROM turns WHERE persona_id=? AND session_id=? AND channel='desktop'
            AND rowid<(SELECT rowid FROM turns WHERE id=?) ORDER BY rowid DESC LIMIT 1""",
            (turn['persona_id'],turn['session_id'],turn['id'])).fetchone()
        if not previous or previous['status']!='completed' or previous['cancelled_at'] or not turn_usable(db,previous['id']):
            return set()
        plan=saved(db,previous['id'],'plan')
        result=saved(db,previous['id'],'readback') or saved(db,previous['id'],'local')
        if not (plan and result and result['status']=='clarify'):
            return set()
        source=dict(previous)
    try:
        _,manifest=prepare(db,source)
    except ReferenceError:
        return set()
    origins=set(manifest['turn_ids'])
    if origins and source['id']!=turn['id']:
        origins.add(source['id'])
    return origins


def translation_input(db,turn,material,limit,*,event_refs=None):
    current=load_turn_input(db,dict(turn))
    packet={'as_of':turn['created_at'],'current':[],'history':[],'references':[]}
    events={} if event_refs is None else event_refs
    def message(row):
        ref=events.setdefault(row['id'],len(events)+1)
        time=row.get('time') or row.get('created_at')
        if not time:
            original=db.execute('SELECT created_at FROM events WHERE id=?',(row['id'],)).fetchone()
            time=original[0] if original else None
        return {'ref':ref,'role':'user' if row['role']=='user' else 'persona',
                'time':time,'text':row['text']}
    bound=[r['id'] for r in current]+material['references']
    if db.execute('SELECT 1 FROM source_exclusions WHERE event_id IN (SELECT value FROM json_each(?))',
                  (encoded(bound),)).fetchone():
        raise RuntimeErrorBase('private_reply_source_changed')
    packet['current']=[message(row) for row in current]
    packet['references']=[message(material['catalog'][ref])|{'kind':'message'} for ref in material['references']]
    if len(encoded(packet))>limit:
        raise RuntimeErrorBase('retrieval_input_budget')
    cap=db.execute("""SELECT json_extract(source_json,'$.retrieval_history_turns'),
        json_extract(source_json,'$.retrieval_complete_history'),json_extract(source_json,'$.retrieval_reference_focus')
        FROM private_reply_source_sets WHERE turn_id=?""",(turn['id'],)).fetchone()
    history_limit=cap[0] if cap and cap[0] is not None else 50
    complete_history=bool(cap and cap[1])
    if not history_limit:
        return packet
    history=db.execute("""SELECT t.id FROM turns t WHERE persona_id=? AND session_id=? AND channel='desktop'
        AND status='completed' AND cancelled_at IS NULL AND rowid<(SELECT rowid FROM turns WHERE id=?)
        AND NOT EXISTS(SELECT 1 FROM source_exclusions x WHERE x.event_id IN (t.user_event_id,t.reply_event_id)
            OR x.event_id IN (SELECT m.event_id FROM input_batch_members m WHERE m.batch_id=t.input_batch_id))
        ORDER BY rowid DESC""",(turn['persona_id'],turn['session_id'],turn['id']))
    readable=0
    with ordinary_materials.history_access(db,turn['session_id'],True):
        focus=reference_history(db,turn,material) if cap and cap[2] else set()
        for (origin,) in history:
            if not turn_usable(db,origin):
                continue
            rows=ordinary_materials._dialogue(db,turn,origin,{},True)
            excluded={r[0] for r in db.execute("""SELECT event_id FROM source_exclusions
                WHERE event_id IN (SELECT value FROM json_each(?))""",(encoded([r['id'] for r in rows]),))}
            visible=[row for row in rows if row['id'] not in excluded
                     and (complete_history or row['id'] not in material['references'])]
            if not visible:
                continue
            if not focus or origin in focus:
                packet['history'].insert(0,{'messages':[message(row) for row in visible]})
                if len(encoded(packet))>limit:
                    packet['history'].pop(0)
                    break
            readable+=1
            if readable==history_limit:
                break
    return packet


def references(packet):
    rows=list(packet['current'])
    for group in packet['history']:
        rows.extend(group['messages'])
    rows.extend(packet['references'])
    return {r['ref']:r for r in rows}


def ref_list(values,allowed):
    return (isinstance(values,list) and all(type(v) is int and v in allowed for v in values)
            and len(set(values))==len(values))


def translation(raw,packet):
    refs=references(packet)
    invalid={'status':'unconfirmed','queries':[],'anchors':[]}
    if not isinstance(raw,dict):
        return invalid
    if set(raw)=={'type','queries','refs'} and raw['type']=='clarify':
        raw={'status':'clarify','queries':raw['queries'],'refs':raw['refs']}
    if set(raw)=={'status','queries','refs'} and raw.get('status')=='clarify' and raw.get('queries')==[] and ref_list(raw.get('refs'),refs):
        chosen=raw['refs']
        queries=[]
    elif set(raw)=={'status','queries'} and raw.get('status')=='ready' and isinstance(raw.get('queries'),list) and raw['queries']:
        by_text={}
        chosen=[]
        for item in raw['queries']:
            if (not isinstance(item,dict) or set(item)!={'text','refs'} or not isinstance(item.get('text'),str) or not item['text'].strip()
                    or not item.get('refs') or not ref_list(item['refs'],refs)):
                return invalid
            entry=by_text.setdefault(item['text'],{'id':len(by_text)+1,'text':item['text'],'refs':[]})
            entry['refs']=list(dict.fromkeys(entry['refs']+item['refs']))
            chosen.extend(item['refs'])
        queries=list(by_text.values())
    else:
        return invalid
    current={r['ref'] for r in packet['current']}
    anchors=[{'kind':'message',**refs[i]} for i in dict.fromkeys(chosen) if i not in current]
    return {'status':raw['status'],'queries':queries,'anchors':anchors}


def check_context(db,turn,base,material,plan):
    if (not base['consent']['enabled'] or fingerprint(base['consent'])!=plan['consent']
            or not semantic.library.enabled(db,turn['persona_id'])
            or fingerprint(translation_input(db,turn,material,plan['budget']))!=plan['context']):
        raise RuntimeErrorBase('private_lookup_scope_changed')


def current_facts(db,turn,base,plan,assessment):
    output={}
    by_query={q['id']:q['text'] for q in plan['queries']}
    for row in assessment['items']:
        pins=row['facts']
        if pins:
            found=semantic.pinned(db,turn,base,by_query[row['id']],[i for i,_ in pins])
            if [[f['id'],fingerprint(f)] for f in found]!=pins:
                raise RuntimeErrorBase('private_reply_source_changed')
            output.update((f['id'],f) for f in found)
    return list(output.values())


def source(index,fact,acquired_at):
    conditions=list(fact.get('qualifications') or [])
    if fact.get('qualification'):
        conditions.append(fact['qualification'])
    return {'id':index,'title':fact['title'],'url':fact['url'],'published_at':fact['date'],
            'acquired_at':acquired_at,'kind':fact['source_kind'],'qualification':conditions,
            'valid_from':fact['valid_from'],'valid_until':fact['valid_until'],'text':fact['text']}


def assessment_input(db,turn,plan,groups,limit):
    queries=[{'id':q['id'],'text':q['text'],'candidates':[]} for q in plan['queries']]
    packet={'as_of':turn['created_at'],'current':plan['current'],'anchors':plan['anchors'],
            'queries':queries,'sources':[]}
    if len(encoded(packet))>limit:
        raise RuntimeErrorBase('retrieval_input_budget')
    docs={}
    numbers={}
    ids=list({f['id'] for group in groups for f in group})
    acquired=dict(db.execute("""SELECT f.id,coalesce(r.created_at,d.acquired_at) FROM local_knowledge_facts f
        JOIN local_knowledge_documents d ON d.id=f.document_id LEFT JOIN private_lookup_records r
        ON r.turn_id=json_extract(f.spec_json,'$.turn_source[0]')
        WHERE f.id IN (SELECT value FROM json_each(?))""",(encoded(ids),)))
    for depth in range(max(map(len,groups),default=0)):
        for query,group in zip(queries,groups):
            if depth>=len(group):
                continue
            fact=group[depth]
            number=numbers.get(fact['id'])
            new=number is None
            if new:
                number=len(docs)+1
                packet['sources'].append(source(number,fact,acquired[fact['id']]))
            query['candidates'].append(number)
            if len(encoded(packet))>limit:
                if new:
                    packet['sources'].pop()
                query['candidates'].pop()
                continue
            numbers[fact['id']]=number
            docs[number]=fact
    if any(group and not q['candidates'] for q,group in zip(queries,groups)):
        raise RuntimeErrorBase('retrieval_input_budget')
    return packet,docs


def assessment(raw,packet,docs,empty=False):
    valid=isinstance(raw,dict) and set(raw)=={'status','items'}
    if valid and raw.get('status')=='clarify' and raw.get('items')==[]:
        return {'status':'clarify','items':[]}
    entries=raw.get('items') if valid and raw.get('status')=='assessed' else None
    if valid and raw['status']=='missing' and isinstance(raw['items'],list) and all(
            isinstance(item,dict) and item.get('result')=='missing' and item.get('passages')==[]
            for item in raw['items']):
        entries=raw['items']
    results=[]
    for q in packet['queries']:
        result='missing' if empty else 'unconfirmed'
        pins=[]
        matches=[v for v in entries if isinstance(v,dict) and type(v.get('id')) is int and v['id']==q['id']] if isinstance(entries,list) else []
        if len(matches)==1:
            item=matches[0]
            state=item.get('result')
            selected=item.get('passages')
            if (set(item)=={'id','result','passages'} and state in ('complete','partial','missing')
                    and ref_list(selected,docs) and ref_list(selected,q['candidates'])
                    and bool(selected)==(state!='missing')):
                result=state
                pins=[[docs[i]['id'],fingerprint(docs[i])] for i in selected]
        results.append({'id':q['id'],'result':result,'facts':pins})
    return {'status':'assessed','items':results}


def cited_urls(receipt, available_urls):
    """Use only annotated URLs also returned by this completed native tool call."""
    from .knowledge_general import public_url

    annotations=receipt.get('annotations')
    if not receipt.get('tool_calls') or not isinstance(annotations,list):
        return []
    matches={}
    for annotation in annotations:
        if not isinstance(annotation,dict) or annotation.get('type')!='url_citation':
            continue
        citation=annotation.get('url_citation')
        if not isinstance(citation,dict):
            continue
        try:
            url=public_url(citation.get('url'))
        except RuntimeErrorBase:
            continue
        if url in available_urls:
            matches[url]=None
    return list(matches)


def fresh_readback_sources(db, turn):
    """Bind newly committed passages to the item that acquired their source."""
    from .private_lookup import normalized_sources

    tid=turn['id']
    commit=db.execute("SELECT fact_ids FROM knowledge_study_commits WHERE turn_id=? AND status='committed'",(tid,)).fetchone()
    if not commit:
        return {},{}
    batch=saved(db,tid,'batch')
    if not batch:
        return {},{}
    source_ids={}
    citations={}
    calls=db.execute("""SELECT i.item_id,i.step,c.result_json FROM retrieval_item_calls i
        JOIN private_lookup_calls c ON c.attempt_id=i.attempt_id
        WHERE i.turn_id=? AND c.turn_id=i.turn_id AND c.state='received'""",(tid,))
    for item,step,raw in calls:
        if item not in batch['items']:
            continue
        receipt=json.loads(raw or '{}')
        if step=='native' and not receipt.get('tool_calls'):
            continue
        verified=normalized_sources(turn,receipt.get('sources',[]),by_id=True)
        source_ids.setdefault(item,set()).update(verified)
        if step=='native':
            citations[item]=cited_urls(receipt,{row['url'] for row in verified.values()})
    admitted=list(db.execute("""SELECT id,json_extract(spec_json,'$.turn_source[1]') FROM local_knowledge_facts
        WHERE id IN (SELECT value FROM json_each(?)) AND json_extract(spec_json,'$.turn_source[0]')=?""",(commit[0],tid)))
    return {item:{fact for fact,source in admitted if source in sources} for item,sources in source_ids.items()},citations


def fresh_readback_ids(db, turn):
    return fresh_readback_sources(db,turn)[0]


def anchored_answer_rows(db, turn, plan, query, rows):
    """Offer still-readable passages from the answer a translated query points back to."""
    from .archive import find_turn
    from .local_lookup_route import answer_receipt,has_local_material

    ids=set()
    for ref in query['refs']:
        eid=plan.get('anchor_events',{}).get(str(ref))
        owner=find_turn(db,'event',eid) if eid else None
        if (not owner or owner['status']!='completed' or owner['cancelled_at']
                or (owner['persona_id'],owner['session_id'])!=(turn['persona_id'],turn['session_id'])):
            continue
        receipt=answer_receipt(db,owner['id'])
        if has_local_material(receipt):
            ids.update(json.loads(receipt['fact_ids']))
    return [row for row in rows if row['id'] in ids]


def related_citation_rows(db, turn, rows, ranked):
    """Revisit citations of the earlier item behind a readable local hit."""
    origins={}
    hits=encoded([row['id'] for row in ranked])
    for fact,origin in db.execute("""SELECT f.id,t.id FROM local_knowledge_facts f JOIN turns t
            ON t.id=json_extract(f.spec_json,'$.turn_source[0]')
            WHERE f.id IN (SELECT value FROM json_each(?)) AND t.persona_id=?
            AND t.status='completed' AND t.cancelled_at IS NULL
            AND t.rowid<(SELECT rowid FROM turns WHERE id=?)""",(hits,turn['persona_id'],turn['id'])):
        origins.setdefault(origin,set()).add(fact)
    related={}
    for origin,matched in origins.items():
        if not saved(db,origin,'batch'):
            continue
        previous=dict(db.execute('SELECT * FROM turns WHERE id=?',(origin,)).fetchone())
        by_item,urls=fresh_readback_sources(db,previous)
        for item,ids in by_item.items():
            if ids.intersection(matched):
                for url in urls.get(item,()):
                    related.setdefault(url,set()).update(ids)
    return [row for row in rows if row['id'] in related.get(row['url'],())]


def promote_citations(paths, question, referenced, ranked):
    if not referenced:
        return ranked
    # Leave room for independently retrieved evidence alongside the native citations.
    promoted=semantic.rank(paths,question,referenced,limit=max(1,semantic.PASSAGE_LIMIT//2),
                           minimum_score=-1.0,one_per_document=True)
    return list({row['id']:row for row in promoted+ranked}.values())[:semantic.PASSAGE_LIMIT]


def rank_candidates(paths, question, rows, fresh_ids, citations):
    ranked=semantic.rank(paths,question,rows)
    if not fresh_ids:
        return ranked
    fresh=[row for row in rows if row['id'] in fresh_ids]
    if not any(f['id'] in fresh_ids for f in ranked) and fresh:
        ranked=semantic.rank(paths,question,fresh,minimum_score=-1.0,one_per_document=True)
    referenced=[row for row in fresh if row['url'] in citations]
    return promote_citations(paths,question,referenced,ranked)


async def assess(flow,turn,base,material,plan,stage):
    tid=turn['id']
    with flow.store.connection() as db:
        check_context(db,turn,base,material,plan)
        previous=saved(db,tid,stage)
        if previous:
            current_facts(db,turn,base,plan,previous)
            return previous
        offer=saved(db,tid,stage+'.offer')
        prior=saved(db,tid,'local') if stage=='readback' else None
        candidates=[semantic.materials(db,turn,base,q['text']) for q in plan['queries']] if not offer else []
        fresh,citations=fresh_readback_sources(db,turn) if prior and not offer else ({},{})
    if not offer:
        groups=[]
        for index,(q,rows) in enumerate(zip(plan['queries'],candidates)):
            ranked=await asyncio.to_thread(rank_candidates,flow.store.paths,q['text'],rows,
                                           fresh.get(q['id'],set()),citations.get(q['id'],()))
            if stage=='local':
                with flow.store.connection() as db:
                    referenced=related_citation_rows(db,turn,rows,ranked) if ranked else []
                    anchored=anchored_answer_rows(db,turn,plan,q,rows) if not ranked else []
                if referenced:
                    ranked=await asyncio.to_thread(promote_citations,flow.store.paths,q['text'],referenced,ranked)
                elif anchored:
                    ranked=await asyncio.to_thread(semantic.rank,flow.store.paths,q['text'],anchored,
                                                   minimum_score=-1.0,one_per_document=True)
            if prior:
                old=prior['items'][index]['facts']
                with flow.store.connection() as db:
                    pins=semantic.pinned(db,turn,base,q['text'],[i for i,_ in old]) if old else []
                if [[f['id'],fingerprint(f)] for f in pins]!=old:
                    raise RuntimeErrorBase('private_reply_source_changed')
                ranked=list({f['id']:f for f in pins+ranked}.values())
            groups.append(ranked)
        offer=[[[f['id'],fingerprint(f)] for f in group] for group in groups]
    def rebuild(db):
        check_context(db,turn,base,material,plan)
        if stage=='readback' and saved(db,tid,'batch')['scope']!=public_scope(flow,db,turn,base):
            raise RuntimeErrorBase('private_lookup_scope_changed')
        return [current_facts(db,turn,base,plan,{'items':[{'id':q['id'],'facts':pins}]})
                for q,pins in zip(plan['queries'],offer)]
    with flow.store.connection(write=True) as db:
        packet,docs=assessment_input(db,turn,plan,rebuild(db),budget(flow,'assess'))
        freeze(db,tid,stage+'.offer',offer)
    raw=None
    if docs:
        raw=await flow._decision(tid,0,'retrieval.'+stage,packet,(),base,material,structured='assess',check_evidence=rebuild)
    result=assessment(raw,packet,docs,empty=not docs)
    with flow.store.connection(write=True) as db:
        check_context(db,turn,base,material,plan)
        if stage=='readback' and saved(db,tid,'batch')['scope']!=public_scope(flow,db,turn,base):
            raise RuntimeErrorBase('private_lookup_scope_changed')
        # A shared candidate may also support another item if it is readable for that question.
        for row in result['items']:
            ids=[i for i,_ in row['facts']]
            if ids:
                q=plan['queries'][row['id']-1]
                eligible={f['id']:fingerprint(semantic.public_material(f))
                          for f in semantic.materials(db,turn,base,q['text'],ids=ids)}
                if any(eligible.get(i)!=digest for i,digest in row['facts']):
                    row.update(result='unconfirmed',facts=[])
        freeze(db,tid,stage,result)
    return result


def public_scope(flow,db,turn,base):
    return session_privacy.public_scope(db,flow.entry.config,base['instance'],turn['persona_id'],
                                        turn['session_id'],base['consent'])


def _public_words(text):
    return re.findall(r'\w+',unicodedata.normalize('NFKC',text).casefold())


def _covered(word, texts):
    if any(word in t for t in texts):
        return True
    if not re.fullmatch(r'[\u3400-\u9fff]+',word):
        return False
    # A Chinese query may join approved spans without spaces. Require contiguous
    # multi-character spans, so unrelated individual characters cannot disclose a name.
    reachable={0}
    for start in range(len(word)):
        if start in reachable:
            for end in range(start+2,len(word)+1):
                piece=word[start:end]
                if not any(piece in t for t in texts):
                    break
                reachable.add(end)
    return len(word) in reachable


def exportable(db,turn,base,plan,items):
    """Bound public disclosure to this input and readable verified public sources."""
    current=[r['text'] for r in plan['current'] if not SENSITIVE_QUERY.search(r['text'])]
    pid,instance=turn['persona_id'],base['instance']
    if not items:
        return []
    # If the translator saw only the approved current public request, synonyms
    # and expansions cannot originate in a private historical message.
    public_input={'as_of':turn['created_at'],'current':plan['current'],'history':[],'references':[]}
    if fingerprint(public_input)==plan['context'] and len(current)==len(plan['current']):
        return [i for i in items if not SENSITIVE_QUERY.search(plan['queries'][i-1]['text'])
                and not NO_QUERY.search(plan['queries'][i-1]['text'])]
    current=[unicodedata.normalize('NFKC',t).casefold() for t in current]
    public_ids=set()
    needs_sources=any(not all(_covered(term,current) for term in _public_words(plan['queries'][i-1]['text']))
                      for i in items)
    live=persona_world._live_privacy(db,instance) if needs_sources else None
    if live:
        entries=persona_world.attestations(db,instance,pid)
        for origin in {r['origin'] for r in entries}:
            public_ids.update(r['fact_id'] for r in persona_world.inspect_turn(db,instance,pid,origin,live=live))
    result=[]
    for item in items:
        query=plan['queries'][item-1]['text']
        if SENSITIVE_QUERY.search(query) or NO_QUERY.search(query):
            continue
        facts=semantic.materials(db,turn,base,query,ids=list(public_ids))
        cited=[unicodedata.normalize('NFKC',r['text']).casefold() for r in plan['anchors']
               if r['ref'] in plan['queries'][item-1]['refs'] and not SENSITIVE_QUERY.search(r['text'])]
        public=[unicodedata.normalize('NFKC',t).casefold() for f in facts for t in (f['text'],f['title'])]
        # This checks disclosure only. Semantic relevance is the assessor's job.
        terms=_public_words(query)
        if terms and all(_covered(term,current+cited) and _covered(term,current+public) for term in terms):
            result.append(item)
    return result


async def decide(flow,turn,base,material,target,web_allowed):
    from . import private_lookup
    tid=turn['id']
    limit=budget(flow,'translate')
    with flow.store.connection() as db:
        if not base['consent']['enabled']:
            raise RuntimeErrorBase('private_cloud_provider_not_authorized')
        event_refs={}
        packet=translation_input(db,turn,material,limit,event_refs=event_refs)
    digest=fingerprint(packet)
    def check(db):
        if fingerprint(translation_input(db,turn,material,limit))!=digest:
            raise RuntimeErrorBase('private_reply_source_changed')
    raw=await flow._decision(tid,0,'retrieval.translate',packet,(),base,material,structured='translate',check_evidence=check)
    plan=translation(raw,packet)|{'budget':limit,'context':digest,'current':packet['current'],
                                  'consent':fingerprint(base['consent'])}
    if getattr(flow.providers,'egress_spec',None):
        anchor_refs={row['ref'] for row in plan['anchors']}
        plan['anchor_events']={str(ref):eid for eid,ref in event_refs.items() if ref in anchor_refs}
    with flow.store.connection(write=True) as db:
        check(db)
        freeze(db,tid,'plan',plan)
    if plan['status']=='ready':
        result=await assess(flow,turn,base,material,plan,'local')
    else:
        result={'status':plan['status'],'items':[]}
    with flow.store.connection() as db:
        gaps=[v['id'] for v in result['items'] if v['result'] in ('partial','missing')]
        scope=public_scope(flow,db,turn,base)
        allow_public=(web_allowed and scope['enabled'] and scope['protocol']==session_privacy.PUBLIC_QUESTION_SCOPE
                      and private_lookup.enabled(db,turn['persona_id']))
    allowed=[]
    if allow_public and gaps:
        if getattr(flow.providers,'egress_spec',None):
            from . import retrieval_egress
            allowed=await retrieval_egress.review(flow,turn,base,material,plan,gaps,scope)
        else:
            with flow.store.connection() as db:
                allowed=exportable(db,turn,base,plan,gaps)
        if allowed:
            await asyncio.to_thread(semantic.require_encoder,flow.store.paths)
    with flow.store.connection(write=True) as db:
        _,live,actual=flow._snapshot(db,tid)
        if live!=base or actual!=material:
            raise RuntimeErrorBase('private_lookup_scope_changed')
        check_context(db,turn,base,material,plan)
        facts=current_facts(db,turn,base,plan,result)
        if allowed and (scope!=public_scope(flow,db,turn,base) or not private_lookup.enabled(db,turn['persona_id'])):
            allowed=[]
        freeze(db,tid,'batch',{'items':allowed,'scope':scope})
        freeze(db,tid,'local',result)
        texts=[plan['queries'][i-1]['text'] for i in allowed]
        task={'question':'\n'.join(texts)} if allowed else None
        state='ready' if allowed else 'none'
        db.execute('INSERT INTO private_lookup_intents VALUES(?,?,?,?,?,?,?,?,?,?)',
                   (tid,encoded(target),'session','retrieve',private_lookup._basis(base,material),None,
                    state,encoded(task) if task else None,None,base['consent']['revision']))
        full=bool(result['items']) and all(r['result']=='complete' for r in result['items'])
        outcome='hit' if full else 'partial' if facts else 'miss'
        db.execute('INSERT INTO local_knowledge_reads VALUES(?,?,?,?,?,?)',
                   (tid,fingerprint(plan),outcome,encoded([f['id'] for f in facts]),turn['created_at'],None))
        for prefix,value in [('private.study.version.',encoded(flow.providers.version)),
                             ('private.study.structured.','true'),('private.study.revisions.','true'),
                             ('private.semantic.turn.','true')]:
            db.execute('INSERT INTO knowledge_control VALUES(?,?)',(prefix+tid,value))


def check_item(flow,db,turn,base,item):
    plan=saved(db,turn['id'],'plan')
    batch=saved(db,turn['id'],'batch')
    root,live,material=flow._snapshot(db,turn['id'])
    if (live!=base or item not in batch['items'] or not batch['scope']['enabled']
            or public_scope(flow,db,turn,base)!=batch['scope']):
        raise RuntimeErrorBase('private_lookup_scope_changed')
    check_context(db,root,base,material,plan)
    if getattr(flow.providers,'egress_spec',None):
        from . import retrieval_egress
        retrieval_egress.validate(db,turn,base,plan,item,batch['scope'])
    elif item not in exportable(db,turn,base,plan,[item]):
        raise RuntimeErrorBase('private_lookup_scope_changed')
    return plan['queries'][item-1]['text']


def init_item_calls(db):
    db.execute("""CREATE TABLE IF NOT EXISTS retrieval_item_calls (
        turn_id TEXT NOT NULL REFERENCES private_lookup_intents(turn_id), item_id INTEGER NOT NULL CHECK(item_id>0),
        step TEXT NOT NULL CHECK(step IN ('native','search','fetch')), query_hash TEXT NOT NULL,
        attempt_id TEXT UNIQUE NOT NULL REFERENCES private_lookup_calls(attempt_id) ON DELETE CASCADE,
        PRIMARY KEY(turn_id,item_id,step))""")
    db.execute("""CREATE TRIGGER IF NOT EXISTS retrieval_item_sealed BEFORE UPDATE ON retrieval_item_calls
        BEGIN SELECT RAISE(ABORT,'immutable retrieval item'); END""")
    db.execute("DROP TRIGGER IF EXISTS retrieval_item_retained")
    db.execute("""CREATE TRIGGER IF NOT EXISTS retrieval_item_retained_v2 BEFORE DELETE ON retrieval_item_calls
        WHEN NOT EXISTS(SELECT 1 FROM knowledge_control WHERE key='private.lookup.unsent.'||OLD.attempt_id)
        AND NOT EXISTS(SELECT 1 FROM desktop_stage_attempts WHERE attempt_id=OLD.attempt_id AND turn_id=OLD.turn_id
        AND stage='lookup' AND outcome='terminal' AND operation IS NOT NULL AND archived IS NOT NULL)
        BEGIN SELECT RAISE(ABORT,'retained retrieval item'); END""")


def handoff(db,turn,base,plan,result):
    tid=turn['id']
    paper=db.execute('SELECT outcome FROM private_lookup_records WHERE turn_id=?',(tid,)).fetchone()
    items=[]
    states={}
    if paper:
        for row in db.execute("""SELECT i.item_id,c.state,c.result_json,i.step FROM retrieval_item_calls i
            JOIN private_lookup_calls c ON c.attempt_id=i.attempt_id WHERE i.turn_id=?""",(tid,)):
            data=json.loads(row[2] or '{}')
            has_sources=bool(data.get('sources') and (row[3]!='native' or data.get('tool_calls')))
            states.setdefault(row[0],[]).append((row[1],has_sources))
    for q in plan['queries']:
        state=next((r for r in result['items'] if r['id']==q['id']),None)
        receipts=states.get(q['id'],[])
        tool='not_dispatched'
        if receipts:
            if any(s=='received' and has_sources for s,has_sources in receipts):
                tool='acquired'
            elif any(s=='received' for s,v in receipts):
                tool='empty'
            else:
                tool='failed'
        item={'id':q['id'],'question':q['text'],'result':state['result'] if state else result['status'],
              'facts':[i for i,_ in state['facts']] if state else [],'tool':tool}
        reason=saved(db,tid,f"item.{q['id']}.skip") or saved(db,tid,f"item.{q['id']}.fallback")
        if reason:
            item['tool_reason']=reason
        items.append(item)
    actions={q['tool'] for q in items}
    tool=next((state for state in ('acquired','failed','empty') if state in actions),'not_dispatched')
    out={'status':result['status'],'items':items,'tool':tool}
    if result['status']=='clarify':
        out['anchors']=plan['anchors']
    return out


async def finish(flow,turn,base,material,context):
    from . import private_lookup,knowledge_study
    tid=turn['id']
    with flow.store.connection() as db:
        plan=saved(db,tid,'plan')
        batch=saved(db,tid,'batch')
        check_context(db,turn,base,material,plan)
        local=saved(db,tid,'local')
        paper=db.execute('SELECT outcome FROM private_lookup_records WHERE turn_id=?',(tid,)).fetchone()
        if batch['items'] and public_scope(flow,db,turn,base)!=batch['scope']:
            raise RuntimeErrorBase('private_lookup_scope_changed')
        if batch['items'] and not paper:
            notice=private_lookup.notice(db,tid)
            if not notice:
                return context|{'phase':'search_notice','status':'before_search','explicit_long':False}
            if not private_lookup.notice_ready(db,tid):
                if notice['state'] in ('unknown','closed'):
                    raise RuntimeErrorBase('search_notice_not_fully_presented')
                return None
    if batch['items'] and not paper:
        if not await acquire(flow,turn,base,batch['items']):
            return None
    result=local
    with flow.store.connection(write=True) as db:
        paper=db.execute('SELECT outcome FROM private_lookup_records WHERE turn_id=?',(tid,)).fetchone()
        got=bool(paper and paper[0]=='acquired')
        if got and not knowledge_study._commit(db,tid):
            for item in batch['items']:
                check_item(flow,db,turn,base,item)
            task=json.loads(private_lookup.intent(db,tid)['query_json'])
            signature=fingerprint([plan,batch])
            knowledge_study._store_semantic(db,turn,base,task,signature,None,{})
    if got:
        result=await assess(flow,turn,base,material,plan,'readback')
    with flow.store.connection(write=True) as db:
        check_context(db,turn,base,material,plan)
        facts=current_facts(db,turn,base,plan,result)
        if got:
            db.execute('INSERT OR IGNORE INTO knowledge_study_reads VALUES(?,?,?,?,?,?,?)',
                       (tid,fingerprint(plan),'hit' if facts else 'insufficient',encoded([f['id'] for f in facts]),
                        turn['created_at'],None,0))
            if facts and persona_world.active(db,turn,base):
                persona_world.index_turn(db,base['instance'],turn['persona_id'],tid)
        summary=handoff(db,turn,base,plan,result)
    return context|{'status':'local_hit' if facts else 'insufficient','facts':facts,'j5_receipt':None,
                    'lookup_outcome':'studied' if got else summary['status'],'retrieval':summary}


async def acquire(flow,turn,base,items):
    from . import private_lookup as lookup
    received={}
    tid=turn['id']
    async def call(item,step,args=None):
        state,value=await lookup._call(flow,turn,base,step,args,item=item)
        if state in ('unknown','sending'):
            raise RuntimeErrorBase('reply_dispatch_unknown')
        return state,value
    for item in items:
        state,raw=await call(item,'native')
        if state=='parked':
            return False
        sources=list(raw['sources']) if state=='received' and raw.get('tool_calls') else []
        if state in ('received','too_large'):
            if not sources:
                with flow.store.connection() as db:
                    query=check_item(flow,db,turn,base,item)
                if len(query)<=128:
                    state,extra=await call(item,'search',{'query':query})
                    if state=='parked':
                        return False
                    if state=='received':
                        sources.extend(extra['sources'])
                else:
                    with flow.store.connection(write=True) as db:
                        freeze(db,tid,f'item.{item}.fallback','query_exceeds_search_limit')
            elif not any(s.get('content') for s in sources if isinstance(s,dict)):
                available=lookup.normalized_sources(turn,sources,by_id=False)
                preferred=cited_urls(raw,available)
                if available:
                    url=preferred[0] if preferred else next(iter(available))
                    state,page=await call(item,'fetch',{'url':url})
                    if state=='parked':
                        return False
                    if state=='received':
                        sources.extend(page['sources'])
        received.update(lookup.normalized_sources(turn,sources,by_id=True))
    with flow.store.connection(write=True) as db:
        lookup._tool_scope(flow,db,turn,base)
        for item in items:
            check_item(flow,db,turn,base,item)
        outcome='acquired' if received else 'unavailable'
        db.execute('INSERT OR IGNORE INTO private_lookup_records VALUES(?,?,?,?,?,?,?,?)',
                   (tid,turn['persona_id'],turn['session_id'],outcome,'',encoded(list(received.values())),None,lookup.timestamp()))
        lookup.record(db,tid,'knowledge_ready' if received else 'knowledge_unavailable')
    return True
