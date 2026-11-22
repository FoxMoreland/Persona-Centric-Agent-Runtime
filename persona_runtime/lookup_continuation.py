"""Continue a verified public readback after the user reauthorizes a changed reply model."""
import json

from . import lookup_target, public_lookup, research_contract, semantic_knowledge, session_privacy
from .archive import fingerprint
from .models import RuntimeErrorBase

MODEL_STOP = 'private_reply_model_changed'
STOPPED_FOLLOWUPS = ('private_reply_review_rejected', *research_contract.REPLY_FAILURES)


def _original_grant(db,turn,base,work):
    latest=base['consent']
    old_revision=work['consent_revision']
    if not latest['enabled'] or not 0<old_revision<=latest['revision']:
        return None
    events=list(db.execute("""SELECT revision,enabled,protocol,recipients_json,operation_hash
        FROM session_privacy_events WHERE instance_id=? AND session_id=? AND persona_id=?
        AND revision>=? ORDER BY revision""",
        (base['instance'],turn['session_id'],turn['persona_id'],old_revision)))
    if (not events or events[-1]['revision']!=latest['revision']
            or len(events)!=latest['revision']-old_revision+1
            or any(not e['enabled'] or e['protocol']!=session_privacy.PROTOCOL for e in events)):
        return None
    original,current=(json.loads(events[i]['recipients_json']) for i in (0,-1))
    research_recipients={k:v for k,v in original.items() if k!='main'}
    if (work['recipients']!=fingerprint(original) or latest['recipient_hash']!=fingerprint(current)
            or any({k:v for k,v in json.loads(e['recipients_json']).items() if k!='main'}!=research_recipients
                   for e in events)):
        return None
    scope=work['authority'].get('scope') or {}
    protocol=scope.get('protocol')
    if (not scope.get('enabled') or protocol not in (session_privacy.PUBLIC_ATTRIBUTE_SCOPE,
                                                    session_privacy.PUBLIC_QUESTION_SCOPE)
            or scope.get('recipient_hash')!=fingerprint([protocol,base['instance'],turn['persona_id'],
                  turn['session_id'],old_revision,work['recipients']])):
        return None
    return events[0]


def retained_result(db,turn,base):
    """Read only the original public passages verified by both admission and readback."""
    from .private_lookup import evidence, intent, presented_notice
    turn=dict(turn)
    tid=turn['id']
    if (turn['channel']!='desktop' or turn['status']!='interrupted' or turn['error_code']!=MODEL_STOP
            or turn['cancelled_at'] or turn['reply_event_id'] or not research_contract.active(db,tid)):
        return None
    notice=presented_notice(db,tid)
    if not notice or db.execute('SELECT 1 FROM source_exclusions WHERE event_id=?',(notice,)).fetchone():
        return None
    work=public_lookup.saved(db,public_lookup.key(tid,'order'))
    need=research_contract.request(db,tid)
    lookup=intent(db,tid)
    original=public_lookup.saved(db,public_lookup.key(tid,'request'))
    target=lookup_target.saved(db,tid)
    if (not work or not need or not original or not lookup or not target or lookup['choice']!='retrieve'
            or lookup['state']!='ready' or lookup['origin']!='session'
            or work['instance']!=base['instance'] or work['session']!=turn['session_id']
            or work['persona']!=turn['persona_id'] or work['turn']!=tid
            or work['consent_revision']!=lookup['consent_revision']
            or work['request_hash']!=fingerprint(original) or original!=public_lookup.request(db,turn)
            or work.get('research_request')!=fingerprint(need) or work['target_hash']!=fingerprint(target)):
        return None
    if not (grant:=_original_grant(db,turn,base,work)):
        return None
    try:
        lookup_target.validate(db,turn,{'enabled':True,'revision':grant['revision'],
                                       'recipient_hash':work['recipients']},target)
    except RuntimeErrorBase:
        return None
    trace=public_lookup.trace(db,tid) or {}
    if trace.get('acquisition')!='acquired' or not any(
            c['state']=='received' and c['results'] for c in trace.get('calls',[])):
        return None
    if not evidence(db,turn['persona_id'],turn['session_id'],tid,active_turn=tid):
        return None
    commit=db.execute("""SELECT binding,fact_ids FROM knowledge_study_commits
        WHERE turn_id=? AND status='committed'""",(tid,)).fetchone()
    read=db.execute("""SELECT query_hash,fact_ids FROM knowledge_study_reads
        WHERE turn_id=? AND outcome='hit'""",(tid,)).fetchone()
    coverage=research_contract.coverage(db,tid,'readback')
    if (not commit or commit['binding']!=fingerprint(work) or not read or read['query_hash']!=fingerprint(need['query'])
            or not coverage or coverage['request']!=fingerprint(need) or coverage['outcome'] not in ('partial','complete')):
        return None
    witnesses={w['id']:w['revision'] for w in coverage['witnesses']}
    fresh=set(json.loads(commit['fact_ids']))
    ids=[ident for ident in json.loads(read['fact_ids']) if ident in fresh and ident in witnesses]
    facts=semantic_knowledge.retained_public_passages(db,turn,need['eligibility_query'],ids,
                                                       grant['revision'],grant['operation_hash'])
    facts=[fact for fact in facts if witnesses[fact['id']]==fingerprint(fact)]
    if not facts:
        return None
    binding={'origin':tid,'order':fingerprint(work),'readback':fingerprint(coverage),
             'consent':fingerprint(base['consent']),'facts':[[f['id'],fingerprint(f)] for f in facts]}
    return facts,binding


