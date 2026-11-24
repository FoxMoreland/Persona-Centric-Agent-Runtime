"""Shared canonical memory ranking, without creating or reinforcing memories."""
import json
from datetime import datetime, timezone
from .models import LOCAL_USER_ID, RuntimeErrorBase
from .recall_sources import epoch, source_epoch
from .recall_policy import priority, INITIAL_PRIORITY

QUOTE_MEMORY_WINDOW_SECONDS = 60 * 60


def automatic_origin(conn, memory):
    """Return the most recent known source time of an automatically organized event."""
    row=conn.execute("SELECT batch_id FROM memory_facts WHERE memory_id=?",(memory["id"],)).fetchone()
    if not row or row[0] is None:
        return None
    from .import_memory_scope import memory_source_ids
    return max((time for ident in memory_source_ids(conn,memory["id"])
                if (time:=source_epoch(conn,ident)) is not None),default=None)


def recency(conn, memory, now):
    origin=automatic_origin(conn,memory)
    if origin is None:
        origin=datetime.fromisoformat(memory["updated_at"]).timestamp()
    return priority("memory",INITIAL_PRIORITY,origin,now)


def quote_priorities(conn,turn,window):
    if not window:
        return {}
    from .desktop_messages import POSITION_STRIDE, event_messages, segments
    from .import_memory_scope import memory_source_ids
    anchor=window["anchor"]
    parts={p["session_id"]:p["end"] for p in segments(conn,turn["persona_id"],turn["session_id"])}
    end=parts.get(anchor["session_id"])
    if end is None:
        return {}
    anchor_id=anchor["event_id"]
    try:
        anchor_time=epoch(anchor["time"]) if anchor["time"] else None
    except (ValueError,OverflowError,RuntimeErrorBase):
        anchor_time=None
    # Compare the original speech time, and allow the exact source even when it has no date.
    nearby={}
    for event in conn.execute("""SELECT e.id FROM events e WHERE e.session_id=? AND e.persona_id=? AND e.seq<=?
        AND (e.id=? OR abs((julianday(e.created_at)-2440587.5)*86400 - ?)<=?)""",
        (anchor["session_id"],turn["persona_id"],end//POSITION_STRIDE,
         anchor_id,anchor_time,QUOTE_MEMORY_WINDOW_SECONDS+1)):
        ident=event["id"]
        stamp=source_epoch(conn,ident)
        if ident==anchor_id:
            nearby[ident]=0.0
        elif stamp is not None and anchor_time is not None:
            distance=abs(stamp-anchor_time)
            if distance<=QUOTE_MEMORY_WINDOW_SECONDS:
                nearby[ident]=distance
    if not nearby:
        return {}
    def in_readable_history(mid):
        from .context_builders import portable_memory_sessions
        portable = portable_memory_sessions(conn, mid)
        for ident in memory_source_ids(conn,mid):
            event=conn.execute("SELECT * FROM events WHERE id=?",(ident,)).fetchone()
            if event is None:
                return False
            cutoff=parts.get(event["session_id"])
            if cutoff is None and turn["session_id"] in portable and event["session_id"] in portable:
                from .import_history import history_allowed
                if history_allowed(conn,event["session_id"],"dialogue"):
                    continue
            if cutoff is None or event["seq"]>cutoff//POSITION_STRIDE:
                return False
            if event["seq"]==cutoff//POSITION_STRIDE:
                if any(row["position"]>cutoff for row in event_messages(conn,turn["persona_id"],event)):
                    return False
        return True

    result={}
    for row in conn.execute("""SELECT m.id, e.id AS source_id FROM memories m
        JOIN memory_facts f ON f.memory_id=m.id AND f.batch_id IS NOT NULL
        JOIN events e ON e.session_id=? AND (e.id=m.source_event_id OR EXISTS(
             SELECT 1 FROM memory_sources ms WHERE ms.memory_id=m.id AND ms.event_id=e.id))
        WHERE m.persona_id=? AND m.user_id=? AND m.status='active' AND e.id IN (SELECT value FROM json_each(?))""",
        (anchor["session_id"],turn["persona_id"],LOCAL_USER_ID,json.dumps(list(nearby)))):
        distance=nearby[row["source_id"]]
        result[row["id"]]=min(result.get(row["id"],distance),distance)
    return {mid:distance for mid,distance in result.items() if in_readable_history(mid)}


def quote_order(proximities, ident):
    distance=proximities.get(ident)
    return (False,0.0) if distance is None else (True,-distance)


def rank_memories(conn,turn,text,recent_queries,*,enforce_scope=False,pinned=None):
    from .chat import terms
    current_terms = terms(text)
    previous_terms = terms(' '.join(recent_queries))
    ranked = []
    now=datetime.now(timezone.utc).timestamp()
    sql="SELECT * FROM memories WHERE persona_id=? AND user_id=? AND status='active'"
    args=[turn['persona_id'],LOCAL_USER_ID]
    if pinned is not None:
        sql+=" AND id IN (SELECT value FROM json_each(?))"
        args.append(json.dumps(list(pinned)))
    for memory in conn.execute(sql,args):
        memory = dict(memory)
        from .automatic_memory import usable_fact
        if not usable_fact(conn,memory):continue
        if enforce_scope:
            from .context_builders import memory_usable
            if not memory_usable(conn,turn,memory):continue
        mt = terms(memory['text'])
        current, history = sorted(mt & current_terms), sorted(mt & previous_terms)
        score = 3*len(current)+len(history)
        if text.strip() and text.strip().lower() in memory['text'].lower():
            score += 9
        if not score and memory['previous_id']:
            # A current revision must accompany an old source present in raw
            # context even if the user's new query contains no matching term.
            old=conn.execute('SELECT source_event_id FROM memories WHERE id=?',(memory['previous_id'],)).fetchone()
            if old:
                original=conn.execute('SELECT session_id FROM events WHERE id=?',(old[0],)).fetchone()
                if original and original[0]==turn['session_id']:score=1
        if score or pinned is not None:
            ranked.append((score, recency(conn,memory,now), memory['id'], memory, current, history))
    ranked.sort(key=lambda r: (*quote_order(pinned or {},r[2]),*r[:3]), reverse=True)
    return ranked
