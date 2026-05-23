import asyncio
import base64
import hashlib
import json
import os
import re
import secrets
import signal
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

from starlette.applications import Starlette
from starlette.authentication import (
    AuthCredentials,
    AuthenticationBackend,
    AuthenticationError,
    SimpleUser,
)
from starlette.middleware import Middleware
from starlette.middleware.authentication import AuthenticationMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles
from starlette.templating import Jinja2Templates

from nanobot.config.loader import (
    load_config,
    save_config,
)
from nanobot.config.schema import Config

ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;]*m")
SECRET_FIELDS = {
    "api_key",
    "apiKey",
    "token",
    "bot_token",
    "botToken",
    "app_token",
    "appToken",
    "app_secret",
    "appSecret",
    "signing_secret",
    "signingSecret",
    "encrypt_key",
    "encryptKey",
    "verification_token",
    "verificationToken",
    "secret",
    "webhook_url",
    "webhookUrl",
}

BASE_DIR = Path(__file__).parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")
MORNEVEN_BACKEND_INTERNAL_URL = os.environ.get("MORNEVEN_BACKEND_INTERNAL_URL", "").strip()
MORNEVEN_BACKEND_PUBLIC_URL = os.environ.get("MORNEVEN_BACKEND_PUBLIC_URL", "").strip()
MORNEVEN_BOT_MANAGER_SYNC_TOKEN = os.environ.get("MORNEVEN_BOT_MANAGER_SYNC_TOKEN", "").strip()
NANOBOT_MORNEVEN_RELOAD_TOKEN = os.environ.get("NANOBOT_MORNEVEN_RELOAD_TOKEN", "").strip()
WORKSPACE_PATH = Path(
    os.environ.get("NANOBOT_AGENTS__DEFAULTS__WORKSPACE", str(Path.home() / ".nanobot" / "workspace"))
).expanduser()
RUNTIME_STATE_PATH = WORKSPACE_PATH / ".morneven-runtime.json"
RUNTIME_MANIFEST_PATH = WORKSPACE_PATH / ".morneven-runtime-manifest.json"
MAX_WORKSPACE_SYNC_BYTES = 500_000

if not ADMIN_PASSWORD:
    ADMIN_PASSWORD = secrets.token_urlsafe(16)
    print(f"Generated admin password: {ADMIN_PASSWORD}")


class BasicAuthBackend(AuthenticationBackend):
    async def authenticate(self, conn):
        if "Authorization" not in conn.headers:
            return None

        auth = conn.headers["Authorization"]
        try:
            scheme, credentials = auth.split()
            if scheme.lower() != "basic":
                return None
            decoded = base64.b64decode(credentials).decode("ascii")
        except (ValueError, UnicodeDecodeError):
            raise AuthenticationError("Invalid credentials")

        username, _, password = decoded.partition(":")
        if username == ADMIN_USERNAME and password == ADMIN_PASSWORD:
            return AuthCredentials(["authenticated"]), SimpleUser(username)

        raise AuthenticationError("Invalid credentials")


def require_auth(request: Request):
    if not request.user.is_authenticated:
        return PlainTextResponse(
            "Unauthorized",
            status_code=401,
            headers={"WWW-Authenticate": 'Basic realm="nanobot"'},
        )
    return None


def require_morneven_token(request: Request):
    expected = NANOBOT_MORNEVEN_RELOAD_TOKEN
    provided = request.headers.get("x-morneven-reload-token", "")
    if not expected:
        return JSONResponse({"error": "NANOBOT_MORNEVEN_RELOAD_TOKEN is not configured"}, status_code=503)
    if not secrets.compare_digest(provided, expected):
        return JSONResponse({"error": "Forbidden"}, status_code=403)
    return None


