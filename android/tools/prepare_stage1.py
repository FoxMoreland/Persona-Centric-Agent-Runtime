"""Build stage-one test assets and freeze the Windows Flash token counts."""

import argparse
import hashlib
import json
from pathlib import Path
import shutil

from persona_runtime.deepseek_card_budget import input_limit, input_tokens


ORT_SHA256 = "d4d50660300ed2c4f38ffdfd5ab5bfa3a22f995f282dda24f42ea3ea55467b49"


def sha256(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def copy_checked(src, dst, expected):
    if sha256(src) != expected:
        raise ValueError(f"Unexpected SHA-256 for {src.name}")
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src, dst)


def cases(fixture):
    text = fixture["queries"][0]["text"]
    token_sample = fixture["lexical"][1]["text"]
    return [
        {"id": "empty", "system": "", "unit": "", "n": 0, "suffix": ""},
        {"id": "short", "system": "", "unit": text, "n": 1, "suffix": ""},
        {"id": "unicode", "system": token_sample, "unit": token_sample, "n": 1, "suffix": ""},
        {"id": "240k", "system": "", "unit": text, "n": 240000 // len(text),
         "suffix": text[:240000 % len(text)]},
        {"id": "at_limit", "system": "", "unit": "0\n", "n": 418608, "suffix": "0"},
        {"id": "over_limit", "system": "", "unit": "0\n", "n": 418609, "suffix": ""},
    ]


def make_messages(case):
    return [{"content": case["system"]}, {"content": case["unit"] * case["n"] + case["suffix"]}]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--bge", required=True, type=Path)
    parser.add_argument("--ort", required=True, type=Path)
    parser.add_argument("--ort-licenses", required=True, type=Path)
    parser.add_argument("--fixture", required=True, type=Path)
    parser.add_argument("--windows-reference", required=True, type=Path)
    parser.add_argument("--prepared", required=True, type=Path)
    args = parser.parse_args()

    assets = args.prepared / "assets"
    work = assets / "stage1"
    work.mkdir(parents=True, exist_ok=True)
    metadata = json.loads((args.source / "model-assets.json").read_bytes())["character_embedding"]
    for name, expected in metadata["files"].items():
        copy_checked(args.bge / name, assets / "bge" / name, expected)
    copy_checked(args.ort, args.prepared / "onnxruntime-android-1.31.1.aar", ORT_SHA256)
    licenses = assets / "licenses"
    licenses.mkdir(exist_ok=True)
    shutil.copyfile(args.source / "LICENSE", licenses / "PCAR-AGPL-3.0.txt")
    shutil.copyfile(args.source / "docs/licenses/FlagEmbedding-MIT.txt", licenses / "FlagEmbedding-MIT.txt")
    for name in ("onnxruntime-LICENSE", "onnxruntime-ThirdPartyNotices.txt"):
        shutil.copyfile(args.ort_licenses / name, licenses / name)
    shutil.copyfile(args.source / "persona_runtime/deepseek_tokenizer_assets/LICENSE.txt",
                    licenses / "DeepSeek-tokenizer-LICENSE.txt")

    fixture_bytes = args.fixture.read_bytes()
    fixture = json.loads(fixture_bytes)
    baseline = json.loads(args.windows_reference.read_bytes())
    assert hashlib.sha256(fixture_bytes).hexdigest() == baseline["fixture_sha256"]
    assert baseline["model_fingerprint"] == metadata["fingerprint"]
    assert baseline["model_sha256"] == metadata["files"]["model_optimized.onnx"]
    shutil.copyfile(args.fixture, work / "vector-fixture.json")
    shutil.copyfile(args.windows_reference, work / "vector-reference.json")
    threshold = input_limit(2048)
    flash = cases(fixture)
    for case in flash:
        messages = make_messages(case)
        case["expected"] = input_tokens(messages)
        print(case["id"], len(messages[1]["content"]), case["expected"])
    assert flash[-2]["expected"] == threshold and flash[-1]["expected"] == threshold + 1
    (work / "flash-reference.json").write_text(json.dumps({"max_output_tokens": 2048,
        "limit": threshold, "cases": flash}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
