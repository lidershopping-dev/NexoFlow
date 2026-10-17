"""Arquivos privados e HTTP de leitura da deduplicação."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path

import requests
from gspread.exceptions import APIError
from gspread.http_client import HTTPClient


def digest(value):
    return hashlib.sha256(
        json.dumps(
            value, sort_keys=True, ensure_ascii=False, separators=(",", ":")
        ).encode()
    ).hexdigest()


def save_private(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temp = tempfile.mkstemp(dir=path.parent, prefix=".dedup-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        Path(temp).unlink(missing_ok=True)


@contextmanager
def execution_lock(directory):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock = directory / "execucao.lock"
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as exc:
        raise RuntimeError(
            "Outra deduplicação está em execução ou foi interrompida. Confira o processo antes de remover logs/duplicados/execucao.lock."
        ) from exc
    try:
        with os.fdopen(fd, "w") as stream:
            stream.write(str(os.getpid()))
        yield
    finally:
        lock.unlink(missing_ok=True)


class DedupHTTPClient(HTTPClient):
    """Somente GET admite repetição. POST/DELETE nunca têm retry automático."""

    _next_read = 0.0

    def request(self, method, *args, **kwargs):
        if method.upper() != "GET":
            return super().request(method, *args, **kwargs)
        for attempt in range(5):
            time.sleep(max(0, self._next_read - time.monotonic()))
            self._next_read = time.monotonic() + 1.3
            try:
                return super().request(method, *args, **kwargs)
            except APIError as exc:
                if (
                    exc.response.status_code not in (429, 500, 502, 503, 504)
                    or attempt == 4
                ):
                    raise
            except (requests.Timeout, requests.ConnectionError):
                if attempt == 4:
                    raise
            time.sleep(min(30, 2 ** (attempt + 1)))
