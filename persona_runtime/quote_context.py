"""Freeze a desktop quote window at acceptance, using the existing reference receipt."""

import json

from . import desktop_messages as messages
from .archive import encoded, fingerprint, serialize_reference_block
from .models import RuntimeErrorBase


VERSION = 2
NEIGHBORS = 20
LATEST = 10


def is_quote(target):
    return isinstance(target, dict) and target.get('kind') == messages.MESSAGE_KIND


def in_batch(db, batch_id):
    return bool(batch_id and db.execute('''SELECT 1 FROM input_references r
        JOIN input_batch_members m ON m.event_id=r.event_id WHERE m.batch_id=? AND r.parser_version=?''',
                                        (batch_id, VERSION)).fetchone())


def freeze(db, pid, sid, accepted_event, target, *, max_input_chars=None):
    parts = messages.segments(db, pid, sid)
    anchor = messages.resolve(db, pid, target)
    part = next((p for p in parts if p['session_id'] == anchor['session_id']
                 and anchor['position'] <= p['end']), None) if anchor else None
    if not part:
        raise RuntimeErrorBase('reference_unavailable')
    accepted = db.execute('SELECT seq,length(text) FROM events WHERE id=?', (accepted_event,)).fetchone()
    if max_input_chars is not None and len(anchor['text']) + accepted[1] > max_input_chars:
        raise RuntimeErrorBase('reference_budget_exceeded')
    before, _ = messages.read_range(db, pid, part, limit=NEIGHBORS, before=anchor['position'])
    after, _ = messages.read_range(db, pid, part, limit=NEIGHBORS, after=anchor['position'])
    edge = accepted[0] * messages.POSITION_STRIDE
    latest = messages.recent(db, pid, parts, LATEST, before=edge)
    seen = {m['id'] for m in [anchor, *before, *after]}

    def pin(row):
        return {'target': row['target'], 'hash': fingerprint(row)}

    return {'status': 'found', 'target': target, 'session': sid, 'anchor': pin(anchor),
            'before': [pin(m) for m in reversed(before)], 'after': [pin(m) for m in after],
            'latest': [pin(m) for m in latest if m['id'] not in seen]}


def read(db, turn):
    rows = db.execute('''SELECT r.binding_json,r.event_id FROM input_references r
        JOIN input_batch_members m ON m.event_id=r.event_id WHERE m.batch_id=? AND r.parser_version=?''',
                      (turn.get('input_batch_id'), VERSION)).fetchall()
    if not rows:
        return None
    if len(rows) != 1:
        raise RuntimeErrorBase('multiple_quote_references')
    owner = turn['persona_id']
    binding = json.loads(rows[0]['binding_json'])
    if binding['session'] != turn['session_id']:
        raise RuntimeErrorBase('reference_source_changed')
    parts = messages.segments(db, owner, turn['session_id'])

    def live(saved):
        item = messages.resolve(db, owner, saved['target'])
        if not item or fingerprint(item) != saved['hash'] or not messages.can_read(parts, item):
            raise RuntimeErrorBase('reference_source_changed')
        return item

    return {'anchor': live(binding['anchor']), 'input_event_id': rows[0]['event_id'],
            **{key: [live(item) for item in binding[key]] for key in ('before', 'after', 'latest')}}


def projection(message):
    return {key: message[key] for key in ('id', 'role', 'text', 'time')}


def neighborhood(window):
    return window['before'] + window['after']


def content(window):
    focus = window['anchor']
    return serialize_reference_block([{
        'anchor': projection(focus), 'before': [projection(m) for m in window['before']],
        'after': [projection(m) for m in window['after']], 'latest': [projection(m) for m in window['latest']],
    }], [{'input_event_id': window['input_event_id'], 'target': focus['target']}])


def manifest(window):
    focus = window['anchor']
    sources = [focus, *neighborhood(window), *window['latest']]
    imported = {m['event_id'].split('.')[1] for m in sources if m['event_id'].startswith('imported.')}
    return {'references': [{'input_event_id': window['input_event_id'], 'target': focus['target'], 'status': 'found'}],
            'turn_ids': sorted(imported), 'reference_characters': len(content(window)),
            'quote_window': {'anchor': focus['id'], **{key: [m['id'] for m in window[key]]
                                                       for key in ('before', 'after', 'latest')}}}


def branch_prefix(db, turn):
    if turn['channel'] != 'desktop':
        return []
    pid, sid = turn['persona_id'], turn['session_id']
    return messages.recent(db, pid, messages.segments(db, pid, sid, include_own=False), NEIGHBORS)


def branch_block(rows):
    return serialize_reference_block([{'messages': [projection(row) for row in rows]}], []) if rows else ''


def trim(window):
    """Remove the farthest neighbor first, and preserve the anchor itself."""
    before, after, latest = (window[key] for key in ('before', 'after', 'latest'))
    if before and len(before) >= len(after):
        before.pop(0)
    elif after:
        after.pop()
    elif latest:
        latest.pop(0)
    else:
        return False
    return True


def fit_packet(packet, window, system, limit):
    """Fit the actual approved writer packet, retaining the quote as the first evidence item."""
    fitted = {**window, **{key: list(window[key]) for key in ('before', 'after', 'latest')}}
    anchor = fitted['anchor']
    occupied = {m['id'] for m in [anchor, *neighborhood(fitted), *fitted['latest']]}
    preserved = [m for m in packet['evidence'] if m['id'] not in occupied]
    while True:
        receipt = manifest(fitted)['quote_window']
        packet = {**packet, 'dialogue': [projection(m) for m in neighborhood(fitted) + fitted['latest']],
                  'evidence': [projection(anchor), *preserved], 'quote_window': receipt}
        if len(system) + len(encoded(packet)) <= limit:
            return packet, receipt
        if not trim(fitted):
            if preserved:
                preserved.pop()
            else:
                raise RuntimeErrorBase('reference_budget_exceeded')
