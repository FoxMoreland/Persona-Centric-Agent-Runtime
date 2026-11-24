"""Bounded, versioned interchange for a role and its selected desktop history."""

import hashlib
import json
import sqlite3
from contextlib import closing
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from zipfile import BadZipFile, ZIP_DEFLATED, ZIP_STORED, ZipFile
from zlib import error as CompressionError

from .archive import encoded, fingerprint
from .character import validate_character
from .models import CATEGORIES, PersonaSpec, RuntimeErrorBase


FORMAT = 'pcar_character_v2'
SUFFIX = '.pcarpack'
MAX_PACK = 64 * 1024**2
MAX_DATA = 256 * 1024**2
MAX_ROW = 1024**2
SECTIONS = ('sessions', 'turns', 'messages', 'memories', 'summaries', 'coverage')
FILES = ('character.json', *(key + '.jsonl' for key in SECTIONS))
FINISHED = frozenset(('processed', 'processed_with_quarantine', 'no_memory', 'revoked'))
DISPOSITIONS = FINISHED | {'unprocessed', 'stale', 'pending', 'failed', 'input_too_long',
                          'blocked_input', 'blocked_authorization', 'history_paused', 'cancelled',
                          'blocked_unknown', 'cancelled_unknown', 'unknown', 'interrupted'}
UNKNOWN = frozenset(('blocked_unknown', 'cancelled_unknown', 'unknown', 'interrupted'))
SUMMARY_STATES = frozenset(('available', 'no_summary', 'pending', 'invalid', 'unavailable', 'disabled'))


def check(value):
    if not value:
        raise RuntimeErrorBase('invalid_character_history_package')


def fields(row, names):
    check(isinstance(row, dict) and set(row) == set(names.split()))


def string(value, size, *, empty=False):
    check(isinstance(value, str) and len(value) <= size and '\0' not in value
          and (empty or bool(value.strip())))


def ident(value):
    string(value, 200)


def date(value, *, optional=False):
    if optional and value is None:
        return
    string(value, 64)
    try:
        check(datetime.fromisoformat(value).utcoffset() is not None)
    except (ValueError, OverflowError):
        check(False)


def unique(pairs):
    result = dict(pairs)
    check(len(result) == len(pairs))
    return result


def reject_constant(_value):
    check(False)


def decode(raw):
    return json.loads(raw.decode('utf-8'), object_pairs_hook=unique,
                      parse_constant=reject_constant)


class Rows:
    """Disk-backed validation and lookup of rows, without retaining the chat in memory."""

    def __init__(self, directory):
        self.directory = directory

    def __enter__(self):
        self.temp = TemporaryDirectory(prefix='rows-', dir=self.directory)
        try:
            self.db = sqlite3.connect(Path(self.temp.name) / 'rows.sqlite3')
            self.db.executescript('''CREATE TABLE rows (
                section TEXT NOT NULL, id TEXT NOT NULL, parent TEXT, payload TEXT NOT NULL,
                UNIQUE(section,id));
                CREATE INDEX row_parent ON rows(section,parent);''')
        except BaseException:
            if hasattr(self, 'db'):
                self.db.close()
            self.temp.cleanup()
            raise
        return self

    def __exit__(self, *_error):
        self.db.close()
        self.temp.cleanup()

    def add(self, section, row):
        parent_key = {'turns': 'session_id', 'messages': 'turn_id', 'summaries': 'session_id'}.get(section)
        self.db.execute('INSERT INTO rows VALUES (?,?,?,?)',
                        (section, row['id'], row.get(parent_key), encoded(row)))

    def set(self, section, row):
        self.db.execute('UPDATE rows SET payload=? WHERE section=? AND id=?',
                        (encoded(row), section, row['id']))

    def get(self, section, key):
        row = self.db.execute('SELECT payload FROM rows WHERE section=? AND id=?', (section, key)).fetchone()
        return json.loads(row[0]) if row else None

    def items(self, section, parent=None):
        if parent is None:
            cursor = self.db.execute('SELECT payload FROM rows WHERE section=? ORDER BY rowid', (section,))
        else:
            cursor = self.db.execute('SELECT payload FROM rows WHERE section=? AND parent=? ORDER BY rowid',
                                     (section, parent))
        with closing(cursor):
            for row in cursor:
                yield json.loads(row[0])


def character(value):
    fields(value, 'persona_spec character relationship')
    spec = PersonaSpec.from_dict(value['persona_spec'])
    check(spec.to_dict() == value['persona_spec'])
    check(validate_character(value['character']) == value['character'])
    string(value['relationship'], 4000, empty=True)


