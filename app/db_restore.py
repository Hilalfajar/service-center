"""Restore database PostgreSQL untuk Service Center."""

from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

ENGINE_LABELS = {"postgresql": "PostgreSQL"}
DEFAULT_PORTS = {"postgresql": "5432"}

_DB_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,62}$")
_PLAIN_SUFFIXES = {".sql"}
_CUSTOM_SUFFIXES = {".dump", ".backup", ".tar"}
_CLEAN_SQL = "DROP SCHEMA IF EXISTS public CASCADE; CREATE SCHEMA public;"


class RestoreError(RuntimeError):
    """Kesalahan yang bisa ditampilkan langsung ke pengguna."""


@dataclass(frozen=True)
class RestoreRequest:
    engine: str
    host: str
    port: str
    user: str
    password: str
    database: str
    dump: Path
    create_database: bool = True
    clean: bool = False

    def validate(self) -> None:
        if self.engine not in ENGINE_LABELS:
            raise RestoreError("Jenis database belum didukung")
        if not self.host.strip():
            raise RestoreError("Host wajib diisi")
        if not self.user.strip():
            raise RestoreError("User wajib diisi")
        if "\n" in self.host or "\n" in self.user:
            raise RestoreError("Host atau user tidak valid")
        if self.host.startswith("-") or self.user.startswith("-"):
            raise RestoreError("Host atau user tidak boleh diawali tanda minus")
        try:
            port = int(self.port)
        except ValueError as exc:
            raise RestoreError("Port harus berupa angka") from exc
        if not 1 <= port <= 65535:
            raise RestoreError("Port harus di antara 1 dan 65535")
        if not _DB_NAME.match(self.database):
            raise RestoreError(
                "Nama database hanya boleh huruf, angka, dan garis bawah, "
                "diawali huruf atau garis bawah"
            )
        if not self.dump.expanduser().is_file():
            raise RestoreError("File dump tidak ditemukan")

    @property
    def port_number(self) -> int:
        return int(self.port)


def quote_ident(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def quote_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def redact(text: str, password: str) -> str:
    if password:
        return text.replace(password, "***")
    return text


def detect_format(path: Path) -> str:
    try:
        with open(path, "rb") as handle:
            magic = handle.read(5)
    except OSError as exc:
        raise RestoreError(f"File dump tidak bisa dibaca: {exc}") from exc
    if magic == b"PGDMP":
        return "custom"
    suffix = path.suffix.lower()
    if suffix in _PLAIN_SUFFIXES:
        return "plain"
    if suffix in _CUSTOM_SUFFIXES:
        return "custom"
    raise RestoreError(
        "Format dump tidak dikenali. Gunakan .sql, .dump, .backup, atau .tar"
    )


def _tool(name: str) -> str:
    path = shutil.which(name)
    if not path:
        raise RestoreError(f"'{name}' tidak ditemukan. Pasang paket postgresql.")
    return path


def _env(password: str) -> dict[str, str]:
    env = os.environ.copy()
    env["PGPASSWORD"] = password
    env["PGCONNECT_TIMEOUT"] = "10"
    return env


def _psql_base(request: RestoreRequest) -> list[str]:
    return [
        _tool("psql"),
        "-X",
        "-w",
        "-h",
        request.host,
        "-p",
        str(request.port_number),
        "-U",
        request.user,
    ]


def _run_capture(
    command: list[str], request: RestoreRequest, timeout: float = 60
) -> subprocess.CompletedProcess:
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            env=_env(request.password),
            timeout=timeout,
        )
    except FileNotFoundError as exc:
        raise RestoreError(f"Perintah '{command[0]}' tidak ditemukan") from exc
    except subprocess.TimeoutExpired as exc:
        raise RestoreError("Perintah database melebihi batas waktu") from exc
    if result.returncode != 0:
        message = (result.stderr or result.stdout).strip()
        raise RestoreError(
            redact(message, request.password)
            or f"Perintah database gagal (kode {result.returncode})"
        )
    return result


def check_connection(request: RestoreRequest) -> None:
    result = _run_capture(
        _psql_base(request) + ["-d", "postgres", "-tAc", "SELECT 1"],
        request,
        timeout=20,
    )
    if result.stdout.strip() != "1":
        raise RestoreError("Koneksi PostgreSQL tidak merespons")


def database_exists(request: RestoreRequest) -> bool:
    result = _run_capture(
        _psql_base(request)
        + [
            "-d",
            "postgres",
            "-tAc",
            f"SELECT 1 FROM pg_database WHERE datname = {quote_literal(request.database)}",
        ],
        request,
    )
    return result.stdout.strip() == "1"


def create_database(request: RestoreRequest) -> None:
    _run_capture(
        _psql_base(request)
        + ["-d", "postgres", "-c", f"CREATE DATABASE {quote_ident(request.database)}"],
        request,
    )


def restore_command(request: RestoreRequest, dump_format: str) -> list[str]:
    if dump_format == "plain":
        command = _psql_base(request) + [
            "-d",
            request.database,
            "-v",
            "ON_ERROR_STOP=1",
            "-1",
        ]
        if request.clean:
            command += ["-c", _CLEAN_SQL]
        command += ["-f", str(request.dump)]
        return command
    if dump_format == "custom":
        command = [
            _tool("pg_restore"),
            "-w",
            "-h",
            request.host,
            "-p",
            str(request.port_number),
            "-U",
            request.user,
            "-d",
            request.database,
            "--no-owner",
            "--no-privileges",
            "--verbose",
            "--single-transaction",
        ]
        if request.clean:
            command += ["--clean", "--if-exists"]
        command.append(str(request.dump))
        return command
    raise RestoreError("Format dump tidak didukung")


def _run_stream(
    command: list[str], request: RestoreRequest, log: Callable[[str], None]
) -> None:
    log("$ " + shlex.join(command))
    try:
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            env=_env(request.password),
        )
    except FileNotFoundError as exc:
        raise RestoreError(f"Perintah '{command[0]}' tidak ditemukan") from exc

    assert process.stdout is not None
    for line in process.stdout:
        log(redact(line.rstrip("\n"), request.password))
    code = process.wait()
    if code != 0:
        raise RestoreError(f"Restore gagal (kode {code}). Lihat log di atas.")


def restore(
    request: RestoreRequest,
    log: Callable[[str], None] | None = None,
) -> None:
    request.validate()
    write = log or (lambda _message: None)
    dump_format = detect_format(request.dump)
    write(f"Format dump: {dump_format}")
    write(f"Koneksi ke {request.host}:{request.port} sebagai {request.user}")
    check_connection(request)

    if database_exists(request):
        write(f"Database {request.database} sudah ada")
    else:
        if not request.create_database:
            raise RestoreError(
                f"Database {request.database} belum ada. "
                "Aktifkan opsi buat database."
            )
        write(f"Membuat database {request.database}")
        create_database(request)

    if request.clean:
        write("Objek lama akan dibersihkan sebelum restore")
    write("Menjalankan restore")
    _run_stream(restore_command(request, dump_format), request, write)
    write("Restore selesai")
