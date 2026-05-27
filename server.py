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
GATEWAY_BASE_PORT = int(os.environ.get("NANOBOT_GATEWAY_BASE_PORT", "18790"))

if not ADMIN_PASSWORD:
    ADMIN_PASSWORD = secrets.token_urlsafe(16)
    print(f"Generated admin password: {ADMIN_PASSWORD}")


def ensure_pythonpath_entry(env: dict[str, str], path: Path) -> None:
    entry = str(path.resolve())
    current = [
        item
        for item in env.get("PYTHONPATH", "").split(os.pathsep)
        if item
    ]
    normalized = {str(Path(item).resolve()) for item in current if item}
    if entry not in normalized:
        current.insert(0, entry)
    env["PYTHONPATH"] = os.pathsep.join(current)


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
    def __init__(
        self,
        identity_id="main",
        name="Main",
        config_path=None,
        runtime_path=None,
        workspace_path=None,
        gateway_port=None,
        telegram_bot_username=None,
        telegram_active_bot_usernames=None,
        auto_dream_enabled=None,
    ):
        self.identity_id = identity_id
        self.name = name
        self.config_path = Path(config_path).expanduser() if config_path else None
        self.runtime_path = Path(runtime_path).expanduser() if runtime_path else None
        self.workspace_path = Path(workspace_path).expanduser() if workspace_path else None
        self.gateway_port = int(gateway_port) if gateway_port else None
        self.telegram_bot_username = str(telegram_bot_username or "").strip().lstrip("@")
        self.telegram_active_bot_usernames = [
            str(username).strip().lstrip("@")
            for username in (telegram_active_bot_usernames or [])
            if str(username or "").strip()
        ]
        self.auto_dream_enabled = auto_dream_enabled
        self.process: asyncio.subprocess.Process | None = None
        self.state = "stopped"
        self.logs: deque[str] = deque(maxlen=500)
        self.start_time: float | None = None
        self.restart_count = 0
        self.last_error: str | None = None
        self.last_exit_code: int | None = None
        self._read_tasks: list[asyncio.Task] = []

    def pid_path(self) -> Path | None:
        if not self.runtime_path:
            return None
        return self.runtime_path / "gateway.pid"

    def read_recorded_pid(self) -> int | None:
        pid_path = self.pid_path()
        if not pid_path or not pid_path.exists():
            return None
        try:
            pid = int(pid_path.read_text(encoding="utf-8").strip())
            return pid if pid > 0 else None
        except Exception:
            return None

    def write_recorded_pid(self) -> None:
        pid_path = self.pid_path()
        if not pid_path or not self.process or not self.process.pid:
            return
        pid_path.parent.mkdir(parents=True, exist_ok=True)
        pid_path.write_text(str(self.process.pid), encoding="utf-8")

    def clear_recorded_pid(self, pid: int | None = None) -> None:
        pid_path = self.pid_path()
        if not pid_path or not pid_path.exists():
            return
        if pid is not None:
            recorded_pid = self.read_recorded_pid()
            if recorded_pid != pid:
                return
        try:
            pid_path.unlink()
        except FileNotFoundError:
            return

    @staticmethod
    def process_alive(pid: int) -> bool:
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        except Exception:
            return False

    @staticmethod
    def pid_looks_like_gateway(pid: int) -> bool:
        cmdline = GatewayManager.commandline_for_pid(pid)
        if cmdline is None:
            return True
        return "nanobot" in cmdline and "gateway" in cmdline

    @staticmethod
    def commandline_for_pid(pid: int) -> str | None:
        cmdline_path = Path(f"/proc/{pid}/cmdline")
        if not cmdline_path.exists():
            return None
        try:
            return cmdline_path.read_bytes().replace(b"\0", b" ").decode("utf-8", errors="replace")
        except Exception:
            return None

    def gateway_match_tokens(self) -> list[str]:
        tokens = []
        for path in (self.config_path, self.workspace_path, self.runtime_path):
            if path:
                tokens.append(str(path))
                tokens.append(str(path.resolve()))
        return [token for token in dict.fromkeys(tokens) if token]

    def find_matching_gateway_pids(self) -> list[int]:
        proc_dir = Path("/proc")
        if not proc_dir.exists():
            return []
        tokens = self.gateway_match_tokens()
        if not tokens:
            return []
        current_pid = os.getpid()
        pids = []
        for entry in proc_dir.iterdir():
            if not entry.name.isdigit():
                continue
            pid = int(entry.name)
            if pid == current_pid:
                continue
            cmdline = self.commandline_for_pid(pid)
            if not cmdline or "nanobot" not in cmdline or "gateway" not in cmdline:
                continue
            if any(token in cmdline for token in tokens):
                pids.append(pid)
        return sorted(set(pids))

    def tracked_gateway_pids(self) -> list[int]:
        pids = []
        if self.process and self.process.returncode is None and self.process.pid:
            pids.append(self.process.pid)
        recorded_pid = self.read_recorded_pid()
        if recorded_pid:
            pids.append(recorded_pid)
        pids.extend(self.find_matching_gateway_pids())
        return sorted({pid for pid in pids if pid and self.process_alive(pid)})

    async def wait_for_pid_exit(self, pid: int, timeout: float) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if not self.process_alive(pid):
                return True
            await asyncio.sleep(0.2)
        return not self.process_alive(pid)

    def send_signal_to_pid(self, pid: int, sig: int) -> bool:
        if pid <= 0 or pid == os.getpid() or not self.process_alive(pid) or not self.pid_looks_like_gateway(pid):
            return False
        try:
            if hasattr(os, "killpg"):
                pgid = os.getpgid(pid)
                if pgid != os.getpgrp():
                    os.killpg(pgid, sig)
                else:
                    os.kill(pid, sig)
            else:
                os.kill(pid, sig)
        except ProcessLookupError:
            return False
        except Exception:
            try:
                os.kill(pid, sig)
            except Exception:
                return False
        return True

    async def terminate_pid(self, pid: int, timeout: float = 10) -> bool:
        if not self.send_signal_to_pid(pid, signal.SIGTERM):
            return False
        if await self.wait_for_pid_exit(pid, timeout):
            return True
        if not self.send_signal_to_pid(pid, signal.SIGKILL):
            return False
        return await self.wait_for_pid_exit(pid, 3)

    async def stop_recorded_process(self) -> None:
        for pid in self.tracked_gateway_pids():
            if self.process and self.process.pid == pid and self.process.returncode is None:
                continue
            stopped = await self.terminate_pid(pid)
            if stopped or not self.process_alive(pid):
                self.clear_recorded_pid(pid)

    async def start(self):
        if self.process and self.process.returncode is None:
            return
        await self.stop_recorded_process()
        remaining_pids = self.tracked_gateway_pids()
        if remaining_pids:
            self.state = "error"
            self.last_error = f"Existing gateway process is still running: {', '.join(str(pid) for pid in remaining_pids)}"
            self.logs.append(self.last_error)
            return
        self.state = "starting"
        self.last_error = None
        self.last_exit_code = None
        try:
            command = ["nanobot", "gateway"]
            if self.config_path:
                command.extend(["--config", str(self.config_path)])
            if self.workspace_path:
                command.extend(["--workspace", str(self.workspace_path)])
            if self.gateway_port:
                command.extend(["--port", str(self.gateway_port)])
            env = os.environ.copy()
            ensure_pythonpath_entry(env, BASE_DIR)
            if self.runtime_path:
                runtime_home = self.runtime_path / "home"
                runtime_home.mkdir(parents=True, exist_ok=True)
                (runtime_home / ".nanobot" / "sessions").mkdir(parents=True, exist_ok=True)
                (runtime_home / ".nanobot" / "cron").mkdir(parents=True, exist_ok=True)
                env["HOME"] = str(runtime_home)
            if self.workspace_path:
                self.workspace_path.mkdir(parents=True, exist_ok=True)
                env["NANOBOT_AGENTS__DEFAULTS__WORKSPACE"] = str(self.workspace_path)
            if self.telegram_bot_username:
                env["MORNEVEN_TELEGRAM_BOT_USERNAME"] = self.telegram_bot_username
            if self.telegram_active_bot_usernames:
                env["MORNEVEN_TELEGRAM_ACTIVE_BOTS"] = ",".join(self.telegram_active_bot_usernames)
            if self.auto_dream_enabled is not None:
                env["MORNEVEN_AUTO_DREAM_ENABLED"] = "1" if self.auto_dream_enabled else "0"
            self.process = await asyncio.create_subprocess_exec(
                *command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                env=env,
                start_new_session=True,
            )
            self.write_recorded_pid()
            self.state = "running"
            self.start_time = time.time()
            task = asyncio.create_task(self._read_output())
            self._read_tasks.append(task)
        except Exception as e:
            self.state = "error"
            self.last_error = f"Failed to start gateway: {e}"
            self.logs.append(self.last_error)

    async def stop(self):
        if not self.process or self.process.returncode is not None:
            self.state = "stopping"
            await self.stop_recorded_process()
            remaining_pids = self.tracked_gateway_pids()
            if remaining_pids:
                self.state = "error"
                self.last_error = f"Failed to stop gateway process: {', '.join(str(pid) for pid in remaining_pids)}"
                self.logs.append(self.last_error)
            else:
                self.state = "stopped"
                self.start_time = None
            return
        self.state = "stopping"
        pid = self.process.pid
        self.send_signal_to_pid(pid, signal.SIGTERM)
        try:
            await asyncio.wait_for(self.process.wait(), timeout=10)
        except asyncio.TimeoutError:
            self.send_signal_to_pid(pid, signal.SIGKILL)
            try:
                await asyncio.wait_for(self.process.wait(), timeout=3)
            except asyncio.TimeoutError:
                pass
        remaining_pids = self.tracked_gateway_pids()
        if remaining_pids:
            self.state = "error"
            self.last_error = f"Failed to stop gateway process: {', '.join(str(pid) for pid in remaining_pids)}"
            self.logs.append(self.last_error)
            return
        self.clear_recorded_pid(pid)
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
        if self.process and self.process.returncode is not None:
            self.last_exit_code = self.process.returncode
            self.clear_recorded_pid(self.process.pid)
            if self.state == "running":
                if self.process.returncode == 0:
                    self.state = "stopped"
                else:
                    self.state = "error"
                    self.last_error = f"Gateway exited with code {self.process.returncode}"
                    self.logs.append(self.last_error)

    def get_status(self) -> dict:
        pid = None
        if self.process and self.process.returncode is None:
            pid = self.process.pid
        elif self.state == "stopped":
            matching_pids = self.tracked_gateway_pids()
            if matching_pids:
                pid = matching_pids[0]
        uptime = None
        status_state = "running" if pid and self.state == "stopped" else self.state
        if self.start_time and status_state == "running":
            uptime = int(time.time() - self.start_time)
        started_at = None
        if self.start_time and status_state == "running":
            started_at = datetime.fromtimestamp(self.start_time, timezone.utc).isoformat()
        return {
            "state": status_state,
            "identityId": self.identity_id,
            "name": self.name,
            "pid": pid,
            "uptime": uptime,
            "startedAt": started_at,
            "restart_count": self.restart_count,
            "gatewayPort": self.gateway_port,
            "telegramBotUsername": self.telegram_bot_username or None,
            "lastError": self.last_error or ("Gateway process is detached from manager" if pid and self.state == "stopped" else None),
            "lastExitCode": self.last_exit_code,
            "lastLogLine": self.logs[-1] if self.logs else None,
        }


