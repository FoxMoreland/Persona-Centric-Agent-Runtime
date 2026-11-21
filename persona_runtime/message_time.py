"""Project delivery evidence for desktop events already admitted by the caller."""
import json
import re
from datetime import datetime

VERSION = 'desktop-message-time-v1'
KEY = 'message_time_version'


def enabled(turn):
    policy = turn.get('context_policy_snapshot') or {}
    if isinstance(policy, str):
        policy = json.loads(policy)
    return turn.get('channel') == 'desktop' and policy.get(KEY) == VERSION


def stamp(value, source, receipt_id=None, raw=None):
    from .import_markdown import valid_message_date
    if not isinstance(value, str) or not valid_message_date(value):
        return None
    if datetime.fromisoformat(value).tzinfo is None:
        return None
    clock = re.search(r'[T ]\d{2}:\d{2}(?::(\d{2})(?:[.,](\d+))?)?', raw or value)
    precision = 'day'
    if clock:
        precision = 'fraction' if clock[2] else 'second' if clock[1] else 'minute'
    result = {'recorded_at': value, 'source': source, 'precision': precision}
    if receipt_id:
        result['receipt_id'] = receipt_id
    return result


def annotate_events(events, times):
    for event in events:
        entries = times.get(event['id'])
        if entries:
            event['message_times'] = wire_times(entries)


def wire_times(entries):
    """Receipt identifiers stay available to the local manifest."""
    return [{k:v for k,v in entry.items() if k not in ('receipt_id', 'fragment_id')} for entry in entries]


def event_times(db, event_ids, turn_id=None):
    """Keep canonical event identity and the position of each confirmed bubble."""
    if not event_ids:
        return {}
    event_ids = list(event_ids)
    ids = json.dumps(event_ids)
    imported_turns = {eid.split('.')[1] for eid in event_ids if eid.startswith('imported.')}
    origins = {}
    if imported_turns:
        origins = {tid:json.loads(raw) for tid,raw in db.execute(
            'SELECT id,origin_json FROM turns WHERE id IN (SELECT value FROM json_each(?))',
            (json.dumps(sorted(imported_turns)),))}
    result = {}
    texts = {}
    for row in db.execute("""SELECT e.id,e.role,e.text,e.created_at,r.id AS receipt_id,r.received_at
            FROM events e LEFT JOIN ingress_receipts r ON r.event_id=e.id AND r.channel='desktop'
            WHERE e.id IN (SELECT value FROM json_each(?))""", (ids,)):
        eid = row['id']
        if eid.startswith('imported.'):
            from .import_history import imported_time
            origin = origins.get(eid.split('.')[1], {})
            raw = origin.get('sources', {}).get(eid, {}).get('raw_time')
            entry = stamp(imported_time(origin, eid, row['created_at']), 'imported', raw=raw)
        elif row['receipt_id']:
            entry = stamp(row['received_at'], 'received', row['receipt_id'])
        else:
            entry = stamp(row['created_at'], 'event_recorded')
        result[eid] = [entry] if entry else []
        if row['role'] == 'persona' and not eid.startswith('imported.'):
            texts[eid] = row['text']
    if not texts:
        return result
    condition = "key=?" if turn_id else "key GLOB 'knowledge.search_notice.turn.*'"
    params = ('knowledge.search_notice.turn.' + turn_id, ids) if turn_id else (ids,)
    for eid, receipt in db.execute(f"""SELECT json_extract(value,'$.data.event_id'),
            json_extract(value,'$.data.notice_id') FROM knowledge_control
            WHERE {condition} AND json_extract(value,'$.data.fully_presented')=1
            AND json_extract(value,'$.data.notice_id') IS NOT NULL
            AND json_extract(value,'$.data.event_id') IN (SELECT value FROM json_each(?))""", params):
        if result.get(eid):
            result[eid][0].update(source='presented', receipt_id=receipt)
    publications = db.execute("""SELECT canonical_event_id,id,0 FROM expression_speech_publications
            WHERE canonical_event_id IN (SELECT value FROM json_each(?))
            UNION ALL SELECT canonical_event_id,id,1 FROM expression_publications
            WHERE canonical_event_id IN (SELECT value FROM json_each(?))""", (ids, ids))
    for eid, publication, legacy in publications:
        if eid not in texts:
            continue
        parts = []
        end = 0
        for row in visible_parts(db, publication, legacy=legacy):
            if not texts[eid].startswith(row['text'], end):
                break
            time = stamp(row['presented_at'], 'presented', row['receipt_id'])
            if not row['receipt_id'] or not time:
                break
            start = end
            end += len(row['text'])
            parts.append(time | {'fragment_id': row['id'], 'start': start, 'end': end})
        if parts and end == len(texts[eid]):
            result[eid] = parts
    return result


def visible_parts(db, publication, *, legacy=False):
    """Read the visible prefix; expose the receipt ID only when its binding still matches."""
    if legacy:
        query = """SELECT v.id,v.ordinal,v.text,r.id AS receipt_id,v.presented_at
            FROM expression_publications p JOIN expression_utterances v
              ON v.publication_id=p.id AND v.revision=p.revision
            LEFT JOIN expression_presentation_items i ON i.id=v.id AND i.publication_id=p.id
                AND i.revision=p.revision AND i.ordinal=v.ordinal AND i.state='presented'
            LEFT JOIN expression_presentation_receipts r ON r.id=v.receipt_id AND i.receipt_id=r.id
                AND r.utterance_id=i.id AND r.body_hash=i.content_hash AND i.text=v.text AND r.lease_id=i.lease_id
                AND r.presenter_id=i.presenter_id AND r.state='presented'
            WHERE p.id=? ORDER BY v.ordinal"""
    else:
        query = """SELECT v.fragment_id AS id,v.ordinal,
                v.text,r.id AS receipt_id,v.presented_at FROM expression_speech_publications p
            JOIN expression_speech_visible v ON v.publication_id=p.id AND v.utterance_id=p.utterance_id
            LEFT JOIN expression_speech_items i ON i.id=v.fragment_id AND i.publication_id=p.id
                AND i.revision=p.revision AND i.ordinal=v.ordinal AND i.state='presented'
            LEFT JOIN expression_speech_receipts r ON r.id=v.receipt_id AND i.receipt_id=r.id
                AND r.fragment_id=i.id AND r.body_hash=i.body_hash AND i.text=v.text AND r.lease_id=i.lease_id
                AND r.presenter_id=i.presenter_id AND r.state='presented'
            WHERE p.id=? ORDER BY v.ordinal"""
    parts = []
    for row in db.execute(query, (publication,)):
        if row['ordinal'] != len(parts):
            break
        parts.append(dict(row))
    return parts
