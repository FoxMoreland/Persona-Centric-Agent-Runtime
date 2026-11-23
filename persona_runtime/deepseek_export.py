"""Read one selected conversation from a DeepSeek v3 export without extracting the bundle."""
from datetime import datetime
import json
from pathlib import Path, PurePosixPath
import re
import zipfile

from .corpus_files import CorpusError, MAX_FILE_BYTES
from .corpus_moect import MAX_RECORDS
from .import_markdown import DEEPSEEK_MD, DEEPSEEK_FORMAT, mode_header
from .models import PRIVATE_CHAT


SESSION_ID = re.compile(r"[A-Za-z0-9_-]{1,128}")
ROLES = {"USER": ("用户", "REQUEST"), "ASSISTANT": ("DeepSeek", "RESPONSE")}


def _unique_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise CorpusError("导出文件包含重复字段。")
        result[key] = value
    return result


def _read(archive, name):
    info = archive.getinfo(name)
    if not 0 < info.file_size <= MAX_FILE_BYTES:
        raise CorpusError("导出条目为空或超过 64 MiB。")
    with archive.open(info) as stream:
        raw = stream.read(MAX_FILE_BYTES + 1)
    if len(raw) != info.file_size:
        raise CorpusError("导出条目大小不符。")
    return raw, json.loads(raw, object_pairs_hook=_unique_keys)


def _entries(archive):
    entries = archive.infolist()
    if len(entries) > MAX_RECORDS:
        raise CorpusError("导出 ZIP 包含过多条目。")
    names = set()
    for entry in entries:
        name = entry.filename
        parts = PurePosixPath(name).parts
        folded = name.casefold()
        if (not parts or name.startswith("/") or "\\" in name or ":" in name
                or ".." in parts or folded in names or entry.flag_bits & 1):
            raise CorpusError("导出 ZIP 中存在无效、重复或加密的条目。")
        names.add(folded)


def _date(value):
    if not isinstance(value, str) or not value:
        return None
    try:
        date = datetime.fromisoformat(value)
    except ValueError:
        return None
    return date.isoformat() if date.tzinfo else None


def _index(archive):
    _, manifest = _read(archive, "manifest.json")
    if not isinstance(manifest, dict) or type(manifest.get("export_format_version")) is not int or manifest["export_format_version"] != 3:
        raise CorpusError("请选择 DeepSeek 官方数据导出 ZIP v3。")
    _, entries = _read(archive, "chat_session_index.json")
    if not isinstance(entries, list) or len(entries) > MAX_RECORDS:
        raise CorpusError("DeepSeek 会话索引无效。")
    result = {}
    for entry in entries:
        ident = entry.get("id") if isinstance(entry, dict) else None
        if not isinstance(ident, str) or not SESSION_ID.fullmatch(ident) or ident in result:
            raise CorpusError("DeepSeek 会话编号无效或重复。")
        title, count = entry.get("title"), entry.get("message_count")
        result[ident] = {"id": ident, "title": title[:240] if isinstance(title, str) and title.strip() else ident,
                         "updated": _date(entry.get("updated_at")),
                         "count": count if type(count) is int and count >= 0 else None}
    return result


def _messages(data, ident):
    if not isinstance(data, dict) or data.get("id") != ident:
        raise CorpusError("会话原件与索引不一致。")
    messages = data.get("messages")
    if not isinstance(messages, list) or not 0 < len(messages) <= MAX_RECORDS:
        raise CorpusError("会话为空或超过现有消息数上限。")
    nodes = {}
    for message in messages:
        ident = message.get("id") if isinstance(message, dict) else None
        parent = message.get("parent_id") if isinstance(message, dict) else None
        if (type(ident) is not int or ident < 0 or ident in nodes
                or "parent_id" not in message or parent is not None and (type(parent) is not int or parent < 0)
                or message.get("role") not in ("USER", "ASSISTANT")
                or not isinstance(message.get("fragments"), list)):
            raise CorpusError("DeepSeek 消息树无效。")
        nodes[ident] = message
    return nodes


def _branch(nodes, leaf):
    seen = set()
    chain = []
    while leaf is not None:
        if leaf in seen or leaf not in nodes:
            raise CorpusError("所选分支存在循环或缺失消息。")
        seen.add(leaf)
        message = nodes[leaf]
        chain.append(message)
        leaf = message["parent_id"]
    return list(reversed(chain))


