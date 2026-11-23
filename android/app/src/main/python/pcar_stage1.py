"""Executable acceptance check for the embedded Community runtime."""

import asyncio
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import sqlite3
import ssl
import sys
import time
import traceback

from persona_runtime.android_platform import bind


def check(condition, reason):
    if not condition:
        raise RuntimeError(reason)


def schema_and_lock(paths):
    from persona_runtime.instance import InstanceLock, current_process_identity, process_identity_alive
    from persona_runtime.models import RuntimeErrorBase
    from persona_runtime.storage import Store

    store = Store(paths)
    store.initialize()
    with store.connection() as db:
        check(db.execute("PRAGMA user_version").fetchone()[0] == 36, "Schema is not version 36")
        check(db.execute("PRAGMA quick_check").fetchone()[0] == "ok", "SQLite database is inconsistent")
        check(db.execute("PRAGMA foreign_keys").fetchone()[0] == 1, "Foreign keys are disabled")
        check(db.execute("SELECT json_extract(?, '$.n')", ('{"n":7}',)).fetchone()[0] == 7,
              "SQLite JSON functions are unavailable")
        db.execute("CREATE VIRTUAL TABLE temp.fts_probe USING fts5(content)")
        db.execute("INSERT INTO temp.fts_probe VALUES ('stage-one')")
        check(db.execute("SELECT count(*) FROM temp.fts_probe WHERE fts_probe MATCH 'stage'").fetchone()[0] == 1,
              "FTS5 did not find the indexed text")
    lock_path = paths.home / "runtime" / "stage1.lock"
    held = InstanceLock(lock_path)
    try:
        try:
            unexpected = InstanceLock(lock_path)
        except RuntimeErrorBase:
            pass
        else:
            unexpected.close()
            raise RuntimeError("An existing runtime lock was bypassed")
    finally:
        held.close()
    InstanceLock(lock_path).close()
    process = current_process_identity()
    check(process_identity_alive(process)
          and not process_identity_alive(process | {"process_token": ""}),
          "Process ownership did not distinguish a stale runtime")
    return {"schema": 36, "sqlite": sqlite3.sqlite_version, "json": True, "fts5": True, "lock": True,
            "process_identity": True}


def credentials(paths):
    from persona_runtime.config import ConfigStore
    from persona_runtime.security import protect, unprotect

    config = ConfigStore(paths)
    config.initialize()
    token = config.token()
    ciphertext = config.load()["admin_token"]
    check(ciphertext.startswith("android-keystore-v1:") and token not in ciphertext,
          "Local service token was not encrypted by the Android KeyStore")
    second = protect(token)
    check(second != ciphertext and unprotect(second) == token, "Encrypted local token round trip failed")
    version, nonce, data = second.split(":")
    tampered = version + ":" + nonce + ":" + ("A" if data[0] != "A" else "B") + data[1:]
    try:
        unprotect(tampered)
    except OSError:
        pass
    else:
        raise RuntimeError("Tampered local token was accepted")
    check(ConfigStore(paths).token() == token, "Local service token did not survive config readback")
    return {"roundtrip": True, "gcm_integrity": True}


