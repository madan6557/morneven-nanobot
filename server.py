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
RUNTIMES_ROOT = WORKSPACE_PATH.parent / "runtimes"
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
    def __init__(self, identity_id="main", name="Main", config_path=None):
        self.identity_id = identity_id
        self.name = name
        self.config_path = Path(config_path).expanduser() if config_path else None
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
            command = ["nanobot", "gateway"]
            if self.config_path:
                command.extend(["--config", str(self.config_path)])
            self.process = await asyncio.create_subprocess_exec(
                *command,
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
                self.logs.append(f"[{self.name}] {cleaned}")
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
            "identityId": self.identity_id,
            "name": self.name,
            "pid": pid,
            "uptime": uptime,
            "startedAt": started_at,
            "restart_count": self.restart_count,
        }


class MultiGatewayManager:
    def __init__(self):
        self.gateways: dict[str, GatewayManager] = {}
        self.logs: deque[str] = deque(maxlen=800)

    @property
    def state(self):
        main = self.main_gateway()
        return main.state if main else "stopped"

    def runtime_state(self):
        return load_morneven_runtime_state()

    def runtimes_from_state(self):
        state = self.runtime_state()
        runtimes = state.get("runtimes") if isinstance(state, dict) else None
        return runtimes if isinstance(runtimes, list) else []

    def main_runtime_id(self):
        state = self.runtime_state()
        main = state.get("mainIdentity") if isinstance(state, dict) else None
        if isinstance(main, dict) and main.get("id"):
            return str(main["id"])
        runtimes = self.runtimes_from_state()
        for runtime in runtimes:
            if isinstance(runtime, dict) and runtime.get("isMain") and runtime.get("identityId"):
                return str(runtime["identityId"])
        for runtime in runtimes:
            if isinstance(runtime, dict) and runtime.get("identityId"):
                return str(runtime["identityId"])
        return "main"

    def runtime_config(self, identity_id):
        for runtime in self.runtimes_from_state():
            if isinstance(runtime, dict) and str(runtime.get("identityId")) == str(identity_id):
                return runtime
        return None

    def ensure_gateway(self, identity_id=None):
        runtime = self.runtime_config(identity_id or self.main_runtime_id())
        if runtime:
            runtime_id = str(runtime["identityId"])
            name = str(runtime.get("name") or runtime_id)
            config_path = runtime.get("configPath")
        else:
            runtime_id = str(identity_id or "main")
            name = "Main"
            config_path = None
        manager = self.gateways.get(runtime_id)
        if not manager:
            manager = GatewayManager(runtime_id, name, config_path)
            self.gateways[runtime_id] = manager
        else:
            manager.name = name
            manager.config_path = Path(config_path).expanduser() if config_path else None
        return manager

    def main_gateway(self):
        return self.ensure_gateway(self.main_runtime_id())

    async def start(self):
        await self.start_identity(self.main_runtime_id())

    async def stop(self):
        await self.stop_identity(self.main_runtime_id())

    async def restart(self):
        await self.restart_identity(self.main_runtime_id())

    async def start_identity(self, identity_id):
        manager = self.ensure_gateway(identity_id)
        await manager.start()
        self.logs.append(f"Started runtime: {manager.name}")

    async def stop_identity(self, identity_id):
        manager = self.ensure_gateway(identity_id)
        await manager.stop()
        self.logs.append(f"Stopped runtime: {manager.name}")

    async def restart_identity(self, identity_id):
        manager = self.ensure_gateway(identity_id)
        await manager.restart()
        self.logs.append(f"Restarted runtime: {manager.name}")

    async def restart_running(self):
        for identity_id, manager in list(self.gateways.items()):
            if manager.state == "running":
                await self.restart_identity(identity_id)

    def get_status(self):
        runtimes = []
        seen = set()
        for runtime in self.runtimes_from_state():
            if not isinstance(runtime, dict) or not runtime.get("identityId"):
                continue
            identity_id = str(runtime["identityId"])
            manager = self.ensure_gateway(identity_id)
            seen.add(identity_id)
            runtimes.append({
                **manager.get_status(),
                "slug": runtime.get("slug"),
                "isMain": bool(runtime.get("isMain")),
                "workspacePath": runtime.get("workspacePath"),
            })
        for identity_id, manager in self.gateways.items():
            if identity_id not in seen:
                runtimes.append(manager.get_status())
        main = self.ensure_gateway(self.main_runtime_id())
        return {
            **main.get_status(),
            "runtimes": runtimes,
        }


