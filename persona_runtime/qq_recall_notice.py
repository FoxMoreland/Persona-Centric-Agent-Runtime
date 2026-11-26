"""Apply an authenticated private recall only to proven PCAR-authored output."""
import json

from .archive import encoded, scope
from .memory_policy import exclude_sources
from .models import RuntimeErrorBase, timestamp
from .qq_native_reply import assert_bound, configured


def notice(event, cfg):
    raw = getattr(getattr(event,'message_obj',None),'raw_message',None)
    if not isinstance(raw,dict) or (raw.get('post_type'),raw.get('notice_type')) != ('notice','friend_recall'):
        return None
    if str(raw.get('self_id','')) != cfg['bot_qq'] or str(raw.get('user_id','')) != cfg['owner_qq']:
        return None
    mid = raw.get('message_id')
    if type(mid) not in (int,str) or not 0 < len(str(mid)) <= 200:
        return None
    return {'bot':cfg['bot_qq'], 'peer':cfg['owner_qq'], 'message_id':str(mid)}


def apply(store, config, value):
    cfg = config.load()
    pid = cfg['runtime']['qq_persona_id']
    bot, peer = cfg['bot_qq'], cfg['owner_qq']
    mid = value['message_id']
    if (not configured(store.paths,cfg,pid) or (value['bot'],value['peer']) != (bot,peer)
            or not isinstance(mid,str) or not 0 < len(mid) <= 200):
        raise RuntimeErrorBase('qq_recall_scope_invalid')
    with store.connection(write=True) as db:
        selected = scope(pid,'qq',bot,peer)
        incoming = db.execute('''SELECT 1 FROM raw_events
            WHERE persona_id=? AND user_id=? AND channel=? AND bot=? AND peer=? AND platform_message_id=?''',
                              (*selected.values(),mid)).fetchone()
        if incoming:
            return {'state':'ignored'}
        opening = db.execute('''SELECT e.id FROM events e JOIN knowledge_control k
            ON k.key='qq.first.confirmed.'||e.session_id
            WHERE e.persona_id=? AND e.role='persona' AND json_extract(k.value,'$.event_id')=e.id
              AND json_extract(k.value,'$.platform_message_id')=?
              AND json_extract(k.value,'$.origin.bot')=? AND json_extract(k.value,'$.origin.peer')=?''',
                             (pid,mid,bot,peer)).fetchall()
        rows = db.execute("""SELECT t.*,p.canonical_event_id FROM knowledge_control k
            JOIN expression_speech_items i ON k.key='qq.platform.speech.'||i.receipt_id
            JOIN expression_speech_publications p ON p.id=i.publication_id AND p.revision=i.revision
            JOIN expression_speech_units u ON u.id=p.utterance_id AND u.source_kind='maibot_private_reply'
            JOIN turns t ON t.id=u.turn_id AND t.channel='qq' AND t.persona_id=?
            WHERE k.value=? AND i.state='presented' AND json_extract(t.origin_json,'$.bot')=?
             AND json_extract(t.origin_json,'$.sender')=?""", (pid,encoded(mid),bot,peer)).fetchall()
        if len(opening) + len(rows) != 1:
            return {'state':'ignored'}
        if opening:
            event = opening[0]
            replay = db.execute('SELECT 1 FROM source_exclusions WHERE event_id=?',(event['id'],)).fetchone()
            if not replay:
                exclude_sources(db,pid,[event['id']],'qq_self_recalled',expand_turns=False)
                db.execute("UPDATE events SET text='' WHERE id=?",(event['id'],))
            return {'state':'withdrawn','already_applied':bool(replay)}
        turn = dict(rows[0])
        assert_bound(db,store.paths,cfg,turn)
        key = 'qq.native.recalled.' + turn['id']
        if db.execute('SELECT 1 FROM knowledge_control WHERE key=?',(key,)).fetchone():
            return {'state':'withdrawn','already_applied':True}
        db.execute('INSERT INTO knowledge_control VALUES (?,?)',(key,encoded(timestamp())))
        event = turn['canonical_event_id']
        if event:
            exclude_sources(db,pid,[event],'qq_self_recalled',expand_turns=False)
            db.execute("UPDATE events SET text='' WHERE id=? AND session_id=? AND role='persona'",
                       (event,turn['session_id']))
        db.execute("""UPDATE local_knowledge_facts SET withdrawn=1 WHERE id IN (
            SELECT f.id FROM local_knowledge_facts f JOIN local_knowledge_documents d ON d.id=f.document_id
            WHERE d.persona_id=? AND d.session_id=? AND json_extract(f.spec_json,'$.turn_source[0]')=?)""",
                   (pid,turn['session_id'],turn['id']))
        db.execute("""UPDATE expression_speech_units SET cancelled_at=COALESCE(cancelled_at,?)
            WHERE turn_id=? AND closed_at IS NULL""", (timestamp(),turn['id']))
        db.execute("""UPDATE expression_speech_items SET state=CASE WHEN state='offered' THEN 'unknown' ELSE 'cancelled' END
            WHERE publication_id IN (SELECT p.id FROM expression_speech_publications p JOIN expression_speech_units u
                ON u.id=p.utterance_id WHERE u.turn_id=?) AND state IN ('pending','offered')""", (turn['id'],))
        db.execute("""UPDATE expression_speech_publications SET state=CASE WHEN EXISTS(
                SELECT 1 FROM expression_speech_items i WHERE i.publication_id=expression_speech_publications.id
                AND i.state='unknown') THEN 'unknown' ELSE 'closed' END,
                reason='qq_self_recalled' WHERE utterance_id IN (SELECT id FROM expression_speech_units WHERE turn_id=?)""",
                   (turn['id'],))
        db.execute("UPDATE turns SET cancelled_at=COALESCE(cancelled_at,?),status='interrupted',error_code='qq_self_recalled' WHERE id=?",
                   (timestamp(),turn['id']))
        return {'state':'withdrawn','already_applied':False}
