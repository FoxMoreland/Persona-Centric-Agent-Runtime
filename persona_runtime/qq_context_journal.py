"""Private local history of explicitly scoped QQ messages outside the automatic turn queue."""
from contextlib import closing
import json
import sqlite3
import time

from .models import RuntimeErrorBase, identifier, timestamp
from .security import protect, unprotect


def configured_journal(paths, cfg, *, timestamp_floor=None):
    profile = paths.home/'private/qq-ingress-observation.json'
    if cfg.get('qq_backend') != 'snowluma' or not profile.is_file():
        return None
    policy = json.loads(profile.read_text(encoding='utf-8')).get('context_sources')
    if not policy:
        return None
    if (not isinstance(policy,dict) or not {'groups','private','incoming_private'} <= set(policy)
            or set(policy) - {'groups','private','incoming_private','core_private','startup_limit'}):
        raise RuntimeErrorBase('qq_context_scope_invalid')
    core = policy.get('core_private',[])
    limit = policy.get('startup_limit')
    if limit is not None and (type(limit) is not int or not 1 <= limit <= 50):
        raise RuntimeErrorBase('qq_context_scope_invalid')
    if any(not isinstance(ids,list) or any(not isinstance(ident,str) or not ident.isascii()
               or not ident.isdecimal() or len(ident)>12 or ident==cfg['bot_qq'] for ident in ids)
           for ids in (policy['groups'],policy['private'],policy['incoming_private'],core)) or not cfg['bot_qq']:
        raise RuntimeErrorBase('qq_context_scope_invalid')
    if (core and core != [cfg['owner_qq']]) or cfg['owner_qq'] in policy['private']:
        raise RuntimeErrorBase('qq_context_scope_invalid')
    return QQContextJournal(paths,cfg['bot_qq'],policy['groups'],policy['private'],policy['incoming_private'],
                            core=core,startup_limit=limit,timestamp_floor=timestamp_floor)


