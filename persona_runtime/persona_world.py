"""Persona-scoped references to admitted passages in received public tool results."""
import hashlib
import json
from pathlib import Path

from . import local_knowledge as library
from . import public_lookup as public
from .archive import encoded, fingerprint
from .knowledge_revisions import canonical_url
from .models import RuntimeErrorBase
from .scene_policy import read, save
from .session_privacy import PROTOCOL, _public_key

VERSION = 'persona-world-v1'
PREFIX = 'private.persona_world.v1.'


def _prefix(instance, pid):
    return PREFIX + fingerprint([instance, pid]) + '.'


def passage_id(instance, pid, item):
    """Identify one literal source span across acquisitions by this persona."""
    return fingerprint([VERSION, instance, pid, canonical_url(item['url']), item['sha256'],
                        item['start'], item['end']])


def enabled(db, instance, pid):
    return read(db, _prefix(instance, pid) + 'enabled') is True


def set_enabled(db, instance, pid, enabled):
    if type(enabled) is not bool or not instance or not pid:
        raise RuntimeErrorBase('persona_world_invalid_scope')
    save(db, _prefix(instance, pid) + 'enabled', enabled)


def _grant(db, instance, pid, sid, revision, recipients):
    current = db.execute("""SELECT revision,enabled,protocol,recipients_json,operation_hash
        FROM session_privacy_events WHERE instance_id=? AND persona_id=? AND session_id=?
        ORDER BY revision DESC LIMIT 1""", (instance, pid, sid)).fetchone()
    if (current and current['revision'] == revision and current['enabled']
            and current['protocol'] == PROTOCOL and fingerprint(json.loads(current['recipients_json'])) == recipients):
        return current['operation_hash']
    return None


def active(db, turn, base):
    pid = turn['persona_id']
    if base.get('knowledge_disabled') or not enabled(db, base['instance'], pid):
        return False
    row = db.execute("""SELECT s.source_json FROM private_reply_source_sets s
        JOIN private_reply_turns p ON p.turn_id=s.turn_id JOIN turns t ON t.id=p.turn_id
        WHERE p.turn_id=? AND p.instance_id=? AND t.persona_id=? AND t.session_id=? AND t.channel=?""",
        (turn['id'], base['instance'], pid, turn['session_id'],turn['channel'])).fetchone()
    if turn['channel'] == 'qq':
        live = _live_privacy(db,base['instance'])
        if not live:
            return False
        from .qq_native_reply import assert_bound
        try:
            if assert_bound(db,live[0].paths,live[1],turn) != base.get('qq_origin'):
                return False
        except RuntimeErrorBase:
            return False
    elif turn['channel'] != 'desktop':
        return False
    return bool(row and json.loads(row[0]).get('persona_world_version') == VERSION and
                base['consent']['enabled'] and _grant(db, base['instance'], pid, turn['session_id'],
                    base['consent']['revision'], base['consent']['recipient_hash']))


def withdraw_source(db, instance, pid, url, sha256=None):
    url = canonical_url(library.public_url(url))
    if sha256 is not None and (not isinstance(sha256, str) or len(sha256) != 64 or
                                any(c not in '0123456789abcdef' for c in sha256)):
        raise RuntimeErrorBase('persona_world_invalid_revision')
    save(db, _prefix(instance, pid) + 'revoked.' + fingerprint([url, sha256]), True)


def withdrawn(db, instance, pid, url, digest):
    url = canonical_url(url)
    key = _prefix(instance, pid) + 'revoked.'
    return any(read(db, key + fingerprint([url, value])) is True for value in (None, digest))


def has_withdrawals(db, instance, pid):
    pattern = _prefix(instance, pid) + 'revoked.*'
    return db.execute('SELECT 1 FROM knowledge_control WHERE key GLOB ? LIMIT 1', (pattern,)).fetchone() is not None


def _live_privacy(db, instance):
    from . import session_privacy
    from .config import ConfigStore
    from .paths import RuntimePaths
    path = Path(db.execute('PRAGMA database_list').fetchone()[2])
    if not path.is_absolute() or path.parts[-3:] != ('data', 'database', 'runtime.sqlite3'):
        return None
    config = ConfigStore(RuntimePaths(path.parents[2]))
    try:
        cfg = config.load()
        if cfg['instance_id'] == instance:
            return config, cfg, session_privacy.recipients(config, cfg)
    except (OSError, ValueError, RuntimeErrorBase):
        pass
    return None


