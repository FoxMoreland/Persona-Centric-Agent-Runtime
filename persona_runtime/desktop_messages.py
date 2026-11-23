"""Bounded, source-checked views of desktop messages and branch prefixes."""

import json

from .archive import find_turn, turn_scope, scope
from .models import LOCAL_USER_ID, RuntimeErrorBase


MESSAGE_KIND = 'desktop_message'
BRANCH_PREFIX = 'desktop.branch.v1.'
POSITION_STRIDE = 4096
MAX_POSITION = 2**53 - 1


def target(value):
    ident = value.get('id') if isinstance(value, dict) else None
    index = value.get('index', 0) if isinstance(value, dict) else None
    if (not isinstance(ident, str) or not ident.strip() or len(ident) > 200 or '\0' in ident
            or value.get('kind') != MESSAGE_KIND or set(value) - {'kind', 'id', 'index'}
            or type(index) is not int or not 0 <= index < POSITION_STRIDE):
        raise RuntimeErrorBase('invalid_reference')
    return {'kind': MESSAGE_KIND, 'id': ident, 'index': index}


def branch(db, sid):
    row = db.execute('SELECT value FROM knowledge_control WHERE key=?', (BRANCH_PREFIX + sid,)).fetchone()
    return json.loads(row[0]) if row else None


def is_desktop_session(db, pid, sid):
    return bool(db.execute('''SELECT 1 FROM session_lifecycle WHERE session_id=? AND persona_id=?
        AND user_id=? AND channel='desktop' AND bot='' AND peer='' ''', (sid, pid, LOCAL_USER_ID)).fetchone())


def segments(db, pid, sid, *, include_own=True):
    info = branch(db, sid)
    result = info['prefix'] if info and info['persona_id'] == pid else []
    if include_own:
        result = result + [{'session_id': sid, 'end': MAX_POSITION}]
    return [row for row in result if is_desktop_session(db, pid, row['session_id'])]


def legacy_reply(displayed, full_text, segments, presentation):
    from .bubble_limits import display_bubbles
    from .presentation import batch_view

    preserve = presentation.get("version") == "roleplay-body-v1"
    visible = '\n\n'.join(displayed) if displayed else batch_view(full_text, segments, historical=True, preserve=preserve)[0]
    return visible, display_bubbles(visible, presentation)


def visible_legacy_reply(db, turn, full_text):
    batch = db.execute('SELECT id FROM reply_batches WHERE turn_id=? ORDER BY created_at DESC LIMIT 1',
                       (turn['id'],)).fetchone()
    segments = [dict(r) for r in db.execute('SELECT * FROM reply_segments WHERE batch_id=? ORDER BY segment_index',
                                           (batch[0],))] if batch else []
    displayed = [r[0] for r in db.execute('SELECT text FROM reply_display_messages WHERE batch_id=? ORDER BY display_index',
                                         (batch[0],))] if batch else []
    row = db.execute('SELECT details_json FROM context_uses WHERE turn_id=? ORDER BY rowid DESC LIMIT 1',
                     (turn['id'],)).fetchone()
    presentation = (json.loads(row[0]).get('presentation') or {}) if row else {}
    return legacy_reply(displayed, full_text, segments, presentation)


def event_messages(db, pid, event):
    """A persisted input or displayed output, expanded into its actual bubbles."""
    if event['role'] not in ('user', 'persona') or not event['text'].strip():
        return []
    event_id = event['id']
    if db.execute('SELECT 1 FROM source_exclusions WHERE event_id=?', (event_id,)).fetchone():
        return []
    turn = find_turn(db, 'event', event_id)
    if not turn or turn['status'] != 'completed' or turn['cancelled_at'] or turn_scope(db, turn) != scope(pid, 'desktop'):
        return []
    from .knowledge_service import turn_usable
    if not turn_usable(db, turn['id']):
        return []
    from .import_history import history_allowed, source_time
    if event_id.startswith('imported.'):
        if not history_allowed(db, event['session_id'], 'dialogue'):
            return []
        parts = [(event_id, event['text'], source_time(db, event_id), 0)]
    else:
        speech = db.execute('''SELECT v.fragment_id,v.text,v.presented_at,v.ordinal
            FROM expression_speech_units u JOIN expression_speech_publications p ON p.utterance_id=u.id
            JOIN expression_speech_visible v ON v.publication_id=p.id
            WHERE u.turn_id=? AND p.canonical_event_id=? ORDER BY v.ordinal''', (turn['id'], event_id)).fetchall()
        utterances = db.execute('''SELECT u.id,u.text,u.presented_at,u.ordinal FROM expression_publications p
            JOIN expression_utterances u ON u.publication_id=p.id
            WHERE p.turn_id=? AND p.canonical_event_id=? ORDER BY u.ordinal''', (turn['id'], event_id)).fetchall() if not speech else []
        if speech or utterances:
            parts = [tuple(row) for row in (speech or utterances)]
        elif event_id == turn['reply_event_id']:
            try:
                visible, bubbles = visible_legacy_reply(db, turn, event['text'])
            except RuntimeErrorBase:
                return []
            parts = [(event_id, text, event['created_at'], index) for index, text in enumerate(bubbles or [visible])]
        else:
            if event['role'] == 'persona':
                from .tk_notice import already_presented
                if not already_presented(db, turn['id']):
                    return []
            parts = [(event_id, event['text'], event['created_at'], 0)]
    rows = []
    for ident, text, recorded, index in parts:
        position = event['seq'] * POSITION_STRIDE + index
        if position >= MAX_POSITION or index >= POSITION_STRIDE or not text.strip():
            continue
        key = ident if ident != event_id or index == 0 else f'{event_id}/{index}'
        rows.append({'id': key, 'event_id': event_id, 'session_id': event['session_id'], 'position': position,
                     'target': {'kind': MESSAGE_KIND, 'id': ident, 'index': index if ident == event_id else 0},
                     'text': text, 'time': recorded, 'role': 'user' if event['role'] == 'user' else 'assistant'})
    return rows


