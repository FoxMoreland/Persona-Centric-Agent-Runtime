"""Apply source and passage checks before a public acquisition can become reusable knowledge."""
import asyncio
import json
from datetime import datetime, timedelta, timezone

from . import lookup_target, public_lookup as public, semantic_knowledge as semantic
from .archive import encoded, fingerprint
from .models import RuntimeErrorBase, timestamp

SOURCE_OPTIONS = ('complete','source/0','insufficient')


def passages(data, query, proof, acquired):
    after = next((p['after'] for p in proof.get('pins',[]) if 'after' in p),None)
    result = []
    for source in (data or {}).get('sources',[]):
        kind = source.get('kind')
        if kind not in ('page_content','native_search_summary','search_content','search_summary'):
            continue
        if query.get('url') and (query['url']!=source['url'] or not source.get('content')):
            continue
        published = source.get('date')
        # An undated excerpt or an old publication does not establish news after an explicit cutoff.
        if after and not _within_window(published,after,acquired):
            continue
        body = source.get('content') or source.get('summary') or ''
        for start in range(0,len(body),432):
            text = body[start:start+480]
            if text.strip():
                result.append({'id':fingerprint([source['id'],start,text]),'source':source['id'],
                    'title':source['title'],'text':text,'url':source['url'],'date':published,
                    'source_kind':kind,'qualification':source.get('qualification'),'verified_at':acquired,
                    'span':[start,start+len(text)],'index_labels':[]})
            if start+480>=len(body):
                break
    return result


def _within_window(date, after, acquired):
    from . import local_knowledge
    import re
    if not date:
        return False
    cutoff = datetime.fromisoformat(after)
    until = datetime.fromisoformat(acquired)
    localized = re.fullmatch(r'(\d{4})年(\d{1,2})月(\d{1,2})日',date)
    if localized:
        year,month,day=map(int,localized.groups())
        date=f'{year:04}-{month:02}-{day:02}'
    try:
        published = datetime.fromisoformat(local_knowledge._utc(date))
        if len(date)==10:
            zone=timezone(timedelta(hours=8))
            return cutoff.astimezone(zone).date()<published.date()<=until.astimezone(zone).date()
        return cutoff<published<=until
    except (ValueError,RuntimeErrorBase):
        return False


def linked_passages(db, resolution):
    ids=set()
    for p in (resolution or {}).get('pins',[]):
        if p['ref'].startswith('r/'):
            origin=p['ref'].split('/',2)[1]
            revision=public.saved(db,public.key(origin,'revision'))
            if revision:
                ids.update(revision['facts'])
    return ids


async def supported(flow, turn, base, material, question, candidates, phase, verify):
    selected, receipts = [], []
    for i,fact in enumerate(candidates):
        state = {'question':question,'source':{k:fact[k] for k in
                 ('title','url','text','date','source_kind','qualification','qualifications','verified_at','valid_from','valid_until')
                 if k in fact}}
        if getattr(flow.providers,'lookup_target_version',None)==lookup_target.QUOTED_TARGET_VERSION:
            state['as_of']=turn['created_at']
            state['candidate_scopes']=[{'title':row['title'],'published':row.get('date'),
                                       'excerpt':row['text'][:200]} for row in candidates if row['id']!=fact['id']]
        node = f'public.J5.{phase}.{i}'
        answer = await flow.choice(turn['id'],0,'J5',state,SOURCE_OPTIONS,base,material,
                                   receipt_node=node,check_evidence=verify)
        with flow.store.connection() as db:
            attempt = db.execute("""SELECT attempt_id FROM private_reply_nodes WHERE turn_id=? AND node=?
                AND state='received' AND draft=0""",(turn['id'],node)).fetchone()[0]
        receipts.append({'passage':fact['id'],'attempt':attempt,'result':answer})
        if answer!='insufficient':
            selected.append(fact)
    return selected,receipts


