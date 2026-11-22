"""Durable first-response deadlines and explicit retry decisions for desktop turns."""
import asyncio
import contextvars
import json
import time

from .archive import encoded, fingerprint
from .models import RuntimeErrorBase, timestamp
from .progress import record


FIRST_REPLY_SECONDS = 180
_first_reply = contextvars.ContextVar('desktop_stage_first_reply', default=None)
SLOTS = {
    'judge': ('private_reply_nodes', 'attempt_id'),
    'writer': ('private_reply_attempts', 'id'),
    'lookup': ('private_lookup_calls', 'attempt_id'),
    'generation': ('attempts','id'),
    'selector': ('character_selection_attempts','id'),
}


def initialize(db):
    db.execute("""CREATE TABLE IF NOT EXISTS desktop_stage_attempts(
        attempt_id TEXT PRIMARY KEY, turn_id TEXT NOT NULL REFERENCES turns(id),
        stage TEXT NOT NULL, binding TEXT NOT NULL, started REAL NOT NULL,
        first_response REAL, outcome TEXT NOT NULL, failure TEXT, archived TEXT,
        operation TEXT, replacement TEXT UNIQUE)""")
    db.execute("""CREATE TABLE IF NOT EXISTS desktop_retry_clicks(
        operation TEXT PRIMARY KEY,turn_id TEXT NOT NULL REFERENCES turns(id),attempt_id TEXT NOT NULL,
        state TEXT NOT NULL CHECK(state IN ('awaiting_receipt','accepted','closed')))""")
    db.execute("""CREATE TABLE IF NOT EXISTS desktop_retry_preparation(
        turn_id TEXT PRIMARY KEY REFERENCES turns(id), prepared_json TEXT NOT NULL)""")
    db.execute("""CREATE TRIGGER IF NOT EXISTS desktop_retry_preparation_finished
        AFTER UPDATE OF status,cancelled_at ON turns WHEN NEW.status='completed' OR NEW.cancelled_at IS NOT NULL
        BEGIN DELETE FROM desktop_retry_preparation WHERE turn_id=NEW.id; END""")
    db.execute("""CREATE TABLE IF NOT EXISTS desktop_turn_checkpoints(
        turn_id TEXT PRIMARY KEY REFERENCES turns(id), contract_hash TEXT NOT NULL)""")
    db.execute('CREATE INDEX IF NOT EXISTS desktop_stage_turn ON desktop_stage_attempts(turn_id,started)')


def admission_scope(db, config, pid, sid):
    from . import session_privacy
    cfg = config.load()
    destinations = session_privacy.recipients(config,cfg)
    consent = session_privacy.current(db,config,cfg,pid,sid,destinations)
    public = session_privacy.public_scope(db,config,cfg['instance_id'],pid,sid,consent)
    marker = config.paths.home / 'private/ordinary-reply/registration.json'
    version = json.loads(marker.read_bytes()).get('reviewed_sha256') if marker.is_file() else None
    return fingerprint([cfg['instance_id'],pid,sid,consent,public,version])


def content_received():
    signal = _first_reply.get()
    if signal is not None and not signal.is_set():
        signal.set()


def start(db, tid, aid, stage, binding):
    # A confirmed pre-send reconnect continues the original logical deadline.
    pending = db.execute("""SELECT attempt_id,stage,binding FROM desktop_stage_attempts
        WHERE turn_id=? AND operation IS NOT NULL AND replacement IS NULL ORDER BY binding=? DESC LIMIT 1""",(tid,binding)).fetchone()
    if pending and (pending['stage'],pending['binding']) != (stage,binding):
        raise RuntimeErrorBase('private_reply_decision_stale')
    if pending:
        db.execute('UPDATE desktop_stage_attempts SET replacement=? WHERE attempt_id=?',(aid,pending['attempt_id']))
    prev = db.execute("""SELECT started,outcome,operation FROM desktop_stage_attempts WHERE turn_id=? AND stage=?
        AND binding=? ORDER BY rowid DESC LIMIT 1""",
                      (tid, stage, binding)).fetchone()
    if (prev and prev['outcome']=='unsent' and not prev['operation']
            and time.time()-prev['started'] > FIRST_REPLY_SECONDS):
        raise RuntimeErrorBase('upstream_no_reply')
    db.execute("""INSERT INTO desktop_stage_attempts(attempt_id,turn_id,stage,binding,started,outcome)
        VALUES(?,?,?,?,?,'inflight')""", (aid, tid, stage, binding, prev[0] if prev and prev['outcome']=='unsent' and not prev['operation'] else time.time()))


