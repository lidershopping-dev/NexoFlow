"""Bloqueio entre processos e recuperação, sem APIs ou dados reais."""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from recompra_lock import (
    MAX_METADATA_BYTES,
    PROTOCOL,
    RecompraLockError,
    _read_metadata,
    state_lock,
)

APP = Path(__file__).resolve().parents[1] / "app"
CHILD = """
import sys
sys.path.insert(0, sys.argv[1])
from recompra_lock import RecompraLockError, state_lock
try:
    with state_lock(sys.argv[2]):
        print('LOCKED', flush=True)
        if sys.argv[3] == 'hold':
            sys.stdin.readline()
except RecompraLockError:
    print('BLOCKED', flush=True)
    sys.exit(42)
"""


class RecompraLockTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.state = Path(directory.name) / "recompra_state.json"
        self.lock = self.state.with_suffix(".lock")
        self.state.write_text('{"contacts": {"sintetico": {}}}', encoding="utf-8")
        self.before = self.state.read_bytes()

    def child(self, mode="hold"):
        process = subprocess.Popen(
            [sys.executable, "-u", "-c", CHILD, str(APP), str(self.state), mode],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
        )

        def cleanup():
            if process.poll() is None:
                process.kill()
            process.communicate(timeout=10)

        self.addCleanup(cleanup)
        return process

    def read_record(self):
        return json.loads(self.lock.read_text(encoding="utf-8"))

    def test_metadata_is_persistent_and_state_is_untouched(self):
        with state_lock(self.state) as record:
            self.assertEqual(record["protocol"], PROTOCOL)
            self.assertEqual(record["pid"], os.getpid())
            self.assertEqual(record["status"], "active")
            self.assertTrue(record["execution_id"])
            self.assertEqual(
                datetime.fromisoformat(record["started_utc"])
                .utcoffset()
                .total_seconds(),
                0,
            )
            self.assertTrue(record["hostname"])
        saved = self.read_record()
        self.assertEqual(saved["execution_id"], record["execution_id"])
        self.assertEqual(saved["status"], "released")
        self.assertIn("finished_utc", saved)
        self.assertEqual(self.state.read_bytes(), self.before)
        inode = self.lock.stat().st_ino
        with state_lock(self.state) as next_run:
            self.assertNotEqual(next_run["execution_id"], record["execution_id"])
        self.assertEqual(self.lock.stat().st_ino, inode)

    def test_same_process_cannot_acquire_twice(self):
        with state_lock(self.state):
            with self.assertRaisesRegex(RecompraLockError, "Outra aplicação"):
                with state_lock(self.state):
                    self.fail("Duas aplicações foram admitidas.")
        with state_lock(self.state):
            pass

    def test_live_process_blocks_parent_and_another_process(self):
        owner = self.child()
        self.assertEqual(owner.stdout.readline().strip(), "LOCKED")
        with self.assertRaisesRegex(RecompraLockError, f"PID {owner.pid}"):
            with state_lock(self.state):
                self.fail("O proprietário ainda está ativo.")
        contender = self.child("once")
        out, err = contender.communicate(timeout=10)
        self.assertEqual((contender.returncode, out.strip(), err), (42, "BLOCKED", ""))
        owner.communicate(input="encerrar\n", timeout=10)
        self.assertEqual(owner.returncode, 0)
        with state_lock(self.state):
            pass

    def test_release_never_overwrites_metadata_from_another_owner(self):
        with state_lock(self.state) as record:
            foreign = {**record, "execution_id": "outra-execucao-sintetica"}
            self.lock.write_text(json.dumps(foreign), encoding="utf-8")
        self.assertEqual(self.read_record(), foreign)
        with state_lock(self.state):
            pass

    def test_new_protocol_uses_kernel_lock_even_if_previous_pid_is_still_alive(self):
        with state_lock(self.state):
            pass
        stale = {**self.read_record(), "status": "active"}
        self.lock.write_text(json.dumps(stale), encoding="utf-8")
        # Um processo pode continuar vivo depois de liberar o lock. PID/idade
        # não substituem a comprovação do kernel para o novo protocolo.
        with patch("recompra_lock._pid_is_alive") as probe:
            with state_lock(self.state):
                pass
        probe.assert_not_called()

    def test_kernel_recovers_crashed_owner_without_unlink(self):
        owner = self.child()
        self.assertEqual(owner.stdout.readline().strip(), "LOCKED")
        inode = self.lock.stat().st_ino
        owner.kill()
        owner.communicate(timeout=10)
        stale = self.read_record()
        self.assertEqual(stale["status"], "active")
        with state_lock(self.state) as recovered:
            self.assertNotEqual(recovered["execution_id"], stale["execution_id"])
        self.assertEqual(self.lock.stat().st_ino, inode)
        self.assertEqual(self.state.read_bytes(), self.before)

    def test_exception_and_keyboard_interrupt_release_own_lock(self):
        for error in (RuntimeError("sintético"), KeyboardInterrupt()):
            with self.subTest(error=type(error).__name__):
                with self.assertRaises(type(error)):
                    with state_lock(self.state):
                        raise error
                self.assertEqual(self.read_record()["status"], "released")
                with state_lock(self.state):
                    pass

    def test_legacy_active_pid_is_never_adopted_or_removed(self):
        original = str(os.getpid())
        self.lock.write_text(original, encoding="utf-8")
        with self.assertRaisesRegex(RecompraLockError, "PID"):
            with state_lock(self.state):
                self.fail("Proprietário legado ativo.")
        self.assertEqual(self.lock.read_text(encoding="utf-8"), original)

    def test_legacy_unknown_owner_is_not_assumed_dead(self):
        self.lock.write_text("12345", encoding="utf-8")
        with patch("recompra_lock._pid_is_alive", return_value=None):
            with self.assertRaises(RecompraLockError):
                with state_lock(self.state):
                    self.fail("Ausência de permissão não comprova encerramento.")
        self.assertEqual(self.lock.read_text(encoding="utf-8"), "12345")

    def test_legacy_pid_from_reaped_process_is_recovered(self):
        owner = subprocess.Popen([sys.executable, "-c", "pass"])
        owner.wait(timeout=10)
        self.lock.write_text(str(owner.pid), encoding="utf-8")
        with state_lock(self.state):
            pass
        self.assertEqual(self.read_record()["status"], "released")

    def test_malformed_or_unsupported_legacy_metadata_remains_untouched(self):
        fixtures = (
            "",
            "inválido",
            "0",
            "-1",
            "true",
            "123.0",
            "2147483648",
            '{"pid": 123, "protocol": "desconhecido"}',
            '{"pid": 123, "hostname": "outro-computador-sintetico"}',
            json.dumps({"schema": 1, "protocol": PROTOCOL, "pid": 123, "status": []}),
            "x" * 16_385,
        )
        for content in fixtures:
            with self.subTest(content=content[:60]):
                self.lock.write_text(content, encoding="utf-8")
                with self.assertRaises(RecompraLockError):
                    with state_lock(self.state):
                        self.fail("Metadados sem prova de propriedade.")
                self.assertEqual(self.lock.read_text(encoding="utf-8"), content)

    def test_metadata_read_never_requests_the_windows_lock_byte(self):
        self.lock.write_text('{"pid": 12345}', encoding="utf-8")
        read = os.read
        with self.lock.open("rb") as handle:
            with patch("recompra_lock.os.read", wraps=read) as read_mock:
                self.assertEqual(_read_metadata(handle.fileno()), {"pid": 12345})
            read_mock.assert_called_once_with(handle.fileno(), MAX_METADATA_BYTES)

    def test_oversized_metadata_is_rejected_before_reading(self):
        self.lock.write_bytes(b"x" * (MAX_METADATA_BYTES + 1))
        with self.lock.open("rb") as handle:
            with patch("recompra_lock.os.read") as read_mock:
                with self.assertRaisesRegex(ValueError, "excedem o limite"):
                    _read_metadata(handle.fileno())
            read_mock.assert_not_called()

    def test_failure_to_write_final_metadata_does_not_mask_original_error(self):
        import recompra_lock

        write = recompra_lock._write_metadata
        calls = 0

        def fail_on_release(fd, value):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise PermissionError("bloqueio sintético")
            return write(fd, value)

        with patch("recompra_lock._write_metadata", side_effect=fail_on_release):
            with self.assertRaisesRegex(RuntimeError, "erro original"):
                with state_lock(self.state):
                    raise RuntimeError("erro original")
        with state_lock(self.state):
            pass

    def test_unlock_failure_still_closes_own_descriptor(self):
        with (
            patch(
                "recompra_lock._release_os_lock",
                side_effect=OSError("falha sintética ao liberar"),
            ),
            patch("recompra_lock.logging.warning") as warning,
        ):
            with state_lock(self.state):
                pass
        warning.assert_called_once()
        with state_lock(self.state):
            pass

    def test_successful_application_is_not_reported_failed_by_metadata_cleanup(self):
        import recompra_lock

        write = recompra_lock._write_metadata

        def fail_on_release(fd, value):
            if value["status"] == "released":
                raise PermissionError("bloqueio sintético")
            return write(fd, value)

        with (
            patch("recompra_lock._write_metadata", side_effect=fail_on_release),
            patch("recompra_lock.logging.warning") as warning,
        ):
            with state_lock(self.state):
                pass
        warning.assert_called_once()
        self.assertEqual(self.read_record()["status"], "active")
        with state_lock(self.state):
            pass

    def test_metadata_failure_before_application_releases_descriptor(self):
        with patch(
            "recompra_lock._write_metadata", side_effect=PermissionError("sintético")
        ):
            with self.assertRaises(PermissionError):
                with state_lock(self.state):
                    self.fail("A aplicação não pode iniciar sem metadados.")
        # O arquivo vazio permanece para diagnóstico, sem bloqueio do kernel.
        self.assertTrue(self.lock.exists())
        with patch("recompra_lock._validate_existing"):
            with state_lock(self.state):
                pass


if __name__ == "__main__":
    unittest.main()