def _live_grant(db, live, pid, sid, revision, recipient_hash, public_scope=None):
    from . import session_privacy
    config, cfg, recipients = live
    try:
        current = session_privacy.current(db, config, cfg, pid, sid, recipients)
        if (not current['enabled'] or current['revision'] != revision
                or current['recipient_hash'] != recipient_hash):
            return False
        if public_scope:
            actual = session_privacy.public_scope(db, config, cfg['instance_id'], pid, sid, current,
                                               protocol=public_scope['protocol'],historical=True)
            return actual == public_scope
    except (OSError, ValueError, RuntimeErrorBase, AttributeError):
        return False
    return True


def _authority(db, instance, pid, tid, live=None):
    work = public.saved(db, public.key(tid, 'order')) or read(db,'private.persona_world.acquisition.'+tid)
    if not work or (work.get('turn'), work.get('instance'), work.get('persona')) != (tid, instance, pid):
        return None
    sid = work['session']
    row = db.execute("""SELECT i.origin,i.consent_revision,i.query_json,r.created_at,t.cancelled_at
        FROM private_lookup_intents i JOIN private_lookup_records r ON r.turn_id=i.turn_id
        JOIN private_reply_turns p ON p.turn_id=i.turn_id JOIN turns t ON t.id=i.turn_id
        WHERE i.turn_id=? AND p.instance_id=? AND t.persona_id=? AND t.session_id=? AND t.channel='desktop'
        AND r.persona_id=t.persona_id AND r.session_id=t.session_id AND r.outcome='acquired'""",
        (tid, instance, pid, sid)).fetchone()
    if (not row or row['cancelled_at'] or row['origin'] not in ('session', 'explicit', 'derived', 'confirmed')
            or (row['query_json'] and json.loads(row['query_json']) != work.get('query'))):
        return None
    authority = work.get('authority') or {}
    if work.get('baseline_receipt'):
        if not _grant(db,instance,pid,sid,work['consent_revision'],work['recipients']):
            return None
        scope=work.get('public_scope')
        if scope:
            grant=read(db,_public_key(instance,pid,sid)) or {}
            if not grant.get('enabled') or (grant.get('revision'),grant.get('scope'))!=(scope['revision'],scope['recipient_hash']):
                return None
        if live and not _live_grant(db,live,pid,sid,work['consent_revision'],work['recipients'],scope):
            return None
        return work
    if authority.get('kind') not in ('context', 'current_query', 'confirmed', 'selection', 'link'):
        return None
    scope = authority.get('scope')
    if row['origin'] == 'session' or scope:
        if not _grant(db, instance, pid, sid, work['consent_revision'], work['recipients']):
            return None
        if live and not _live_grant(db, live, pid, sid, work['consent_revision'], work['recipients'], scope):
            return None
    if scope:
        saved = read(db, _public_key(instance, pid, sid)) or {}
        if not saved.get('enabled') or (saved.get('revision'), saved.get('scope')) != (
                scope['revision'], scope['recipient_hash']):
            return None
    return work


def register_baseline_acquisition(db,turn,base,signature):
    """Attach shared storage to the existing local-first acquisition, without a new model decision."""
    from .private_lookup import intent
    if not active(db,turn,base):
        return
    acquisition=intent(db,turn['id'])
    consent=base['consent']
    if not acquisition or not acquisition['query_json']:
        return
    from . import single_retrieval
    batch=single_retrieval.saved(db,turn['id'],'batch')
    extra={'public_scope':batch['scope'],'items':batch['items']} if batch else {}
    save(db,'private.persona_world.acquisition.'+turn['id'],{
        'turn':turn['id'],'instance':base['instance'],'persona':turn['persona_id'],'session':turn['session_id'],
        'query':json.loads(acquisition['query_json']),'consent_revision':consent['revision'],
        'recipients':consent['recipient_hash'],'baseline_receipt':signature,**extra})


def _source_signature(source):
    try:
        url = library.public_url(source.get('url'))
    except RuntimeErrorBase:
        return None
    fields = [source.get(k) if isinstance(source.get(k), str) else None
              for k in ('kind', 'content', 'summary', 'date', 'qualification')]
    title = source.get('title')
    return (url, title if isinstance(title, str) and title else url, *fields)