def mark(db, aid, outcome, failure=None):
    db.execute('UPDATE desktop_stage_attempts SET outcome=?,failure=? WHERE attempt_id=? AND operation IS NULL',
               (outcome, failure, aid))


def manual_terminal(error):
    return _first_reply.get() is not None and classify(error)[0]=='terminal'


def classify(error):
    diag = getattr(error, 'diagnostics', {}) or {}
    http = diag.get('http_status')
    if (diag.get('provider_terminal_error') or type(http) is int and 400 <= http < 600
            or (getattr(error, 'native', None) or {}).get('outcome') == 'failed'
            or (getattr(error,'output_validation',None) or {}).get('status')=='rejected'
            or str(error)=='provider_invalid_response' and diag.get('phase')=='parsing_response'
            or str(error) in ('provider_empty_response','private_reply_invalid_receipt',
                              'knowledge_study_invalid_result','private_lookup_invalid_result')):
        return 'terminal', 'upstream_error'
    return 'unknown', None


def mark_error(db, aid, error, parked):
    state=db.execute('SELECT outcome FROM desktop_stage_attempts WHERE attempt_id=?',(aid,)).fetchone()
    if not state or state['outcome']=='received':
        return
    if parked:
        mark(db, aid, 'unsent')
    elif str(error) == 'upstream_no_reply':
        mark(db, aid, 'unknown', 'upstream_no_reply')
    else:
        outcome, reason = classify(error)
        if reason is None:
            db.execute("UPDATE desktop_stage_attempts SET outcome=? WHERE attempt_id=? AND operation IS NULL",
                       (outcome,aid))
        else:
            mark(db,aid,outcome,reason)


async def observe(store, aid, call):
    signal = asyncio.Event()
    token = _first_reply.set(signal)
    task = asyncio.ensure_future(call)
    _first_reply.reset(token)
    progress = asyncio.create_task(signal.wait())
    try:
        with store.connection() as db:
            row = db.execute('SELECT turn_id,started,stage FROM desktop_stage_attempts WHERE attempt_id=?',(aid,)).fetchone()
        remaining = max(0, row['started'] + FIRST_REPLY_SECONDS - time.time())
        await asyncio.wait((task, progress), timeout=remaining + 0.001, return_when=asyncio.FIRST_COMPLETED)
        if signal.is_set():
            with store.connection(write=True) as db:
                db.execute('UPDATE desktop_stage_attempts SET first_response=? WHERE attempt_id=?',(time.time(),aid))
        elif not task.done():
            with store.connection(write=True) as db:
                mark(db, aid, 'unknown', 'upstream_no_reply')
                if row['stage'] in ('generation','selector'):
                    db.execute("UPDATE turns SET error_code='upstream_no_reply',updated_at=? WHERE id=? AND status IN ('queued','running')",
                               (timestamp(),row['turn_id']))
                else:
                    db.execute("UPDATE turns SET status='interrupted',error_code='upstream_no_reply',updated_at=? WHERE id=? AND cancelled_at IS NULL AND status!='completed'",
                               (timestamp(),row['turn_id']))
                record(db,row['turn_id'],'interrupted')
            # Retain the physical call so a late response settles its original slot.
        return await task
    finally:
        progress.cancel()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, progress, return_exceptions=True)


def failure(db, turn, now=None):
    if (turn['status'] not in ('interrupted','failed','queued','running') or turn['cancelled_at']
            or turn['reply_event_id'] or turn['channel'] != 'desktop'
            or turn['status']=='queued' and turn['error_code'] not in ('desktop_network_wait','upstream_no_reply')
            or turn['status']=='running' and turn['error_code']!='upstream_no_reply'):
        return None
    if not db.execute("SELECT 1 FROM sqlite_master WHERE name='desktop_turn_checkpoints'").fetchone():
        return None
    if not db.execute('SELECT 1 FROM desktop_turn_checkpoints WHERE turn_id=?',(turn['id'],)).fetchone():
        return None
    row = db.execute("""SELECT * FROM desktop_stage_attempts a WHERE turn_id=? AND operation IS NULL
        AND outcome IN ('unknown','terminal','inflight','unsent')
        AND NOT EXISTS(SELECT 1 FROM desktop_stage_attempts b WHERE b.turn_id=a.turn_id AND b.stage=a.stage
          AND b.binding=a.binding AND b.rowid>a.rowid)
        ORDER BY (failure IS NOT NULL) DESC,rowid DESC LIMIT 1""",(turn['id'],)).fetchone()
    if not row:
        return None
    reason = row['failure']
    if (not reason and row['first_response'] is None and
            (time.time() if now is None else now) - row['started'] > FIRST_REPLY_SECONDS):
        reason = 'upstream_no_reply'
    return {'version':row['attempt_id'],'reason':reason} if reason else None