def save_revision(db, turn, work, ids, at, consent, coverage):
    if not ids:
        return
    from .private_lookup import evidence
    ref = work['authority'].get('topics',[])
    previous = ref[0] if ref else None
    if previous is None:
        from .persona_world_topics import partial_parent
        previous=partial_parent(db,turn,work,consent)
    parent = public.saved(db,public.key(previous,'revision')) if previous else None
    topic = parent['topic'] if parent else fingerprint([work['instance'],work['persona'],work['session'],
                                             ['turn',previous] if previous else ['query',work['query']]])
    prior = db.execute("""SELECT t.id,k.value FROM turns t JOIN knowledge_control k
        ON k.key=?||t.id||'.revision' WHERE t.persona_id=? AND t.session_id=? AND
        t.rowid<(SELECT rowid FROM turns WHERE id=?) AND json_extract(k.value,'$.topic')=? ORDER BY t.rowid DESC""",
        (public.PREFIX,work['persona'],work['session'],turn['id'],topic))
    for origin,value in prior:
        if lookup_target._research(db,turn,consent,origin):
            previous,parent=origin,json.loads(value)
            break
    previous_sources = []
    if previous:
        reading = evidence(db,work['persona'],work['session'],previous)
        if reading:
            previous_sources = [[s['id'],fingerprint(s)] for s in reading['sources']]
    docs = [dict(row) for row in db.execute("""SELECT DISTINCT d.id,d.sha256,d.url,
        coalesce(json_extract(f.spec_json,'$.verification.published_at'),d.published_at) published_at
        FROM local_knowledge_documents d JOIN local_knowledge_facts f ON f.document_id=d.id
        WHERE f.id IN (SELECT value FROM json_each(?)) ORDER BY d.id""",(encoded(ids),))]
    public.freeze(db,public.key(turn['id'],'revision'),{'topic':topic,'previous':previous,'at':at,
        'previous_sources':previous_sources,'documents':docs,'facts':ids,'coverage':coverage})


def cited_selection(native, notes, selected):
    """Prefer attributable partial support when the native answer has source-bound citations."""
    if not notes or native.get('notes')!=notes:
        return selected
    native_urls={s.get('url') for s in native.get('sources',[]) if isinstance(s,dict)}
    cited=set()
    for item in native.get('annotations') or []:
        if not isinstance(item,dict) or item.get('type')!='url_citation':
            continue
        citation=item.get('url_citation')
        if not isinstance(citation,dict):
            continue
        start,end=citation.get('start_index'),citation.get('end_index')
        url=citation.get('url')
        if (isinstance(url,str) and url in native_urls and type(start) is int and type(end) is int
                and 0<=start<end<=len(notes)):
            cited.add(url)
    if not any(f['url'] in cited for f in selected):
        return selected
    return sorted(selected,key=lambda fact:fact['url'] not in cited)


def ranked_observations(paths, question, pool, notes):
    """Include topical, synthesis-related and recently published passages for the coverage decision."""
    by_question=semantic.rank(paths,question,pool,minimum_score=-1.0)
    if not pool:
        return by_question
    guided=question+'\n'+notes[:1600] if notes else question
    by_notes=semantic.rank(paths,guided,pool,minimum_score=-1.0) if notes else by_question
    dates={}
    for row in pool:
        try:
            date=datetime.fromisoformat(row['date']).date() if row.get('date') else None
        except (TypeError,ValueError):
            date=None
        if date:
            dates[row['id']]=date
    newest=set(sorted(set(dates.values()),reverse=True)[:2])
    dated=[row for row in pool if dates.get(row['id']) in newest]
    by_date=semantic.rank(paths,guided,dated,minimum_score=-1.0) if dated else []
    choices={}
    for i in range(semantic.PASSAGE_LIMIT):
        for rows in (by_notes,by_date,by_question):
            if i<len(rows):
                choices.setdefault(rows[i]['id'],rows[i])
    return list(choices.values())[:semantic.PASSAGE_LIMIT]


