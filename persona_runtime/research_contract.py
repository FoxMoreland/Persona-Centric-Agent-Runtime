"""Versioned research requirements and coverage receipts for the reviewed desktop route."""
import json
import re
import unicodedata

from .archive import fingerprint
from .models import RuntimeErrorBase
from .public_lookup import freeze, key, saved

VERSION = 'research-contract-v1'
PRECISIONS = ('unspecified', 'calendar_date', 'clock_time')
OUTCOMES = ('complete', 'partial', 'insufficient')
GAPS = ('none', 'precision', 'information', 'conflict')
REPLY_FAILURES = ('lookup_reply_format_invalid','lookup_reply_citation_invalid','lookup_reply_unbound')
PREFIX = 'private.research.contract.'
HOUR_FORMAT = re.compile(r'(\d{1,2}|[零〇一二两三四五六七八九十]{1,3})(?:时|点)'
                         r'(?:(\d{1,2}|[零〇一二两三四五六七八九十]{1,3})分?|半|整)?')
CLOCK_FORMAT = re.compile(r'(\d{1,2}):(\d{2})(?::(\d{2}))?(?:\s*(am|pm))?',re.I)


def active(db,tid):
    row=db.execute('SELECT source_json FROM private_reply_source_sets WHERE turn_id=?',(tid,)).fetchone()
    return bool(row and json.loads(row[0]).get('research_contract')==VERSION)


def request(db,tid):
    return saved(db,PREFIX+tid+'.request')


def coverage(db,tid,phase):
    return saved(db,PREFIX+tid+'.'+phase)


def freeze_request(db,tid,need):
    freeze(db,PREFIX+tid+'.request',need)


def build_request(turn,base,material,target,query,resolution,j4,*,eligibility_query):
    """Freeze the understood question separately from any approved public wire."""
    return {'version':VERSION,'instance':base['instance'],'persona':turn['persona_id'],
            'session':turn['session_id'],'revision':turn['execution_revision'],
            'inputs':[[i,fingerprint(material['catalog'][i]['text'])] for i in material['current']],
            'target':target,'query':query,'eligibility_query':eligibility_query,
            'target_status':resolution['status'] if resolution else None,
            'target_receipt':resolution.get('attempt_id') if resolution else None,
            'target_hash':fingerprint(resolution),'precision':(resolution or {}).get('precision','unspecified'),
            'consent':fingerprint(base['consent']),'j4':j4}


def assessment_wire(need,turn,material,passages):
    sources=[{k:fact[k] for k in ('id','title','text','date','source_kind','kind',
                                   'qualification','qualifications','valid_from','valid_until')
              if k in fact} for fact in passages]
    return {'version':VERSION,'question':[material['catalog'][i]['text'] for i,_ in need['inputs']],
            'resolved_query':need['query'],'precision':need['precision'],
            'as_of':turn['created_at'],'sources':sources}


def _clock_literal(value):
    """Check the form of an extracted literal, with event relevance supplied by the assessment."""
    from .knowledge_catalog import _number
    text=unicodedata.normalize('NFKC',value).strip().lower()
    if text in ('正午','午夜','noon','midnight'):
        return True
    numeric=CLOCK_FORMAT.fullmatch(text)
    if numeric:
        hour,minute,second,period=numeric.groups()
        hours=int(hour)
        return ((1<=hours<=12 if period else 0<=hours<24) and int(minute)<60
                and (second is None or int(second)<60))
    text=re.sub(r'^(?:北京时间|当地时间|上午|下午|凌晨|早上|中午|晚上|夜间)\s*','',text)
    chinese=HOUR_FORMAT.fullmatch(text)
    if not chinese:
        return False
    hour,minute=chinese.groups()
    h=_number(hour)
    m=_number(minute) if minute else 0
    return h is not None and m is not None and h<24 and m<60


