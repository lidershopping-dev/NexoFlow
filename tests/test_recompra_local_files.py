"""Gravação local com bloqueios simulados, sem APIs nem dados reais."""

import errno
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from recompra import atomic_json


class RecompraLocalFilesTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "state.json"
        atomic_json(self.path, {"anterior": "preservado"})

    def test_transient_lock_retries_same_flushed_file(self):
        original = os.replace
        calls = []

        def replace(source, destination):
            calls.append(source)
            if len(calls) < 3:
                raise PermissionError(errno.EACCES, "bloqueio sintético")
            original(source, destination)

        with (
            patch("recompra.os.replace", side_effect=replace),
            patch("recompra.sleep") as pause,
            patch("recompra.os.fsync", wraps=os.fsync) as flush,
        ):
            atomic_json(self.path, {"novo": "ação"})
        self.assertEqual(len(calls), 3)
        self.assertEqual(len(set(calls)), 1)
        flush.assert_called_once()
        self.assertEqual(pause.call_count, 2)
        self.assertEqual(
            json.loads(self.path.read_text(encoding="utf-8")), {"novo": "ação"}
        )
        self.assertEqual(list(self.path.parent.glob("recompra-*.json")), [])

    def test_persistent_lock_is_bounded_and_keeps_previous_state(self):
        before = self.path.read_bytes()
        with (
            patch(
                "recompra.os.replace",
                side_effect=PermissionError(errno.EACCES, "bloqueio"),
            ) as replace,
            patch("recompra.sleep") as pause,
        ):
            with self.assertRaises(PermissionError):
                atomic_json(self.path, {"novo": "não confirmado"})
        self.assertEqual(replace.call_count, 6)
        self.assertEqual(pause.call_count, 5)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(list(self.path.parent.glob("recompra-*.json")), [])

    def test_disk_full_does_not_retry_or_damage_state(self):
        before = self.path.read_bytes()
        with (
            patch(
                "recompra.os.replace", side_effect=OSError(errno.ENOSPC, "sem espaço")
            ) as replace,
            patch("recompra.sleep") as pause,
        ):
            with self.assertRaises(OSError) as result:
                atomic_json(self.path, {"novo": True})
        self.assertEqual(result.exception.errno, errno.ENOSPC)
        replace.assert_called_once()
        pause.assert_not_called()
        self.assertEqual(self.path.read_bytes(), before)

    def test_cleanup_failure_does_not_hide_original_error(self):
        with (
            patch(
                "recompra.os.replace", side_effect=OSError(errno.ENOSPC, "sem espaço")
            ),
            patch(
                "recompra.os.unlink",
                side_effect=PermissionError(errno.EACCES, "bloqueado"),
            ),
        ):
            with self.assertRaises(OSError) as result:
                atomic_json(self.path, {"novo": True})
        self.assertEqual(result.exception.errno, errno.ENOSPC)

    def test_cleanup_failure_after_success_does_not_report_false_failure(self):
        with patch(
            "recompra.os.unlink", side_effect=PermissionError(errno.EACCES, "bloqueado")
        ):
            atomic_json(self.path, {"novo": True})
        self.assertEqual(
            json.loads(self.path.read_text(encoding="utf-8")), {"novo": True}
        )
