"""Check ABI, packaged model digests and 16 KiB ELF load segments."""

import argparse
import hashlib
import io
import json
from pathlib import Path
import struct
import subprocess
from zipfile import ZipFile


ARCHES = {"x86_64": 62, "arm64-v8a": 183}
PAGE = 16384
WEIGHTS_SHA = "1294ea4b6331115a353d81f96b85e8c8d7fdcc284453d5b2fab5b016230aad38"
NATIVE_LIBRARIES = {"libpython3.12.so", "libpcar_tokenizers.so", "libonnxruntime.so",
                    "libonnxruntime4j_jni.so"}


def elf_segments(data, name, architecture):
    if data[:6] != b"\x7fELF\x02\x01" or struct.unpack_from("<H", data, 18)[0] != architecture:
        raise ValueError(f"Unexpected ELF class or machine in {name}")
    position, size, count = (struct.unpack_from(fmt, data, offset)[0]
                              for fmt, offset in (("<Q", 32), ("<H", 54), ("<H", 56)))
    alignments = []
    for i in range(count):
        start = position + i * size
        if struct.unpack_from("<I", data, start)[0] != 1:
            continue
        offset, address = struct.unpack_from("<QQ", data, start + 8)
        alignment = struct.unpack_from("<Q", data, start + 48)[0]
        if alignment < PAGE or (address - offset) % PAGE:
            raise ValueError(f"{name} has a PT_LOAD segment incompatible with 16 KiB pages")
        alignments.append(alignment)
    if not alignments:
        raise ValueError(f"{name} has no PT_LOAD segments")
    return min(alignments)


def native_members(archive, prefix=""):
    for member in archive.infolist():
        if member.is_dir():
            continue
        if member.filename.endswith(".so"):
            yield prefix + member.filename, archive.read(member)
        elif member.filename.endswith((".imy", ".zip")):
            with archive.open(member) as entry:
                if entry.read(4) != b"PK\x03\x04":
                    continue
            with ZipFile(io.BytesIO(archive.read(member))) as nested:
                yield from native_members(nested, prefix + member.filename + "/")


def verify(apk, abi, zipalign):
    with ZipFile(apk) as archive:
        libs = [name.split("/") for name in archive.namelist() if name.startswith("lib/") and name.endswith(".so")]
        if {name[1] for name in libs} != {abi} or not NATIVE_LIBRARIES <= {name[2] for name in libs}:
            raise ValueError(f"{apk.name} has unexpected APK native libraries")
        with archive.open("assets/bge/model_optimized.onnx") as model:
            if hashlib.file_digest(model, "sha256").hexdigest() != WEIGHTS_SHA:
                raise ValueError(f"{apk.name} has unexpected BGE weights")
        records = [{"path": name, "minimum_load_alignment": elf_segments(data, name, ARCHES[abi])}
                   for name, data in native_members(archive)]
    subprocess.run([str(zipalign), "-c", "-P", "16", "4", str(apk)], check=True)
    with apk.open("rb") as file:
        digest = hashlib.file_digest(file, "sha256").hexdigest()
    return {"file": apk.name, "abi": abi, "bytes": apk.stat().st_size, "sha256": digest,
            "zipalign_16k": True, "elf": records}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--apk-dir", required=True, type=Path)
    parser.add_argument("--prefix", default="PCAR-Android-Stage1")
    parser.add_argument("--zipalign", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    results = [verify(args.apk_dir / f"{args.prefix}-{abi}.apk", abi, args.zipalign)
               for abi in ARCHES]
    args.output.write_text(json.dumps(results, indent=2) + "\n", encoding="utf-8")
    for result in results:
        print(result["file"], result["sha256"], len(result["elf"]), "16 KiB ELF libraries checked")


if __name__ == "__main__":
    main()
