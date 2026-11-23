"""Resolve author-approved roleplay prompts for a particular purpose."""
import json

from .character_embedding import safe_path
from .corpus_files import digest
from .models import RuntimeErrorBase

VERSION = "pcar-roleplay-prompts-v1"
PURPOSES = ("profile_extract", "profile_merge", "card_compose", "reply", "memory_extract", "memory_verify")
APPROVAL_REQUIRED = "roleplay_prompts_review_required"
OFFICIAL = "https://api.deepseek.com"


def official(model):
    return model.get("base_url", "").rstrip("/") in (OFFICIAL, OFFICIAL + "/v1")


def _read(root, file, limit):
    with safe_path(root, root / "private/persona-import" / file).open("rb") as stream:
        raw = stream.read(limit + 1)
    if len(raw) > limit:
        raise ValueError("roleplay_asset_too_large")
    return raw


def approved(paths, *purposes):
    root = paths.home
    try:
        raw = _read(root, VERSION + ".json", 64 * 1024)
        approval = json.loads(_read(root, "roleplay-approval.json", 4096))
        bundle = json.loads(raw)
        checksum = digest(raw)
        if (not isinstance(approval, dict) or approval.get("decision") != "approved"
                or approval.get("sha256") != checksum or approval.get("version") != VERSION
                or approval.get("base_url") != OFFICIAL or not isinstance(approval.get("purposes"), list)
                or any(p not in approval["purposes"] or p not in PURPOSES for p in purposes)
                or not isinstance(bundle, dict) or set(bundle) != {"version", *PURPOSES}
                or bundle["version"] != VERSION
                or any(not isinstance(bundle[p], str) or not bundle[p].strip() for p in PURPOSES)):
            return None
        return checksum, {p: bundle[p] for p in purposes}
    except (OSError, ValueError, RecursionError):
        return None


def require_approved(paths, model, *purposes):
    if not official(model):
        raise RuntimeErrorBase("roleplay_official_deepseek_required")
    bundle = approved(paths, *purposes)
    if bundle is None:
        raise RuntimeErrorBase(APPROVAL_REQUIRED)
    return bundle


def story_memory_contract(paths, model):
    checksum, prompts = require_approved(paths, model, "memory_extract", "memory_verify")
    return {"sha256": checksum, "extract": prompts["memory_extract"], "verify": prompts["memory_verify"]}
