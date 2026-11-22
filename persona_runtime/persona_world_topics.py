"""Public title references admitted to a new session's existing target resolver."""

import asyncio
import re

from . import persona_world, public_lookup, semantic_knowledge as semantic
from .archive import encoded, fingerprint


def _bindings(db, ids):
    return {row[0]:(row[1],row[2]) for row in db.execute("""
        SELECT f.id, t.id, json_extract(k.value,'$.topic')
        FROM local_knowledge_facts f JOIN turns t ON t.id=json_extract(f.spec_json,'$.turn_source[0]')
        JOIN knowledge_control k ON k.key=?||t.id||'.revision'
        WHERE f.id IN (SELECT value FROM json_each(?))
        ORDER BY t.rowid DESC, f.id""",(public_lookup.PREFIX,encoded(ids)))}


async def prepare(flow, turn, base, material, target):
    """Rank locally and freeze only titles belonging to one matching public topic."""
    tid=turn['id']
    key=public_lookup.key(tid,'world_titles')
    current=[target] if target in material['current'] else material['current']
    query='\n'.join(material['catalog'][item]['text'] for item in current)
    with flow.store.connection() as db:
        if not persona_world.active(db,turn,base) or public_lookup.saved(db,key) is not None:
            return
        shared=persona_world.eligible_fact_ids(db,turn,base)
        ids=[i for i,sid in shared.items() if sid!=turn['session_id']]
        passages=semantic.materials(db,turn,base,query,ids=ids)
    ranked=await asyncio.to_thread(semantic.rank,flow.store.paths,query,passages,limit=len(passages))
    with flow.store.connection(write=True) as db:
        bindings=_bindings(db,[fact['id'] for fact in ranked])
        topics={topic for _,topic in bindings.values() if topic}
        chosen=[]
        if len(topics)==1:
            seen=set()
            for row in ranked:
                if row['id'] in bindings and row['title'] not in seen:
                    chosen.append([row['id'],fingerprint(row)])
                    seen.add(row['title'])
                if len(chosen)==3:
                    break
        public_lookup.freeze(db,key,{'target':target,'facts':chosen})


def fields(db, turn, consent):
    tid=turn['id']
    chosen=public_lookup.saved(db,public_lookup.key(tid,'world_titles'))
    if not chosen or not chosen['facts']:
        return []
    registration=db.execute('SELECT instance_id FROM private_reply_turns WHERE turn_id=?',(tid,)).fetchone()
    if not registration:
        return []
    base={'instance':registration[0],'consent':consent}
    ids=[fact_id for fact_id,_ in chosen['facts']]
    shared=persona_world.eligible_fact_ids(db,turn,base,ids)
    ids=[fact_id for fact_id in ids if fact_id in shared and shared[fact_id]!=turn['session_id']]
    from .local_lookup_route import query
    # Use the same current member range that selected the frozen titles.
    question=query(db,turn,chosen['target'])
    live={f['id']:semantic.public_material(f) for f in semantic.materials(db,turn,base,question,ids=ids)}
    bindings=_bindings(db,ids)
    return [{'ref':'r/'+bindings[i][0]+'/world.'+i,'kind':'title','text':live[i]['title'][:160],
             'date':live[i]['date'],'revision':digest}
            for i,digest in chosen['facts'] if i in live and i in bindings and fingerprint(live[i])==digest]


def referenced_passages(db,turn,base,question):
    """Resolve one explicitly linked public resource for a local continuation of an older topic."""
    from . import local_knowledge as library
    from .models import RuntimeErrorBase
    raw=set(re.findall(r'https?://[^\s，。；！？）)]+',question))
    if len(raw)!=1 or not persona_world.active(db,turn,base):
        return []
    try:
        url=library.revisions.canonical_url(library.public_url(raw.pop()))
    except (RuntimeErrorBase,ValueError):
        return []
    offered=[r[0] for r in db.execute("""SELECT f.id FROM local_knowledge_facts f
        JOIN local_knowledge_documents d ON d.id=f.document_id
        WHERE d.persona_id=? AND d.session_id!=? AND d.url=?""",(turn['persona_id'],turn['session_id'],url))]
    eligible=persona_world.eligible_fact_ids(db,turn,base,offered)
    facts=semantic.materials(db,turn,base,question,ids=list(eligible))
    bindings=_bindings(db,[f['id'] for f in facts])
    if len({topic for _,topic in bindings.values()})!=1:
        return []
    return [f for f in facts if f['id'] in bindings]


def partial_parent(db, turn, work, consent):
    """Only an assessed, still readable public source can link a supplementary lookup."""
    from . import research_contract
    assessed=research_contract.coverage(db,turn['id'],'local')
    if not assessed or assessed['outcome']!='partial':
        return None
    base={'instance':work['instance'],'consent':consent}
    ids=list(dict.fromkeys(w['id'] for w in assessed['witnesses']))
    allowed=persona_world.eligible_fact_ids(db,turn,base,ids)
    bindings=_bindings(db,[i for i in ids if i in allowed])
    if ids and len(bindings)==len(ids) and len({topic for _,topic in bindings.values()})==1:
        return next(iter(bindings.values()))[0]
    return None