def resolve(db, pid, reference):
    reference = target(reference)
    ident = reference['id']
    event = db.execute('SELECT * FROM events WHERE id=? AND persona_id=? AND user_id=?',
                       (ident, pid, LOCAL_USER_ID)).fetchone()
    if not event:
        event = db.execute('''SELECT e.* FROM expression_speech_visible v
            JOIN expression_speech_publications p ON p.id=v.publication_id JOIN events e ON e.id=p.canonical_event_id
            WHERE v.fragment_id=? AND e.persona_id=? AND e.user_id=? UNION ALL
            SELECT e.* FROM expression_utterances v JOIN expression_publications p ON p.id=v.publication_id
                JOIN events e ON e.id=p.canonical_event_id WHERE v.id=? AND e.persona_id=? AND e.user_id=?''',
                           (ident, pid, LOCAL_USER_ID, ident, pid, LOCAL_USER_ID)).fetchone()
    if event:
        return next((row for row in event_messages(db, pid, event) if row['target'] == reference), None)
    return None


def can_read(parts, message):
    return any(part['session_id'] == message['session_id'] and message['position'] <= part['end'] for part in parts)


def read_range(db, pid, part, *, limit, before=None, after=None, query=None):
    """An indexed range in one physical session, with message-granular cursors."""
    forward = after is not None
    bound = after if forward else min(part['end'] + 1, before if before is not None else MAX_POSITION)
    relation, direction = ('>=', 'ASC') if forward else ('<=', 'DESC')
    filled = []
    edge = bound
    scanned = 0
    max_scan = 1000 if query else max(128, limit * 4)
    last_position = None
    more = True
    while scanned < max_scan and len(filled) < limit:
        batch = db.execute(f'''SELECT * FROM events WHERE session_id=? AND persona_id=? AND user_id=?
            AND role IN ('user','persona')
            AND seq {relation} ? AND seq<=? ORDER BY seq {direction} LIMIT ?''',
                           (part['session_id'], pid, LOCAL_USER_ID, edge // POSITION_STRIDE,
                            part['end'] // POSITION_STRIDE, min(64, max_scan - scanned))).fetchall()
        if not batch:
            more = False
            break
        for event in batch:
            scanned += 1
            messages = event_messages(db, pid, event)
            # Cursor must also advance past local rows that are ineligible to quote.
            last_position = event['seq'] * POSITION_STRIDE + (POSITION_STRIDE - 1 if forward else 0)
            for msg in messages if forward else reversed(messages):
                pos = msg['position']
                if pos > part['end'] or (pos <= bound if forward else pos >= bound):
                    continue
                if query and query.casefold() not in msg['text'].casefold():
                    continue
                filled.append(msg)
                if len(filled) == limit:
                    return filled, pos
        edge = (batch[-1]['seq'] + (1 if forward else -1)) * POSITION_STRIDE
        if edge < 0 or edge > MAX_POSITION:
            more = False
            break
    return filled, last_position if more else None


def recent(db, pid, parts, limit, *, before=None):
    result = []
    for part in reversed(parts):
        rows, _ = read_range(db, pid, part, limit=limit-len(result), before=before)
        result.extend(rows)
        if len(result) == limit:
            break
    return list(reversed(result))


def branch_page(db, pid, sid, *, before=None, after=None, limit=80, query=None):
    pieces = segments(db, pid, sid, include_own=False)
    rows, cursor = [], None
    forward = after is not None
    for part in pieces if forward else reversed(pieces):
        if forward and part['end'] <= after:
            continue
        batch, cursor = read_range(db, pid, part, limit=limit-len(rows), before=before, after=after, query=query)
        rows.extend(batch)
        if cursor is not None:
            break
    if not forward:
        rows.reverse()
    from .message_time import stamp
    values = [{'id': r['id'], 'text': r['text'], 'seq': r['position'], 'role': 'user' if r['role'] == 'user' else 'persona',
               'created_at': r['time'], 'timestamp': stamp(r['time'], 'event_recorded'),
               'referenceable': True, 'message_target': r['target']} for r in rows]
    return {'rows': values, 'after': cursor, 'before': values[0]['seq'] if values else None} if forward else {
        'rows': values, 'before': cursor}