gateway = MultiGatewayManager()
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


def bot_manager_config_secrets_url(base_url):
    if base_url.endswith("/api"):
        return f"{base_url}/bot-manager/runtime/config-secrets"
    return f"{base_url}/api/bot-manager/runtime/config-secrets"


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


def push_morneven_config_secrets(config_data):
    if not MORNEVEN_BOT_MANAGER_SYNC_TOKEN:
        return {"synced": False, "reason": "MORNEVEN_BOT_MANAGER_SYNC_TOKEN is not configured"}
    urls = backend_base_urls()
    if not urls:
        return {"synced": False, "reason": "Morneven backend URL is not configured"}

    morneven_state = load_morneven_runtime_state()
    identity = morneven_state.get("identity") if isinstance(morneven_state, dict) else {}
    payload = {
        "identityId": identity.get("id") if isinstance(identity, dict) else None,
        "identity": identity if isinstance(identity, dict) else {},
        "providers": config_data.get("providers", {}) if isinstance(config_data, dict) else {},
        "channels": config_data.get("channels", {}) if isinstance(config_data, dict) else {},
        "tools": config_data.get("tools", {}) if isinstance(config_data, dict) else {},
        "agents": config_data.get("agents", {}) if isinstance(config_data, dict) else {},
        "morneven": morneven_state,
    }
    body = json.dumps(payload).encode("utf-8")
    last_error = None
    for base_url in urls:
        url = bot_manager_config_secrets_url(base_url)
        request = urllib.request.Request(
            url,
            data=body,
            headers={
                "accept": "application/json",
                "content-type": "application/json",
                "x-bot-manager-sync-token": MORNEVEN_BOT_MANAGER_SYNC_TOKEN,
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                response_payload = json.loads(response.read().decode("utf-8"))
                return {"synced": True, "response": response_payload}
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            last_error = exc

    return {"synced": False, "reason": f"Unable to push Morneven config secrets: {last_error}"}


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


def runtime_slug(identity):
    raw = ""
    if isinstance(identity, dict):
        raw = str(identity.get("slug") or identity.get("name") or identity.get("id") or "")
    slug = re.sub(r"[^a-zA-Z0-9._-]+", "-", raw.strip().lower()).strip("-")
    return slug or "identity"


def runtime_dir_for_identity(identity):
    identity_id = str(identity.get("id") or runtime_slug(identity)) if isinstance(identity, dict) else "identity"
    slug = runtime_slug(identity)
    safe_id = re.sub(r"[^a-zA-Z0-9._-]+", "-", identity_id).strip("-") or slug
    return RUNTIMES_ROOT / f"{slug}-{safe_id[:8]}"


def load_manifest_at(manifest_path):
    try:
        if manifest_path.exists():
            payload = json.loads(manifest_path.read_text(encoding="utf-8"))
            if isinstance(payload, dict) and isinstance(payload.get("files"), dict):
                return payload
    except Exception:
        pass
    return {"version": 1, "syncedAt": None, "files": {}}


def write_manifest_at(manifest_path, files, active_identity=None):
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
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
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return manifest


def resolve_runtime_workspace_file(workspace_root, relative_path):
    root = Path(workspace_root).resolve()
    target = (root / Path(*relative_path.split("/"))).resolve()
    if target != root and root not in target.parents:
        raise ValueError("Runtime file path escaped workspace")
    return target


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


def list_workspace_changes_at(workspace_root, manifest_path):
    workspace_root = Path(workspace_root)
    manifest = load_manifest_at(Path(manifest_path))
    manifest_files = manifest.get("files", {}) if isinstance(manifest.get("files"), dict) else {}
    changes = []
    skipped = []
    if not workspace_root.exists():
        return {"syncedAt": manifest.get("syncedAt"), "changedCount": 0, "changes": [], "skipped": []}
    for path in workspace_root.rglob("*"):
        if not path.is_file():
            continue
        try:
            relative_path = normalize_runtime_path(path.relative_to(workspace_root).as_posix())
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
            skipped.append({"path": path.name, "reason": str(exc)})
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


def apply_bundle_to_config(bundle, workspace_path=WORKSPACE_PATH, config_path=None, persist_default=True):
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
    defaults["workspace"] = str(workspace_path)
    preferred_provider = defaults.get("provider")
    if preferred_provider in credential_models:
        defaults["model"] = credential_models[preferred_provider]
    elif preferred_provider in {None, "", "auto"} and len(credential_models) == 1:
        provider, model_id = next(iter(credential_models.items()))
        defaults["provider"] = provider
        defaults["model"] = model_id

    validated = Config.model_validate(data)
    if config_path:
        config_path = Path(config_path)
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text(json.dumps(validated.model_dump(by_alias=True), indent=2), encoding="utf-8")
    if persist_default:
        save_config(validated)
    return validated.model_dump(by_alias=True)


def runtime_entries_from_bundle(bundle):
    if not isinstance(bundle, dict):
        raise ValueError("Runtime bundle must be an object")
    entries = bundle.get("identities")
    if isinstance(entries, list) and entries:
        return [entry for entry in entries if isinstance(entry, dict) and isinstance(entry.get("identity"), dict)]
    identity = bundle.get("activeIdentity") if isinstance(bundle.get("activeIdentity"), dict) else bundle.get("mainIdentity")
    if not isinstance(identity, dict):
        raise ValueError("Runtime bundle does not contain an active identity")
    return [{
        "identity": identity,
        "credentials": bundle.get("credentials", {}),
        "channels": bundle.get("channels", {}),
        "settings": bundle.get("settings", {}),
        "files": bundle.get("files", []),
    }]


def materialize_runtime_entry(entry, general_config, mode, main_identity_id, persist_default=False):
    identity = entry.get("identity") if isinstance(entry.get("identity"), dict) else {}
    runtime_dir = runtime_dir_for_identity(identity)
    workspace_path = runtime_dir / "workspace"
    manifest_path = runtime_dir / ".morneven-runtime-manifest.json"
    config_path = runtime_dir / "config.json"
    workspace_path.mkdir(parents=True, exist_ok=True)
    previous_manifest = load_manifest_at(manifest_path)
    previous_files = previous_manifest.get("files", {}) if isinstance(previous_manifest.get("files"), dict) else {}
    written = []
    files = entry.get("files", [])
    if not isinstance(files, list):
        raise ValueError("Runtime bundle files must be a list")

    for item in files:
        if not isinstance(item, dict):
            continue
        relative_path = normalize_runtime_path(item.get("path", ""))
        target = resolve_runtime_workspace_file(workspace_path, relative_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        content_text = str(item.get("content", ""))
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
            target = resolve_runtime_workspace_file(workspace_path, normalize_runtime_path(previous_path))
            if target.exists() and target.is_file():
                target.unlink()
        except Exception:
            continue

    runtime_bundle = {
        "credentials": entry.get("credentials", {}),
        "channels": entry.get("channels", {}),
        "settings": entry.get("settings", {}),
        "generalConfig": general_config,
    }
    apply_bundle_to_config(runtime_bundle, workspace_path=workspace_path, config_path=config_path, persist_default=persist_default)
    state = {
        "identityId": identity.get("id"),
        "slug": identity.get("slug"),
        "name": identity.get("name"),
        "roleTitle": identity.get("roleTitle"),
        "isMain": identity.get("id") == main_identity_id or bool(identity.get("isMain")),
        "workspacePath": str(workspace_path),
        "configPath": str(config_path),
        "fileCount": len(written),
        "files": [item["path"] for item in written],
        "syncedAt": now_iso(),
    }
    write_manifest_at(manifest_path, written, {
        "id": state["identityId"],
        "slug": state["slug"],
        "name": state["name"],
        "roleTitle": state["roleTitle"],
    })
    return state


def materialize_morneven_runtime(bundle):
    if not isinstance(bundle, dict):
        raise ValueError("Runtime bundle must be an object")

    WORKSPACE_PATH.mkdir(parents=True, exist_ok=True)
    RUNTIMES_ROOT.mkdir(parents=True, exist_ok=True)
    entries = runtime_entries_from_bundle(bundle)
    main_identity = bundle.get("mainIdentity") if isinstance(bundle.get("mainIdentity"), dict) else bundle.get("activeIdentity", {})
    main_identity_id = str(main_identity.get("id") or entries[0]["identity"].get("id"))
    general_config = bundle.get("generalConfig") if isinstance(bundle.get("generalConfig"), dict) else {}
    runtimes = []
    for entry in entries:
        identity = entry.get("identity") if isinstance(entry.get("identity"), dict) else {}
        persist_default = str(identity.get("id")) == main_identity_id
        runtimes.append(materialize_runtime_entry(entry, general_config, bundle.get("mode", "single-active-personality"), main_identity_id, persist_default=persist_default))

    main_runtime = next((runtime for runtime in runtimes if runtime.get("isMain")), runtimes[0])
    state = {
        "syncedAt": now_iso(),
        "mode": bundle.get("mode", "single-active-personality"),
        "mainIdentity": {
            "id": main_runtime.get("identityId"),
            "slug": main_runtime.get("slug"),
            "name": main_runtime.get("name"),
            "roleTitle": main_runtime.get("roleTitle"),
        },
        "identity": {
            "id": main_runtime.get("identityId"),
            "slug": main_runtime.get("slug"),
            "name": main_runtime.get("name"),
            "roleTitle": main_runtime.get("roleTitle"),
        },
        "runtimeCount": len(runtimes),
        "runtimes": runtimes,
        "fileCount": sum(runtime.get("fileCount", 0) for runtime in runtimes),
        "files": main_runtime.get("files", []),
    }
    RUNTIME_STATE_PATH.write_text(json.dumps(state, indent=2), encoding="utf-8")
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
        "mainIdentity": None,
        "runtimeCount": 0,
        "runtimes": [],
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
            gateway.logs.append(f"Morneven runtime synced: {state.get('runtimeCount', 1)} runtime(s), main {identity.get('name') or 'active personality'}")
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
        saved_data = None

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
            saved_data = new_config.model_dump(by_alias=True)

        morneven_sync = await asyncio.to_thread(push_morneven_config_secrets, saved_data or {})
        if not morneven_sync.get("synced"):
            gateway.logs.append(f"Morneven config secret push skipped: {morneven_sync.get('reason')}")

        if restart:
            asyncio.create_task(gateway.restart())

        return JSONResponse({"ok": True, "restarting": restart, "mornevenSync": morneven_sync})
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
    lines = list(gateway.logs)
    for manager in gateway.gateways.values():
        lines.extend(list(manager.logs)[-200:])
    return JSONResponse({"lines": lines[-500:]})


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


async def api_runtime_gateway_action(request: Request):
    auth_err = require_auth(request)
    if auth_err:
        return auth_err
    identity_id = request.path_params.get("identity_id")
    action = request.path_params.get("action")
    if action not in {"start", "stop", "restart"}:
        return JSONResponse({"ok": False, "error": "Invalid runtime action"}, status_code=404)
    if action in {"start", "restart"}:
        await sync_morneven_runtime(strict=False)
    if action == "start":
        await gateway.start_identity(identity_id)
    elif action == "stop":
        await gateway.stop_identity(identity_id)
    else:
        await gateway.restart_identity(identity_id)
    return JSONResponse({"ok": True, "action": action, "identityId": identity_id, "gateway": gateway.get_status()})


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
        state = load_morneven_runtime_state()
        runtimes = state.get("runtimes") if isinstance(state, dict) else None
        if isinstance(runtimes, list) and runtimes:
            runtime_changes = []
            for runtime in runtimes:
                if not isinstance(runtime, dict):
                    continue
                runtime_dir = Path(runtime.get("workspacePath", "")).parent
                changes = list_workspace_changes_at(runtime.get("workspacePath", ""), runtime_dir / ".morneven-runtime-manifest.json")
                runtime_changes.append({
                    "identityId": runtime.get("identityId"),
                    "identity": {
                        "id": runtime.get("identityId"),
                        "slug": runtime.get("slug"),
                        "name": runtime.get("name"),
                    },
                    **changes,
                })
            return JSONResponse({"ok": True, "runtimes": runtime_changes})
        return JSONResponse({"ok": True, **list_workspace_changes()})
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=502)


async def api_morneven_config_secrets(request: Request):
    auth_err = require_morneven_token(request)
    if auth_err:
        return auth_err

    state = load_morneven_runtime_state()
    runtimes = state.get("runtimes") if isinstance(state, dict) else None
    if isinstance(runtimes, list) and runtimes:
        runtime_configs = []
        for runtime in runtimes:
            if not isinstance(runtime, dict):
                continue
            config_path = runtime.get("configPath")
            data = {}
            try:
                if config_path:
                    data = json.loads(Path(config_path).read_text(encoding="utf-8"))
            except Exception:
                data = {}
            runtime_configs.append({
                "identityId": runtime.get("identityId"),
                "identity": {
                    "id": runtime.get("identityId"),
                    "slug": runtime.get("slug"),
                    "name": runtime.get("name"),
                },
                "providers": data.get("providers", {}) if isinstance(data, dict) else {},
                "channels": data.get("channels", {}) if isinstance(data, dict) else {},
                "tools": data.get("tools", {}) if isinstance(data, dict) else {},
                "agents": data.get("agents", {}) if isinstance(data, dict) else {},
            })
        return JSONResponse({"ok": True, "runtimes": runtime_configs})

    config = load_config()
    data = config.model_dump(by_alias=True)
    return JSONResponse({
        "ok": True,
        "providers": data.get("providers", {}),
        "channels": data.get("channels", {}),
        "tools": data.get("tools", {}),
        "agents": data.get("agents", {}),
    })


async def api_morneven_runtime_gateway_action(request: Request):
    auth_err = require_morneven_token(request)
    if auth_err:
        return auth_err
    identity_id = request.path_params.get("identity_id")
    action = request.path_params.get("action")
    if action not in {"start", "stop", "restart"}:
        return JSONResponse({"ok": False, "error": "Invalid runtime action"}, status_code=404)
    if action in {"start", "restart"}:
        await sync_morneven_runtime(strict=False)
    if action == "start":
        await gateway.start_identity(identity_id)
    elif action == "stop":
        await gateway.stop_identity(identity_id)
    else:
        await gateway.restart_identity(identity_id)
    return JSONResponse({
        "ok": True,
        "action": action,
        "identityId": identity_id,
        "gateway": gateway.get_status(),
        "morneven": load_morneven_runtime_state(),
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
            await gateway.restart_running()
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
    Route("/api/runtimes/{identity_id}/gateway/{action}", api_runtime_gateway_action, methods=["POST"]),
    Route("/api/morneven/status", api_morneven_status),
    Route("/api/morneven/gateway/start", api_morneven_gateway_start, methods=["POST"]),
    Route("/api/morneven/gateway/stop", api_morneven_gateway_stop, methods=["POST"]),
    Route("/api/morneven/gateway/restart", api_morneven_gateway_restart, methods=["POST"]),
    Route("/api/morneven/runtimes/{identity_id}/gateway/{action}", api_morneven_runtime_gateway_action, methods=["POST"]),
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