def interpret(answer,need,passages):
    """Bind one overall assessment to exact offered passages and preserve partial evidence."""
    invalid={'outcome':'unconfirmed','gap':'information','witnesses':[],'reason':'invalid_assessment'}
    if (not isinstance(answer,dict) or set(answer)!={'outcome','gap','witnesses'}
            or answer['outcome'] not in OUTCOMES or answer['gap'] not in GAPS
            or not isinstance(answer['witnesses'],list)):
        return invalid
    outcome,gap=answer['outcome'],answer['gap']
    if (outcome=='insufficient')==bool(answer['witnesses']) or (outcome=='complete')!=(gap=='none'):
        return invalid
    offered={f['id']:f for f in passages}
    seen=set()
    witnesses=[]
    clock=False
    for witness in answer['witnesses']:
        if (not isinstance(witness,dict) or set(witness) not in ({'id','quote'},{'id','quote','clock'})
                or not isinstance(witness['id'],str)
                or not isinstance(witness['quote'],str) or not witness['quote'].strip()):
            return invalid
        fact=offered.get(witness['id'])
        if not fact:
            return invalid
        quote=witness['quote']
        offset=fact['text'].find(quote)
        if offset<0 or fact['text'].find(quote,offset+1)>=0:
            return invalid
        key=(fact['id'],offset,offset+len(quote))
        if key in seen:
            return invalid
        span={'id':fact['id'],'revision':fingerprint(fact),'span':[offset,offset+len(quote)]}
        literal=witness.get('clock')
        if literal is not None:
            if not isinstance(literal,str) or not literal.strip() or literal not in quote:
                return invalid
            if _clock_literal(literal):
                clock=True
                span['clock']=literal
        witnesses.append(span)
        seen.add(key)
    adjusted=outcome=='complete' and need['precision']=='clock_time' and not clock
    return {'outcome':'partial' if adjusted else outcome,'gap':'precision' if adjusted else gap,
            'witnesses':witnesses,'reason':'clock_witness_missing' if adjusted else 'assessed'}


def restore_offer(db,tid,need,phase,passages,read):
    frozen=saved(db,PREFIX+tid+'.'+phase+'.offer')
    if not frozen:
        return passages
    if frozen['request']!=fingerprint(need):
        raise RuntimeErrorBase('private_reply_source_changed')
    candidates=frozen['candidates']
    restored=read([fact_id for fact_id,_ in candidates]) if candidates else []
    if [[f['id'],fingerprint(f)] for f in restored]!=candidates:
        raise RuntimeErrorBase('private_reply_source_changed')
    return restored


async def assess(flow,turn,base,material,need,passages,phase,check):
    """Apply the same bound assessment before search and on the local readback."""
    tid=turn['id']
    node='lookup.coverage.'+phase
    candidates=[[f['id'],fingerprint(f)] for f in passages]
    digest=fingerprint(need)
    with flow.store.connection(write=True) as db:
        check(db)
        freeze(db,PREFIX+tid+'.'+phase+'.offer',{'request':digest,'candidates':candidates})
        previous=coverage(db,tid,phase)
    if previous:
        if previous['request']!=digest or previous['candidates']!=candidates:
            raise RuntimeErrorBase('private_reply_source_changed')
        return previous
    attempt=None
    if passages:
        packet=assessment_wire(need,turn,material,passages)
        try:
            answer=await flow.assess_coverage(tid,packet,base,material,check,node=node)
        except RuntimeErrorBase as error:
            if str(error)!='research_coverage_input_budget':
                raise
            result={'outcome':'unconfirmed','gap':'information','witnesses':[],'reason':'input_budget'}
        else:
            result=interpret(answer,need,passages)
        with flow.store.connection() as db:
            row=db.execute("""SELECT attempt_id FROM private_reply_nodes
                WHERE turn_id=? AND node=? AND draft=0 AND state='received'""",(tid,node)).fetchone()
            if not row and result['reason']!='input_budget':
                raise RuntimeErrorBase('reply_decision_required')
            attempt=row[0] if row else None
    else:
        result={'outcome':'insufficient','gap':'information','witnesses':[],'reason':'no_passages'}
    record=result|{'request':digest,'candidates':candidates,'attempt':attempt}
    with flow.store.connection(write=True) as db:
        check(db)
        freeze(db,PREFIX+tid+'.'+phase,record)
        from .progress import record as activity
        activity(db,tid,'research_assessed')
    return record


