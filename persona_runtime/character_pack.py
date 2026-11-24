"""Local file handles and snapshot export for character packages."""

import json
import os
import re
from collections import Counter, defaultdict
from itertools import groupby
from pathlib import Path
from tempfile import NamedTemporaryFile
from time import time

from . import character_pack_format as fmt
from .archive import encoded, find_turn, fingerprint, scope
from .models import LOCAL_USER_ID, PersonaSpec, RuntimeErrorBase, identifier
from .private_personas import PrivatePersonas, safe
from .service import RuntimeService


HANDLE = re.compile(r'[0-9a-f-]{36}')
LIFETIME = 60 * 60
OP_PREFIX = 'character-pack:'


def stage_directory(paths):
    from .character_embedding import private_directory
    directory = paths.home / 'private' / 'character-packs'
    safe(directory, paths.home)
    if not directory.is_dir():
        private_directory(paths.home, directory)
    return directory


def stage_path(paths, handle):
    if not isinstance(handle, str) or HANDLE.fullmatch(handle) is None:
        raise RuntimeErrorBase('invalid_character_package_handle')
    directory = stage_directory(paths)
    return safe(directory / (handle + fmt.SUFFIX), directory)


def clean(paths):
    cutoff = time() - LIFETIME
    for path in stage_directory(paths).glob('*' + fmt.SUFFIX):
        if not HANDLE.fullmatch(path.stem):
            continue
        path = stage_path(paths, path.stem)
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink()
        except (FileNotFoundError, PermissionError):
            pass


def copy_file(source, output, maximum=fmt.MAX_PACK):
    size = 0
    with Path(source).open('rb') as stream:
        while chunk := stream.read(256 * 1024):
            size += len(chunk)
            if size > maximum:
                raise RuntimeErrorBase('character_history_package_too_large')
            output.write(chunk)


def stage_input(paths, source):
    clean(paths)
    handle = identifier()
    target = stage_path(paths, handle)
    try:
        with target.open('xb') as output:
            copy_file(source, output)
    except BaseException:
        target.unlink(missing_ok=True)
        raise
    return handle


def save_selected(source, destination):
    destination = Path(destination)
    with NamedTemporaryFile(dir=destination.parent, prefix='.pcar-', delete=False) as output:
        temporary = Path(output.name)
        try:
            copy_file(source, output)
            output.flush()
            os.fsync(output.fileno())
            output.close()
            os.replace(temporary, destination)
        finally:
            if not output.closed:
                output.close()
            temporary.unlink(missing_ok=True)


def _desktop_sessions(db, pid):
    from .workbench_timeline import DESKTOP, SESSION_TITLES
    return {row['id']: dict(row) for row in db.execute(f"""SELECT s.id,s.created_at,
        coalesce(json_extract(n.value,'$'),'') AS title FROM sessions s
        LEFT JOIN session_lifecycle l ON l.session_id=s.id
        LEFT JOIN knowledge_control n ON n.key=?||s.id
        WHERE {DESKTOP} ORDER BY s.created_at,s.rowid""", (SESSION_TITLES, pid, LOCAL_USER_ID))}


def _check_export(store, db, pid):
    RuntimeService._persona(db, pid)
    if PrivatePersonas(store.paths).protected(pid):
        raise RuntimeErrorBase('character_history_package_protected')