def _original_sources(db, tid, work):
    result = {}
    rows = db.execute("""SELECT step,attempt_id,result_json FROM private_lookup_calls WHERE turn_id=?
        AND state='received' AND step IN ('native','search','fetch') ORDER BY rowid""", (tid,))
    if 'items' in work:
        from . import single_retrieval
        plan=single_retrieval.saved(db,tid,'plan')
        hashes={q['id']:fingerprint(q['text']) for q in plan['queries'] if q['id'] in work['items']}
        children=db.execute("""SELECT i.step,c.attempt_id,c.result_json,i.item_id,i.query_hash
            FROM retrieval_item_calls i JOIN private_lookup_calls c ON i.attempt_id=c.attempt_id AND i.turn_id=c.turn_id
            WHERE i.turn_id=? AND c.state='received' ORDER BY c.rowid""",(tid,))
        rows=(r[:3] for r in children if hashes.get(r[3])==r[4])
    for step, attempt, raw in rows:
        if not work.get('baseline_receipt'):
            wire = public.saved(db, public.key(tid, 'wire.' + attempt)) or {}
            if (wire.get('attempt'), wire.get('step'), wire.get('order_hash'), wire.get('query_hash')) != (
                    attempt, step, fingerprint(work), fingerprint(work['query'])):
                continue
        data = json.loads(raw)
        if step=='native' and not data.get('tool_calls'):
            continue
        for source in data.get('sources') or []:
            if not isinstance(source, dict):
                continue
            kind = source.get('kind')
            if step == 'native':
                if (kind, source.get('material_kind')) not in (
                        ('native_search_summary', 'provider_snippet'), ('page_content', 'page_content')):
                    continue
            elif (step, kind) not in (('fetch', 'page_content'), ('search', 'search_content'), ('search', 'search_summary')):
                continue
            signature = _source_signature(source)
            if signature and (source.get('content') or source.get('summary')):
                result[signature] = attempt
    return result


def public_body(db, instance, pid, tid, source):
    """Check a new local document before deduplicating its public source bytes."""
    if not enabled(db, instance, pid):
        return False
    work = _authority(db, instance, pid, tid)
    if not work:
        return False
    entry = (public.saved(db, public.key(tid, 'admission')) or {}).get(source['id'], {})
    admitted=work.get('baseline_receipt') or (entry.get('spans') and entry.get('revision')==fingerprint(source))
    return bool(admitted and _source_signature(source) in _original_sources(db,tid,work))


def inspect_turn(db, instance, pid, tid, *, live=None):
    """Return reference metadata for live, committed public passages without modifying the database."""
    from .private_lookup import evidence
    from .lookup_target import excluded_sources
    from . import research_contract
    work = _authority(db, instance, pid, tid, live)
    if not work:
        return []
    commit = db.execute("""SELECT binding,fact_ids FROM knowledge_study_commits
        WHERE turn_id=? AND status='committed'""", (tid,)).fetchone()
    baseline=work.get('baseline_receipt')
    revision = public.saved(db, public.key(tid, 'revision'))
    if not commit or commit['binding'] != (baseline or fingerprint(work)) or (not baseline and not revision):
        return []
    sid = work['session']
    reading = evidence(db, pid, sid, tid, active_turn=tid)
    if not reading:
        return []
    evidence_by_id = {s['id']:s for s in reading['sources']}
    accepted = public.saved(db, public.key(tid, 'admission')) or {}
    originals = _original_sources(db, tid, work)
    excluded = excluded_sources(db, tid)
    ids = set(json.loads(commit['fact_ids']))
    if 'items' in work:
        from . import single_retrieval
        assessment=single_retrieval.saved(db,tid,'readback')
        if not assessment:
            return []
        ids.intersection_update(i for row in assessment['items'] for i,_ in row['facts'])
    if not baseline:
        ids.intersection_update(revision['facts'])
    if request_hash:=work.get('research_request'):
        assessment=research_contract.coverage(db,tid,'readback')
        if (not assessment or assessment['request']!=request_hash
                or assessment['outcome'] not in ('complete','partial')):
            return []
        ids.intersection_update(w['id'] for w in assessment['witnesses'])
    rows = db.execute("""SELECT d.*,f.id AS fact_id,f.spec_json FROM local_knowledge_documents d
        JOIN local_knowledge_facts f ON f.document_id=d.id WHERE d.scope='session' AND d.persona_id=?
        AND d.session_id=? AND f.withdrawn=0 AND f.id IN (SELECT value FROM json_each(?))""",
        (pid, sid, encoded(sorted(ids))))
    passages = []
    for doc in rows:
        spec = json.loads(doc['spec_json'])
        source_turn = spec.get('turn_source') or []
        if (len(source_turn) != 3 or source_turn[0] != tid or source_turn[1] in excluded
                or spec['state'] != 'source_excerpt'):
            continue
        if doc['consent_revision'] and library._grant(db, instance, pid, sid, doc['consent_revision']) != doc['grant_hash']:
            continue
        key = source_turn[1]
        source = evidence_by_id.get(key)
        entry = accepted.get(key, {})
        if (not source or fingerprint(source)!=source_turn[2]
                or (not baseline and entry.get('revision')!=source_turn[2])):
            continue
        attempt = originals.get(_source_signature(source))
        span = [spec['start'], spec['end']]
        if not attempt or (not baseline and span not in entry.get('spans', [])):
            continue
        body = source.get('content') or source.get('summary') or ''
        if not (0 <= span[0] < span[1] <= len(body) and body[span[0]:span[1]] == spec['quote'] == spec['value']):
            continue
        # Only source-derived metadata may follow the passage into a different session.
        check = spec.get('verification') or {}
        if (spec['subject'] != source['title'][:128] or spec['predicate'] != source['kind']
                or spec['attributes'] or spec['conditions'] or spec['aliases']
                or check.get('title') != source['title'] or check.get('source_kind') != source['kind']
                or check.get('receipt') != tid or check.get('document') != doc['id']):
            continue
        url = canonical_url(source['url'])
        body_bytes = body.encode()
        digest = hashlib.sha256(body_bytes).hexdigest()
        if (doc['sha256'] != digest or canonical_url(doc['url']) != url or library.document_body(db, doc) != body
                or withdrawn(db, instance, pid, url, digest)):
            continue
        passages.append(dict(fact_id=doc['fact_id'], document_id=doc['id'], source_id=key,
                             session_id=sid, origin=tid, attempt=attempt, url=url, sha256=digest,
                             context_hash=fingerprint([source['title'], source.get('date'),
                                                       source['kind'], source.get('qualification')]),
                             start=span[0], end=span[1], bytes=len(body_bytes), acquired_at=check['at']))
    return passages