def selected_facts(record,passages):
    selected={item['id'] for item in record['witnesses']}
    return [f for f in passages if f['id'] in selected]


def validate_request(db,turn,base,target):
    from . import lookup_target
    from .turn_input import load_turn_input
    need=request(db,turn['id'])
    if not need:
        return
    inputs=[[m['id'],fingerprint(m['text'])] for m in load_turn_input(db,dict(turn))]
    if (need['instance']!=base['instance'] or need['persona']!=turn['persona_id']
            or need['session']!=turn['session_id'] or need['revision']!=turn['execution_revision']
            or need['inputs']!=inputs or need['target']!=target or need['consent']!=fingerprint(base['consent'])
            or need['target_hash']!=fingerprint(lookup_target.saved(db,turn['id']))):
        raise RuntimeErrorBase('private_reply_source_changed')
    choice='none' if need.get('world_reference') else 'retrieve'
    if not db.execute("""SELECT 1 FROM private_reply_nodes WHERE turn_id=? AND node='J4' AND draft=0
                AND attempt_id=? AND state='received' AND json_extract(result_json,'$')=?""",
                (turn['id'],need['j4'],choice)).fetchone():
        raise RuntimeErrorBase('private_reply_source_changed')


def handoff(db,tid):
    need=request(db,tid)
    if not need:
        return None
    local=coverage(db,tid,'local')
    external=coverage(db,tid,'readback')
    result=external if external and (external['witnesses'] or not local or not local['witnesses']) else local
    order=saved(db,key(tid,'order'))
    record=db.execute('SELECT outcome FROM private_lookup_records WHERE turn_id=?',(tid,)).fetchone()
    sent=db.execute("""SELECT state FROM private_lookup_calls WHERE turn_id=? AND step IN ('native','search','fetch')""",(tid,)).fetchall()
    if record:
        tool=record[0]
    elif any(row[0] in ('sending','unknown') for row in sent):
        tool='unconfirmed'
    elif sent:
        tool='attempted'
    else:
        tool='not_dispatched'
    return {'version':VERSION,'precision':need['precision'],
            'coverage':result['outcome'] if result else 'unconfirmed',
            'gap':result['gap'] if result else 'information',
            'assessment':external['outcome'] if external else local['outcome'] if local else 'unconfirmed',
            'tool':tool,'public_bound':bool(order)}


def failed_reply_with_evidence(db,turn):
    if turn['status']!='interrupted' or turn['error_code'] not in REPLY_FAILURES or turn['cancelled_at']:
        return None
    if not request(db,turn['id']):
        return None
    return db.execute("""SELECT d.contract_json,d.binding,s.source_json,s.material_hash
        FROM private_reply_attempts a JOIN private_reply_decisions d ON a.decision_id=d.id
        JOIN private_reply_source_sets s ON s.turn_id=a.turn_id
        WHERE a.turn_id=? AND a.phase='main' AND a.state='received'
        AND json_extract(d.contract_json,'$.ordinary.research.status')='local_hit'
        AND json_array_length(d.contract_json,'$.ordinary.research.facts')>0
        ORDER BY a.draft DESC LIMIT 1""",(turn['id'],)).fetchone()


def progress(db,turn):
    if not active(db,turn['id']):
        return None
    summary=handoff(db,turn['id'])
    if summary:
        summary['target_status']=request(db,turn['id'])['target_status']
    return summary
