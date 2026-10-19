"""Exclusão mútua local da Recompra, mantida pelo sistema operacional.

O arquivo permanece no disco: removê-lo criaria uma corrida entre processos
com descritores apontando para arquivos diferentes. O kernel libera o bloqueio
ao encerrar o proprietário, inclusive quando ele não consegue executar finally.
"""

from __future__ import annotations

import errno
import json
import logging
import os
import socket
import stat
import sys
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

PROTOCOL = "nexoflow-recompra-os-lock-v1"
MAX_METADATA_BYTES = 16_384
DEFAULT_STATE = Path("logs/recompra_state.json")


class RecompraLockError(RuntimeError):
    """Outra execução está ativa ou a propriedade não pode ser comprovada."""


def _pid_is_alive(pid: int) -> bool | None:
    """False somente quando o SO confirma que o PID não está mais ativo."""
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel.WaitForSingleObject.restype = wintypes.DWORD
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.CloseHandle.restype = wintypes.BOOL
        handle = kernel.OpenProcess(0x00100000, False, pid)  # SYNCHRONIZE
        if not handle:
            # InvalidParameter é a resposta para um PID inexistente; acesso
            # negado e outros erros não comprovam encerramento.
            return False if ctypes.get_last_error() == 87 else None
        try:
            result = kernel.WaitForSingleObject(handle, 0)
            if result == 0:  # WAIT_OBJECT_0: processo encerrado.
                return False
            if result == 258:  # WAIT_TIMEOUT: processo ainda ativo.
                return True
            return None
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except (PermissionError, OSError):
        return None
    return True


def _acquire_os_lock(fd: int) -> None:
    if sys.platform == "win32":
        import msvcrt

        # Uma faixa além dos metadados permite ler PID/início para diagnóstico
        # durante a execução. O Windows permite locks além do fim do arquivo.
        os.lseek(fd, MAX_METADATA_BYTES, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
    else:
        import fcntl

        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)