def _origin(db,turn):
    """Use the adjacent lookup or the original lookup bound to an adjacent reply."""
    from . import reply_provenance
    recent=list(db.execute("""SELECT * FROM turns WHERE persona_id=? AND session_id=? AND channel='desktop'
        AND rowid<(SELECT rowid FROM turns WHERE id=?) ORDER BY rowid DESC LIMIT 2""",
        (turn['persona_id'],turn['session_id'],turn['id'])))
    if not recent:
        return None
    previous=recent[0]
    if previous['error_code']==MODEL_STOP:
        return previous
    if previous['status']=='completed' and previous['reply_event_id'] and not previous['cancelled_at']:
        answered=db.execute("""SELECT d.contract_json FROM private_reply_attempts a
            JOIN private_reply_decisions d ON a.decision_id=d.id
            JOIN private_reply_reviews r ON r.attempt_id=a.id AND r.approved=1
            JOIN expression_speech_units u ON u.source_id=a.id AND u.phase='main'
            JOIN expression_speech_publications p ON p.utterance_id=u.id AND p.state='complete'
            WHERE a.turn_id=? AND a.phase='main' AND a.state='received' AND p.canonical_event_id=?
              AND NOT EXISTS (SELECT 1 FROM source_exclusions x WHERE x.event_id=p.canonical_event_id)
              AND NOT EXISTS (SELECT 1 FROM source_exclusions x WHERE x.event_id=? OR x.event_id IN
                  (SELECT event_id FROM input_batch_members WHERE batch_id=?))""",
            (previous['id'],previous['reply_event_id'],previous['user_event_id'],previous['input_batch_id'])).fetchone()
        retained=((json.loads(answered[0])['ordinary'].get('research') or {}).get('retained_read') if answered else None)
        if retained:
            return db.execute("""SELECT * FROM turns WHERE id=? AND persona_id=? AND session_id=?
                AND rowid<(SELECT rowid FROM turns WHERE id=?)""",
                (retained['origin'],turn['persona_id'],turn['session_id'],previous['id'])).fetchone()
        return None
    if (len(recent)!=2 or previous['status']!='interrupted' or previous['error_code'] not in STOPPED_FOLLOWUPS
            or previous['cancelled_at'] or previous['reply_event_id']):
        return None
    attempt=db.execute("""SELECT d.contract_json FROM private_reply_attempts a
        JOIN private_reply_decisions d ON d.id=a.decision_id
        WHERE a.turn_id=? AND a.phase='main' AND a.state='received' ORDER BY a.draft DESC LIMIT 1""",
        (previous['id'],)).fetchone()
    if not attempt or db.execute("""SELECT 1 FROM source_exclusions WHERE event_id=?
        OR event_id IN (SELECT event_id FROM input_batch_members WHERE batch_id=?)""",
        (previous['user_event_id'],previous['input_batch_id'])).fetchone():
        return None
    proof=json.loads(attempt[0])['ordinary']
    research=proof.get('research') or {}
    if (not proof.get('provenance') or research.get('status')!='ordinary' or research.get('facts')
            or not reply_provenance.scope(db,previous,research)):
        return None
    return recent[1]