async def service(paths, assets):
    import aiohttp
    from aiohttp import web
    import certifi
    from persona_runtime.server import create_app

    app = create_app(paths, foreground_only=True)
    runner = web.AppRunner(app, access_log=None, shutdown_timeout=3)
    await runner.setup()
    try:
        await web.TCPSite(runner, "127.0.0.1", 0).start()
        http = f"http://127.0.0.1:{runner.addresses[0][1]}/v1/health?view=desktop"
        tls_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        tls_context.load_cert_chain(assets / "loopback.crt", assets / "loopback.key")
        await web.TCPSite(runner, "127.0.0.1", 0, ssl_context=tls_context).start()
        https = f"https://127.0.0.1:{runner.addresses[1][1]}/v1/health?view=desktop"
        headers = {"Authorization": "Bearer " + app["config"].token(),
                   "X-PCAR-Instance": app["instance_id"]}
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=5), trust_env=False) as client:
            async with client.get(http) as response:
                check(response.status == 401, "Loopback service accepted a request without a token")
            async with client.get(http, headers=headers | {"Origin": "https://fixture.invalid"}) as response:
                check(response.status == 403, "Loopback service accepted a browser origin")
            async with client.get(http, headers=headers) as response:
                check(response.status == 200 and (await response.json())["service"] == "persona-runtime",
                      "Local HTTP health request failed")
            try:
                async with client.get(https, headers=headers,
                                      ssl=ssl.create_default_context(cafile=certifi.where())):
                    raise RuntimeError("Untrusted loopback certificate was accepted")
            except aiohttp.ClientConnectorCertificateError:
                pass
            trusted = ssl.create_default_context(cafile=str(assets / "loopback.crt"))
            async with client.get(https, headers=headers, ssl=trusted) as response:
                check(response.status == 200 and (await response.json())["instance_id"] == app["instance_id"],
                      "Local TLS health request failed")
    finally:
        await runner.cleanup()
    return {"aiohttp": aiohttp.__version__, "ca_bundle": certifi.__version__, "ssl": ssl.OPENSSL_VERSION,
            "http_health": 200, "https_health": 200, "missing_token": 401, "browser_origin": 403,
            "untrusted_certificate_rejected": True}


def flash(assets):
    from persona_runtime.deepseek_card_budget import input_limit, input_tokens

    reference = json.loads((assets / "flash-reference.json").read_bytes())
    limit = input_limit(reference["max_output_tokens"])
    check(limit == reference["limit"], "Flash context budget differs from Windows")
    observed = []
    for sample in reference["cases"]:
        text = sample["unit"] * sample["n"] + sample["suffix"]
        start = time.monotonic()
        count = input_tokens([{"content": sample["system"]}, {"content": text}])
        check(count == sample["expected"], f"Flash token count differs for {sample['id']}")
        observed.append({"id": sample["id"], "input_chars": len(text), "tokens": count,
                         "over_budget": count > limit, "seconds": round(time.monotonic() - start, 3)})
    check(not observed[-2]["over_budget"] and observed[-1]["over_budget"],
          "Flash context boundary did not admit/reject the expected counts")
    return {"limit": limit, "cases": observed}


