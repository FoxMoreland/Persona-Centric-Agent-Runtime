"""Instance-bound proof that the desktop credential passed official DeepSeek authentication."""
import asyncio
from contextvars import ContextVar
import hashlib
import hmac
import json
import ssl

import aiohttp
import certifi

from .config import atomic_json
from .models import RuntimeErrorBase
from .security import unprotect


BASE = "https://api.deepseek.com"
BALANCE = BASE + "/user/balance"
ERROR = "deepseek_activation_required"
current = ContextVar("desktop_activation", default=None)


def _official(model):
    return model.get("base_url", "").rstrip("/") in (BASE, BASE + "/v1", BASE + "/beta")


def _binding(cfg):
    model = cfg["model"]
    if _official(model) and model.get("encrypted_key"):
        return hashlib.sha256((cfg["instance_id"] + "\0" + model["encrypted_key"]).encode()).hexdigest()
    return None


async def validate_key(key):
    from .chat import ModelError
    from .network_proxy import outbound_proxy
    try:
        proxy = outbound_proxy(BALANCE)
        timeout = aiohttp.ClientTimeout(total=15)
        tls = ssl.create_default_context(cafile=certifi.where())
        async with aiohttp.ClientSession(timeout=timeout, trust_env=False) as client:
            async with client.get(BALANCE, headers={"Authorization": "Bearer " + key},
                                  proxy=proxy, ssl=tls, allow_redirects=False) as response:
                if response.status == 401:
                    return {"state": "invalid_key"}
                if response.status != 200:
                    return {"state": "network_error"}
                chunks = bytearray()
                async for chunk in response.content.iter_chunked(8192):
                    chunks.extend(chunk)
                    if len(chunks) > 65536:
                        return {"state": "network_error"}
                payload = json.loads(chunks)
                if not isinstance(payload, dict) or type(payload.get("is_available")) is not bool:
                    return {"state": "network_error"}
                return {"state": "verified", "balance_available": payload["is_available"]}
    except ModelError as error:
        return {"state": "proxy_required" if str(error) == "system_proxy_required" else "network_error"}
    except (aiohttp.ClientError, TimeoutError, ValueError, OSError):
        return {"state": "network_error"}


class DesktopActivation:
    def __init__(self, config, verify_key=validate_key):
        self.config = config
        self.file = config.paths.home / "runtime/deepseek-activation.json"
        self.verify_key = verify_key
        self.verification = asyncio.Lock()

    def status(self, cfg=None):
        cfg = cfg or self.config.load()
        binding = _binding(cfg)
        if binding is None:
            return {"state": "missing", "has_key": False}
        try:
            saved = json.loads(self.file.read_bytes())
        except (OSError, ValueError):
            saved = {}
        if not isinstance(saved, dict) or saved.get("version") != 1 or binding != saved.get("binding"):
            return {"state": "unverified", "has_key": True}
        try:
            unprotect(cfg["model"]["encrypted_key"])
        except (OSError, ValueError):
            return {"state": "missing", "has_key": False}
        state = "verified" if type(saved.get("balance_available")) is bool else "invalid_key"
        response = {"state": state, "has_key": True}
        if state == "verified":
            response["balance_available"] = saved["balance_available"]
        return response

    def require(self):
        if self.status()["state"] != "verified":
            raise RuntimeErrorBase(ERROR)

    def invalidate(self, model):
        cfg = self.config.load()
        binding = _binding(cfg)
        if (binding and _official(model) and model.get("encrypted_key") == cfg["model"]["encrypted_key"]):
            self.file.parent.mkdir(parents=True, exist_ok=True)
            atomic_json(self.file, {"version": 1, "binding": binding}, durable=True)

    async def clear(self):
        async with self.verification:
            cfg = self.config.load()
            if _binding(cfg):
                self.config.update({"model": {"api_key": ""}})
            self.file.unlink(missing_ok=True)
            return {"state": "missing", "has_key": False}

    async def verify(self, store, key="", confirm=None):
        from .session_privacy import connection
        if not isinstance(key, str) or len(key) > 512 or any(c.isspace() for c in key):
            return {"state": "invalid_key"}
        async with self.verification:
            cfg = self.config.load()
            previous = cfg["model"]
            plan = None
            if key:
                name = previous["name"] if _official(previous) else "deepseek-v4-pro"
                plan = connection(store, self.config, "main", BASE, name, key)
                if (plan["affected"] or previous["base_url"] and not _official(previous)) and confirm is None:
                    return {"state": "confirm", "confirmation": plan["confirmation"],
                            "affected": len(plan["affected"])}
            else:
                if not _binding(cfg):
                    return {"state": "missing"}
                try:
                    key = unprotect(previous["encrypted_key"])
                except (OSError, ValueError):
                    return {"state": "invalid_key"}
            result = await self.verify_key(key)
            if self.config.load()["model"] != previous:
                return {"state": "connection_changed"}
            if result["state"] != "verified":
                if result["state"] == "invalid_key" and _binding(cfg):
                    try:
                        if hmac.compare_digest(key.encode(), unprotect(previous["encrypted_key"]).encode()):
                            self.invalidate(previous)
                    except (OSError, ValueError):
                        pass
                return result
            if plan is not None:
                connection(store, self.config, "main", BASE, name, key,
                           confirm=confirm if confirm is not None else plan["confirmation"])
            active = self.config.load()
            binding = _binding(active)
            if binding is None or not hmac.compare_digest(key.encode(), unprotect(active["model"]["encrypted_key"]).encode()):
                return {"state": "connection_changed"}
            self.file.parent.mkdir(parents=True, exist_ok=True)
            atomic_json(self.file, {"version": 1, "binding": binding,
                                   "balance_available": result["balance_available"]}, durable=True)
            return result


def locked():
    gate = current.get()
    return gate is not None and gate.status()["state"] != "verified"


def admit():
    if locked():
        from .chat import ModelError
        raise ModelError(ERROR, diagnostics={"request_headers_sent": False})


def unauthorized(model):
    gate = current.get()
    if gate is not None:
        gate.invalidate(model)


def allowed_while_locked(method, path, action):
    if path == "/v1/health" or (method == "GET" and path == "/v1/config"):
        return True
    if path == "/v1/manage":
        return action in {"desktop.activation.status", "desktop.activation.verify", "desktop.activation.clear"}
    if path.startswith("/v1/control/"):
        return path.rsplit("/", 1)[-1] in {"drain", "stop", "checkpoint", "resume"}
    return method == "POST" and (
        path.startswith("/v1/requests/") and path.rsplit("/", 1)[-1] in {"cancel", "delivery", "notice"}
        or path.startswith("/v2/batches/") and path.endswith("/cancel")
        or path.startswith("/v2/replies/") and path.rsplit("/", 1)[-1] in {"result", "resolve"}
    )