def followup(db,turn,base,context):
    from . import reply_provenance
    from .private_lookup import intent
    tid=turn['id']
    lookup=intent(db,tid)
    if (not research_contract.active(db,tid) or not lookup or lookup['choice']!='none'
            or lookup['state']!='none' or not context.get('j4_receipt')):
        return None
    if db.execute("""SELECT 1 FROM input_references r WHERE r.event_id=?
            OR r.event_id IN (SELECT event_id FROM input_batch_members WHERE batch_id=?)""",
            (turn['user_event_id'],turn['input_batch_id'])).fetchone():
        return None
    decisions={row['node']:json.loads(row['result_json']) for row in db.execute("""SELECT node,result_json
        FROM private_reply_nodes WHERE turn_id=? AND draft=0 AND state='received' AND node IN ('J2.action','J2.task')""",(tid,))}
    if decisions!={'J2.action':'respond','J2.task':'open'}:
        return None
    previous=_origin(db,turn)
    if not previous or not (result:=retained_result(db,previous,base)):
        return None
    facts,binding=result
    source_readback=research_contract.coverage(db,binding['origin'],'readback')
    need=research_contract.request(db,binding['origin'])
    preserved={f['id'] for f in facts}=={w['id'] for w in source_readback['witnesses']}
    assessment={'version':research_contract.VERSION,'precision':need['precision'],
                'coverage':source_readback['outcome'] if preserved else 'partial',
                'gap':source_readback['gap'] if preserved else 'information',
                'assessment':source_readback['outcome'],'tool':'not_dispatched','public_bound':False}
    next_context=context|{'status':'local_hit','lookup_outcome':'continued','facts':facts,
                          'retained_read':binding,'assessment':assessment}
    return next_context if reply_provenance.scope(db,turn,next_context) else None


def _bound_subject(value, passage):
    if value in passage:
        return True
    # A joint reference may be displayed without the parentheticals in its source.
    members=lookup_target.subject_members(value)
    if not 2<=len(members)<=6 or not all(members):
        return False
    offset=0
    for member in members:
        found=passage.find(member,offset)
        if found<0:
            return False
        offset=found+len(member)
    return True


def _published_lookup(db, origin, base, config, context):
    from .private_lookup import intent
    tid=origin['id']
    need=research_contract.request(db,tid)
    assessment=research_contract.coverage(db,tid,'readback')
    stored=db.execute("""SELECT binding,fact_ids FROM knowledge_study_commits
        WHERE turn_id=? AND status='committed'""",(tid,)).fetchone()
    if (not need or not assessment or not stored or assessment['request']!=fingerprint(need)
            or assessment['outcome'] not in ('complete','partial')):
        return None
    try:
        work=public_lookup.require_order(db,config,origin,base,intent(db,tid))
    except RuntimeErrorBase:
        return None
    if stored['binding']!=fingerprint(work):
        return None
    fresh=set(json.loads(stored['fact_ids']))
    witnessed={w['id']:w['revision'] for w in assessment['witnesses']}
    ids=[f['id'] for f in context.get('facts',[]) if f['id'] in fresh and f['id'] in witnessed]
    facts=[semantic_knowledge.public_material(f) for f in semantic_knowledge.materials(
        db,origin,base,need['eligibility_query'],ids=ids)]
    return [f for f in facts if witnessed[f['id']]==fingerprint(f)],{'origin':tid}


