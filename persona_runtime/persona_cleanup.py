"""Permanently remove one persona's live records and reclaim their storage."""

from collections import defaultdict
from contextlib import closing
import json
import re
import sqlite3

from .models import RuntimeErrorBase


def _retention_triggers(db):
    from .schema_dialogue_generation import TABLES
    names = {
        *(table+'_no_delete' for table in TABLES[1:]),
        *(table+'_no_delete' for table in ('learning_approvals','learning_jobs','learning_proposals',
            'expression_speech_units','expression_speech_runs','expression_speech_publications','expression_speech_items')),
        *('expression_speech_'+table+'_immutable_delete' for table in ('contexts','versions','visible','receipts')),
        *(table+'_delete' for table in ('session_packages','session_events','session_operations','topic_versions',
            'legacy_delivery_dispositions','disposition_versions','disposition_revocations','disposition_decisions')),
        *(table+'_immutable_delete' for table in ('expression_asset_source_versions','expression_asset_versions',
            'expression_asset_supports','expression_main_receipts','expression_publication_versions',
            'expression_utterances','expression_presentation_receipts','dialogue_source_bundles','dialogue_corpus_supports')),
        *(table+'_immutable_DELETE' for table in ('session_privacy_events','private_reply_turns','private_reply_decisions',
            'private_reply_reviews')),
        'knowledge_study_commits_DELETE','knowledge_study_reads_DELETE',
        'context_manifest_immutable_delete','expression_learning_immutable_delete','local_knowledge_read_retained',
        'private_reply_attempt_retained','private_reply_attempt_history','private_lookup_record_retained',
        'private_lookup_intent_retained','private_lookup_attempt_retained','private_reply_sources_retained',
        'private_reply_node_history','retrieval_item_retained_v2',
    }
    return [(r['name'],r['sql']) for r in db.execute("""SELECT name,sql FROM sqlite_schema
        WHERE type='trigger' AND tbl_name IN (SELECT name FROM doomed)""") if r['name'] in names]


def q(name):
    return '"' + name.replace('"', '""') + '"'


def owned_rows(db, pid):
    """Use the current schema's foreign keys, including composite identities."""
    tables = {
        row['name']: {c['name'] for c in db.execute('PRAGMA table_info(' + q(row['name']) + ')')}
        for row in db.execute('PRAGMA table_list')
        if row['schema'] == 'main' and row['type'] == 'table' and not row['name'].startswith('sqlite_')
    }
    db.execute('CREATE TEMP TABLE doomed(name TEXT, rid INTEGER, PRIMARY KEY(name,rid))')
    db.execute("INSERT INTO doomed SELECT 'personas',rowid FROM personas WHERE id=?", (pid,))
    edges = []
    for name, columns in tables.items():
        table = q(name)
        owner = next((c for c in ('persona_id','owner_persona') if c in columns), None)
        if owner:
            db.execute(f'INSERT OR IGNORE INTO doomed SELECT ?,rowid FROM {table} WHERE {q(owner)}=?', (name,pid))
        keys = defaultdict(list)
        for row in db.execute('PRAGMA foreign_key_list(' + table + ')'):
            if name == 'persona_lifecycle' and row['from'] == 'copy_of':
                continue
            keys[(row['id'],row['table'])].append(f'c.{q(row["from"])}=p.{q(row["to"])}')
        for (_, parent), columns in keys.items():
            edges.append((name,parent,' AND '.join(columns)))
    while True:
        previous = db.total_changes
        for child,parent,condition in edges:
            db.execute(f'''INSERT OR IGNORE INTO doomed
                SELECT ?,c.rowid FROM doomed d JOIN {q(parent)} p ON p.rowid=d.rid
                JOIN {q(child)} c ON {condition} WHERE d.name=?''', (child,parent))
        if db.total_changes == previous:
            break
    for name, columns in tables.items():
        owner = next((c for c in ('persona_id','owner_persona') if c in columns), None)
        if owner and db.execute(f'''SELECT 1 FROM doomed d JOIN {q(name)} t ON t.rowid=d.rid
                WHERE d.name=? AND t.{q(owner)} IS NOT NULL AND t.{q(owner)}!=? LIMIT 1''',(name,pid)).fetchone():
            raise RuntimeErrorBase('persona_cleanup_shared_reference')
    return tables


