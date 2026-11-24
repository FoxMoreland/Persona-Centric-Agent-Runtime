"""Public search permission and turn scope for a standard Community session."""
import json

from .archive import encoded, fingerprint, scope
from .models import RuntimeErrorBase
from .sessions import owned

PROTOCOL = "community-explicit-search-v1"
AUTOMATIC = "community-automatic-search-v1"
PROTOCOLS = (PROTOCOL, AUTOMATIC)
ENDPOINT = "https://api.deepseek.com/v1/tools/search"


def outcome(retrieval):
    if retrieval["search_outcome"] == "completed":
        return "found" if retrieval["material_acquired"] else "empty"
    if retrieval["search_outcome"] == "unconfirmed":
        return "unconfirmed"
    if not retrieval["task_ids"] or "provider_network_error" in retrieval.get("errors", []):
        return "not_dispatched"
    return "failed"


def state(db, config, cfg, pid, sid, protocol=PROTOCOL):
    from . import community
    from .models import PRIVATE_CHAT, persona_chat_mode
    from .private_reply_entry import selected
    from .provider_policy import is_official_deepseek
    from .session_privacy import destination, _witness

    owned(db, sid, scope(pid, "desktop"))
    model = cfg["model"]
    instance = cfg["instance_id"]
    available = bool(community.enabled() and model.get("encrypted_key") and is_official_deepseek(model)
                     and not selected(db, instance, pid))
    if protocol == AUTOMATIC:
        available = available and persona_chat_mode(db, pid) == PRIVATE_CHAT
    binding = fingerprint([protocol, instance, pid, sid, ENDPOINT, destination(config, model)]) if available else None
    suffix = ".automatic" if protocol == AUTOMATIC else ""
    key = "community.search.consent." + fingerprint([instance, pid, sid]) + suffix
    path = _witness(config, cfg, sid).with_suffix(".community-search" + suffix + ".json")
    row = db.execute("SELECT value FROM knowledge_control WHERE key=?", (key,)).fetchone()
    stored = json.loads(row[0]) if row else {}
    witness = json.loads(path.read_bytes()) if path.is_file() else {}
    return {"protocol": protocol, "available": available, "endpoint": ENDPOINT,
            "revision": max(stored.get("revision", 0), witness.get("revision", 0)),
            "recipient_hash": binding,
            "enabled": bool(available and stored == witness and stored.get("binding") == binding
                            and stored.get("enabled"))}, key, path


def update(db, config, cfg, pid, sid, enabled, revision, recipient_hash, operation_id, protocol=PROTOCOL):
    from .config import atomic_json

    current, key, path = state(db, config, cfg, pid, sid, protocol)
    signature = fingerprint([enabled, revision, recipient_hash])
    op = key + ".op." + operation_id
    old = db.execute("SELECT value FROM knowledge_control WHERE key=?", (op,)).fetchone()
    if old:
        if json.loads(old[0]) != signature:
            raise RuntimeErrorBase("session_privacy_operation_conflict")
        return current
    if current["revision"] != revision:
        raise RuntimeErrorBase("version_conflict")
    if enabled and (not current["available"] or current["recipient_hash"] != recipient_hash):
        raise RuntimeErrorBase("community_search_consent_required")
    receipt = {"enabled": enabled, "revision": revision + 1, "binding": current["recipient_hash"]}
    atomic_json(path, receipt, durable=True)
    db.execute("INSERT OR REPLACE INTO knowledge_control VALUES(?,?)", (key, encoded(receipt)))
    db.execute("INSERT INTO knowledge_control VALUES(?,?)", (op, encoded(signature)))
    return state(db, config, cfg, pid, sid, protocol)[0]


def require_consent(db, config, pid, sid, consent, protocol=PROTOCOL):
    if config is None:
        raise RuntimeErrorBase("community_search_consent_required")
    cfg = config.load()
    current, _, _ = state(db, config, cfg, pid, sid, protocol)
    if not current["enabled"] or any(current[key] != consent[key] for key in ("revision", "recipient_hash")):
        raise RuntimeErrorBase("community_search_consent_required")


def task_for_turn(db, turn):
    from .knowledge_service import read
    from .turn_input import load_turn_input

    if turn["channel"] != "desktop":
        return None
    for event in reversed(load_turn_input(db, turn)):
        saved = read(db, "native.public_event." + event["id"], {})["data"]
        task = saved.get("task", {})
        if "community_consent" not in task:
            continue
        if (saved.get("input_hash") != fingerprint(event["text"]) or
                (saved.get("persona_id"), saved.get("session_id"), saved.get("channel")) !=
                (turn["persona_id"], turn["session_id"], turn["channel"]) or
                db.execute("SELECT 1 FROM source_exclusions WHERE event_id=?", (event["id"],)).fetchone()):
            raise RuntimeErrorBase("native_public_task_scope_changed")
        return task
    return None


def automatic_consent(db, config, pid, sid):
    try:
        grant, _, _ = state(db, config, config.load(), pid, sid, AUTOMATIC)
    except (OSError, ValueError):
        return None
    if grant["enabled"]:
        return {key: grant[key] for key in ("revision", "recipient_hash")}


def automatic_request(db, turn, native):
    from datetime import datetime, timedelta, timezone
    from .community_decisions import TURN
    from .knowledge_native import NO_QUERY, SENSITIVE_QUERY, public_query_span
    from .knowledge_web_decision import NATIVE_DECISION_PROFILE
    from .scene_policy import read
    from .turn_input import load_turn_input

    plan = read(db, TURN + turn["id"]) or {}
    if not (consent := plan.get("search_consent")):
        return None
    members = load_turn_input(db, turn)
    if not members:
        return None
    latest = members[-1]
    if latest["role"] != "user" or db.execute("SELECT 1 FROM source_exclusions WHERE event_id=?", (latest["id"],)).fetchone():
        return None
    question = latest["text"].strip()
    explicit = public_query_span(question)
    if explicit:
        query = explicit["question"]
    elif not native.protected(db, turn["persona_id"]):
        query = question
    else:
        return None
    if not query or len(query) > 128 or SENSITIVE_QUERY.search(query) or NO_QUERY.search(question):
        return None
    today = datetime.fromisoformat(turn["created_at"]).astimezone(timezone(timedelta(hours=8))).date().isoformat()
    inputs = {"question": query, "recent_context": [], "today": today, "decision_profile": NATIVE_DECISION_PROFILE,
              "explicit_public_query_requested": explicit is not None, "public_task_available": True,
              "available_sources": [], "cached_public_notes": "", "cached_notes_available": False}
    return inputs, {"query": query, "community_consent": consent}


def require_decision(db, turn, task_id):
    from .knowledge_service import read

    row = db.execute("SELECT payload_json FROM organization_tasks WHERE id=? AND status='completed'", (task_id,)).fetchone()
    spec = json.loads(row[0]).get("knowledge_processing", {}) if row else {}
    result = read(db, "processing.result." + task_id, {})["data"] if isinstance(task_id, str) else {}
    if ((spec.get("root_turn_id"), spec.get("stage"), spec.get("community_search_protocol")) !=
            (turn["id"], "conversation_web_decision", AUTOMATIC) or result.get("need_web") is not True):
        raise RuntimeErrorBase("native_public_task_scope_changed")
