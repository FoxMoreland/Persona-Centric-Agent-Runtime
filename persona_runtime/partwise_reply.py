"""Number and bind the sources of each multi-item conversational answer."""

import json
from dataclasses import dataclass


@dataclass(frozen=True)
class PreparedReply:
    packet: dict
    sources: dict


@dataclass(frozen=True)
class AssembledReply:
    body: str
    bindings: tuple


def prepare(packet):
    research = packet.get("research", {})
    retrieval = research.get("retrieval", {})
    items = retrieval.get("items", [])
    if (
        packet.get("phase") != "main"
        or packet.get("mode") != "short_answer"
        or packet.get("action") != "respond"
        or research.get("status") != "local_hit"
        or research.get("lookup_outcome") != "studied"
        or retrieval.get("tool") != "acquired"
        or len(items) < 2
    ):
        raise ValueError("partwise_reply_ineligible")

    facts = research.get("facts") or []
    by_id = {fact["id"]: fact for fact in facts}
    selected = {fact_id for item in items for fact_id in item["facts"]}
    if len(by_id) != len(facts) or selected != set(by_id):
        raise ValueError("partwise_reply_source_changed")
    known_facts = {}
    sources = {}
    groups = []
    for item in items:
        item_id = item["id"]
        if type(item_id) is not int or item_id in sources:
            raise ValueError("partwise_reply_duplicate_item")
        ids = item["facts"]
        if item["result"] not in ("partial", "complete") or not ids or len(ids) != len(set(ids)):
            raise ValueError("partwise_reply_group_ineligible")
        numbered = []
        source_map = {}
        for fact_id in ids:
            fact = by_id[fact_id]
            original, number = known_facts.setdefault(fact_id, (fact, len(known_facts) + 1))
            if fact != original or "source_number" in fact:
                raise ValueError("partwise_reply_source_changed")
            source_map[number] = fact_id
            numbered.append(fact | {"source_number": number})
        groups.append({key: item[key] for key in ("id", "question", "result")} | {"facts": numbered})
        sources[item_id] = source_map

    constraints = packet["output_constraints"] | {
        "output_fields": ["items"],
    }
    return PreparedReply(packet | {
        "evidence": groups + packet["evidence"],
        "research": {key: value for key, value in research.items() if key != "facts"},
        "output_constraints": constraints,
    }, sources)


def _unique_json_object(pairs):
    result = dict(pairs)
    if len(result) != len(pairs):
        raise ValueError("partwise_reply_duplicate_field")
    return result


def assemble(raw, prepared):
    value = json.loads(raw, object_pairs_hook=_unique_json_object)
    if not isinstance(value, dict) or set(value) != {"items"} or not isinstance(value["items"], list):
        raise ValueError("partwise_reply_invalid_shape")
    answers = {}
    bindings = {}
    for item in value["items"]:
        if not isinstance(item, dict) or set(item) != {"id", "body", "sources"}:
            raise ValueError("partwise_reply_invalid_shape")
        item_id = item["id"]
        if type(item_id) is not int or item_id not in prepared.sources or item_id in answers:
            raise ValueError("partwise_reply_invalid_item")
        body = item["body"]
        if not isinstance(body, str) or not body.strip():
            raise ValueError("partwise_reply_missing_body")
        source_numbers = item["sources"]
        source_map = prepared.sources[item_id]
        if (
            not isinstance(source_numbers, list)
            or not source_numbers
            or any(type(number) is not int or number not in source_map for number in source_numbers)
            or len(set(source_numbers)) != len(source_numbers)
        ):
            raise ValueError("partwise_reply_invalid_sources")
        answers[item_id] = body.strip()
        bindings[item_id] = tuple(source_map[number] for number in source_numbers)
    if answers.keys() != prepared.sources.keys():
        raise ValueError("partwise_reply_incomplete")
    body = "\n".join(answers[item_id] for item_id in prepared.sources)
    return AssembledReply(
        body,
        tuple((item_id, bindings[item_id]) for item_id in prepared.sources),
    )