class GatewayManager:
    def __init__(self):
        self.process: asyncio.subprocess.Process | None = None
        self.state = "stopped"
        self.logs: deque[str] = deque(maxlen=500)
        self.start_time: float | None = None
        self.restart_count = 0
        self._read_tasks: list[asyncio.Task] = []

    async def start(self):
        if self.process and self.process.returncode is None:
            return
        self.state = "starting"
        try:
            self.process = await asyncio.create_subprocess_exec(
                "nanobot", "gateway",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            self.state = "running"
            self.start_time = time.time()
            task = asyncio.create_task(self._read_output())
            self._read_tasks.append(task)
        except Exception as e:
            self.state = "error"
            self.logs.append(f"Failed to start gateway: {e}")

    async def stop(self):
        if not self.process or self.process.returncode is not None:
            self.state = "stopped"
            return
        self.state = "stopping"
        self.process.terminate()
        try:
            await asyncio.wait_for(self.process.wait(), timeout=10)
        except asyncio.TimeoutError:
            self.process.kill()
            await self.process.wait()
        self.state = "stopped"
        self.start_time = None

    async def restart(self):
        await self.stop()
        self.restart_count += 1
        await self.start()

    async def _read_output(self):
        try:
            while self.process and self.process.stdout:
                line = await self.process.stdout.readline()
                if not line:
                    break
                decoded = line.decode("utf-8", errors="replace").rstrip()
                cleaned = ANSI_ESCAPE.sub("", decoded)
                self.logs.append(cleaned)
        except asyncio.CancelledError:
            return
        if self.process and self.process.returncode is not None and self.state == "running":
            self.state = "error"
            self.logs.append(f"Gateway exited with code {self.process.returncode}")

    def get_status(self) -> dict:
        pid = None
        if self.process and self.process.returncode is None:
            pid = self.process.pid
        uptime = None
        if self.start_time and self.state == "running":
            uptime = int(time.time() - self.start_time)
        started_at = None
        if self.start_time and self.state == "running":
            started_at = datetime.fromtimestamp(self.start_time, timezone.utc).isoformat()
        return {
            "state": self.state,
            "pid": pid,
            "uptime": uptime,
            "startedAt": started_at,
            "restart_count": self.restart_count,
        }


gateway = GatewayManager()
config_lock = asyncio.Lock()
morneven_sync_lock = asyncio.Lock()


def mask_secrets(data, _path=""):
    if isinstance(data, dict):
        result = {}
        for k, v in data.items():
            if k in SECRET_FIELDS and isinstance(v, str) and v:
                result[k] = v[:8] + "***" if len(v) > 8 else "***"
            else:
                result[k] = mask_secrets(v, f"{_path}.{k}")
        return result
    if isinstance(data, list):
        return [mask_secrets(item, _path) for item in data]
    return data


def _collect_secret_values(data, field_name):
    values = []
    if isinstance(data, dict):
        for k, v in data.items():
            if k == field_name and isinstance(v, str):
                values.append(v)
            else:
                values.extend(_collect_secret_values(v, field_name))
    elif isinstance(data, list):
        for item in data:
            values.extend(_collect_secret_values(item, field_name))
    return values


def merge_secrets(new_data, existing_data):
    if isinstance(new_data, dict) and isinstance(existing_data, dict):
        result = {}
        for k, v in new_data.items():
            if k in SECRET_FIELDS and isinstance(v, str) and (v.endswith("***") or v == ""):
                result[k] = existing_data.get(k, "")
            else:
                result[k] = merge_secrets(v, existing_data.get(k, {}))
        return result
    return new_data


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def normalize_service_url(raw, default_port=""):
    clean = raw.strip().rstrip("/")
    if not clean:
        return ""
    if not re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", clean):
        clean = f"http://{clean}"
    parsed = urllib.parse.urlsplit(clean)
    if parsed.hostname and parsed.hostname.endswith(".railway.internal") and not parsed.port and default_port:
        netloc = f"{parsed.hostname}:{default_port}"
        clean = urllib.parse.urlunsplit((parsed.scheme, netloc, parsed.path, parsed.query, parsed.fragment)).rstrip("/")
    return clean


def backend_base_urls():
    urls = []
    for raw, default_port in (
        (MORNEVEN_BACKEND_INTERNAL_URL, "8080"),
        (MORNEVEN_BACKEND_PUBLIC_URL, ""),
    ):
        clean = normalize_service_url(raw, default_port)
        if clean and clean not in urls:
            urls.append(clean)
    return urls


def bot_manager_bundle_url(base_url):
    if base_url.endswith("/api"):
        return f"{base_url}/bot-manager/runtime/bundle"
    return f"{base_url}/api/bot-manager/runtime/bundle"


def fetch_morneven_runtime_bundle():
    if not MORNEVEN_BOT_MANAGER_SYNC_TOKEN:
        raise RuntimeError("MORNEVEN_BOT_MANAGER_SYNC_TOKEN is not configured")
    urls = backend_base_urls()
    if not urls:
        raise RuntimeError("Morneven backend URL is not configured")

    last_error = None
    for base_url in urls:
        url = bot_manager_bundle_url(base_url)
        request = urllib.request.Request(
            url,
            headers={
                "accept": "application/json",
                "x-bot-manager-sync-token": MORNEVEN_BOT_MANAGER_SYNC_TOKEN,
            },
            method="GET",
        )
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                payload = json.loads(response.read().decode("utf-8"))
                if isinstance(payload, dict) and payload.get("success") is True:
                    return payload.get("data")
                return payload
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            last_error = exc

    raise RuntimeError(f"Unable to fetch Morneven runtime bundle: {last_error}")


def normalize_runtime_path(value):
    normalized = str(value).strip().replace("\\", "/").lstrip("/")
    segments = normalized.split("/")
    if not normalized or len(normalized) > 240:
        raise ValueError("Invalid runtime file path")
    if any(segment in {"", ".", ".."} for segment in segments):
        raise ValueError("Runtime file path cannot traverse directories")
    if not re.fullmatch(r"[A-Za-z0-9._/-]+", normalized):
        raise ValueError("Runtime file path contains unsupported characters")
    return normalized


def resolve_workspace_file(relative_path):
    workspace_root = WORKSPACE_PATH.resolve()
    target = (WORKSPACE_PATH / Path(*relative_path.split("/"))).resolve()
    if target != workspace_root and workspace_root not in target.parents:
        raise ValueError("Runtime file path escaped workspace")
    return target


def workspace_content_hash(content):
    if isinstance(content, str):
        raw = content.encode("utf-8")
    else:
        raw = bytes(content)
    return hashlib.sha256(raw).hexdigest()


def infer_workspace_kind(relative_path):
    normalized = relative_path.lower()
    filename = normalized.rsplit("/", 1)[-1]
    if filename in {"agents.md", "soul.md", "lore.md"}:
        return "identity"
    if filename == "memory.md" or normalized.startswith("memory/"):
        return "memory"
    if normalized.startswith("cron/"):
        return "cron"
    if normalized.startswith("sessions/"):
        return "session"
    if normalized.startswith("skills/"):
        return "skill"
    if filename == "tools.md" or normalized.startswith("tools/"):
        return "tool"
    if filename == "user.md":
        return "user"
    if filename == "heartbeat.md":
        return "system"
    return "other"


def load_runtime_manifest():
    try:
        if RUNTIME_MANIFEST_PATH.exists():
            payload = json.loads(RUNTIME_MANIFEST_PATH.read_text(encoding="utf-8"))
            if isinstance(payload, dict):
                files = payload.get("files")
                if isinstance(files, dict):
                    return payload
    except Exception:
        pass
    return {"version": 1, "syncedAt": None, "files": {}}


def write_runtime_manifest(files, active_identity=None):
    manifest = {
        "version": 1,
        "syncedAt": now_iso(),
        "identity": active_identity or {},
        "files": {
            item["path"]: {
                "contentHash": item["contentHash"],
                "size": item["size"],
                "syncedAt": item["syncedAt"],
            }
            for item in files
        },
    }
    RUNTIME_MANIFEST_PATH.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return manifest


def iter_workspace_files():
    if not WORKSPACE_PATH.exists():
        return
    internal_paths = {
        normalize_runtime_path(str(RUNTIME_STATE_PATH.relative_to(WORKSPACE_PATH))),
        normalize_runtime_path(str(RUNTIME_MANIFEST_PATH.relative_to(WORKSPACE_PATH))),
    }
    for path in WORKSPACE_PATH.rglob("*"):
        if not path.is_file():
            continue
        try:
            relative_path = normalize_runtime_path(path.relative_to(WORKSPACE_PATH).as_posix())
        except ValueError:
            continue
        if relative_path in internal_paths:
            continue
        yield relative_path, path


def read_workspace_text(path):
    stat = path.stat()
    if stat.st_size > MAX_WORKSPACE_SYNC_BYTES:
        raise ValueError("File exceeds workspace sync size limit")
    raw = path.read_bytes()
    if b"\x00" in raw:
        raise ValueError("Binary files are not supported")
    try:
        content = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("File is not valid UTF-8") from exc
    return content, stat


def list_workspace_changes():
    manifest = load_runtime_manifest()
    manifest_files = manifest.get("files", {}) if isinstance(manifest.get("files"), dict) else {}
    changes = []
    skipped = []
    for relative_path, path in iter_workspace_files():
        try:
            content, stat = read_workspace_text(path)
            content_hash = workspace_content_hash(content)
            base = manifest_files.get(relative_path, {}) if isinstance(manifest_files.get(relative_path), dict) else {}
            base_hash = base.get("contentHash") if isinstance(base.get("contentHash"), str) else None
            if base_hash == content_hash:
                continue
            changes.append({
                "path": relative_path,
                "kind": infer_workspace_kind(relative_path),
                "content": content,
                "contentHash": content_hash,
                "baseHash": base_hash,
                "size": stat.st_size,
                "updatedAt": datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(),
            })
        except Exception as exc:
            skipped.append({"path": relative_path, "reason": str(exc)})
    changes.sort(key=lambda item: item["path"])
    skipped.sort(key=lambda item: item["path"])
    return {
        "syncedAt": manifest.get("syncedAt"),
        "changedCount": len(changes),
        "changes": changes,
        "skipped": skipped,
    }


def merge_deep(target, source):
    if not isinstance(target, dict) or not isinstance(source, dict):
        return source
    result = dict(target)
    for key, value in source.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = merge_deep(result[key], value)
        else:
            result[key] = value
    return result


def apply_bundle_to_config(bundle):
    config = load_config()
    data = config.model_dump(by_alias=True)

    credentials = bundle.get("credentials") if isinstance(bundle, dict) else None
    credential_models = {}
    if isinstance(credentials, dict):
        providers = data.setdefault("providers", {})
        for provider, credential in credentials.items():
            if not isinstance(credential, dict):
                continue
            provider_config = providers.setdefault(provider, {})
            api_key = credential.get("apiKey") or credential.get("api_key")
            api_base = credential.get("apiBase") or credential.get("api_base")
            model_id = credential.get("modelId") or credential.get("model_id")
            if api_key:
                provider_config["apiKey"] = api_key
            if api_base is not None:
                provider_config["apiBase"] = api_base
            if api_key and model_id:
                credential_models[provider] = model_id

    channels = bundle.get("channels") if isinstance(bundle, dict) else None
    if isinstance(channels, dict):
        data["channels"] = merge_deep(data.get("channels", {}), channels)

    settings = bundle.get("settings") if isinstance(bundle, dict) else None
    if isinstance(settings, dict):
        known_config_keys = {"agents", "channels", "gateway", "providers", "tools"}
        runtime_settings = {
            key: value
            for key, value in settings.items()
            if key in known_config_keys
        }
        if isinstance(settings.get("nanobotConfig"), dict):
            runtime_settings = merge_deep(runtime_settings, settings["nanobotConfig"])
        if runtime_settings:
            data = merge_deep(data, runtime_settings)

    general_config = bundle.get("generalConfig") if isinstance(bundle, dict) else None
    if isinstance(general_config, dict) and isinstance(general_config.get("nanobotConfig"), dict):
        data = merge_deep(data, general_config["nanobotConfig"])

    agents = data.setdefault("agents", {})
    defaults = agents.setdefault("defaults", {})
    defaults["workspace"] = str(WORKSPACE_PATH)
    preferred_provider = defaults.get("provider")
    if preferred_provider in credential_models:
        defaults["model"] = credential_models[preferred_provider]
    elif preferred_provider in {None, "", "auto"} and len(credential_models) == 1:
        provider, model_id = next(iter(credential_models.items()))
        defaults["provider"] = provider
        defaults["model"] = model_id

    save_config(Config.model_validate(data))


def materialize_morneven_runtime(bundle):
    if not isinstance(bundle, dict):
        raise ValueError("Runtime bundle must be an object")

    WORKSPACE_PATH.mkdir(parents=True, exist_ok=True)
    previous_manifest = load_runtime_manifest()
    previous_files = previous_manifest.get("files", {}) if isinstance(previous_manifest.get("files"), dict) else {}
    written = []
    files = bundle.get("files", [])
    if not isinstance(files, list):
        raise ValueError("Runtime bundle files must be a list")

    for item in files:
        if not isinstance(item, dict):
            continue
        relative_path = normalize_runtime_path(item.get("path", ""))
        target = resolve_workspace_file(relative_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        content = item.get("content", "")
        content_text = str(content)
        target.write_text(content_text, encoding="utf-8")
        written.append({
            "path": relative_path,
            "contentHash": workspace_content_hash(content_text),
            "size": len(content_text.encode("utf-8")),
            "syncedAt": now_iso(),
        })

    written_paths = {item["path"] for item in written}
    for previous_path in previous_files:
        if previous_path in written_paths:
            continue
        try:
            target = resolve_workspace_file(normalize_runtime_path(previous_path))
            if target.exists() and target.is_file():
                target.unlink()
        except Exception:
            continue

    active_identity = bundle.get("activeIdentity") if isinstance(bundle.get("activeIdentity"), dict) else {}
    state = {
        "syncedAt": now_iso(),
        "mode": bundle.get("mode", "single-active-personality"),
        "identity": {
            "id": active_identity.get("id"),
            "slug": active_identity.get("slug"),
            "name": active_identity.get("name"),
            "roleTitle": active_identity.get("roleTitle"),
        },
        "fileCount": len(written),
        "files": [item["path"] for item in written],
    }
    RUNTIME_STATE_PATH.write_text(json.dumps(state, indent=2), encoding="utf-8")
    write_runtime_manifest(written, state["identity"])
    apply_bundle_to_config(bundle)
    return state


def load_morneven_runtime_state():
    try:
        if RUNTIME_STATE_PATH.exists():
            return json.loads(RUNTIME_STATE_PATH.read_text(encoding="utf-8"))
    except Exception as exc:
        return {"error": str(exc)}
    return {
        "syncedAt": None,
        "mode": "single-active-personality",
        "identity": None,
        "fileCount": 0,
        "files": [],
    }


async def sync_morneven_runtime(strict=False):
    if not MORNEVEN_BOT_MANAGER_SYNC_TOKEN or not backend_base_urls():
        result = {"synced": False, "reason": "Morneven backend sync is not configured"}
        if strict:
            raise RuntimeError(result["reason"])
        return result

    async with morneven_sync_lock:
        try:
            bundle = await asyncio.to_thread(fetch_morneven_runtime_bundle)
            state = await asyncio.to_thread(materialize_morneven_runtime, bundle)
            identity = state.get("identity") or {}
            gateway.logs.append(f"Morneven runtime synced: {identity.get('name') or 'active personality'}")
            return {"synced": True, "state": state}
        except Exception as exc:
            gateway.logs.append(f"Morneven runtime sync failed: {exc}")
            if strict:
                raise
            return {"synced": False, "reason": str(exc)}


async def homepage(request: Request):
    auth_err = require_auth(request)
    if auth_err:
        return auth_err
    return templates.TemplateResponse(request, "index.html")


async def health(request: Request):
    return JSONResponse({"status": "ok", "gateway": gateway.state, "morneven": load_morneven_runtime_state()})


async def api_config_get(request: Request):
    auth_err = require_auth(request)
    if auth_err:
        return auth_err
    config = load_config()
    data = config.model_dump(by_alias=True)
    return JSONResponse(mask_secrets(data))


async def api_config_put(request: Request):
    auth_err = require_auth(request)
    if auth_err:
        return auth_err

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)

    try:
        restart = body.pop("_restartGateway", False)

        async with config_lock:
            existing_config = load_config()
            existing_data = existing_config.model_dump(by_alias=True)

            merged = merge_secrets(body, existing_data)

            try:
                new_config = Config.model_validate(merged)
            except Exception as e:
                err_msg = str(e)
                for field in SECRET_FIELDS:
                    for val in _collect_secret_values(merged, field):
                        if val and len(val) > 3:
                            err_msg = err_msg.replace(val, "***")
                return JSONResponse({"error": f"Validation error: {err_msg}"}, status_code=400)

            save_config(new_config)

        if restart:
            asyncio.create_task(gateway.restart())

        return JSONResponse({"ok": True, "restarting": restart})
    except Exception as e:
        print(f"Config save error: {type(e).__name__}: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


async def api_status(request: Request):
    auth_err = require_auth(request)
    if auth_err:
        return auth_err

    config = load_config()
    data = config.model_dump()

    providers = {}
    for name, prov in data["providers"].items():
        providers[name] = {"configured": bool(prov.get("api_key"))}

    # ChannelsConfig mixes global fields with per-channel dictionaries.
    # Only dict-shaped entries represent actual channels with an "enabled" flag.
    channels = {}
    for name, chan in data["channels"].items():
        if not isinstance(chan, dict):
            continue
        if "enabled" not in chan:
            continue
        channels[name] = {"enabled": bool(chan.get("enabled", False))}

    cron_dir = Path.home() / ".nanobot" / "cron"
    cron_jobs = []
    if cron_dir.exists():
        for f in cron_dir.glob("*.json"):
            try:
                cron_jobs.append(json.loads(f.read_text()))
            except Exception:
                pass

    return JSONResponse({
        "gateway": gateway.get_status(),
        "morneven": load_morneven_runtime_state(),
        "providers": providers,
        "channels": channels,
        "cron": {"count": len(cron_jobs), "jobs": cron_jobs},
    })


async def api_logs(request: Request):
    auth_err = require_auth(request)
    if auth_err:
        return auth_err
    return JSONResponse({"lines": list(gateway.logs)})


async def api_gateway_start(request: Request):
    auth_err = require_auth(request)
    if auth_err:
        return auth_err
    asyncio.create_task(gateway.start())
    return JSONResponse({"ok": True})


async def api_gateway_stop(request: Request):
    auth_err = require_auth(request)
    if auth_err:
        return auth_err
    asyncio.create_task(gateway.stop())
    return JSONResponse({"ok": True})


async def api_gateway_restart(request: Request):
    auth_err = require_auth(request)
    if auth_err:
        return auth_err
    asyncio.create_task(gateway.restart())
    return JSONResponse({"ok": True})


async def api_morneven_status(request: Request):
    auth_err = require_morneven_token(request)
    if auth_err:
        return auth_err
    return JSONResponse({
        "ok": True,
        "gateway": gateway.get_status(),
        "morneven": load_morneven_runtime_state(),
        "logs": list(gateway.logs)[-50:],
    })


async def api_morneven_workspace_changes(request: Request):
    auth_err = require_morneven_token(request)
    if auth_err:
        return auth_err

    try:
        return JSONResponse({"ok": True, **list_workspace_changes()})
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=502)


async def api_morneven_config_secrets(request: Request):
    auth_err = require_morneven_token(request)
    if auth_err:
        return auth_err

    config = load_config()
    data = config.model_dump(by_alias=True)
    return JSONResponse({
        "ok": True,
        "providers": data.get("providers", {}),
        "channels": data.get("channels", {}),
        "tools": data.get("tools", {}),
        "agents": data.get("agents", {}),
    })


async def api_morneven_gateway_start(request: Request):
    auth_err = require_morneven_token(request)
    if auth_err:
        return auth_err
    sync_result = await sync_morneven_runtime(strict=False)
    await gateway.start()
    return JSONResponse({
        "ok": True,
        "action": "start",
        "sync": sync_result,
        "gateway": gateway.get_status(),
        "morneven": load_morneven_runtime_state(),
    })


async def api_morneven_gateway_stop(request: Request):
    auth_err = require_morneven_token(request)
    if auth_err:
        return auth_err
    await gateway.stop()
    return JSONResponse({
        "ok": True,
        "action": "stop",
        "gateway": gateway.get_status(),
        "morneven": load_morneven_runtime_state(),
    })


async def api_morneven_gateway_restart(request: Request):
    auth_err = require_morneven_token(request)
    if auth_err:
        return auth_err
    sync_result = await sync_morneven_runtime(strict=False)
    await gateway.restart()
    return JSONResponse({
        "ok": True,
        "action": "restart",
        "sync": sync_result,
        "gateway": gateway.get_status(),
        "morneven": load_morneven_runtime_state(),
    })


async def api_morneven_reload(request: Request):
    auth_err = require_morneven_token(request)
    if auth_err:
        return auth_err

    restart_gateway = False
    try:
        body = await request.json()
        if isinstance(body, dict):
            restart_gateway = bool(body.get("restartGateway", False))
    except Exception:
        restart_gateway = False

    try:
        result = await sync_morneven_runtime(strict=True)
        if restart_gateway:
            await gateway.restart()
        return JSONResponse({
            "ok": True,
            "result": result,
            "gateway": gateway.get_status(),
            "restarted": restart_gateway,
        })
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=502)