class MultiGatewayManager:
    def __init__(self):
        self.gateways: dict[str, GatewayManager] = {}
        self.logs: deque[str] = deque(maxlen=800)

    @property
    def state(self):
        states = [runtime.get("state") for runtime in self.get_status().get("runtimes", [])]
        if any(state == "error" for state in states):
            return "error"
        if any(state == "running" for state in states):
            return "running"
        if any(state in {"starting", "stopping"} for state in states):
            return "starting"
        return "stopped"

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
            runtime_path = runtime.get("runtimePath")
            workspace_path = runtime.get("workspacePath")
            gateway_port = runtime.get("gatewayPort")
            telegram_bot_username = runtime.get("telegramBotUsername")
            telegram_active_bot_usernames = runtime.get("telegramActiveBotUsernames")
            auto_dream_enabled = runtime.get("autoDreamEnabled")
        else:
            runtime_id = str(identity_id or "main")
            name = "Main"
            config_path = None
            runtime_path = None
            workspace_path = None
            gateway_port = None
            telegram_bot_username = None
            telegram_active_bot_usernames = None
            auto_dream_enabled = None
        manager = self.gateways.get(runtime_id)
        if not manager:
            manager = GatewayManager(
                runtime_id,
                name,
                config_path,
                runtime_path,
                workspace_path,
                gateway_port,
                telegram_bot_username,
                telegram_active_bot_usernames,
                auto_dream_enabled,
            )
            self.gateways[runtime_id] = manager
        else:
            manager.name = name
            manager.config_path = Path(config_path).expanduser() if config_path else None
            manager.runtime_path = Path(runtime_path).expanduser() if runtime_path else None
            manager.workspace_path = Path(workspace_path).expanduser() if workspace_path else None
            manager.gateway_port = int(gateway_port) if gateway_port else None
            manager.telegram_bot_username = str(telegram_bot_username or "").strip().lstrip("@")
            manager.telegram_active_bot_usernames = [
                str(username).strip().lstrip("@")
                for username in (telegram_active_bot_usernames or [])
                if str(username or "").strip()
            ]
            manager.auto_dream_enabled = auto_dream_enabled
        return manager

    def main_gateway(self):
        return self.ensure_gateway(self.main_runtime_id())

    async def start(self):
        await self.start_all()

    async def stop(self):
        await self.stop_all()

    async def restart(self):
        await self.restart_all()

    async def start_all(self):
        for runtime in self.runtimes_from_state():
            if isinstance(runtime, dict) and runtime.get("identityId"):
                await self.start_identity(runtime["identityId"])
        if not self.runtimes_from_state():
            await self.start_identity(self.main_runtime_id())

    async def stop_all(self):
        runtime_ids = {
            str(runtime["identityId"])
            for runtime in self.runtimes_from_state()
            if isinstance(runtime, dict) and runtime.get("identityId")
        }
        runtime_ids.update(self.gateways.keys())
        if not runtime_ids:
            runtime_ids.add(self.main_runtime_id())
        for identity_id in list(runtime_ids):
            await self.stop_identity(identity_id)

    async def restart_all(self):
        await self.stop_all()
        await self.start_all()

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

    async def prune_to_state(self):
        active_ids = {
            str(runtime["identityId"])
            for runtime in self.runtimes_from_state()
            if isinstance(runtime, dict) and runtime.get("identityId")
        }
        for identity_id, manager in list(self.gateways.items()):
            if identity_id in active_ids:
                continue
            await manager.stop()
            self.logs.append(f"Stopped stale runtime: {manager.name}")
            del self.gateways[identity_id]

    def get_status(self):
        runtimes = []
        seen = set()
        for runtime in self.runtimes_from_state():
            if not isinstance(runtime, dict) or not runtime.get("identityId"):
                continue
            identity_id = str(runtime["identityId"])
            manager = self.ensure_gateway(identity_id)
            enabled_channels = []
            provider = "auto"
            try:
                config_path = runtime.get("configPath")
                if config_path:
                    config_data = json.loads(Path(config_path).read_text(encoding="utf-8"))
                    channels = config_data.get("channels", {}) if isinstance(config_data, dict) else {}
                    if isinstance(channels, dict):
                        enabled_channels = [
                            name
                            for name, channel in channels.items()
                            if isinstance(channel, dict) and channel.get("enabled") is True
                        ]
                    agents = config_data.get("agents", {}) if isinstance(config_data, dict) else {}
                    defaults = agents.get("defaults", {}) if isinstance(agents, dict) else {}
                    provider = defaults.get("provider") or "auto"
            except Exception:
                enabled_channels = []
            seen.add(identity_id)
            runtimes.append({
                **manager.get_status(),
                "slug": runtime.get("slug"),
                "isMain": bool(runtime.get("isMain")),
                "workspacePath": runtime.get("workspacePath"),
                "provider": provider,
                "enabledChannels": enabled_channels,
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


def runtime_entry_for_identity(identity_id):
    if not identity_id:
        return None
    state = load_morneven_runtime_state()
    runtimes = state.get("runtimes") if isinstance(state, dict) else None
    if not isinstance(runtimes, list):
        return None
    for runtime in runtimes:
        if isinstance(runtime, dict) and str(runtime.get("identityId") or "") == str(identity_id):
            return runtime
    return None


def runtime_identity_payload(runtime):
    if not isinstance(runtime, dict):
        return {}
    return {
        "id": runtime.get("identityId"),
        "slug": runtime.get("slug"),
        "name": runtime.get("name"),
        "isMain": bool(runtime.get("isMain")),
    }


def load_runtime_config_data(identity_id):
    runtime = runtime_entry_for_identity(identity_id)
    if not runtime:
        raise ValueError("Runtime identity is not available")
    config_path = runtime.get("configPath")
    if not config_path:
        raise ValueError("Runtime config path is not available")
    path = Path(config_path)
    if not path.exists():
        raise ValueError("Runtime config has not been materialized")
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("Runtime config is invalid")
    return runtime, path, data


def write_runtime_config_data(identity_id, body):
    runtime, config_path, existing_data = load_runtime_config_data(identity_id)
    merged = merge_secrets(body, existing_data)
    validated = Config.model_validate(merged)
    saved_data = validated.model_dump(by_alias=True)
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(json.dumps(saved_data, indent=2), encoding="utf-8")
    if runtime.get("isMain"):
        save_config(validated)
    return runtime, saved_data


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


def push_morneven_config_secrets(config_data, identity=None):
    if not MORNEVEN_BOT_MANAGER_SYNC_TOKEN:
        return {"synced": False, "reason": "MORNEVEN_BOT_MANAGER_SYNC_TOKEN is not configured"}
    urls = backend_base_urls()
    if not urls:
        return {"synced": False, "reason": "Morneven backend URL is not configured"}

    morneven_state = load_morneven_runtime_state()
    if identity is None:
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


TELEGRAM_USERNAME_CACHE: dict[str, str] = {}


def normalize_bot_username(value):
    return str(value or "").strip().lstrip("@")


def telegram_token_from_entry(entry):
    channels = entry.get("channels") if isinstance(entry, dict) else None
    if not isinstance(channels, dict):
        return ""
    telegram = channels.get("telegram")
    if not isinstance(telegram, dict) or telegram.get("enabled") is not True:
        return ""
    token = telegram.get("token")
    return str(token).strip() if token else ""


def telegram_username_for_token(token):
    token = str(token or "").strip()
    if not token:
        return ""
    token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
    if token_hash in TELEGRAM_USERNAME_CACHE:
        return TELEGRAM_USERNAME_CACHE[token_hash]
    try:
        request = urllib.request.Request(
            f"https://api.telegram.org/bot{urllib.parse.quote(token, safe=':')}/getMe",
            headers={"accept": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            payload = json.loads(response.read().decode("utf-8"))
        result = payload.get("result") if isinstance(payload, dict) else None
        username = normalize_bot_username(result.get("username") if isinstance(result, dict) else "")
        if username:
            TELEGRAM_USERNAME_CACHE[token_hash] = username
            return username
    except Exception:
        return ""
    return ""


def telegram_usernames_for_entries(entries):
    usernames_by_identity = {}
    active_usernames = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        identity = entry.get("identity") if isinstance(entry.get("identity"), dict) else {}
        identity_id = str(identity.get("id") or "")
        username = telegram_username_for_token(telegram_token_from_entry(entry))
        if username:
            if identity_id:
                usernames_by_identity[identity_id] = username
            active_usernames.append(username)
    return usernames_by_identity, active_usernames


def apply_bundle_to_config(bundle, workspace_path=WORKSPACE_PATH, config_path=None, persist_default=True, gateway_port=None):
    config = load_config()
    data = config.model_dump(by_alias=True)
    if config_path:
        # Runtime configs must not inherit channel tokens or provider secrets from
        # the global/default config, otherwise two active personalities can start
        # with the same Telegram token and fight over one polling session.
        data["providers"] = {}
        data["channels"] = {}

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
    gateway_config = data.setdefault("gateway", {})
    if gateway_port:
        gateway_config["port"] = int(gateway_port)
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


def runtime_auto_dream_enabled(settings):
    if not isinstance(settings, dict):
        return None
    auto_dream = settings.get("autoDream")
    if not isinstance(auto_dream, dict) or "enabled" not in auto_dream:
        return None
    value = auto_dream.get("enabled")
    if isinstance(value, str):
        return value.strip().lower() not in {"0", "false", "off", "no"}
    return bool(value)


def materialize_runtime_entry(
    entry,
    general_config,
    mode,
    main_identity_id,
    gateway_port,
    telegram_bot_username="",
    telegram_active_bot_usernames=None,
    persist_default=False,
):
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
    apply_bundle_to_config(
        runtime_bundle,
        workspace_path=workspace_path,
        config_path=config_path,
        persist_default=persist_default,
        gateway_port=gateway_port,
    )
    state = {
        "identityId": identity.get("id"),
        "slug": identity.get("slug"),
        "name": identity.get("name"),
        "roleTitle": identity.get("roleTitle"),
        "isMain": identity.get("id") == main_identity_id or bool(identity.get("isMain")),
        "runtimePath": str(runtime_dir),
        "workspacePath": str(workspace_path),
        "configPath": str(config_path),
        "gatewayPort": gateway_port,
        "telegramBotUsername": normalize_bot_username(telegram_bot_username) or None,
        "telegramActiveBotUsernames": [
            normalize_bot_username(username)
            for username in (telegram_active_bot_usernames or [])
            if normalize_bot_username(username)
        ],
        "autoDreamEnabled": runtime_auto_dream_enabled(entry.get("settings")),
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
    telegram_usernames_by_identity, telegram_active_usernames = telegram_usernames_for_entries(entries)
    runtimes = []
    for index, entry in enumerate(entries):
        identity = entry.get("identity") if isinstance(entry.get("identity"), dict) else {}
        identity_id = str(identity.get("id") or "")
        persist_default = str(identity.get("id")) == main_identity_id
        runtimes.append(materialize_runtime_entry(
            entry,
            general_config,
            bundle.get("mode", "single-active-personality"),
            main_identity_id,
            GATEWAY_BASE_PORT + index,
            telegram_usernames_by_identity.get(identity_id, ""),
            telegram_active_usernames,
            persist_default=persist_default,
        ))

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
            await gateway.prune_to_state()
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


async def api_runtime_config_get(request: Request):
    auth_err = require_auth(request)
    if auth_err:
        return auth_err
    try:
        identity_id = request.path_params.get("identity_id")
        runtime, _, data = load_runtime_config_data(identity_id)
        return JSONResponse({
            "identity": runtime_identity_payload(runtime),
            "config": mask_secrets(data),
        })
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=404)


async def api_runtime_config_put(request: Request):
    auth_err = require_auth(request)
    if auth_err:
        return auth_err
    identity_id = request.path_params.get("identity_id")
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)

    try:
        restart = body.pop("_restartGateway", False)
        async with config_lock:
            runtime, saved_data = write_runtime_config_data(identity_id, body)

        morneven_sync = await asyncio.to_thread(
            push_morneven_config_secrets,
            saved_data,
            runtime_identity_payload(runtime),
        )
        if not morneven_sync.get("synced"):
            gateway.logs.append(f"Morneven config secret push skipped: {morneven_sync.get('reason')}")

        if restart:
            asyncio.create_task(gateway.restart_identity(identity_id))

        return JSONResponse({
            "ok": True,
            "restarting": restart,
            "identity": runtime_identity_payload(runtime),
            "mornevenSync": morneven_sync,
        })
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


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


async def api_runtime_logs(request: Request):
    auth_err = require_auth(request)
    if auth_err:
        return auth_err
    identity_id = request.path_params.get("identity_id")
    manager = gateway.gateways.get(str(identity_id))
    runtime = runtime_entry_for_identity(identity_id)
    lines = []
    if manager:
        lines.extend(list(manager.logs)[-500:])
    if runtime and not lines:
        lines.append(f"[{runtime.get('name') or runtime.get('slug') or identity_id}] No runtime logs available.")
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
    Route("/api/runtimes/{identity_id}/config", api_runtime_config_get, methods=["GET"]),
    Route("/api/runtimes/{identity_id}/config", api_runtime_config_put, methods=["PUT"]),
    Route("/api/status", api_status),
    Route("/api/logs", api_logs),
    Route("/api/runtimes/{identity_id}/logs", api_runtime_logs),
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
        loop.create_task(gateway.stop_all())
        server.should_exit = True

    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, handle_signal)

    loop.run_until_complete(server.serve(sockets=sockets))
    loop.run_until_complete(gateway.stop_all())