def delete_rows(db, pid, instance):
    from .archive import fingerprint
    db.execute('PRAGMA defer_foreign_keys=ON')
    db.execute('PRAGMA secure_delete=ON')
    tables = owned_rows(db,pid)
    sessions = [row[0] for row in db.execute('SELECT id FROM sessions WHERE persona_id=?',(pid,))]
    ids = {pid,*sessions}
    for table, columns in tables.items():
        if 'id' in columns:
            ids.update(row[0] for row in db.execute(f'''SELECT c.id FROM doomed d
                JOIN {q(table)} c ON c.rowid=d.rid WHERE d.name=? AND typeof(c.id)='text' ''',(table,)))
    # Include hashed consent keys and their operation receipts.
    ids.update(fingerprint([instance,pid,sid]) for sid in sessions)
    for (key,) in db.execute('SELECT key FROM knowledge_control').fetchall():
        if any(part in ids for part in key.split('.')):
            db.execute('DELETE FROM knowledge_control WHERE key=?',(key,))
    db.execute("""DELETE FROM knowledge_control WHERE json_valid(value) AND (
        json_extract(value,'$.persona_id')=? OR json_extract(value,'$.data.persona_id')=?
        OR json_extract(value,'$.owner_persona')=? OR json_extract(value,'$.data.owner_persona')=?)""",(pid,pid,pid,pid))
    db.execute("""DELETE FROM memory_scan_positions WHERE name IN (SELECT value FROM json_each(?))""",
               (json.dumps(['sessions/'+pid, *[prefix+sid for sid in sessions for prefix in ('native/','imported/')]]),))
    db.execute("""DELETE FROM recall_support_searches WHERE root_id IN (
        SELECT t.id FROM turns t JOIN doomed d ON d.name='turns' AND d.rid=t.rowid)""")
    db.execute("UPDATE persona_lifecycle SET copy_of=NULL WHERE copy_of=?",(pid,))
    db.execute("DELETE FROM persona_operations WHERE json_extract(result_json,'$.id')=?",(pid,))
    # Restore the exact retention rules before committing the explicitly confirmed
    # persona deletion. Other connections never see a schema without the rules.
    retention = _retention_triggers(db)
    for trigger,_ in retention:
        db.execute('DROP TRIGGER '+q(trigger))
    for (table,) in db.execute('SELECT DISTINCT name FROM doomed').fetchall():
        db.execute(f'DELETE FROM {q(table)} WHERE rowid IN (SELECT rid FROM doomed WHERE name=?)',(table,))
    for _, statement in retention:
        db.execute(statement)
    if db.execute('PRAGMA foreign_key_check').fetchone():
        raise RuntimeErrorBase('persona_cleanup_shared_reference')
    return sessions


def _compact(db):
    if db.execute('PRAGMA freelist_count').fetchone()[0]:
        db.execute('VACUUM')
    return db.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()[0] == 0


def _archive_cache(store, pid):
    from .archive_index import ArchiveIndex
    index = ArchiveIndex(store)
    index.safe_path()
    if not index.path.exists():
        return True
    with index.connection() as (db, meta):
        db.execute('BEGIN IMMEDIATE')
        scope = "SELECT partition FROM scope_generations WHERE json_extract(scope,'$.persona_id')=?"
        if meta['fts']:
            db.execute('DELETE FROM fts_documents WHERE rowid IN (SELECT rowid FROM documents WHERE partition IN ('+scope+'))',(pid,))
        db.execute('DELETE FROM documents WHERE partition IN ('+scope+')',(pid,))
        db.execute("DELETE FROM scope_generations WHERE json_extract(scope,'$.persona_id')=?",(pid,))
        db.commit()
        return _compact(db)


def _drafts(paths, pid):
    from .config import atomic_json
    from .security import protect, unprotect
    from .submissions import SubmissionJournal
    from .private_personas import safe
    journal = SubmissionJournal(paths,'webview')
    with journal._locked():
        runtime = paths.home / 'runtime'
        for filename in ('webview-composer.dpapi.json','webview-reading.dpapi.json'):
            path = safe(runtime/filename, paths.home)
            if not path.is_file():
                continue
            values = json.loads(unprotect(json.loads(path.read_bytes())['encrypted']))
            if filename.startswith('webview-composer'):
                values = {k:v for k,v in values.items() if json.loads(k)[1]!=pid}
            else:
                for state in values.values():
                    state['sessions'] = {k:v for k,v in state['sessions'].items() if json.loads(k)[0]!=pid}
                    if (state.get('last') or {}).get('persona_id')==pid:
                        state['last'] = None
            atomic_json(path, {'version':1,'encrypted':protect(json.dumps(values,ensure_ascii=False))})
        remaining = journal._read()
        kept = [r for r in remaining if r['payload'].get('persona_id')!=pid]
        if kept != remaining:
            journal._write(kept)


def reclaim(store, pid, sessions, config, cfg):
    from .archive_query import SearchFailure
    from .private_personas import safe
    from .session_privacy import _witness
    from .workbench_appearance import IMAGE_TYPES
    ok = True
    try:
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,128}',pid):
            raise RuntimeErrorBase('invalid_persona_id')
        root = store.paths.home/'private'/'workbench-appearance'/pid
        for kind,(suffix,_) in IMAGE_TYPES.items():
            safe(root/f'{kind}.{suffix}',store.paths.home).unlink(missing_ok=True)
        if root.is_dir() and not any(root.iterdir()):
            safe(root,store.paths.home).rmdir()
        for sid in sessions:
            witness = _witness(config,cfg,sid)
            for suffix in ('.json','.public.json','.community-search.json','.community-search.automatic.json','.openrouter.json'):
                safe(witness.with_suffix(suffix),store.paths.home).unlink(missing_ok=True)
        _drafts(store.paths,pid)
    except (OSError,ValueError):
        ok = False
    try:
        ok = _archive_cache(store,pid) and ok
    except (OSError,sqlite3.Error,ValueError,SearchFailure):
        ok = False
    try:
        with closing(store._connect()) as db:
            ok = _compact(db) and ok
    except (OSError,sqlite3.Error):
        ok = False
    return ok
