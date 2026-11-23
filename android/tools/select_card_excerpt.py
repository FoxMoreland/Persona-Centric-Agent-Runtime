"""Freeze a recent dyadic card-test excerpt without altering message text."""

import argparse
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path

from persona_runtime.corpus_files import digest, inspect_bytes
from persona_runtime.import_markdown import parse


def position_of_line(text, number):
    return sum(len(line) for line in text.splitlines(keepends=True)[:number - 1])


def timeline(rows):
    local = timezone(timedelta(hours=8))
    values = [datetime.fromisoformat(row["date"]) for row in rows if row["date"]]
    return [value if value.tzinfo else value.replace(tzinfo=local) for value in values]


def select(raw, target, rounds):
    info = inspect_bytes(raw, ".md")
    encoding = info["encoding"]
    utf16 = encoding.startswith("utf-16")
    text = raw.decode("utf-16" if utf16 else encoding)
    parsed = parse(raw)
    if target not in parsed["counts"] or len(parsed["counts"]) != 2:
        raise ValueError("Expected exactly two speakers, including the specified card target")
    rows = parsed["rows"]
    timestamps = timeline(rows)
    if timestamps != sorted(timestamps):
        raise ValueError("The source is not in chronological order")

    starts = []
    previous = None
    for index, row in enumerate(rows):
        speaker = row["speaker"]
        if speaker not in parsed["counts"]:
            continue
        if speaker != previous:
            starts.append(index)
            previous = speaker
    responses = [left for left, right in zip(starts, starts[1:]) if rows[right]["speaker"] == target]
    if len(responses) < rounds:
        raise ValueError(f"Source contains {len(responses)} target-response exchanges, fewer than {rounds}")
    first = responses[-rounds]
    suffix = text[position_of_line(text, rows[first]["line"]):]
    if parsed["format"] == "Moect Markdown":
        header = "".join(text.splitlines(keepends=True)[:2])
        newline = "\r\n" if header.endswith("\r\n") else "\n"
        suffix = header + newline + suffix
    selected = raw[:2] + suffix.encode(encoding) if utf16 else suffix.encode(encoding)

    reread = parse(selected)
    core = ("speaker", "date", "text", "original")
    before = [tuple(row[key] for key in core) for row in rows[first:]]
    after = [tuple(row[key] for key in core) for row in reread["rows"]]
    if before != after:
        raise ValueError("Selected Markdown does not reproduce the source records exactly")
    dates = [row["date"] for row in reread["rows"] if row["date"]]
    return selected, {
        "source_sha256": digest(raw),
        "excerpt_sha256": digest(selected),
        "rounds": rounds,
        "definition": "One contiguous non-target speaker run followed by one target run",
        "source_message_start": first + 1,
        "source_message_end": len(rows),
        "excerpt_messages": len(after),
        "target_messages": reread["counts"][target],
        "first_at": dates[0] if dates else None,
        "last_at": dates[-1] if dates else None,
        "source_format": parsed["format"],
        "includes_tail": True,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("destination", type=Path)
    parser.add_argument("--target", required=True)
    parser.add_argument("--rounds", type=int, default=200)
    args = parser.parse_args()
    if args.rounds < 1 or args.destination.suffix.lower() != ".md":
        raise ValueError("Select a positive exchange count and a Markdown destination")
    if args.source.resolve() == args.destination.resolve():
        raise ValueError("The selected excerpt needs a separate destination")
    payload, manifest = select(args.source.read_bytes(), args.target, args.rounds)
    if args.destination.exists() or args.destination.with_suffix(".selection.json").exists():
        raise FileExistsError("Use a fresh path for a new frozen excerpt")
    args.destination.write_bytes(payload)
    args.destination.with_suffix(".selection.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(manifest, ensure_ascii=False))


if __name__ == "__main__":
    main()
