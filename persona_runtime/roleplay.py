"""Prepare roleplay from one persona and its authorized story sources."""
import json

from .archive import encoded, read_exact, turn_scope
from .models import ROLEPLAY, RuntimeErrorBase
from .roleplay_prompts import require_approved

TURN_MODE = {"chat_mode": ROLEPLAY}
RECENT_TURNS = 20
CANON_BUDGET = 6000


def is_turn(turn):
    return json.loads(turn.get("context_policy_snapshot") or "{}") == TURN_MODE


def admit(paths, channel, model, *, public_lookup=False):
    if channel != "desktop" or public_lookup:
        raise RuntimeErrorBase("roleplay_desktop_story_only")
    require_approved(paths, model, "reply")


def _quoted_material(db, turn):
    from .quote_context import manifest, projection, read
    window = read(db, turn)
    if not window:
        return {}, {}, []
    quote = projection(window["anchor"])
    nearby = [projection(row) for name in ("before", "after", "latest") for row in window[name]]
    # The exact quoted message is mandatory. Nearby lines are optional context.
    return {"quote": quote}, manifest(window), nearby


def _history(db, turn, remaining, omit):
    from .import_history import chronological
    scope = turn_scope(db, turn)
    rows = db.execute("""SELECT id FROM turns WHERE persona_id=? AND session_id=?
        AND status='completed' AND rowid<(SELECT rowid FROM turns WHERE id=?)
        ORDER BY rowid DESC LIMIT ?""", (turn["persona_id"], turn["session_id"], turn["id"], RECENT_TURNS))
    groups, messages = [], []
    for item in rows:
        if item["id"] in omit:
            continue
        group = read_exact(db, scope, {"kind": "turn", "id": item["id"]})
        if not group.get("usable"):
            continue
        input_ids = {event["id"] for event in group["members"]}
        batch = [{"role": "user" if event["id"] in input_ids else "assistant", "content": event["text"]}
                 for event in chronological(group) if event["text"]]
        size = sum(len(event["content"]) for event in batch)
        if size > remaining:
            break
        remaining -= size
        groups.append({"turn_id": item["id"], "kind": "story_history"})
        messages.append(batch)
    return [msg for group in reversed(messages) for msg in group], groups


def prepare(engine, db, turn):
    from .context_builders import memory_usable, summary_usable
    from .context_memory import rank_memories
    from .history_context import capture, validate
    from .organization import latest_summary
    from .profile import persona_prompt_fields, source_blocked
    from .import_card_publish import uses_sparse_card
    from .quote_context import branch_prefix, projection
    from .turn_input import load_turn_input

    model = json.loads(turn["model_snapshot"])
    checksum, prompts = require_approved(engine.store.paths, model, "reply")
    frozen = json.loads(turn["profile_snapshot"])
    members = load_turn_input(db, turn)
    current = [{"role": "user", "content": row["text"]} for row in members]
    if not current:
        raise RuntimeErrorBase("roleplay_input_missing")
    material, references, nearby = _quoted_material(db, turn)
    material["persona"] = persona_prompt_fields(
        frozen["core"]["data"] | {"expression_style": frozen["expression"]["data"]["style"]})
    relation = frozen["relationship"]
    if relation["data"]["description"] and not source_blocked(db, relation["source_ids"]):
        material["relationship"] = relation["data"]["description"]
    template = prompts["reply"] + "\n"
    limit = model["max_input_chars"]
    input_cost = sum(len(row["content"]) for row in current)

    def system(value):
        return template + encoded(value)

    if len(system(material)) + input_cost > limit:
        raise RuntimeErrorBase("roleplay_input_too_long")

    details = {"dialogue_voice": ROLEPLAY, "roleplay_prompt_sha256": checksum, "references": references}

    def include(key, value, room=limit):
        proposed = material | {key: value}
        if value and len(system(proposed)) + input_cost <= room:
            material[key] = value
            return True
        return False

    optional_room = min(limit, len(system(material)) + input_cost + CANON_BUDGET)
    character = json.loads(turn.get("character_snapshot") or "{}").get("data") or {}
    character_data = {key: value for key, value in character.items()
                      if key in ("identity", "age", "appearance", "biography", "interests", "social", "world", "lorebook") and value}
    if uses_sparse_card(db, turn["persona_id"]):
        character_data = {
            key: (persona_prompt_fields(value) if isinstance(value, dict) else value)
            for key, value in character_data.items()
        }
        if character_data.get("age", {}).get("policy") == "unspecified":
            del character_data["age"]["policy"]
        character_data = {key: value for key, value in character_data.items() if value}
    if not include("character", character_data, optional_room):
        include("world", character_data.get("world"), optional_room)
    include("quote_context", nearby, optional_room)
    branch = branch_prefix(db, turn)
    if include("branch_prefix", [projection(item) for item in branch], optional_room):
        details["branch_message_ids"] = [item["id"] for item in branch]

    summary = latest_summary(db, turn["session_id"])
    if summary and summary.get("reason") == "automatic_memory" and summary_usable(db, turn, summary):
        items = json.loads(summary["items_json"])
        for count in range(min(len(items), 20), 0, -1):
            if include("story_summary", items[-count:], optional_room):
                details.update(summary_id=summary["id"], summary_revision=summary["revision"])
                break
    recent = []
    for ranked in rank_memories(db, turn, current[-1]["content"], []):
        memory = ranked[3]
        if memory["category"] not in ("shared_experience", "world_knowledge") or not memory_usable(db, turn, memory):
            continue
        foreign = db.execute("""SELECT 1 FROM events WHERE session_id<>? AND id IN (
            SELECT source_event_id FROM memories WHERE id=? UNION SELECT event_id FROM memory_sources WHERE memory_id=?)""",
            (turn["session_id"], memory["id"], memory["id"])).fetchone()
        if foreign:
            continue
        candidate = recent + [memory]
        if include("story_memories", [row["text"] for row in candidate], optional_room):
            recent = candidate
        if len(recent) == 8:
            break
    if recent:
        details["memories"] = [{"id": m["id"], "revision": m["revision"]} for m in recent]

    system_message = {"role": "system", "content": system(material)}
    remaining = limit - len(system_message["content"]) - input_cost
    history, groups = _history(db, turn, remaining, set(references.get("turn_ids", [])))
    details["history_groups"] = groups
    details["profile_versions"] = {k: {"id": frozen[k]["id"], "revision": frozen[k]["revision"]}
                                  for k in ("core", "expression", "relationship")}
    proof = capture(db, turn, details)
    if proof:
        validate(db, turn, proof)
        details["import_history_sources"] = proof
    messages = [system_message, *history, *current]
    manifest = engine.cognitive._manifest(db, turn, messages, details)
    manifest["roleplay_prompt_sha256"] = checksum
    return {"messages": messages, "details": details, "manifest": manifest}
