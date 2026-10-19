"""Revisões sintéticas: retomada e preservação, sem serviços externos."""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from run_delivery_tickets import _archive_delivery_review, main

ROOT = Path(__file__).resolve().parents[1]


class DeliveryResumeCliTests(unittest.TestCase):
    def run_main(self, args, *, result=None, prepare_error=None):
        with ExitStack() as stack:
            stack.enter_context(patch("sys.argv", ["run_delivery_tickets.py", *args]))
            stack.enter_context(patch("logging_config.configure_logging"))
            stack.enter_context(patch("logging.error"))
            stack.enter_context(patch("proxy_config.configure_proxy"))
            printed = stack.enter_context(patch("builtins.print"))
            weni_factory = stack.enter_context(patch("weni_client.WeniClient"))
            stack.enter_context(patch("weni_update._open_farmalider"))
            stack.enter_context(
                patch("weni_update._available_weni_fields", return_value=set())
            )
            stack.enter_context(
                patch(
                    "delivery_spreadsheets.read_delivery_spreadsheets",
                    return_value=([], [], {"summary": {}}),
                )
            )
            stack.enter_context(
                patch(
                    "delivery_spreadsheets.validate_current_contacts",
                    return_value=([], [], {}),
                )
            )
            stack.enter_context(
                patch(
                    "delivery_tickets.reconcile_tickets",
                    return_value=([], {"tickets": [], "orders": []}),
                    side_effect=prepare_error,
                )
            )
            writer = stack.enter_context(
                patch("delivery_tickets.write_delivery_review")
            )
            applied = stack.enter_context(
                patch(
                    "delivery_tickets.apply_delivery_review",
                    return_value=result or {"conflicts": 0},
                )
            )
            exit_code = main()
        return exit_code, writer, applied, weni_factory, printed

    def test_existing_review_blocks_replacement_before_opening_clients(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "delivery_weni_review.json"
            original = b'{"snapshot": "revisado"}'
            path.write_bytes(original)
            code, writer, applied, client, _ = self.run_main(
                ["--dry-run", "--planilhas", "--review-out", str(path)]
            )
            self.assertEqual(code, 1)
            self.assertEqual(path.read_bytes(), original)
            self.assertFalse((path.parent / "delivery_historico").exists())
        writer.assert_not_called()
        applied.assert_not_called()
        client.assert_not_called()

    def test_archive_preserves_review_report_and_conflict_evidence_exactly(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "delivery_weni_review.json"
            report = path.with_suffix(".txt")
            conflict = path.parent / "privado" / f"{path.stem}_conflitos.json"
            conflict.parent.mkdir()
            originals = {
                path: b'{"snapshot": "imutavel", "sha256":"sintetico"}',
                report: "Revisão humana: ação sintética.\n".encode("utf-8-sig"),
                conflict: b'{"conflicts": [{"reference": "sintetica"}]}',
            }
            for file, content in originals.items():
                file.write_bytes(content)
            archive = _archive_delivery_review(path)
            again = _archive_delivery_review(path)
            self.assertNotEqual(archive, again)
            for file, content in originals.items():
                self.assertEqual((archive / file.name).read_bytes(), content)
                self.assertEqual(file.read_bytes(), content)
                if os.name != "nt":
                    self.assertEqual(
                        (archive / file.name).stat().st_mode & 0o777, 0o600
                    )

    def test_explicit_new_review_archives_old_snapshot_before_writer(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "delivery_weni_review.json"
            original = b'{"snapshot": "revisado"}'
            path.write_bytes(original)
            code, writer, applied, _, _ = self.run_main(
                [
                    "--dry-run",
                    "--planilhas",
                    "--review-out",
                    str(path),
                    "--archive-review",
                ]
            )
            self.assertEqual(code, 0)
            copies = list((path.parent / "delivery_historico").glob(f"*/{path.name}"))
            self.assertEqual(len(copies), 1)
            self.assertEqual(copies[0].read_bytes(), original)
            self.assertEqual(path.read_bytes(), original)
        writer.assert_called_once()
        applied.assert_not_called()

    def test_failed_preparation_preserves_previous_review_without_archiving(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "delivery_weni_review.json"
            original = b'{"snapshot": "revisado"}'
            path.write_bytes(original)
            code, writer, _, _, _ = self.run_main(
                [
                    "--dry-run",
                    "--planilhas",
                    "--review-out",
                    str(path),
                    "--archive-review",
                ],
                prepare_error=RuntimeError("Falha sintética"),
            )
            self.assertEqual(code, 1)
            self.assertEqual(path.read_bytes(), original)
            self.assertFalse((path.parent / "delivery_historico").exists())
        writer.assert_not_called()

    def test_review_created_during_preparation_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "delivery_weni_review.json"
            original = b'{"snapshot": "revisado"}'

            def prepare(*args, **kwargs):
                path.write_bytes(original)
                return [], {"tickets": [], "orders": []}

            code, writer, _, _, _ = self.run_main(
                ["--dry-run", "--planilhas", "--review-out", str(path)],
                prepare_error=prepare,
            )
            self.assertEqual(code, 1)
            self.assertEqual(path.read_bytes(), original)
        writer.assert_not_called()

    def test_resume_calls_only_apply_and_partial_result_is_warning(self):
        snapshot = Path("logs/delivery_weni_review.json")
        code, writer, applied, _, printed = self.run_main(
            ["--apply", "--require-review", str(snapshot), "--target", "weni"],
            result={
                "contacts_accepted": 12,
                "conflicts": 1,
                "conflict_report": "logs/privado/synthetic_conflitos.json",
                "conflict_reasons": {"CAMPO_ALTERADO": 1},
            },
        )
        self.assertEqual(code, 3)
        self.assertEqual(applied.call_args.args[0], snapshot)
        writer.assert_not_called()
        output = "\n".join(str(call.args[0]) for call in printed.call_args_list)
        self.assertIn("[AVISO] Reconciliação parcial", output)
        self.assertIn("Diagnóstico privado", output)
        self.assertNotIn("[OK] Reconciliação aplicada", output)

    def test_archive_failure_does_not_call_review_writer(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "delivery_weni_review.json"
            path.write_text("{}", encoding="utf-8")
            with patch(
                "run_delivery_tickets._archive_delivery_review",
                side_effect=PermissionError("Falha sintética"),
            ):
                code, writer, _, _, _ = self.run_main(
                    [
                        "--dry-run",
                        "--planilhas",
                        "--review-out",
                        str(path),
                        "--archive-review",
                    ]
                )
            self.assertEqual(code, 1)
            self.assertEqual(path.read_text(encoding="utf-8"), "{}")
        writer.assert_not_called()


@unittest.skipUnless(os.name == "nt", "Requer CMD do Windows")
class DeliveryResumeBatchWindowsTests(unittest.TestCase):
    def test_resume_menu_reuses_snapshot_and_keeps_partial_exit_code(self):
        with tempfile.TemporaryDirectory(prefix="nexo retomar ") as temporary:
            root = Path(temporary)
            (root / "app").mkdir()
            (root / "comandos").mkdir()
            (root / "scripts").mkdir()
            (root / "logs").mkdir()
            path = root / "logs/delivery_weni_review.json"
            original = b'{"snapshot": "revisado"}'
            path.write_bytes(original)
            path.with_suffix(".txt").write_text("Revisão sintética.", encoding="utf-8")
            (root / "app/run_delivery_tickets.py").write_text(
                "import json,sys\nfrom pathlib import Path\n"
                "args=sys.argv[1:]\n"
                "Path('called.json').write_text(json.dumps(args))\n"
                "raise SystemExit(3 if '--apply' in args else 99)\n",
                encoding="utf-8",
            )
            (root / "menu_choice.py").write_text(
                "raise SystemExit(1)\n", encoding="utf-8"
            )
            (root / "scripts/confirmar_revisao_weni.ps1").write_text(
                "exit 0\n", encoding="utf-8-sig"
            )
            batch = (ROOT / "comandos/carga_planilhas.bat").read_text(encoding="utf-8")
            batch = batch.replace('".venv\\Scripts\\python.exe"', f'"{sys.executable}"')
            batch = batch.replace(
                'choice /C 123 /N /M "Escolha uma opção: "',
                f'"{sys.executable}" menu_choice.py',
            )
            batch = batch.replace(
                'start "" notepad.exe "%CD%\\%REVIEW_TXT%" >nul 2>&1',
                '"%SystemRoot%\\System32\\cmd.exe" /d /c exit 0',
            )
            (root / "comandos/carga_planilhas.bat").write_text(batch, encoding="utf-8")
            process = subprocess.run(
                [
                    os.environ.get("COMSPEC", "cmd.exe"),
                    "/d",
                    "/c",
                    "comandos\\carga_planilhas.bat",
                    "weni",
                ],
                cwd=root,
                env={**os.environ, "NEXOFLOW_LAUNCHER": "1"},
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=30,
                check=False,
            )
            output = process.stdout.decode("utf-8", errors="replace")
            self.assertEqual(process.returncode, 3, output)
            self.assertEqual(path.read_bytes(), original)
            args = json.loads((root / "called.json").read_text(encoding="utf-8"))
            self.assertIn("--apply", args)
            self.assertNotIn("--dry-run", args)
            self.assertEqual(
                args[args.index("--require-review") + 1],
                "logs\\delivery_weni_review.json",
            )
            self.assertIn("[AVISO] Carga parcial", output)
            self.assertNotIn("[ERRO] Aplicação", output)