def expire(store):
    with store.connection(write=True) as db:
        rows = db.execute("""SELECT a.attempt_id,a.turn_id FROM desktop_stage_attempts a
            JOIN turns t ON t.id=a.turn_id WHERE a.first_response IS NULL AND a.failure IS NULL
            AND a.operation IS NULL AND a.outcome IN ('unsent','unknown','inflight')
            AND (t.status IN ('interrupted','failed') OR t.status='queued' AND t.error_code='desktop_network_wait')
            AND t.cancelled_at IS NULL
            AND a.started<? AND NOT EXISTS(SELECT 1 FROM desktop_stage_attempts b WHERE b.turn_id=t.id
            AND b.stage=a.stage AND b.binding=a.binding AND b.rowid>a.rowid)""",
            (time.time()-FIRST_REPLY_SECONDS,)).fetchall()
        for aid, tid in rows:
            db.execute("UPDATE desktop_stage_attempts SET failure='upstream_no_reply' WHERE attempt_id=?",(aid,))
            db.execute("UPDATE turns SET status='interrupted',error_code='upstream_no_reply',updated_at=? WHERE id=?",
                       (timestamp(),tid))
            record(db,tid,'interrupted')


def retry(store, pid, sid, tid, expected, op):
    if not isinstance(op,str) or not op or len(op)>128:
        raise RuntimeErrorBase('operation_id_required')
    from .local_repair import require_open
    from .migration import ensure_cloud_ready
    require_open(store.paths)
    ensure_cloud_ready(store.paths)
    with store.connection(write=True) as db:
        turn = db.execute("SELECT * FROM turns WHERE id=? AND persona_id=? AND session_id=? AND channel='desktop'",
                          (tid,pid,sid)).fetchone()
        if not turn:
            raise RuntimeErrorBase('session_unavailable')
        from .desktop_recovery import validate_turn
        validate_turn(db,turn)
        old = db.execute('SELECT turn_id,attempt_id,state FROM desktop_retry_clicks WHERE operation=?',(op,)).fetchone()
        if old:
            if (old['turn_id'],old['attempt_id'])!=(tid,expected):
                raise RuntimeErrorBase('operation_conflict')
            if old['state']!='awaiting_receipt':
                return {'state':old['state']}
        consumed = db.execute('SELECT operation FROM desktop_stage_attempts WHERE turn_id=? AND attempt_id=?',
                              (tid,expected)).fetchone()
        if consumed and consumed[0]:
            db.execute("UPDATE desktop_retry_clicks SET state='accepted' WHERE operation=?",(op,))
            return {'state':'accepted'}
        checkpoint = db.execute('SELECT contract_hash FROM desktop_turn_checkpoints WHERE turn_id=?',(tid,)).fetchone()
        from .config import ConfigStore
        if not checkpoint or checkpoint[0]!=admission_scope(db,ConfigStore(store.paths),pid,sid):
            raise RuntimeErrorBase('private_lookup_scope_changed')
        current = failure(db,turn)
        if not current or current['version'] != expected:
            raise RuntimeErrorBase('desktop_retry_stale')
        row = db.execute('SELECT * FROM desktop_stage_attempts WHERE attempt_id=?',(expected,)).fetchone()
        db.execute("INSERT OR IGNORE INTO desktop_retry_clicks VALUES(?,?,?,'awaiting_receipt')",
                   (op,tid,expected))
        from .desktop_recovery import contract
        owner = contract(db,turn)
        if (row['stage'] not in ('generation','selector') or turn['status'] in ('queued','running')) and owner and owner['owner'] is not None:
            return {'state':'awaiting_receipt'}
        table,column = SLOTS[row['stage']]
        slot = db.execute(f'SELECT * FROM {table} WHERE turn_id=? AND {column}=?',(tid,expected)).fetchone()
        if (row['stage']=='writer' and slot and slot['phase']=='main'
                and json.loads(turn['model_snapshot']).get('_main_reply') and row['outcome']=='terminal'):
            from .generation_retry import MAX_RETRIES
            error = json.loads(slot['usage_json'] or '{}').get('transport_error',{})
            count = db.execute("SELECT count(*) FROM desktop_stage_attempts WHERE turn_id=? AND stage='writer' AND binding=?",
                               (tid,row['binding'])).fetchone()[0]
            if error.get('http_status')!=429 or count > MAX_RETRIES:
                raise RuntimeErrorBase('main_reply_retry_unavailable')
            if time.time() < error.get('retry_not_before_epoch',0):
                raise RuntimeErrorBase('main_reply_retry_wait')
        if row['stage']=='selector':
            slot=db.execute('SELECT * FROM character_selection_attempts WHERE turn_id=? AND execution_revision=?',
                            (tid,turn['execution_revision'])).fetchone()
        if row['outcome'] not in ('unsent','terminal') and row['stage'] not in ('generation','selector') and slot and slot['state']=='received':
            db.execute("UPDATE desktop_stage_attempts SET outcome='received',failure=NULL,operation=?,replacement=? WHERE attempt_id=?",
                       (op,expected,expected))
            db.execute("UPDATE turns SET status='queued',error_code=NULL,updated_at=? WHERE id=?",(timestamp(),tid))
            db.execute("UPDATE desktop_retry_clicks SET state='accepted' WHERE operation=?",(op,))
            record(db,tid,'retry_queued')
            return {'state':'accepted'}
        if row['outcome'] not in ('unsent','terminal'):
            return {'state':'awaiting_receipt'}
        targets = [(expected,slot)]
        parallel = ('J2.action','J2.task','J2.stance','J3')
        if row['stage']=='judge' and slot and slot['node'].removeprefix('notice.') in parallel:
            prefix = 'notice.' if slot['node'].startswith('notice.') else ''
            for other in db.execute("""SELECT n.*,a.outcome FROM private_reply_nodes n
                    JOIN desktop_stage_attempts a ON n.attempt_id=a.attempt_id
                    WHERE n.turn_id=? AND n.draft=? AND a.operation IS NULL AND a.attempt_id!=?""",
                                    (tid,slot['draft'],expected)).fetchall():
                if other['node'] not in [prefix+name for name in parallel] or other['outcome']=='received':
                    continue
                if other['outcome']!='terminal':
                    return {'state':'awaiting_receipt'}
                targets.append((other['attempt_id'],other))
        for aid, saved in targets:
            receipt = dict(saved) if saved else {}
            receipt.pop('model_snapshot',None)
            if row['stage']=='lookup' and db.execute("SELECT 1 FROM sqlite_master WHERE name='retrieval_item_calls'").fetchone():
                item=db.execute('SELECT * FROM retrieval_item_calls WHERE attempt_id=?',(aid,)).fetchone()
                if item:
                    receipt['retrieval_item']=dict(item)
            db.execute('UPDATE desktop_stage_attempts SET archived=?,operation=? WHERE attempt_id=?',
                       (encoded(receipt),op,aid))
            if row['stage']=='generation':
                from .scene_policy import save
                recovery = contract(db,turn)
                if not recovery or recovery.get('attempt') != aid:
                    raise RuntimeErrorBase('desktop_retry_stale')
                recovery.pop('attempt')
                recovery.update(owner=None,process=None,phase='ready',recovered=True)
                save(db,'desktop.recovery.'+tid,recovery)
            elif row['stage']=='selector':
                from .desktop_recovery import release_for_continuation
                if owner:
                    release_for_continuation(db,turn,owner['owner'])
            elif saved:
                db.execute(f'DELETE FROM {table} WHERE turn_id=? AND {column}=?',(tid,aid))
        db.execute("UPDATE turns SET status='queued',error_code=NULL,updated_at=? WHERE id=?",(timestamp(),tid))
        db.execute("UPDATE desktop_retry_clicks SET state='accepted' WHERE operation=?",(op,))
        record(db,tid,'retry_queued')
        return {'state':'accepted'}


