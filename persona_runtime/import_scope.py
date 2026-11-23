"""Validate a local selection before storing or analyzing chat material."""
from collections import Counter

from .corpus_files import digest
from .deepseek_export import markdown
from .import_library import require
from .import_markdown import scope_markdown
from .models import chat_mode

LARGE_IMPORT = 5000


def check_count(total, count, confirmed):
    require((count is None or type(count) is int and 1 <= count <= total) and total > 0,
            "请输入有效的导入条数。")
    count = total if count is None else count
    require(count <= LARGE_IMPORT or confirmed is True, "请确认超过 5000 条消息的导入。")
    return count


def selected(source, count, mode, confirmed):
    mode = chat_mode(mode)
    if "deepseek" not in source:
        parsed, raw = source["parsed"], source["raw"]
        count = check_count(len(parsed["rows"]), count, confirmed)
        return scope_markdown(raw, parsed, count, mode), None

    deepseek = source["deepseek"]
    rows = deepseek["rows"]
    count = check_count(len(rows), count, confirmed)
    window = rows[-count:]
    counts = dict(Counter(speaker for speaker, _, _ in window))
    require(len(counts) == 2, "这段范围缺少一方的正文，请扩大导入条数。")
    dates = [date for _, date, _ in window if date]
    summary = deepseek["conversation"] | {
        "counts": counts, "from": dates[0] if dates else None, "to": dates[-1] if dates else None,
        "text_count": len(rows), "imported_count": count, "source_sha256": digest(deepseek["raw"]),
    }
    raw = markdown(window, summary["id"], summary["branch"], mode)
    return raw, (deepseek["raw"] if count == len(rows) else b"", summary)