def index_turn(db, instance, pid, tid):
    prefix = _prefix(instance, pid)
    count = 0
    for item in inspect_turn(db, instance, pid, tid):
        library.register_world_body(db, instance, pid, item['document_id'])
        revision = fingerprint([item['url'], item['sha256']])
        db.execute('INSERT OR IGNORE INTO knowledge_control VALUES(?,?)',
                   (prefix + 'document.' + revision, encoded(item['document_id'])))
        key = prefix + 'fact.' + item['fact_id']
        existing = read(db, key)
        if existing is None:
            db.execute('INSERT INTO knowledge_control VALUES(?,?)', (key, encoded(item)))
            count += 1
        elif existing != item:
            raise RuntimeErrorBase('persona_world_reference_changed')
    return count


def attestations(db, instance, pid):
    prefix = _prefix(instance, pid) + 'fact.'
    return [json.loads(row[0]) for row in db.execute(
        'SELECT value FROM knowledge_control WHERE key GLOB ? ORDER BY key', (prefix + '*',))]


def eligible_fact_ids(db, turn, base, ids=None):
    """Map each shareable original fact to the source session owning its private receipt."""
    if ids == [] or not active(db, turn, base):
        return {}
    pid, instance = turn['persona_id'], base['instance']
    if ids is None:
        indexed = attestations(db, instance, pid)
    else:
        prefix = _prefix(instance, pid) + 'fact.'
        keys = encoded([prefix + ident for ident in set(ids)])
        indexed = (json.loads(row[0]) for row in db.execute(
            'SELECT value FROM knowledge_control WHERE key IN (SELECT value FROM json_each(?))', (keys,)))
    stored = {}
    for item in indexed:
        if item['session_id'] != turn['session_id']:
            stored.setdefault(item['origin'], {})[item['fact_id']] = item
    if not stored:
        return {}
    live = _live_privacy(db, instance)
    consent = base['consent']
    if not live or not _live_grant(db, live, pid, turn['session_id'], consent['revision'], consent['recipient_hash']):
        return {}
    eligible = []
    for origin, snapshot in stored.items():
        earlier = db.execute("""SELECT 1 FROM turns a JOIN turns b ON b.id=? AND a.rowid<b.rowid
            WHERE a.id=?""", (turn['id'], origin)).fetchone()
        if earlier:
            eligible.extend(item for item in inspect_turn(db, instance, pid, origin, live=live)
                            if snapshot.get(item['fact_id']) == item)
    if ids is None:
        unique = {}
        for item in sorted(eligible, key=lambda x:(x['acquired_at'], x['fact_id'])):
            unique[passage_id(instance, pid, item), item['context_hash']] = item
        eligible = unique.values()
    return {item['fact_id']:item['session_id'] for item in eligible}
