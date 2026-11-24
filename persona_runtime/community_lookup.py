"""Explicit public search for a standard Community session."""
import json

from .archive import encoded, fingerprint, scope
from .models import RuntimeErrorBase
from .sessions import owned

PROTOCOL = "community-explicit-search-v1"
ENDPOINT = "https://api.deepseek.com/v1/tools/search"


def outcome(retrieval):
    if retrieval["search_outcome"] == "completed":
        return "found" if retrieval["material_acquired"] else "empty"
    if retrieval["search_outcome"] == "unconfirmed":
        return "unconfirmed"
    if not retrieval["task_ids"] or "provider_network_error" in retrieval.get("errors", []):
        return "not_dispatched"
    return "failed"


def state(db, config, cfg, pid, sid):
    from . import community
    from .private_reply_entry import selected
    from .provider_policy import is_official_deepseek
    from .session_privacy import destination, _witness

    owned(db, sid, scope(pid, "desktop"))
    model = cfg["model"]
    instance = cfg["instance_id"]
    available = bool(community.enabled() and model.get("encrypted_key") and is_official_deepseek(model)
                     and not selected(db, instance, pid))
    binding = fingerprint([PROTOCOL, instance, pid, sid, ENDPOINT, destination(config, model)]) if available else None
    key = "community.search.consent." + fingerprint([instance, pid, sid])
    path = _witness(config, cfg, sid).with_suffix(".community-search.json")
    row = db.execute("SELECT value FROM knowledge_control WHERE key=?", (key,)).fetchone()
    stored = json.loads(row[0]) if row else {}
    witness = json.loads(path.read_bytes()) if path.is_file() else {}
    return {"protocol": PROTOCOL, "available": available, "endpoint": ENDPOINT,
            "revision": max(stored.get("revision", 0), witness.get("revision", 0)),
            "recipient_hash": binding,
            "enabled": bool(available and stored == witness and stored.get("binding") == binding
                            and stored.get("enabled"))}, key, path


def update(db, config, cfg, pid, sid, enabled, revision, recipient_hash, operation_id):
    from .config import atomic_json

    current, key, path = state(db, config, cfg, pid, sid)
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
    return state(db, config, cfg, pid, sid)[0]


def require_consent(db, config, pid, sid, consent):
    if config is None:
        raise RuntimeErrorBase("community_search_consent_required")
    cfg = config.load()
    current, _, _ = state(db, config, cfg, pid, sid)
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
