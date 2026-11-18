"""Backend layanan systemd (user) untuk Service Center."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

APP_ID = "io.github.hilalfajar.ServiceCenter"
DATA_HOME = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
CONFIG_HOME = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
REGISTRY_FILE = DATA_HOME / "service-center" / "services.json"
UNIT_DIR = CONFIG_HOME / "systemd" / "user"
UNIT_PREFIX = "sc-"

RESTART_OPTIONS = ("on-failure", "always", "no")
RESTART_LABELS = {
    "on-failure": "Saat gagal",
    "always": "Selalu",
    "no": "Tidak",
}

_SAFE_ARG = re.compile(r"^[A-Za-z0-9_@%+=:,./-]+$")
_VENV_DIRS = (".venv", "venv", "env")


def _run(cmd: list[str], timeout: float = 30) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError as exc:
        raise RuntimeError(f"Perintah '{cmd[0]}' tidak ditemukan") from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"Perintah '{cmd[0]}' melebihi batas waktu") from exc


def systemctl(*args: str, timeout: float = 30) -> subprocess.CompletedProcess:
    return _run(["systemctl", "--user", *args], timeout=timeout)


def current_user() -> str:
    return os.environ.get("USER") or Path.home().name


def linger_enabled() -> bool:
    try:
        res = _run(["loginctl", "show-user", current_user(), "-p", "Linger"], timeout=10)
    except RuntimeError:
        return False
    return res.returncode == 0 and res.stdout.strip().endswith("yes")


def enable_linger() -> tuple[bool, str]:
    try:
        res = _run(["pkexec", "loginctl", "enable-linger", current_user()], timeout=120)
    except RuntimeError as exc:
        return False, str(exc)
    if res.returncode == 0:
        return True, ""
    return False, (res.stderr or res.stdout).strip() or "Gagal mengaktifkan linger"


def slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug or "layanan"


def pretty_name(path: Path) -> str:
    base = path.parent.name if path.is_file() else path.name
    base = re.sub(r"[-_]+", " ", base).strip()
    return base.title() or "Layanan"


def quote_unit_arg(value: str) -> str:
    if value and _SAFE_ARG.match(value):
        return value
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _quote_env(key: str, value: str) -> str:
    text = f"{key}={value}".replace("%", "%%")
    escaped = text.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _is_elf(path: Path) -> bool:
    try:
        with open(path, "rb") as handle:
            return handle.read(4) == b"\x7fELF"
    except OSError:
        return False


def _python_for(path: Path) -> str:
    for base in (path.parent, path.parent.parent):
        for venv in _VENV_DIRS:
            candidate = base / venv / "bin" / "python"
            if candidate.is_file() and _is_elf(candidate):
                return str(candidate)
    return shutil.which("python3") or "/usr/bin/python3"


def detect_command(path: Path) -> tuple[list[str], Path]:
    """Tebak perintah dan direktori kerja dari file yang dipilih."""
    path = path.expanduser()
    if not path.exists():
        raise ValueError("File tidak ditemukan")
    workdir = path.parent if path.is_file() else path

    if path.name == "package.json":
        try:
            pkg = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError("package.json tidak bisa dibaca") from exc
        scripts = pkg.get("scripts") or {}
        start = scripts.get("start")
        if start:
            return [shutil.which("npm") or "npm", "start"], workdir
        main = pkg.get("main")
        if main and (workdir / main).is_file():
            return [shutil.which("node") or "/usr/bin/node", str(workdir / main)], workdir
        if (workdir / "index.js").is_file():
            return [shutil.which("node") or "/usr/bin/node", str(workdir / "index.js")], workdir
        raise ValueError("package.json tidak punya scripts.start atau main. Isi perintah manual")

    suffix = path.suffix.lower()
    if suffix in {".js", ".mjs", ".cjs"}:
        return [shutil.which("node") or "/usr/bin/node", str(path)], workdir
    if suffix == ".py":
        return [_python_for(path), str(path)], workdir
    if suffix in {".sh", ".bash"}:
        return ["bash", str(path)], workdir
    if suffix == ".jar":
        return [shutil.which("java") or "java", "-jar", str(path)], workdir
    if path.is_file() and os.access(path, os.X_OK):
        return [str(path)], workdir
    raise ValueError(
        "Tipe file tidak dikenali. Pilih .py, .js, .sh, .jar, package.json, atau binary"
    )


@dataclass
class Service:
    id: str
    name: str
    file: str
    command: list[str]
    workdir: str
    env: dict[str, str] = field(default_factory=dict)
    restart: str = "on-failure"
    created_at: float = field(default_factory=time.time)

    @property
    def unit_name(self) -> str:
        return f"{UNIT_PREFIX}{self.id}.service"

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "file": self.file,
            "command": self.command,
            "workdir": self.workdir,
            "env": self.env,
            "restart": self.restart,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, sid: str, data: dict) -> "Service":
        return cls(
            id=sid,
            name=str(data["name"]),
            file=str(data["file"]),
            command=[str(part) for part in data["command"]],
            workdir=str(data["workdir"]),
            env={str(k): str(v) for k, v in (data.get("env") or {}).items()},
            restart=str(data.get("restart", "on-failure")),
            created_at=float(data.get("created_at", time.time())),
        )


def build_unit(service: Service) -> str:
    exec_start = " ".join(quote_unit_arg(part) for part in service.command)
    restart = service.restart if service.restart in RESTART_OPTIONS else "on-failure"
    lines = [
        "[Unit]",
        f"Description={service.name.replace('%', '%%')}",
        f"ConditionPathExists={quote_unit_arg(service.file)}",
        "After=network.target",
        "",
        "[Service]",
        "Type=simple",
        f"WorkingDirectory={quote_unit_arg(service.workdir)}",
        f"ExecStart={exec_start}",
        f"Restart={restart}",
        "RestartSec=3",
        "SyslogIdentifier=service-center",
    ]
    for key, value in service.env.items():
        key = key.strip()
        if key:
            lines.append(f"Environment={_quote_env(key, value)}")
    lines += ["", "[Install]", "WantedBy=default.target", ""]
    return "\n".join(lines)


@dataclass
class ServiceState:
    active: str = "inactive"
    sub: str = "dead"
    enabled: str = ""
    pid: str = ""

    @property
    def running(self) -> bool:
        return self.active == "active"

    @property
    def failed(self) -> bool:
        return self.active == "failed"


def parse_show(output: str) -> dict[str, ServiceState]:
    states: dict[str, ServiceState] = {}
    block: dict[str, str] = {}

    def flush() -> None:
        unit = block.get("Id")
        if unit:
            states[unit] = ServiceState(
                active=block.get("ActiveState", "inactive"),
                sub=block.get("SubState", "dead"),
                enabled=block.get("UnitFileState", ""),
                pid=block.get("MainPID", ""),
            )

    for line in output.splitlines():
        if not line.strip():
            flush()
            block = {}
            continue
        if "=" in line:
            key, _, value = line.partition("=")
            block[key] = value
    flush()
    return states


class Manager:
    def __init__(
        self,
        registry_file: Path = REGISTRY_FILE,
        unit_dir: Path = UNIT_DIR,
    ) -> None:
        self.registry_file = Path(registry_file)
        self.unit_dir = Path(unit_dir)
        self._services: dict[str, Service] = self._load()

    def _load(self) -> dict[str, Service]:
        try:
            raw = json.loads(self.registry_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        services: dict[str, Service] = {}
        for sid, data in (raw.get("services") or {}).items():
            try:
                services[sid] = Service.from_dict(sid, data)
            except (KeyError, TypeError, ValueError):
                continue
        return services

    def reload(self) -> None:
        self._services = self._load()

    def save(self) -> None:
        self.registry_file.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": 1,
            "services": {sid: svc.to_dict() for sid, svc in self._services.items()},
        }
        self.registry_file.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )

    def services(self) -> list[Service]:
        return sorted(self._services.values(), key=lambda svc: svc.name.lower())

    def get(self, sid: str) -> Service | None:
        return self._services.get(sid)

    def unit_path(self, sid: str) -> Path:
        return self.unit_dir / f"{UNIT_PREFIX}{sid}.service"

    def unit_exists(self, sid: str) -> bool:
        return self.unit_path(sid).exists()

    def _require(self, sid: str) -> Service:
        service = self._services.get(sid)
        if service is None:
            raise ValueError("Layanan tidak ditemukan")
        return service

    def states(self, units: list[str] | None = None) -> dict[str, ServiceState]:
        if units is None:
            units = [svc.unit_name for svc in self.services()]
        if not units:
            return {}
        res = systemctl(
            "show",
            *units,
            "-p",
            "Id",
            "-p",
            "ActiveState",
            "-p",
            "SubState",
            "-p",
            "UnitFileState",
            "-p",
            "MainPID",
        )
        if res.returncode != 0:
            return {}
        return parse_show(res.stdout)

    def daemon_reload(self) -> subprocess.CompletedProcess:
        return systemctl("daemon-reload")

    def _unique_id(self, base: str) -> str:
        candidate = base
        counter = 2
        while candidate in self._services or self.unit_path(candidate).exists():
            candidate = f"{base}-{counter}"
            counter += 1
        return candidate

    def add(
        self,
        *,
        name: str,
        file: str,
        command: list[str],
        workdir: str,
        env: dict[str, str],
        restart: str,
        autostart: bool,
        start_now: bool,
    ) -> tuple[Service, list[str]]:
        name = name.strip()
        if not name:
            raise ValueError("Nama layanan wajib diisi")
        file_path = Path(file).expanduser()
        if not file_path.exists():
            raise ValueError("File program tidak ditemukan")
        workdir_path = Path(workdir).expanduser()
        if not workdir_path.is_dir():
            raise ValueError("Direktori kerja tidak ditemukan")
        if not command:
            raise ValueError("Perintah kosong")
        if restart not in RESTART_OPTIONS:
            restart = "on-failure"

        sid = self._unique_id(slugify(name))
        service = Service(
            id=sid,
            name=name,
            file=str(file_path),
            command=list(command),
            workdir=str(workdir_path),
            env=dict(env),
            restart=restart,
        )

        self.unit_dir.mkdir(parents=True, exist_ok=True)
        self.unit_path(sid).write_text(build_unit(service), encoding="utf-8")
        self._services[sid] = service
        self.save()

        warnings: list[str] = []
        reload_res = self.daemon_reload()
        if reload_res.returncode != 0:
            warnings.append(
                (reload_res.stderr or reload_res.stdout).strip() or "daemon-reload gagal"
            )
        if autostart:
            ok, message = self.set_enabled(sid, True)
            if not ok:
                warnings.append(f"Autostart gagal: {message}")
        if start_now:
            ok, message = self.start(sid)
            if not ok:
                warnings.append(f"Gagal menjalankan: {message}")
        return service, warnings

    def action(self, sid: str, verb: str) -> tuple[bool, str]:
        if verb not in {"start", "stop", "restart"}:
            raise ValueError(f"Aksi tidak dikenal: {verb}")
        service = self._require(sid)
        res = systemctl(verb, service.unit_name)
        if res.returncode == 0:
            return True, ""
        return False, (res.stderr or res.stdout).strip() or f"{verb} gagal"

    def start(self, sid: str) -> tuple[bool, str]:
        return self.action(sid, "start")

    def stop(self, sid: str) -> tuple[bool, str]:
        return self.action(sid, "stop")

    def restart(self, sid: str) -> tuple[bool, str]:
        return self.action(sid, "restart")

    def set_enabled(self, sid: str, enabled: bool) -> tuple[bool, str]:
        service = self._require(sid)
        verb = "enable" if enabled else "disable"
        res = systemctl(verb, service.unit_name)
        if res.returncode == 0:
            return True, ""
        return False, (res.stderr or res.stdout).strip() or f"{verb} gagal"

    def logs(self, sid: str, lines: int = 200) -> tuple[bool, str]:
        service = self._require(sid)
        res = _run(
            [
                "journalctl",
                "--user",
                "-u",
                service.unit_name,
                "-n",
                str(lines),
                "--no-pager",
                "-o",
                "short-iso",
            ],
            timeout=20,
        )
        if res.returncode != 0:
            return False, (res.stderr or res.stdout).strip() or "journalctl gagal"
        return True, res.stdout

    def delete(self, sid: str) -> None:
        service = self._require(sid)
        systemctl("stop", service.unit_name)
        systemctl("disable", service.unit_name)
        path = self.unit_path(sid)
        if path.exists():
            path.unlink()
        self._services.pop(sid, None)
        self.save()
        self.daemon_reload()
        systemctl("reset-failed", service.unit_name)