def public_subject(db,turn,base,proof,*,config=None):
    """Locate an outgoing subject within a visible, source-backed prior statement."""
    if (not proof or not proof.get('quoted') or not (pins:=proof.get('pins')) or pins[0].get('role')!='subject'
            or not research_contract.active(db,turn['id'])):
        return None
    pin=pins[0]
    utterance=db.execute("""SELECT r.id,e.text,d.contract_json,t.id AS turn_id FROM events e
        JOIN turns t ON t.reply_event_id=e.id JOIN private_reply_attempts r ON r.turn_id=t.id AND r.phase='main'
        JOIN private_reply_reviews v ON v.attempt_id=r.id AND v.approved=1
        JOIN expression_speech_units u ON u.source_id=r.id AND u.phase='main'
        JOIN expression_speech_publications p ON p.utterance_id=u.id AND p.state='complete' AND p.canonical_event_id=e.id
        JOIN private_reply_decisions d ON d.id=r.decision_id
        WHERE e.id=? AND e.role='persona' AND r.body=e.text AND t.session_id=? AND t.persona_id=?
        AND t.status='completed' AND t.cancelled_at IS NULL AND t.rowid<(SELECT rowid FROM turns WHERE id=?)
        AND NOT EXISTS (SELECT 1 FROM source_exclusions x WHERE x.event_id IN (e.id,t.user_event_id)
             OR x.event_id IN (SELECT event_id FROM input_batch_members WHERE batch_id=t.input_batch_id))""",
        (pin['ref'],turn['session_id'],turn['persona_id'],turn['id'])).fetchone()
    if not utterance or fingerprint(utterance['text'])!=pin['revision']:
        return None
    context=json.loads(utterance['contract_json'])['ordinary'].get('research') or {}
    retained=context.get('retained_read')
    origin=db.execute("SELECT * FROM turns WHERE id=? AND session_id=? AND persona_id=?",
                      (retained['origin'] if retained else utterance['turn_id'],turn['session_id'],turn['persona_id'])).fetchone()
    if not origin:
        return None
    checked=retained_result(db,origin,base) if retained else (
        _published_lookup(db,origin,base,config,context) if config and context.get('facts') else None)
    if not checked:
        return None
    facts,binding=checked
    audit=public_lookup.saved(db,'private.reply.attribution.'+utterance['id'])
    if not audit or audit['verdict']!='grounded':
        return None
    by_id={f['id']:f for f in facts}
    start,end=pin['start'],pin['end']
    value=utterance['text'][start:end]
    if not value.strip():
        return None
    for citation in audit['bindings'] or []:
        if citation['field']!='body' or not citation['statement'][0]<=start<end<=citation['statement'][1]:
            continue
        fact=by_id.get(citation['fact'])
        if not fact or citation['source_hash']!=fingerprint(fact):
            continue
        left,right=citation['passage']
        if _bound_subject(value,fact['text'][left:right]):
            return {'origin':binding['origin'],'facts':facts,'witness':{
                'event':pin['ref'],'span':[start,end],'prior_attribution':utterance['id'],
                'fact':fact['id'],'source_revision':citation['source_hash']}}
    return None


def validate(db,turn,base,context):
    current=followup(db,turn,base,context)
    if current and 'assessment' not in context:
        current.pop('assessment',None)
    if current!=context:
        raise RuntimeErrorBase('private_reply_source_changed')


def retained_detail(db,store,turn):
    """Recheck both grants when the session owner opens the original lookup's sources."""
    if turn['error_code']!=MODEL_STOP:
        return None
    from .config import ConfigStore
    config=ConfigStore(store.paths)
    cfg=config.load()
    consent=session_privacy.current(db,config,cfg,turn['persona_id'],turn['session_id'],session_privacy.recipients(config,cfg))
    return retained_result(db,turn,{'instance':cfg['instance_id'],'consent':consent})