routes = [
    Mount("/assets", StaticFiles(directory=str(BASE_DIR / "img")), name="assets"),
    Route("/", homepage),
    Route("/health", health),
    Route("/api/config", api_config_get, methods=["GET"]),
    Route("/api/config", api_config_put, methods=["PUT"]),
    Route("/api/status", api_status),
    Route("/api/logs", api_logs),
    Route("/api/gateway/start", api_gateway_start, methods=["POST"]),
    Route("/api/gateway/stop", api_gateway_stop, methods=["POST"]),
    Route("/api/gateway/restart", api_gateway_restart, methods=["POST"]),
    Route("/api/morneven/status", api_morneven_status),
    Route("/api/morneven/gateway/start", api_morneven_gateway_start, methods=["POST"]),
    Route("/api/morneven/gateway/stop", api_morneven_gateway_stop, methods=["POST"]),
    Route("/api/morneven/gateway/restart", api_morneven_gateway_restart, methods=["POST"]),
    Route("/api/morneven/reload", api_morneven_reload, methods=["POST"]),
    Route("/api/morneven/workspace/changes", api_morneven_workspace_changes),
    Route("/api/morneven/config-secrets", api_morneven_config_secrets),
]

app = Starlette(
    routes=routes,
    middleware=[Middleware(AuthenticationMiddleware, backend=BasicAuthBackend())],
)