def _release_os_lock(fd: int) -> None:
    if sys.platform == "win32":
        import msvcrt

        os.lseek(fd, MAX_METADATA_BYTES, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(fd, fcntl.LOCK_UN)


def _read_metadata(fd: int) -> object:
    os.lseek(fd, 0, os.SEEK_SET)
    # Não ler MAX+1: no Windows o byte MAX é a faixa de lock. Até uma
    # leitura que ultrapasse o EOF pode intersectar essa faixa e ser negada.
    if os.fstat(fd).st_size > MAX_METADATA_BYTES:
        raise ValueError("Metadados do bloqueio excedem o limite.")
    raw = os.read(fd, MAX_METADATA_BYTES)
    if os.fstat(fd).st_size > MAX_METADATA_BYTES:
        raise ValueError("Metadados do bloqueio excedem o limite.")
    return json.loads(raw.decode("utf-8"))


def _positive_pid(value: object) -> int | None:
    if type(value) is int and 0 < value <= 2_147_483_647:
        return value
    return None


def _owner_description(fd: int) -> str:
    try:
        record = _read_metadata(fd)
    except (OSError, ValueError, UnicodeError):
        return ""
    if isinstance(record, dict):
        pid = _positive_pid(record.get("pid"))
        if pid is not None:
            return f" PID {pid}."
    pid = _positive_pid(record)
    return f" PID {pid}." if pid is not None else ""


def _validate_existing(fd: int, path: Path) -> None:
    try:
        record = _read_metadata(fd)
    except (ValueError, UnicodeError) as exc:
        raise RecompraLockError(
            f"Bloqueio de recompra sem identificação válida em {path}. "
            "Não foi possível comprovar o encerramento do proprietário; "
            "preserve o arquivo para diagnóstico."
        ) from exc
    if isinstance(record, dict) and record.get("protocol") == PROTOCOL:
        if (
            record.get("schema") == 1
            and _positive_pid(record.get("pid")) is not None
            and isinstance(record.get("execution_id"), str)
            and record["execution_id"]
            and isinstance(record.get("started_utc"), str)
            and record["started_utc"]
            and record.get("status") in ("active", "released")
        ):
            # Já possuímos o lock do kernel. Um PID ainda existente pode ter
            # sido reutilizado ou pertencer a outra carga: não é o proprietário
            # deste lock. A aquisição pelo SO é a prova de liberação.
            return
        raise RecompraLockError(
            f"Metadados de bloqueio inválidos em {path}; preserve o arquivo."
        )
    legacy_pid = _positive_pid(record)
    if isinstance(record, dict) and "protocol" not in record:
        legacy_pid = _positive_pid(record.get("pid"))
        host = record.get("hostname")
        if host and host != socket.gethostname():
            legacy_pid = None
    if legacy_pid is None:
        raise RecompraLockError(
            f"Bloqueio de recompra desconhecido em {path}. "
            "Preserve o arquivo; não há comprovação segura de encerramento."
        )
    if _pid_is_alive(legacy_pid) is not False:
        raise RecompraLockError(
            f"A carga de recompra do PID {legacy_pid} está ativa ou seu "
            f"encerramento não pôde ser confirmado ({path}). "
            "Não remova o bloqueio enquanto essa execução puder estar ativa."
        )
    logging.warning(
        "Recompra: bloqueio legado recuperado após o sistema operacional "
        "confirmar o encerramento do PID %s.",
        legacy_pid,
    )


def _write_metadata(fd: int, record: dict) -> None:
    payload = (json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n").encode(
        "utf-8"
    )
    os.lseek(fd, 0, os.SEEK_SET)
    remaining = memoryview(payload)
    while remaining:
        written = os.write(fd, remaining)
        if not written:
            raise OSError("Não foi possível gravar os metadados do bloqueio.")
        remaining = remaining[written:]
    os.ftruncate(fd, len(payload))
    os.fsync(fd)


@contextmanager
def state_lock(path: Path | str = DEFAULT_STATE):
    """Bloqueia uma aplicação e preserva estado/auditoria existentes.

    Arquivos legados (PID simples) só são adotados após confirmação de que seu
    proprietário encerrou. Arquivos vazios ou corrompidos permanecem bloqueados
    para diagnóstico: idade ou falha ao consultar o processo não bastam.
    """
    state_path = Path(path)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = state_path.with_suffix(".lock")
    flags = os.O_RDWR | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(lock_path, flags | os.O_CREAT | os.O_EXCL, 0o600)
        created = True
    except FileExistsError:
        fd = os.open(lock_path, flags)
        created = False
    acquired = False
    record = None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise RecompraLockError("O bloqueio de recompra não é um arquivo regular.")
        try:
            _acquire_os_lock(fd)
        except OSError as exc:
            if exc.errno not in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                raise
            raise RecompraLockError(
                f"Outra aplicação da Recompra está em execução ({lock_path}). "
                f"{_owner_description(fd)} "
                "Aguarde sua conclusão; não remova o arquivo de bloqueio."
            ) from exc
        acquired = True
        if not created:
            _validate_existing(fd, lock_path)
        record = {
            "schema": 1,
            "protocol": PROTOCOL,
            "pid": os.getpid(),
            "execution_id": uuid4().hex,
            "started_utc": datetime.now(UTC).isoformat(),
            "hostname": socket.gethostname(),
            "status": "active",
        }
        _write_metadata(fd, record)
        yield record.copy()
    finally:
        try:
            if acquired:
                if record is not None:
                    try:
                        current = _read_metadata(fd)
                        if (
                            isinstance(current, dict)
                            and current.get("execution_id") == record["execution_id"]
                        ):
                            _write_metadata(
                                fd,
                                {
                                    **record,
                                    "status": "released",
                                    "finished_utc": datetime.now(UTC).isoformat(),
                                },
                            )
                    except (OSError, ValueError, UnicodeError):
                        logging.warning(
                            "Recompra: metadados finais do bloqueio não foram "
                            "gravados. O bloqueio do sistema operacional será "
                            "liberado; preserve o arquivo para diagnóstico."
                        )
                try:
                    _release_os_lock(fd)
                except OSError:
                    # Fechar nosso próprio descritor também libera o lock.
                    # Uma falha de limpeza não pode sugerir repetir ações já
                    # confirmadas na Weni, nem ocultar a exceção original.
                    logging.warning(
                        "Recompra: a liberação explícita do bloqueio falhou; "
                        "o descritor da execução será fechado."
                    )
        finally:
            os.close(fd)
