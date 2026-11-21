"""Inspect and stage the persona world index for an existing desktop instance."""

import argparse
from collections import Counter, defaultdict
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from persona_runtime import persona_world
from persona_runtime.archive import fingerprint
from persona_runtime.knowledge_revisions import canonical_url
from persona_runtime.storage import Store


def committed_turns(db, instance, persona, after='', limit=None):
    sql = """SELECT c.turn_id FROM knowledge_study_commits c
             JOIN private_reply_turns p ON p.turn_id=c.turn_id
             JOIN turns t ON t.id=c.turn_id
             WHERE c.status='committed' AND p.instance_id=? AND t.persona_id=?
               AND c.turn_id>? ORDER BY c.turn_id"""
    return [r[0] for r in db.execute(sql + ' LIMIT ?', (instance, persona, after, limit or -1))]


def audit(db, instance, persona):
    """Read a consistent snapshot. Report opaque identifiers and byte counts."""
    documents = {}
    revisions = defaultdict(set)
    copies = defaultdict(list)
    for row in db.execute("""SELECT d.id,d.session_id,d.url,d.sha256,d.source_kind,
                                    length(CAST(d.body AS BLOB)) AS body_bytes
                             FROM local_knowledge_documents d
                             JOIN private_reply_turns p ON p.turn_id=d.receipt
                             JOIN turns t ON t.id=p.turn_id AND t.persona_id=d.persona_id
                                          AND t.session_id=d.session_id
                             WHERE d.scope='session' AND d.persona_id=? AND p.instance_id=?
                             ORDER BY d.id""", (persona, instance)):
        documents[row['id']] = row
        url = canonical_url(row['url'])
        revisions[url].add(row['sha256'])
        copies[url, row['sha256']].append(row)
    facts = set()
    eligible_docs = set()
    spans = set()
    acquisitions = set()
    turns = committed_turns(db, instance, persona)
    for tid in turns:
        for entry in persona_world.inspect_turn(db, instance, persona, tid):
            facts.add(entry['fact_id'])
            eligible_docs.add(entry['document_id'])
            spans.add((entry['url'], entry['sha256'], entry['start'], entry['end'], entry['context_hash']))
            acquisitions.add((tid, entry['source_id']))
    indexed = list(persona_world.attestations(db, instance, persona))
    duplicates = []
    eligible_duplicate_bytes = 0
    for key, group in sorted(copies.items()):
        if len(group) < 2:
            continue
        eligible = [r for r in group if r['id'] in eligible_docs]
        sizes = [r['body_bytes'] for r in eligible]
        eligible_duplicate_bytes += sum(sizes) - max(sizes, default=0)
        group_sizes = [r['body_bytes'] for r in group]
        duplicates.append({'source_revision': fingerprint(key), 'documents': [r['id'] for r in group],
                           'sessions': len({r['session_id'] for r in group}),
                           'duplicate_body_bytes': sum(group_sizes) - max(group_sizes)})
    excluded = set(documents).difference(eligible_docs)
    unbound = db.execute("""SELECT COUNT(*) FROM local_knowledge_documents d
                            WHERE d.scope='session' AND d.persona_id=?
                            AND NOT EXISTS(SELECT 1 FROM private_reply_turns p
                                           JOIN turns t ON t.id=p.turn_id
                                           WHERE p.turn_id=d.receipt AND t.persona_id=d.persona_id
                                             AND t.session_id=d.session_id)""", (persona,)).fetchone()[0]
    return {
        'enabled': persona_world.enabled(db, instance, persona),
        'legacy_documents': len(documents),
        'legacy_sessions': len({r['session_id'] for r in documents.values()}),
        'legacy_body_bytes': sum(r['body_bytes'] for r in documents.values()),
        'documents_with_body_reference': sum(r['body_bytes'] == 0 for r in documents.values()),
        'allocated_database_bytes': (db.execute('PRAGMA page_count').fetchone()[0]
                                     * db.execute('PRAGMA page_size').fetchone()[0]),
        'committed_turns': len(turns),
        'shareable_documents': sorted(eligible_docs),
        'shareable_facts': len(facts),
        'shareable_distinct_spans': len(spans),
        'shareable_acquisitions': len(acquisitions),
        'indexed_attestations': len(indexed),
        'duplicate_groups': duplicates,
        'shareable_duplicate_body_bytes': eligible_duplicate_bytes,
        'urls_with_multiple_content_revisions': {
            fingerprint(url): sorted(hashes) for url, hashes in revisions.items() if len(hashes) > 1
        },
        'ineligible_documents': sorted(excluded),
        'ineligible_by_source_kind': dict(Counter(documents[key]['source_kind'] for key in excluded)),
        'persona_documents_without_instance_receipt': unbound,
    }