def create_server_socket(host, port):
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    server_socket = socket.socket(family, socket.SOCK_STREAM)
    server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    if family == socket.AF_INET6 and hasattr(socket, "IPV6_V6ONLY"):
        server_socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
    server_socket.bind((host, port))
    server_socket.listen(2048)
    server_socket.set_inheritable(True)
    return server_socket


def create_server_sockets(port):
    configured = os.environ.get("HOSTS") or os.environ.get("HOST") or "0.0.0.0,::"
    hosts = []
    for host in configured.split(","):
        clean = host.strip()
        if clean and clean not in hosts:
            hosts.append(clean)

    sockets = []
    errors = []
    for host in hosts:
        try:
            sockets.append(create_server_socket(host, port))
        except OSError as exc:
            errors.append(f"{host}: {exc}")

    if not sockets:
        raise RuntimeError(f"Unable to bind any server socket on port {port}: {'; '.join(errors)}")
    if errors:
        print(f"Socket bind warnings: {'; '.join(errors)}")
    return sockets


if __name__ == "__main__":
    import uvicorn

    port = int(os.environ.get("PORT", "8080"))

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    sockets = create_server_sockets(port)
    config = uvicorn.Config(app, log_level="info", loop="asyncio")
    server = uvicorn.Server(config)

    def handle_signal():
        loop.create_task(gateway.stop())
        server.should_exit = True

    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, handle_signal)

    loop.run_until_complete(server.serve(sockets=sockets))