def reconcile_clicks(store):
    """Continue an explicit click after the original wire outcome becomes known."""
    with store.connection() as db:
        waiting = db.execute("""SELECT t.persona_id,t.session_id,t.id,c.attempt_id,c.operation,t.status
            FROM desktop_retry_clicks c JOIN turns t ON t.id=c.turn_id
            JOIN desktop_stage_attempts a ON a.attempt_id=c.attempt_id
            WHERE c.state='awaiting_receipt' AND (t.cancelled_at IS NOT NULL OR t.status='completed'
                OR a.outcome IN ('terminal','unsent') AND t.status IN ('interrupted','failed'))
            ORDER BY c.rowid LIMIT 32""").fetchall()
    queued = False
    for pid,sid,tid,aid,op,status in waiting:
        if status=='completed':
            with store.connection(write=True) as db:
                db.execute("UPDATE desktop_retry_clicks SET state='accepted' WHERE operation=? AND state='awaiting_receipt'",(op,))
            continue
        try:
            response=retry(store,pid,sid,tid,aid,op)
        except RuntimeErrorBase as exc:
            if str(exc) in ('local_repair_active','storage_write_fault','storage_low_pause_dispatch'):
                continue
            with store.connection(write=True) as db:
                db.execute("UPDATE desktop_retry_clicks SET state='closed' WHERE operation=?",(op,))
        else:
            queued |= response['state']=='accepted'
    return queued