def bge(bridge, paths, assets):
    from persona_runtime.character_embedding import file_hash, provider_for, vector_math
    from persona_runtime.installation import SOURCE_ROOT
    from persona_runtime.semantic_knowledge import rank

    manifest = json.loads((SOURCE_ROOT / "model-assets.json").read_bytes())["character_embedding"]
    model_root = paths.home / "models" / "character-embedding" / manifest["fingerprint"]
    model_root.mkdir(parents=True, exist_ok=True)
    for name, digest in manifest["files"].items():
        target = model_root / name
        if not target.is_file():
            bridge.installAsset(f"bge/{name}", str(target))
        check(file_hash(target) == digest, f"Embedded BGE asset failed verification: {name}")
    stamp = model_root / "manifest.json"
    if not stamp.exists():
        from persona_runtime.config import atomic_json
        atomic_json(stamp, manifest)

    fixture_bytes = (assets / "vector-fixture.json").read_bytes()
    fixture = json.loads(fixture_bytes)
    reference = json.loads((assets / "vector-reference.json").read_bytes())
    check(hashlib.sha256(fixture_bytes).hexdigest() == reference["fixture_sha256"],
          "Unexpected BGE fixture digest")
    provider = provider_for(paths)
    check(provider.identity()["fingerprint"] == reference["model_fingerprint"],
          "Android and Windows BGE fingerprints differ")
    vectors = vector_math()
    lowest_cosine = 1.0
    largest_norm_error = 0.0
    trace_count = 0
    vector_count = 0
    rows = fixture["documents"]
    inputs = [("D:" + row["id"], "\n".join([row["title"], *row["index_labels"], row["text"]]))
              for row in rows]
    inputs.extend(("Q:" + row["id"], row["text"]) for row in fixture["queries"])
    inputs.extend(("T:" + row["id"], row["text"]) for row in fixture["lexical"])
    for key, text in inputs:
        expected = reference["entries"][key]
        chunks = provider.chunks(text)
        with provider._model() as (_, tokenizer):
            raw = tokenizer.encode(text, add_special_tokens=False)
            check(raw.ids == expected["raw"]["ids"] and raw.offsets == [tuple(p) for p in expected["raw"]["offsets"]],
                  f"BGE raw token trace differs for {key}")
            check(len(chunks) == len(expected["chunks"]), f"BGE chunk count differs for {key}")
            for chunk, target in zip(chunks, expected["chunks"], strict=True):
                check(all(value == target[field] for field, value in chunk.items()),
                      f"BGE chunk offsets differ for {key}")
                tokens = tokenizer.encode(chunk["text"])
                check(tokens.ids == target["ids"] and tokens.offsets == [tuple(p) for p in target["offsets"]],
                      f"BGE chunk tokens differ for {key}")
        trace_count += 1
        if key.startswith("T:"):
            continue
        actual = provider.embed_documents(chunk["text"] for chunk in chunks)
        check(vectors.are_unit(actual, atol=0.0001), f"BGE norm differs for {key}")
        for vector, target in zip(actual, expected["chunks"], strict=True):
            prior = target["vector"]
            norm = math.sqrt(math.fsum(x * x for x in vector))
            other_norm = math.sqrt(math.fsum(x * x for x in prior))
            cosine = math.fsum(a * b for a, b in zip(vector, prior, strict=True)) / (norm * other_norm)
            lowest_cosine = min(lowest_cosine, cosine)
            largest_norm_error = max(largest_norm_error, abs(norm - 1))
            check(cosine >= 0.9999, f"BGE vector differs for {key}")
            vector_count += 1

    observed = {}
    for query in fixture["queries"]:
        args = (paths, query["text"], rows)
        options = {"one_per_document": query["one_per_document"]}
        expected = reference["queries"][query["id"]]["selected"]
        match = [row["id"] for row in rank(*args, **options)]
        again = [row["id"] for row in rank(*args, **options)]
        check(match == expected and again == expected, f"BGE retrieval differs for {query['id']}")
        observed[query["id"]] = match
    return {"model_sha256": manifest["files"]["model_optimized.onnx"], "traces": trace_count,
            "vectors": vector_count, "min_cosine": lowest_cosine, "max_norm_error": largest_norm_error,
            "selection": observed}


def run(bridge, home):
    report = {"python": platform.python_version(), "machine": platform.machine(),
              "page_bytes": os.sysconf("SC_PAGE_SIZE"), "platform": sys.platform}
    stage = "host"
    lock = None

    def step(name, check_step):
        nonlocal stage
        stage = name
        bridge.writeReport(json.dumps(report | {"status": "running", "stage": name},
                                      ensure_ascii=False, allow_nan=False))
        report[name] = check_step()

    try:
        bind(bridge)
        os.environ["PCAR_EDITION"] = "community"
        os.environ["PERSONA_RUNTIME_HOME"] = home
        from persona_runtime.paths import RuntimePaths
        from persona_runtime.instance import InstanceLock

        paths = RuntimePaths(Path(home))
        paths.initialize()
        lock = InstanceLock(paths.home / "runtime" / "service.lock")
        assets = paths.home / "stage1"
        stage = "test_resources"
        for name in ("vector-fixture.json", "vector-reference.json", "flash-reference.json",
                     "loopback.crt", "loopback.key"):
            bridge.installAsset(f"stage1/{name}", str(assets / name))
        step("schema", lambda: schema_and_lock(paths))
        step("credentials", lambda: credentials(paths))
        step("service", lambda: asyncio.run(service(paths, assets)))
        step("flash", lambda: flash(assets))
        step("bge", lambda: bge(bridge, paths, assets))
        report["status"] = "ok"
    except Exception as error:
        report.update(status="failed", stage=stage, error=str(error), trace=traceback.format_exc())
    finally:
        if lock is not None:
            lock.close()
    return json.dumps(report, ensure_ascii=False, allow_nan=False)