def backfill(db, instance, persona, batch_size):
    added = scanned = 0
    after = ''
    while True:
        with db:
            db.execute('BEGIN IMMEDIATE')
            batch = committed_turns(db, instance, persona, after, batch_size)
            if not batch:
                break
            for turn in batch:
                added += persona_world.index_turn(db, instance, persona, turn)
            scanned += len(batch)
            after = batch[-1]
    return {'committed_turns_scanned': scanned, 'attestations_added': added}


def switch(db, instance, persona, enable):
    with db:
        db.execute('BEGIN IMMEDIATE')
        added = 0
        if enable:
            # Catch commits that arrived during a staged backfill before enabling live writes.
            for turn in committed_turns(db, instance, persona):
                added += persona_world.index_turn(db, instance, persona, turn)
        persona_world.set_enabled(db, instance, persona, enable)
        return {'enabled': persona_world.enabled(db, instance, persona), 'attestations_added': added}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('audit', 'backfill', 'enable', 'disable', 'withdraw-source'))
    parser.add_argument('--home', type=Path, required=True)
    parser.add_argument('--instance', required=True)
    parser.add_argument('--persona', required=True)
    parser.add_argument('--batch-size', type=int, default=32)
    parser.add_argument('--url', help='Public source URL to withdraw for this persona')
    parser.add_argument('--sha256', help='Limit the withdrawal to one content revision')
    args = parser.parse_args(argv)
    if args.batch_size < 1:
        parser.error('batch size must be positive')
    if args.action == 'withdraw-source' and not args.url:
        parser.error('withdraw-source requires --url')
    if args.action != 'withdraw-source' and (args.url is not None or args.sha256 is not None):
        parser.error('--url and --sha256 apply only to withdraw-source')
    path = args.home.resolve() / 'data/database/runtime.sqlite3'
    mode = 'ro' if args.action == 'audit' else 'rw'
    with closing(sqlite3.connect(path.as_uri() + '?mode=' + mode, uri=True, isolation_level=None)) as db:
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA foreign_keys=ON')
        Store._check(db)
        if not db.execute('SELECT 1 FROM personas WHERE id=?', (args.persona,)).fetchone():
            parser.error('unknown persona ID')
        if not db.execute('SELECT 1 FROM private_reply_instances WHERE instance_id=?', (args.instance,)).fetchone():
            parser.error('unknown desktop instance ID')
        if args.action == 'audit':
            db.execute('BEGIN')
            result = audit(db, args.instance, args.persona)
        elif args.action == 'backfill':
            result = backfill(db, args.instance, args.persona, args.batch_size)
        elif args.action == 'withdraw-source':
            with db:
                db.execute('BEGIN IMMEDIATE')
                persona_world.withdraw_source(db, args.instance, args.persona, args.url, args.sha256)
                result = {'withdrawn': True, 'scope': 'revision' if args.sha256 else 'source'}
        else:
            result = switch(db, args.instance, args.persona, args.action == 'enable')
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))


if __name__ == '__main__':
    main()