class QQContextJournal:
    def __init__(self, paths, bot, groups, peers, incoming_peers, *, core=(), startup_limit=None, timestamp_floor=None):
        self.path = paths.home / 'private/qq-context.sqlite3'
        self.path.parent.mkdir(parents=True,exist_ok=True)
        self.bot = bot
        self.core = frozenset(core)
        self.allowed = {'group': frozenset(groups), 'private': frozenset(peers) | self.core}
        self.incoming_peers = frozenset(incoming_peers) | self.core
        self.startup_limit = startup_limit
        self.startup_id = identifier()
        self.timestamp_floor = int(time.time()) if timestamp_floor is None else timestamp_floor
        if not self.incoming_peers <= self.allowed['private']:
            raise RuntimeErrorBase('qq_context_scope_invalid')
        with closing(sqlite3.connect(self.path)) as db:
            db.execute("""CREATE TABLE IF NOT EXISTS messages (
                account TEXT NOT NULL, kind TEXT NOT NULL, conversation TEXT NOT NULL,
                platform_message_id TEXT NOT NULL, author TEXT NOT NULL, direction TEXT NOT NULL,
                platform_time INTEGER, recorded_at TEXT NOT NULL, content TEXT NOT NULL,
                PRIMARY KEY(account,kind,conversation,platform_message_id))""")
            db.execute("""CREATE TABLE IF NOT EXISTS startup_quota (
                account TEXT NOT NULL, kind TEXT NOT NULL, conversation TEXT NOT NULL,
                startup_id TEXT NOT NULL, used INTEGER NOT NULL,
                PRIMARY KEY(account,kind,conversation))""")
            db.execute("""CREATE TABLE IF NOT EXISTS sync_watermarks (
                account TEXT NOT NULL, peer TEXT NOT NULL, newest_id TEXT NOT NULL,
                PRIMARY KEY(account,peer))""")

    def core_incoming(self, raw):
        return (isinstance(raw,dict) and str(raw.get('self_id','')) == self.bot
                and raw.get('post_type') == 'message' and raw.get('message_type') == 'private'
                and raw.get('sub_type') == 'friend' and str(raw.get('user_id','')) in self.core)

    def current_message(self, raw):
        when = raw.get('time')
        return type(when) is int and when >= self.timestamp_floor

    def capture(self, raw):
        if not isinstance(raw,dict) or str(raw.get('self_id','')) != self.bot:
            return None
        kind = raw.get('message_type')
        if kind not in self.allowed or (kind == 'private' and raw.get('sub_type') != 'friend'):
            return None
        author = str(raw.get('user_id',''))
        own = raw.get('post_type') == 'message_sent' and author == self.bot
        incoming = (raw.get('post_type') == 'message' and author != self.bot
                    and ((kind == 'private' and (author in self.incoming_peers or self.startup_limit is not None))
                         or (kind == 'group' and self.startup_limit is not None)))
        if not own and not incoming:
            return None
        conversation = str(raw.get('group_id','') if kind == 'group' else
                           raw.get('target_id','') if own else author)
        if conversation not in self.allowed[kind]:
            return None
        core = kind == 'private' and conversation in self.core
        limited = self.startup_limit is not None and not core
        if limited and not self.current_message(raw):
            return 'offline_ignored'
        mid = raw.get('message_id')
        if (type(mid) not in (int,str) or not str(mid).strip() or str(mid) == '0' or len(str(mid)) > 200
                or not isinstance(raw.get('message'),list)):
            return None
        texts, segment_types = [], []
        for part in raw['message']:
            if not isinstance(part,dict) or not isinstance(part.get('type'),str):
                continue
            segment_types.append(part['type'])
            data = part.get('data')
            if part['type'] == 'text' and isinstance(data,dict) and isinstance(data.get('text'),str):
                texts.append(data['text'])
        if not segment_types:
            return None
        direction = 'account_self' if own else 'incoming'
        when = raw.get('time')
        when = when if type(when) is int and 0 < when < 2**63 else None
        with closing(sqlite3.connect(self.path)) as db, db:
            if limited:
                db.execute('BEGIN IMMEDIATE')
                quota = db.execute('''SELECT startup_id,used FROM startup_quota
                    WHERE account=? AND kind=? AND conversation=?''',(self.bot,kind,conversation)).fetchone()
                used = quota[1] if quota and quota[0] == self.startup_id else 0
                if used >= self.startup_limit:
                    return 'startup_limit'
            content = protect(json.dumps({'text': ''.join(texts), 'segment_types':segment_types},ensure_ascii=False))
            added = db.execute('INSERT OR IGNORE INTO messages VALUES (?,?,?,?,?,?,?,?,?)',
                               (self.bot,kind,conversation,str(mid),author,direction,when,timestamp(),content)).rowcount
            if limited and added:
                db.execute('''INSERT INTO startup_quota VALUES (?,?,?,?,?)
                    ON CONFLICT(account,kind,conversation) DO UPDATE
                    SET startup_id=excluded.startup_id,used=excluded.used''',
                           (self.bot,kind,conversation,self.startup_id,used+1))
        return direction if added else 'duplicate'

    def recent(self, kind, conversation, limit=50):
        if (kind not in self.allowed or conversation not in self.allowed[kind]
                or type(limit) is not int or not 1 <= limit <= 200):
            raise RuntimeErrorBase('qq_context_scope_invalid')
        with closing(sqlite3.connect(self.path)) as db:
            db.row_factory = sqlite3.Row
            rows = db.execute('''SELECT platform_message_id,author,direction,platform_time,recorded_at,content
                FROM messages WHERE account=? AND kind=? AND conversation=? ORDER BY platform_time DESC,rowid DESC LIMIT ?''',
                              (self.bot,kind,conversation,limit)).fetchall()
        result = []
        for row in reversed(rows):
            item = dict(row)
            result.append(item | json.loads(unprotect(item.pop('content'))))
        return result

    def watermark(self, peer, newest=None):
        if peer not in self.core:
            raise RuntimeErrorBase('qq_context_scope_invalid')
        with closing(sqlite3.connect(self.path)) as db, db:
            if newest is not None:
                db.execute('''INSERT INTO sync_watermarks VALUES (?,?,?)
                    ON CONFLICT(account,peer) DO UPDATE SET newest_id=excluded.newest_id''',
                           (self.bot,peer,newest))
            row = db.execute('SELECT newest_id FROM sync_watermarks WHERE account=? AND peer=?',
                             (self.bot,peer)).fetchone()
        return row[0] if row else None