class Projection:
    def __init__(self, db, rows, pid, selected, available):
        self.db, self.rows, self.pid = db, rows, pid
        self.selected, self.available = selected, available
        self.aliases = defaultdict(lambda: defaultdict(list))
        self.targets = {}
        self.quotes = {}
        self.origins = {}
        self.turns = defaultdict(list)
        self.dependencies = Counter()
        self.omitted = Counter()
        self.next_turn = 0
        self.next_message = 0

    def collect(self):
        from .character import head as character
        from .profile import head, source_blocked
        spec = PersonaSpec.from_dict(RuntimeService._persona(self.db, self.pid)['spec'])
        relation = head(self.db, self.pid, 'relationship')
        self.rows.character = {
            'persona_spec': spec.to_dict(), 'character': character(self.db, self.pid)['data'],
            'relationship': '' if source_blocked(self.db, relation['source_ids']) else relation['data']['description'],
        }
        fmt.character(self.rows.character)
        for sid in self.selected:
            self.rows.add('sessions', self.available[sid])
            self.history(sid)
        self.attach_quotes()
        self.memories()
        self.summaries()

    def history(self, destination):
        from .desktop_messages import POSITION_STRIDE, segments
        from .import_history import META
        from .scene_policy import read
        from .sessions import blockers
        prior = None
        for section in segments(self.db, self.pid, destination, include_own=False) + [
                {'session_id': destination, 'end': 2**53 - 1}]:
            sid, end = section['session_id'], section['end']
            if sid == destination and blockers(self.db, sid):
                raise RuntimeErrorBase('character_history_package_session_busy')
            meta = read(self.db, META + sid) or {}
            events = self.db.execute("""SELECT e.*,x.reason AS exclusion FROM events e
                LEFT JOIN source_exclusions x ON x.event_id=e.id
                WHERE e.session_id=? AND e.persona_id=? AND e.user_id=? AND e.seq<=? ORDER BY e.seq""",
                                     (sid, self.pid, LOCAL_USER_ID, end // POSITION_STRIDE))
            for event in events:
                source_turn = find_turn(self.db, 'event', event['id'])
                messages = self.project_event(dict(event), source_turn, meta, end, sid == destination)
                if not messages:
                    continue
                source_id = source_turn['id'] if source_turn else event['id']
                if (sid, source_id) != prior:
                    prior = (sid, source_id)
                    self.next_turn += 1
                    turn_id = 't' + str(self.next_turn)
                    source = source_turn or {'created_at': event['created_at'], 'updated_at': event['created_at']}
                    self.rows.add('turns', {'id': turn_id, 'session_id': destination,
                                           **{k: source[k] for k in ('created_at', 'updated_at')}})
                    self.turns[(destination, source_id)].append(turn_id)
                    coverage = self.db.execute('SELECT disposition,summary_state FROM memory_coverage WHERE turn_id=?',
                                               (source_id,)).fetchone()
                    states = dict(coverage) if coverage else {'disposition': 'unprocessed', 'summary_state': 'pending'}
                    if states['disposition'] not in fmt.DISPOSITIONS or states['summary_state'] not in fmt.SUMMARY_STATES:
                        states = {'disposition': 'unprocessed', 'summary_state': 'pending'}
                    self.rows.add('coverage', {'id': turn_id, **states})
                for target, msg in messages:
                    self.next_message += 1
                    key = 'e' + str(self.next_message)
                    self.rows.add('messages', dict(msg, id=key, turn_id=turn_id, quote=None))
                    self.aliases[event['id']][destination].append(key)
                    if target:
                        if target[0] != event['id']:
                            self.aliases[target[0]][destination].append(key)
                        self.targets[(destination, *target)] = key
                        self.targets.setdefault((destination, event['id'], 0), key)
                    if msg['role'] == 'user':
                        reference = self.db.execute('SELECT * FROM input_references WHERE event_id=?',
                                                    (event['id'],)).fetchone()
                        portable = self.origins.get(source_id, {}).get('sources', {}).get(event['id'], {}).get('quote')
                        if reference or portable:
                            self.quotes[key] = (destination, dict(reference) if reference else None, portable)

    def project_event(self, event, turn, meta, end, own):
        from .desktop_messages import POSITION_STRIDE, event_messages
        from .import_history import PREFIX
        ident = event['id']
        blocked = event['exclusion'] or ('history_withdrawn' if ident.startswith(PREFIX) and meta.get('withdrawn') else None)
        if blocked:
            # Keep the identity needed by obsolete revisions, without their withdrawn text.
            return [(None, {'role': 'note', 'text': '', 'time': event['created_at'], 'speaker': '',
                            'raw_time': None, 'excluded': 'memory_revised' if blocked == 'memory_revised' else 'history_withdrawn'})] if own else []
        if ident.startswith(PREFIX) and turn:
            if turn['id'] not in self.origins:
                self.origins[turn['id']] = json.loads(turn['origin_json'])
            detail = self.origins[turn['id']].get('sources', {}).get(ident, {})
            # A portable transcript carries the normalized visible speech, not the raw input file.
            if not event['text'].strip():
                return []
            return [((ident, 0), {'role': event['role'], 'text': event['text'],
                                  'time': detail.get('time', event['created_at']), 'speaker': detail.get('speaker', ''),
                                  'raw_time': detail.get('raw_time'), 'excluded': None})]
        if turn:
            visible = [row for row in event_messages(self.db, self.pid, event) if row['position'] <= end]
            if visible:
                return [((row['target']['id'], row['target']['index']),
                         {'role': event['role'], 'text': row['text'], 'time': row['time'],
                          'speaker': '', 'raw_time': None, 'excluded': None}) for row in visible]
            # Retain a terminal user input even when that turn produced no visible reply.
            if not (own and event['role'] == 'user' and turn['status'] in ('failed', 'interrupted')):
                return []
        elif not own or self.db.execute("""SELECT 1 FROM raw_events WHERE event_id=? UNION ALL
                SELECT 1 FROM input_batch_members WHERE event_id=?""", (ident, ident)).fetchone():
            return []
        if event['seq'] * POSITION_STRIDE > end or not event['text'].strip():
            return []
        return [((ident, 0), {'role': event['role'], 'text': event['text'], 'time': event['created_at'],
                              'speaker': '', 'raw_time': None, 'excluded': None})]

    def attach_quotes(self):
        from .desktop_messages import MESSAGE_KIND, resolve
        for key, (sid, row, portable) in self.quotes.items():
            if portable:
                target = portable['id']
                excerpt = portable['text']
                archived = self.db.execute('SELECT 1 FROM source_exclusions WHERE event_id=?',
                                           (target,)).fetchone() if target else None
                if archived:
                    continue
                destination = self.targets.get((sid, target, 0))
            else:
                target = json.loads(row['target_json'])
                bound = json.loads(row['binding_json'])
                if bound.get('status') != 'found' or target.get('kind') != MESSAGE_KIND:
                    continue
                item = resolve(self.db, self.pid, target)
                if not item or fingerprint(item) != (bound.get('anchor') or {}).get('hash'):
                    continue
                destination = self.targets.get((sid, target['id'], target.get('index', 0)))
                excerpt = item['text'][:4000]
                if destination is None and item['session_id'] in self.selected:
                    destination = self.targets.get((item['session_id'], target['id'], target.get('index', 0)))
            if destination == key:
                continue
            message = self.rows.get('messages', key)
            message['quote'] = {'id': destination, 'text': excerpt}
            self.rows.set('messages', message)

    def evidence(self, ident, quote, preferred=None):
        options = self.aliases.get(ident, {})
        origin = self.db.execute('SELECT session_id FROM events WHERE id=?', (ident,)).fetchone()
        destination = preferred if preferred in options else origin[0] if origin and origin[0] in options else next(iter(options), None)
        if destination is None or preferred is not None and destination != preferred:
            return None, []
        rows = [self.rows.get('messages', key) for key in options[destination]]
        usable = [row for row in rows if not row['excluded']]
        if not quote:
            return destination, [(row['id'], '') for row in (usable or rows)[:1]]
        for row in usable:
            if quote in row['text']:
                return destination, [(row['id'], quote)]
        # Stored source quotations can span two visible reply bubbles.
        # Use only contiguous, verbatim fragments from the displayed bubbles.
        spans, texts, length = [], [], 0
        for row in usable:
            text = row['text']
            positions = [i for i, char in enumerate(text) if not char.isspace()]
            spans.append((length, length + len(positions), row, positions))
            texts.append(''.join(text[i] for i in positions))
            length += len(positions)
        needle = ''.join(quote.split())
        start = ''.join(texts).find(needle)
        if not needle or start < 0:
            return destination, []
        stop = start + len(needle)
        return destination, [(row['id'], row['text'][positions[max(0, start - left)]:positions[min(right, stop) - left - 1] + 1])
                             for left, right, row, positions in spans if left < stop and right > start]

    def memories(self):
        selected, available = set(self.selected), self.available
        quarantined = set()
        for row in self.db.execute("SELECT data_json FROM memory_decisions WHERE persona_id=? AND state='quarantined'", (self.pid,)):
            quarantined.update(json.loads(row[0]).get('blocked_memory_ids', ()))
        accepted = {}
        revisions = self.db.execute("""SELECT m.*, f.subject,f.attribute,f.valid_from,f.valid_until,f.batch_id,
            f.data_json,f.scope_json FROM memories m LEFT JOIN memory_facts f ON f.memory_id=m.id
            WHERE m.persona_id=? AND m.user_id=? ORDER BY m.root_id,m.revision""", (self.pid, LOCAL_USER_ID))

        def selected_revisions():
            for _, chain in groupby(revisions, key=lambda row: row['root_id']):
                evidence = []
                for memory in chain:
                    sources = {row['event_id']: row['quote'] for row in self.db.execute(
                        'SELECT event_id,quote FROM memory_sources WHERE memory_id=? ORDER BY event_id', (memory['id'],))}
                    sources.setdefault(memory['source_event_id'], '')
                    evidence.append((memory, sources))
                if any(eid in self.aliases for _, sources in evidence for eid in sources):
                    yield from evidence

        for memory, sources in selected_revisions():
            event_ids = set(sources)
            missing = set()
            for event_id in event_ids:
                aliases = self.aliases.get(event_id, {})
                if aliases:
                    continue
                row = self.db.execute('SELECT session_id FROM events WHERE id=? AND persona_id=? AND user_id=?',
                                      (event_id, self.pid, LOCAL_USER_ID)).fetchone()
                missing.add(row[0] if row else None)
            for sid in missing & (set(available) - selected):
                self.dependencies[sid] += 1
            if missing & (set(available) - selected):
                self.omitted['unselected_source'] += 1
                continue
            if missing or memory['scope_json'] and json.loads(memory['scope_json']) != scope(self.pid, 'desktop'):
                self.omitted['unavailable_source'] += 1
                continue
            previous = memory['previous_id']
            if previous and previous not in accepted:
                self.omitted['unavailable_revision'] += 1
                continue
            detail = json.loads(memory['data_json'] or '{}')
            fact = None if memory['subject'] is None else {
                **{k: memory[k] for k in ('subject', 'attribute', 'valid_from', 'valid_until')},
                'automatic': memory['batch_id'] is not None, 'manual': bool(detail.get('manual_authority')),
                'blocked': bool(detail.get('import_review_required')) or memory['id'] in quarantined,
            }
            redacted = (memory['status'] == 'retracted' or detail.get('import_review_required')
                        or any(self.rows.get('messages', key)['excluded'] == 'history_withdrawn'
                               for eid in event_ids for keys in self.aliases[eid].values() for key in keys))
            evidence, locations, primary = {}, set(), None
            # Prefer the physical primary session and its source IDs; a selected branch
            # can provide an alias for a prefix that it actually displays.
            for eid, quote in sources.items():
                sid, points = self.evidence(eid, '' if redacted else quote)
                if not points:
                    _, base = self.evidence(eid, '')
                    if not base or memory['status'] == 'active':
                        self.omitted['unavailable_source'] += 1
                        break
                    points = base
                locations.add(sid)
                if eid == memory['source_event_id']:
                    primary = points[0][0]
                if not redacted:
                    evidence.update((key, text) for key, text in points if text)
            else:
                # A retracted record retains its revision linkage and just the primary origin.
                if redacted:
                    locations = {self.rows.get('turns', self.rows.get('messages', primary)['turn_id'])['session_id']}
                    if fact:
                        fact.update(subject='', attribute='')
                if memory['status'] == 'active' and self.rows.get('messages', primary)['excluded'] and not redacted:
                    self.omitted['unavailable_source'] += 1
                    continue
                key = 'm' + str(len(accepted) + 1)
                self.rows.add('memories', {
                    'id': key, 'root_id': accepted[memory['root_id']] if previous else key,
                    'previous_id': accepted[previous] if previous else None, 'event_id': primary,
                    **{k: memory[k] for k in ('category', 'revision', 'created_at', 'updated_at')},
                    'status': 'retracted' if redacted else memory['status'], 'text': '' if redacted else memory['text'],
                    'fact': fact, 'sessions': [sid for sid in self.selected if sid in locations],
                    'sources': [{'event_id': eid, 'quote': text} for eid, text in evidence.items()],
                })
                accepted[memory['id']] = key

    def summaries(self):
        from .organization import latest_summary
        for sid in self.selected:
            summary = latest_summary(self.db, sid)
            if summary:
                self.add_summary(sid, summary, 'automatic_summary' if summary['reason'] == 'automatic_memory' else 'summary',
                                 json.loads(summary['items_json']))
            for row in self.db.execute("SELECT * FROM memory_fragments WHERE session_id=? AND state='active' ORDER BY rowid", (sid,)):
                data = json.loads(row['data_json'])
                self.add_summary(sid, dict(row, revision=1), 'fragment', data['items'])

    def add_summary(self, sid, row, kind, items):
        kept = []
        for item in items:
            evidence = {}
            for source in item['sources']:
                eid = source.get('source_id') or source.get('event_id')
                _, points = self.evidence(eid, source['quote'], sid)
                if not points:
                    self.omitted['unavailable_summary_source'] += 1
                    break
                evidence.update(points)
            else:
                if evidence:
                    kept.append({'text': item['text'], 'sources': [{'event_id': key, 'quote': quote}
                                                                 for key, quote in evidence.items()]})
        if kept:
            self.rows.add('summaries', {'id': row['id'], 'session_id': sid, 'kind': kind,
                                        'revision': row['revision'], 'created_at': row['created_at'], 'items': kept})


class CharacterPacks:
    def __init__(self, store):
        self.store = store
        self.paths = store.paths

    def sessions(self, persona_id):
        with self.store.connection() as db:
            _check_export(self.store, db, persona_id)
            return list(_desktop_sessions(db, persona_id).values())

    def export(self, persona_id, session_ids, main_session, expected=None):
        fmt.ident(persona_id)
        fmt.ident(main_session)
        if not isinstance(session_ids, list) or not 1 <= len(session_ids) <= 512:
            raise RuntimeErrorBase('invalid_character_package_sessions')
        for sid in session_ids:
            fmt.ident(sid)
        if main_session not in session_ids or len(set(session_ids)) != len(session_ids):
            raise RuntimeErrorBase('invalid_character_package_sessions')
        clean(self.paths)
        handle = identifier()
        path = stage_path(self.paths, handle)
        keep = False
        try:
            with fmt.Rows(path.parent) as rows:
                with self.store.connection() as db:
                    _check_export(self.store, db, persona_id)
                    available = _desktop_sessions(db, persona_id)
                    if not set(session_ids).issubset(available):
                        raise RuntimeErrorBase('invalid_character_package_sessions')
                    projection = Projection(db, rows, persona_id, session_ids, available)
                    projection.collect()
                manifest = fmt.write_pack(path, rows, main_session)
                result = fmt.preview(rows, manifest, path.stat().st_size)
                result['dependencies'] = [dict(available[sid], memories=count)
                                          for sid, count in sorted(projection.dependencies.items())]
                result['omitted'] = dict(projection.omitted)
                result['digest'] = fingerprint([result['digest'], result['dependencies'], result['omitted']])
            if expected is None:
                return result
            if result['digest'] != expected:
                raise RuntimeErrorBase('character_history_package_changed')
            keep = True
            return result | {'handle': handle}
        finally:
            if not keep:
                path.unlink(missing_ok=True)

    def preview(self, handle):
        path = stage_path(self.paths, handle)
        with fmt.Rows(path.parent) as rows:
            manifest = fmt.read_pack(path, rows)
            return fmt.preview(rows, manifest, path.stat().st_size) | {'card': rows.character}

    def restore(self, handle, digest, operation_id, card=None):
        from .character_pack_restore import restore
        fmt.ident(operation_id)
        fmt.ident(digest)
        operation = OP_PREFIX + operation_id
        request = digest
        if card is not None:
            fmt.character(card)
            request = fingerprint([digest, card])
        with self.store.connection() as db:
            previous = completed(db, operation, request)
        if previous:
            return previous
        path = stage_path(self.paths, handle)
        with fmt.Rows(path.parent) as rows:
            manifest = fmt.read_pack(path, rows)
            if fingerprint(manifest) != digest:
                raise RuntimeErrorBase('character_history_package_changed')
            if card is not None:
                rows.character = card
            with self.store.connection(write=True) as db:
                previous = completed(db, operation, request)
                if previous:
                    return previous
                result = restore(db, rows, manifest['main_session'])
                from .models import timestamp
                db.execute('INSERT INTO memory_operations VALUES (?,?,?,?,?,?)',
                           (operation, result['id'], LOCAL_USER_ID, request, encoded(result), timestamp()))
        callback = getattr(self.store, 'character_committed', None)
        if callback:
            callback(result['id'])
        return result


def completed(db, operation, digest):
    row = db.execute('SELECT fingerprint,result_json FROM memory_operations WHERE id=?', (operation,)).fetchone()
    if row:
        if row['fingerprint'] != digest:
            raise RuntimeErrorBase('submission_conflict')
        return json.loads(row['result_json'])
    return None
