"""Use the acceptance APK's one-shot local relay to seal a key in the emulator."""

import argparse
import json
from pathlib import Path
from queue import Empty, Queue
import re
import secrets
import socket
import struct
import subprocess
from threading import Thread

from persona_runtime.provider_policy import is_official_deepseek
from persona_runtime.security import unprotect


RUNNER = ("io.github.foxmoreland.pcar.community.debug.test/"
          "io.github.foxmoreland.pcar.workbench.StageThreeProbe")


def adb(adb_path, serial, *args):
    return subprocess.run([str(adb_path), "-s", serial, *args], check=True, capture_output=True,
                          text=True, encoding="utf-8", timeout=15).stdout.strip()


def key_bytes(config):
    if config is None:
        return bytearray(b"pcar-stage3-local-fixture")
    model = json.loads(config.read_bytes())["model"]
    if not is_official_deepseek(model) or not model.get("encrypted_key"):
        raise ValueError("The approved config does not contain an official DeepSeek credential")
    return bytearray(unprotect(model["encrypted_key"]), "utf-8")


def seal(args):
    if not re.fullmatch(r"emulator-\d+", args.serial):
        raise ValueError("Select a local Android Emulator")
    if args.avd not in adb(args.adb, args.serial, "emu", "avd", "name").splitlines():
        raise ValueError("Unexpected Android Virtual Device")
    if args.config and not args.expected_proxy:
        raise ValueError("The official-key acceptance requires a configured Android HTTP proxy")
    challenge = secrets.token_hex(32)
    command = [str(args.adb), "-s", args.serial, "shell", "am", "instrument", "-w",
               "-e", "mode", "credential", "-e", "challenge", challenge]
    if args.expected_proxy:
        command += ["-e", "expected_proxy", args.expected_proxy]
    command.append(RUNNER)
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                               encoding="utf-8", text=True)
    replies = []
    ports = Queue()

    def receive():
        try:
            for line in process.stdout:
                replies.append(line.rstrip())
                found = re.fullmatch(r"INSTRUMENTATION_STATUS: relay_port=(\d+)\s*", line)
                if found:
                    ports.put(found[1])
        finally:
            ports.put(None)

    reader = Thread(target=receive, daemon=True)
    reader.start()
    forward = None
    try:
        try:
            destination = ports.get(timeout=20)
        except Empty:
            destination = None
        if destination is None:
            error = next((line.rsplit("=", 1)[-1] for line in replies if "stage_error=" in line),
                         "timeout")
            raise RuntimeError("Android acceptance relay did not start: " + error)
        forward = adb(args.adb, args.serial, "forward", "tcp:0", f"tcp:{destination}")
        if not forward.isdecimal():
            raise RuntimeError("ADB did not select a local relay port")
        material = key_bytes(args.config)
        try:
            if not 1 <= len(material) <= 512 or any(value < 33 for value in material):
                raise ValueError("The approved key has an unexpected format")
            with socket.create_connection(("127.0.0.1", int(forward)), timeout=8) as connection:
                connection.settimeout(60)
                connection.sendall(challenge.encode("ascii") + struct.pack(">H", len(material)))
                connection.sendall(material)
                if connection.recv(1) != b"\x01":
                    raise RuntimeError("Android did not confirm Keystore persistence")
        finally:
            material[:] = bytes(len(material))
        process.wait(timeout=12)
        reader.join(timeout=3)
        if "INSTRUMENTATION_RESULT: credential_sealed=true" not in replies:
            raise RuntimeError("Android acceptance credential receipt is missing")
        print(json.dumps({"sealed": True, "official_configuration": args.config is not None,
                          "android_proxy_verified": bool(args.expected_proxy)}))
    finally:
        if forward is not None and forward.isdecimal():
            adb(args.adb, args.serial, "forward", "--remove", "tcp:" + forward)
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=5)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--adb", required=True, type=Path)
    parser.add_argument("--serial", required=True)
    parser.add_argument("--avd", required=True)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--config", type=Path)
    mode.add_argument("--fixture", action="store_true")
    parser.add_argument("--expected-proxy")
    seal(parser.parse_args())


if __name__ == "__main__":
    main()
