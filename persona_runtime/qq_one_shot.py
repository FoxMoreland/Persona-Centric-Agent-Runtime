"""One-shot outbound QQ acceptance, scoped to a newly initialized test root."""

import argparse
import asyncio
from contextlib import closing
import getpass
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import warnings

import aiohttp

from .config import atomic_json
from .instance import InstanceLock
from .models import RuntimeErrorBase, generic_persona, identifier, timestamp
from .paths import RuntimePaths
from .qq_client import send_private_text
from .service import RuntimeService
from .storage import Store

SEND_TIMEOUT = 30
APP_NAME = "SnowLuma"
APP_VERSION = "2.2.0-node"


class AcceptanceError(RuntimeErrorBase):
    pass


def state_path(paths):
    return paths.home / "runtime" / "qq-one-shot-acceptance.json"


def initialize(home):
    home.mkdir(parents=True, exist_ok=False)
    paths = RuntimePaths(home)
    store = Store(paths)
    store.initialize()
    persona_id = RuntimeService(store).create_persona(generic_persona())["id"]
    state = {"schema": 1, "home": str(home), "persona_id": persona_id,
             "binding": None, "attempt": None}
    atomic_json(state_path(paths), state, durable=True)
    return state


def validate_binding(binding):
    sender, recipient = binding["sender"], binding["recipient"]
    port = binding["port"]
    if (not all(isinstance(value, str) and re.fullmatch(r"[1-9][0-9]{4,11}", value)
                for value in (sender, recipient))
            or sender == recipient or type(port) is not int or not 1 <= port <= 65535
            or binding["app_name"] != APP_NAME or binding["app_version"] != APP_VERSION):
        raise AcceptanceError("qq_acceptance_binding_invalid")


def load(paths):
    try:
        state = json.loads(state_path(paths).read_text(encoding="utf-8"))
        valid = (state["schema"] == 1 and state["home"] == str(paths.home)
                 and Store(paths).path.is_file())
        binding = state["binding"]
        if binding is not None:
            validate_binding(binding)
    except AcceptanceError:
        raise
    except (OSError, ValueError, TypeError, KeyError):
        valid = False
    if not valid:
        raise AcceptanceError("qq_acceptance_root_invalid")
    RuntimeService(Store(paths)).show_persona(state["persona_id"])
    return state


def instance_lock(paths):
    if not state_path(paths).is_file():
        raise AcceptanceError("qq_acceptance_root_invalid")
    return closing(InstanceLock(paths.home / "runtime" / "service.lock"))


def bind(paths, args):
    with instance_lock(paths):
        state = load(paths)
        if state["binding"] is not None or state["attempt"] is not None:
            raise AcceptanceError("qq_acceptance_already_bound")
        binding = {"sender": args.sender, "recipient": args.recipient, "port": args.port,
                   "app_name": APP_NAME, "app_version": APP_VERSION}
        validate_binding(binding)
        with args.client_artifact.open("rb") as source:
            digest = hashlib.file_digest(source, "sha256").hexdigest()
        if digest != args.client_sha256.lower():
            raise AcceptanceError("qq_acceptance_client_hash_mismatch")
        state["binding"] = binding | {"client_sha256": digest}
        atomic_json(state_path(paths), state, durable=True)
        return state


class LocalOneBot:
    def __init__(self, session, binding, token):
        self.session = session
        self.base = f"http://127.0.0.1:{binding['port']}"
        self.sender = int(binding["sender"])
        self.headers = {"Authorization": f"Bearer {token}"}

    async def check_authentication(self):
        try:
            async with self.session.post(f"{self.base}/get_version_info", json={},
                                         timeout=aiohttp.ClientTimeout(total=3),
                                         allow_redirects=False) as response:
                if response.status not in (401, 403):
                    raise AcceptanceError("onebot_authentication_not_enforced")
        except TimeoutError:
            raise AcceptanceError("onebot_timeout") from None
        except aiohttp.ClientError:
            raise AcceptanceError("onebot_connection_error") from None

    async def action(self, name, body, *, timeout=3):
        try:
            async with self.session.post(f"{self.base}/{name}", json=body,
                                         headers=self.headers,
                                         timeout=aiohttp.ClientTimeout(total=timeout),
                                         allow_redirects=False) as response:
                if response.status in (401, 403):
                    raise AcceptanceError("onebot_authentication_failed")
                if response.status != 200:
                    raise AcceptanceError("onebot_http_error")
                try:
                    payload = await response.json()
                except (aiohttp.ContentTypeError, ValueError):
                    raise AcceptanceError("onebot_invalid_response") from None
        except TimeoutError:
            raise AcceptanceError("onebot_timeout") from None
        except aiohttp.ClientError:
            raise AcceptanceError("onebot_connection_error") from None
        if not isinstance(payload, dict) or type(payload.get("retcode")) is not int:
            raise AcceptanceError("onebot_invalid_response")
        if payload["retcode"] != 0 or payload.get("status") != "ok":
            raise AcceptanceError(f"onebot_action_failed:{payload['retcode']}")
        return payload.get("data")

    async def send_private_msg(self, *, user_id, message):
        return await self.action("send_msg", {"message_type": "private", "self_id": self.sender,
                                              "user_id": user_id, "message": message},
                                 timeout=SEND_TIMEOUT)