def _valid_leaves(nodes):
    parents = {node["parent_id"] for node in nodes.values()}
    validity = {}
    for ident in nodes:
        path = set()
        cursor = ident
        while cursor in nodes and cursor not in path and cursor not in validity:
            path.add(cursor)
            cursor = nodes[cursor]["parent_id"]
        good = cursor is None or validity.get(cursor, False)
        validity.update(dict.fromkeys(path, good))
    return [ident for ident in reversed(nodes) if ident not in parents and validity[ident]]


def _text_rows(chain):
    rows = []
    for node in chain:
        speaker, fragment_type = ROLES[node["role"]]
        if node["role"] == "ASSISTANT" and node.get("status") != "FINISHED":
            continue
        fragments = node["fragments"]
        if any(not isinstance(fragment, dict) for fragment in fragments):
            raise CorpusError("DeepSeek 消息片段无效。")
        content = [fragment.get("content") for fragment in fragments if fragment.get("type") == fragment_type]
        if any(not isinstance(part, str) for part in content):
            raise CorpusError("DeepSeek 正文片段无效。")
        body = "".join(content)
        if body.strip():
            rows.append((speaker, _date(node.get("inserted_at")), body))
    return rows


def markdown_messages(rows):
    return "".join(f"## {speaker + (' [' + date + ']') if date else speaker}\n<!-- {len(body)} -->\n{body}\n\n"
                   for speaker, date, body in rows)


def markdown(rows, ident, leaf, mode=PRIVATE_CHAT):
    raw = (mode_header(mode) + DEEPSEEK_MD + f"<!-- {ident} {leaf} -->\n" + markdown_messages(rows)).encode("utf-8")
    if len(raw) > MAX_FILE_BYTES:
        raise CorpusError("转换后的 Markdown 超过 64 MiB。")
    return raw


def revise_markdown(raw, ident, branch, edits):
    from .import_markdown import parse

    parsed = parse(raw)
    if parsed["format"] != DEEPSEEK_FORMAT:
        raise CorpusError("请选择要审阅的 DeepSeek Markdown。")
    if not isinstance(edits, dict) or any(not isinstance(v, str) for v in edits.values()):
        raise CorpusError("Markdown 编辑内容无效。")
    rows = parsed["rows"]
    if set(edits) - {row["id"] for row in rows}:
        raise CorpusError("消息编号已变化，请重新打开审阅。")
    revised = [(row["speaker"], row["date"], edits.get(row["id"], row["original"])) for row in rows]
    revised = [row for row in revised if row[2].strip()]
    if {row[0] for row in revised} != {"用户", "DeepSeek"}:
        raise CorpusError("建卡需要保留用户和 DeepSeek 双方的正文。")
    rendered = markdown(revised, ident, branch, parsed.get("chat_mode", PRIVATE_CHAT))
    parse(rendered)
    return rendered


def inspect(path, ident=None, branch=None, *, prepare=False):
    """Return the index or inspect one conversation with an optional explicit branch."""
    try:
        with Path(path).open("rb") as stream, zipfile.ZipFile(stream) as archive:
            _entries(archive)
            index = _index(archive)
            if ident is None:
                return {"conversations": sorted(index.values(), key=lambda row: row["updated"] or "", reverse=True)}
            if not isinstance(ident, str) or ident not in index:
                return {"missing": True}
            raw, data = _read(archive, f"chat_sessions/{ident}.json")
            nodes = _messages(data, ident)
            leaf = data.get("current_message_id") if branch is None else branch
            if type(leaf) is not int or leaf not in nodes:
                if branch is not None:
                    raise CorpusError("请选择导出包中可还原的分支。")
                choices = _valid_leaves(nodes)
                if not choices:
                    raise CorpusError("该会话没有可还原的分支。")
                return {"branches": [{"id": i, "date": _date(nodes[i].get("inserted_at"))} for i in choices],
                        "conversation": index[ident]}
            chain = _branch(nodes, leaf)
            rows = _text_rows(chain)
            dates = [date for _, date, _ in rows if date]
            counts = {role: sum(speaker == role for speaker, _, _ in rows) for role, _ in ROLES.values()}
            summary = index[ident] | {"branch": leaf, "from": dates[0] if dates else None,
                                     "to": dates[-1] if dates else None, "counts": counts,
                                     "source_count": len(chain)}
            selected = {"conversation": summary, "raw": raw, "format": DEEPSEEK_FORMAT}
            if prepare:
                return selected | {"rows": rows}
            return selected | {"markdown": markdown(rows, ident, leaf)}
    except (OSError, RuntimeError, ValueError, KeyError, RecursionError, zipfile.BadZipFile) as error:
        if isinstance(error, CorpusError):
            raise
        raise CorpusError("无法读取 DeepSeek 导出 ZIP 或所选会话。") from None