def row_shape(section, row):
    shapes = {
        'sessions': 'id title created_at',
        'turns': 'id session_id created_at updated_at',
        'messages': 'id turn_id role text time speaker raw_time excluded quote',
        'memories': 'id category text event_id root_id previous_id revision status created_at updated_at fact sources sessions',
        'summaries': 'id session_id kind revision created_at items',
        'coverage': 'id disposition summary_state',
    }
    fields(row, shapes[section])
    ident(row['id'])
    if section == 'sessions':
        from .workbench_timeline import session_title
        check(session_title(row['title']) == row['title'])
    if section in ('turns', 'summaries'):
        ident(row['session_id'])
    for key in ('created_at', 'updated_at'):
        if key in row:
            date(row[key])
    if section == 'messages':
        ident(row['turn_id'])
        check(row['role'] in ('user', 'persona', 'note'))
        string(row['text'], MAX_ROW, empty=True)
        string(row['speaker'], 256, empty=True)
        if row['raw_time'] is not None:
            string(row['raw_time'], 256, empty=True)
        date(row['time'], optional=True)
        check(row['excluded'] in (None, 'history_withdrawn', 'memory_revised'))
        if row['excluded']:
            check(row['role'] == 'note' and not row['text'] and row['quote'] is None)
        else:
            check(row['text'].strip())
        if row['quote'] is not None:
            fields(row['quote'], 'id text')
            if row['quote']['id'] is not None:
                ident(row['quote']['id'])
            string(row['quote']['text'], 4000)
    elif section == 'memories':
        for key in ('event_id', 'root_id'):
            ident(row[key])
        if row['previous_id'] is not None:
            ident(row['previous_id'])
        check(row['category'] in CATEGORIES and row['status'] in ('active', 'superseded', 'retracted'))
        check(type(row['revision']) is int and 0 < row['revision'] < 2**31)
        string(row['text'], MAX_ROW, empty=row['status'] == 'retracted')
        check(row['status'] != 'retracted' or not row['text'])
        if row['fact'] is not None:
            fact = row['fact']
            fields(fact, 'subject attribute valid_from valid_until automatic manual blocked')
            for key in ('subject', 'attribute'):
                string(fact[key], 512, empty=True)
            for key in ('valid_from', 'valid_until'):
                date(fact[key], optional=True)
            if fact['valid_from'] and fact['valid_until']:
                check(datetime.fromisoformat(fact['valid_from']) < datetime.fromisoformat(fact['valid_until']))
            check(all(type(fact[key]) is bool for key in ('automatic', 'manual', 'blocked')))
        check(isinstance(row['sessions'], list) and bool(row['sessions']))
        for sid in row['sessions']:
            ident(sid)
        check(len(set(row['sessions'])) == len(row['sessions']))
        source_shape(row['sources'])
        check(row['status'] != 'retracted' or not row['sources'])
    elif section == 'summaries':
        check(row['kind'] in ('summary', 'automatic_summary', 'fragment'))
        check(type(row['revision']) is int and 0 < row['revision'] < 2**31)
        check(isinstance(row['items'], list) and 1 <= len(row['items']) <= 200)
        for item in row['items']:
            fields(item, 'text sources')
            string(item['text'], MAX_ROW)
            source_shape(item['sources'])
            check(item['sources'])
    elif section == 'coverage':
        check(row['disposition'] in DISPOSITIONS and row['summary_state'] in SUMMARY_STATES)


def source_shape(sources):
    check(isinstance(sources, list) and len(sources) <= 200)
    for source in sources:
        fields(source, 'event_id quote')
        ident(source['event_id'])
        string(source['quote'], MAX_ROW)
    check(len({source['event_id'] for source in sources}) == len(sources))


def event_scope(rows, event):
    return rows.get('turns', event['turn_id'])['session_id']


def checked_sources(rows, sources):
    for source in sources:
        event = rows.get('messages', source['event_id'])
        check(event and not event['excluded'] and source['quote'] in event['text'])
        yield event_scope(rows, event)


def validate_graph(rows, main):
    check(rows.get('sessions', main) is not None)
    check(isinstance(rows.character, dict))
    for turn in rows.items('turns'):
        check(rows.get('sessions', turn['session_id']) and rows.get('coverage', turn['id']))
        check(next(rows.items('messages', turn['id']), None))
    for msg in rows.items('messages'):
        check(rows.get('turns', msg['turn_id']))
        if msg['quote'] and msg['quote']['id'] is not None:
            event = rows.get('messages', msg['quote']['id'])
            check(event and not event['excluded'] and event['role'] != 'note' and event['id'] != msg['id']
                  and msg['quote']['text'] in event['text'])
    for cover in rows.items('coverage'):
        check(rows.get('turns', cover['id']))
    check(not rows.db.execute("""SELECT 1 FROM rows WHERE section='memories'
        GROUP BY json_extract(payload,'$.root_id'),json_extract(payload,'$.revision')
        HAVING count(*)>1 LIMIT 1""").fetchone())
    for memory in rows.items('memories'):
        event = rows.get('messages', memory['event_id'])
        check(event and (memory['status'] != 'active' or not event['excluded']))
        evidence = {event_scope(rows, event), *checked_sources(rows, memory['sources'])}
        check(set(memory['sessions']) == evidence)
        if memory['previous_id'] is None:
            check(memory['id'] == memory['root_id'] and memory['revision'] == 1)
        else:
            previous = rows.get('memories', memory['previous_id'])
            check(previous and previous['root_id'] == memory['root_id'] and previous['revision'] < memory['revision'])
    for summary in rows.items('summaries'):
        check(rows.get('sessions', summary['session_id']))
        for item in summary['items']:
            check(set(checked_sources(rows, item['sources'])) == {summary['session_id']})