async def preflight(client, binding):
    await client.check_authentication()
    version = await client.action("get_version_info", {})
    if (not isinstance(version, dict)
            or version.get("app_name") != binding["app_name"]
            or version.get("app_version") != binding["app_version"]
            or str(version.get("protocol_version")).lower() not in {"v11", "11"}):
        raise AcceptanceError("qq_acceptance_client_version_mismatch")
    login = await client.action("get_login_info", {})
    if not isinstance(login, dict) or str(login.get("user_id")) != binding["sender"]:
        raise AcceptanceError("qq_acceptance_sender_mismatch")
    status = await client.action("get_status", {})
    if not isinstance(status, dict) or status.get("online") is not True:
        raise AcceptanceError("qq_acceptance_client_offline")


def message_id(data):
    value = data.get("message_id") if isinstance(data, dict) else None
    if type(value) in (str, int) and re.fullmatch(r"-?[0-9]{1,20}", str(value)) and int(value):
        return str(value)
    return None


async def run_online(paths, operation, token):
    if not token or not token.isascii() or any(not 33 <= ord(char) <= 126 for char in token):
        raise AcceptanceError("qq_acceptance_token_required")
    if operation == "send":
        with instance_lock(paths):
            return await online(paths, operation, token)
    return await online(paths, operation, token)


async def online(paths, operation, token):
    state = load(paths)
    binding = state["binding"]
    if binding is None:
        raise AcceptanceError("qq_acceptance_binding_required")
    if operation == "send" and state["attempt"] is not None:
        raise AcceptanceError("qq_acceptance_attempt_already_recorded")
    async with aiohttp.ClientSession(trust_env=False, cookie_jar=aiohttp.DummyCookieJar()) as session:
        client = LocalOneBot(session, binding, token)
        await preflight(client, binding)
        if operation == "preflight":
            return state | {"preflight": "passed"}

        attempt = {"code": identifier(), "started_at": timestamp(), "dispatch": "dispatching",
                   "bridge": None, "platform_message_id": None, "recipient_received_at": None}
        state["attempt"] = attempt
        atomic_json(state_path(paths), state, durable=True)
        try:
            # Reuse PCAR's text serialization while the transport selects SnowLuma's action.
            data = await send_private_text(client, {"owner_qq": binding["recipient"]},
                                           f"PCAR QQ {attempt['code']}")
        except AcceptanceError as error:
            attempt.update(dispatch="uncertain", bridge=str(error))
        else:
            receipt = message_id(data)
            attempt.update(dispatch="platform_accepted" if receipt else "uncertain",
                           bridge="ok" if receipt else "ok_without_message_id",
                           platform_message_id=receipt)
        attempt["finished_at"] = timestamp()
        atomic_json(state_path(paths), state, durable=True)
        return state


def confirm_received(paths, code):
    with instance_lock(paths):
        state = load(paths)
        attempt = state["attempt"]
        if attempt is None or attempt["code"] != code:
            raise AcceptanceError("qq_acceptance_code_mismatch")
        if attempt["recipient_received_at"] is None:
            attempt["recipient_received_at"] = timestamp()
            atomic_json(state_path(paths), state, durable=True)
        return state


def summary(state):
    output = {"persona_id": state["persona_id"], "bound": state["binding"] is not None}
    attempt = state["attempt"]
    if attempt:
        if attempt["recipient_received_at"]:
            result = ("received" if attempt["platform_message_id"]
                      else "received_without_bridge_receipt")
        elif attempt["dispatch"] == "platform_accepted":
            result = "awaiting_recipient_confirmation"
        else:
            result = "delivery_uncertain"
        output.update(attempt=attempt, result=result)
    if state.get("preflight"):
        output["preflight"] = state["preflight"]
    return output


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--home", type=Path, required=True)
    commands = parser.add_subparsers(dest="operation", required=True)
    for operation in ("init", "status", "preflight", "send"):
        commands.add_parser(operation)
    binding = commands.add_parser("bind")
    binding.add_argument("--sender", required=True)
    binding.add_argument("--recipient", required=True)
    binding.add_argument("--port", type=int, required=True)
    binding.add_argument("--client-artifact", type=Path, required=True)
    binding.add_argument("--client-sha256", required=True)
    confirmation = commands.add_parser("confirm-received")
    confirmation.add_argument("--code", required=True)
    args = parser.parse_args()
    if not args.home.is_absolute():
        parser.error("--home requires an absolute path")
    paths = RuntimePaths(args.home.resolve())
    try:
        match args.operation:
            case "init":
                state = initialize(paths.home)
            case "bind":
                state = bind(paths, args)
            case "confirm-received":
                state = confirm_received(paths, args.code)
            case "status":
                state = load(paths)
            case _:
                if load(paths)["binding"] is None:
                    raise AcceptanceError("qq_acceptance_binding_required")
                try:
                    with warnings.catch_warnings():
                        warnings.simplefilter("error", getpass.GetPassWarning)
                        token = getpass.getpass("OneBot token: ")
                except (getpass.GetPassWarning, EOFError):
                    raise AcceptanceError("qq_acceptance_hidden_token_input_required") from None
                state = asyncio.run(run_online(paths, args.operation, token))
    except AcceptanceError as error:
        print(json.dumps({"error": str(error)}))
        return 1
    except (OSError, RuntimeErrorBase, sqlite3.Error):
        print(json.dumps({"error": "qq_acceptance_local_error"}))
        return 1
    output = summary(state)
    print(json.dumps(output))
    return 2 if args.operation == "send" and output["result"] == "delivery_uncertain" else 0


if __name__ == "__main__":
    raise SystemExit(main())