async def commit(flow, turn, base, material):
    from .private_lookup import _tool_scope, evidence, record
    tid=turn['id']
    with flow.store.connection() as db:
        work = public.saved(db,public.key(tid,'order'))
        query = work['query']
        proof = lookup_target.for_query(db,tid)
        acquired = db.execute('SELECT created_at FROM private_lookup_records WHERE turn_id=?',(tid,)).fetchone()[0]
        data = evidence(db,turn['persona_id'],turn['session_id'],tid,active_turn=tid,acquisition=True)
        digest = fingerprint(data)
    def verify(db):
        _tool_scope(flow,db,turn,base)
        actual=evidence(db,turn['persona_id'],turn['session_id'],tid,active_turn=tid,acquisition=True)
        if fingerprint(actual)!=digest:
            raise RuntimeErrorBase('private_reply_source_changed')
    pool=passages(data,query,proof,acquired)
    # Similarity orders a bounded shortlist. The reviewed J5 decision tests support for the actual question.
    if getattr(flow.providers,'lookup_target_version',None)==lookup_target.QUOTED_TARGET_VERSION:
        ranked=await asyncio.to_thread(ranked_observations,flow.store.paths,query['question'],pool,(data or {}).get('notes',''))
    else:
        ranked=await asyncio.to_thread(semantic.rank,flow.store.paths,query['question'],pool,minimum_score=-1.0)
    selected,receipts=await supported(flow,turn,base,material,query['question'],ranked,'acquired',verify)
    if getattr(flow.providers,'lookup_target_version',None)==lookup_target.QUOTED_TARGET_VERSION:
        with flow.store.connection() as db:
            native=db.execute("""SELECT result_json FROM private_lookup_calls WHERE turn_id=? AND step='native'
                AND state='received'""",(tid,)).fetchone()
        if native:
            selected=cited_selection(json.loads(native[0]),(data or {}).get('notes',''),selected)
    sources={s['id']:s for s in (data or {}).get('sources',[])}
    accepted={}
    for passage in selected:
        source=sources[passage['source']]
        entry=accepted.setdefault(source['id'],{'revision':fingerprint(source),'spans':[]})
        entry['spans'].append(passage['span'])
    with flow.store.connection(write=True) as db:
        verify(db)
        if db.execute('SELECT 1 FROM knowledge_study_commits WHERE turn_id=?',(tid,)).fetchone():
            return
        public.freeze(db,public.key(tid,'selection'),{'acquisition_hash':digest,'checks':receipts})
        public.freeze(db,public.key(tid,'admission'),accepted)
        admitted=evidence(db,turn['persona_id'],turn['session_id'],tid,active_turn=tid)
        ids=semantic.store_sources(db,turn,base,admitted,query,passages=accepted)
        eligible=semantic.materials(db,turn,base,query['question'],ids=ids)
        by_span={(f['url'],f['text'],tuple(f['span'])):f['id'] for f in lookup_target.current_enough(eligible,proof)}
        ids=[by_span[k] for f in selected if (k:=(f['url'],f['text'],tuple(f['span']))) in by_span]
        db.execute('INSERT INTO knowledge_study_commits VALUES(?,?,?,?,?,?,?)',(tid,None,fingerprint(work),
            'committed' if ids else 'insufficient',encoded(ids),0,timestamp()))
        db.execute('INSERT INTO knowledge_control VALUES(?,?)',('private.semantic.sources.'+tid,'true'))
        admitted_passages={f['id'] for f in selected if (f['url'],f['text'],tuple(f['span'])) in by_span}
        complete=any(r['passage'] in admitted_passages and r['result']=='complete' for r in receipts)
        coverage='unassessed' if work.get('research_request') else 'complete' if complete else 'partial'
        save_revision(db,turn,work,ids,acquired,base['consent'],coverage)
        from . import persona_world
        if ids and not work.get('research_request') and persona_world.enabled(db,base['instance'],turn['persona_id']):
            persona_world.index_turn(db,base['instance'],turn['persona_id'],tid)
        record(db,tid,'knowledge_study_committed' if ids else 'knowledge_no_matches')