def write_pack(path, rows, main):
    parts, total = {}, 0
    with ZipFile(path, 'w', ZIP_DEFLATED, compresslevel=6) as archive:
        for name in FILES:
            records = (rows.character,) if name == 'character.json' else rows.items(name.removesuffix('.jsonl'))
            hasher, length, count = hashlib.sha256(), 0, 0
            with archive.open(name, 'w') as stream:
                for record in records:
                    raw = (encoded(record) + '\n').encode('utf-8')
                    if len(raw) > MAX_ROW or total + len(raw) > MAX_DATA:
                        raise RuntimeErrorBase('character_history_package_too_large')
                    stream.write(raw)
                    hasher.update(raw)
                    count += 1
                    length += len(raw)
                    total += len(raw)
            parts[name] = {'sha256': hasher.hexdigest(), 'bytes': length, 'count': count}
        manifest = {'format': FORMAT, 'main_session': main, 'parts': parts}
        raw = encoded(manifest).encode('utf-8')
        if total + len(raw) > MAX_DATA:
            raise RuntimeErrorBase('character_history_package_too_large')
        archive.writestr('manifest.json', raw)
    if path.stat().st_size > MAX_PACK:
        raise RuntimeErrorBase('character_history_package_too_large')
    return manifest


def read_pack(path, rows):
    try:
        return _read_pack(path, rows)
    except FileNotFoundError:
        raise RuntimeErrorBase('character_history_package_expired') from None
    except (BadZipFile, EOFError, KeyError, ValueError, TypeError, OverflowError, RecursionError,
            sqlite3.IntegrityError, NotImplementedError, UnicodeError, CompressionError):
        raise RuntimeErrorBase('invalid_character_history_package') from None


def _read_pack(path, rows):
    if path.stat().st_size > MAX_PACK:
        raise RuntimeErrorBase('character_history_package_too_large')
    with ZipFile(path) as archive:
        entries = archive.infolist()
        check(len(entries) == len(FILES) + 1 and set(archive.namelist()) == {'manifest.json', *FILES})
        check(all(member.compress_type in (ZIP_DEFLATED, ZIP_STORED) and not member.flag_bits & 1
                  for member in entries))
        if sum(member.file_size for member in entries) > MAX_DATA:
            raise RuntimeErrorBase('character_history_package_too_large')
        check(archive.getinfo('manifest.json').file_size <= MAX_ROW)
        manifest = decode(archive.read('manifest.json'))
        fields(manifest, 'format main_session parts')
        check(manifest['format'] == FORMAT)
        ident(manifest['main_session'])
        check(isinstance(manifest['parts'], dict) and set(manifest['parts']) == set(FILES))
        for name in FILES:
            expected = manifest['parts'][name]
            fields(expected, 'sha256 bytes count')
            check(type(expected['bytes']) is int and expected['bytes'] == archive.getinfo(name).file_size)
            check(type(expected['count']) is int and expected['count'] >= 0)
            hasher, count, length = hashlib.sha256(), 0, 0
            section = name.removesuffix('.jsonl')
            with archive.open(name) as stream:
                while raw := stream.readline(MAX_ROW + 1):
                    check(len(raw) <= MAX_ROW and raw.endswith(b'\n'))
                    length += len(raw)
                    count += 1
                    check(length <= expected['bytes'] and count <= expected['count'])
                    hasher.update(raw)
                    record = decode(raw)
                    if name == 'character.json':
                        check(count == 1)
                        character(record)
                        rows.character = record
                    else:
                        row_shape(section, record)
                        rows.add(section, record)
            check(length == expected['bytes'] and count == expected['count'] and hasher.hexdigest() == expected['sha256'])
        check(manifest['parts']['character.json']['count'] == 1)
    validate_graph(rows, manifest['main_session'])
    return manifest


def preview(rows, manifest, size):
    sessions = {row['id']: dict(row, messages=0, memories=0, remaining=0, first=None, last=None)
                for row in rows.items('sessions')}
    turns = {row['id']: row['session_id'] for row in rows.items('turns')}
    for event in rows.items('messages'):
        if event['excluded']:
            continue
        session = sessions[turns[event['turn_id']]]
        session['messages'] += 1
        if time := event['time']:
            if session['first'] is None or datetime.fromisoformat(time) < datetime.fromisoformat(session['first']):
                session['first'] = time
            if session['last'] is None or datetime.fromisoformat(time) > datetime.fromisoformat(session['last']):
                session['last'] = time
    for memory in rows.items('memories'):
        if memory['status'] == 'active' and not (memory['fact'] or {}).get('blocked'):
            for sid in memory['sessions']:
                sessions[sid]['memories'] += 1
    for coverage in rows.items('coverage'):
        if coverage['disposition'] not in FINISHED:
            sessions[turns[coverage['id']]]['remaining'] += 1
    return {'name': rows.character['persona_spec']['name'], 'sessions': list(sessions.values()),
            'main_session': manifest['main_session'], 'bytes': size, 'digest': fingerprint(manifest)}
