"""Exercise the Windows review gate without opening UI or calling services."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

REPOSITORY = Path(__file__).resolve().parents[1]


@unittest.skipUnless(os.name == "nt", "Requires Windows CMD and Windows PowerShell")
class WeniReviewBatchWindowsTests(unittest.TestCase):
    """Run the real BAT, replacing only its external service and UI calls."""

    def test_native_weni_launcher_runs_after_icon_update(self):
        with tempfile.TemporaryDirectory(prefix="nexo weni icon ") as temporary:
            directory = Path(temporary)
            (directory / "comandos").mkdir()
            executable = directory / "NexoFlow Weni.exe"
            shutil.copy2(REPOSITORY / executable.name, executable)
            (directory / "comandos/atualizar_weni.bat").write_text(
                "@echo off\necho SYNTHETIC_WENI_LAUNCH_OK\nexit /b 0\n",
                encoding="ascii",
            )
            completed = subprocess.run(
                [str(executable)],
                cwd=directory,
                env={**os.environ, "NEXOFLOW_INTERNAL_BUILD_INFO": "1"},
                capture_output=True,
                timeout=15,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertIn(b"SYNTHETIC_WENI_LAUNCH_OK", completed.stdout)
            self.assertIn(b"NexoFlowWeniIconRevision=1.0", completed.stdout)

    def _wait_until(
        self, predicate, process, output_path: Path, description: str
    ) -> None:
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if predicate():
                return
            if process.poll() is not None:
                self.fail(
                    f"CMD exited before {description}: {process.returncode}\n"
                    f"{output_path.read_text(encoding='utf-8', errors='replace')}"
                )
            time.sleep(0.05)
        self.fail(
            f"Timed out waiting for {description}\n{output_path.read_text(encoding='utf-8', errors='replace')}"
        )

    def _close_process(self, process) -> None:
        if process.poll() is None:
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=10,
                check=False,
            )
            process.wait(timeout=10)
        if process.stdin:
            process.stdin.close()

    def _fixture(self, directory: Path) -> None:
        (directory / "app").mkdir()
        (directory / "comandos").mkdir()
        (directory / "scripts").mkdir()
        (directory / "scripts" / "confirmar_revisao_weni.ps1").write_bytes(
            (REPOSITORY / "scripts" / "confirmar_revisao_weni.ps1").read_bytes()
        )
        for name in ("comandos/check-up.bat", "comandos/testar_update_weni.bat"):
            (directory / name).write_text("@echo off\nexit /b 0\n", encoding="ascii")
        (directory / "app/run_update_weni.py").write_text(
            """import json
import sys
from pathlib import Path

args = sys.argv[1:]
if '--dry-run' in args:
    snapshot = Path(args[args.index('--review-out') + 1])
    snapshot.parent.mkdir(parents=True, exist_ok=True)
    payload = {'fixture': 'frozen-review-snapshot', 'dry_run_args': args}
    snapshot.write_text(json.dumps(payload), encoding='utf-8')
    snapshot.with_suffix('.txt').write_text('Synthetic review, no customer data.', encoding='utf-8')
    Path('dry_run.json').write_text(json.dumps(args), encoding='utf-8')
elif '--apply' in args:
    snapshot = Path(args[args.index('--require-review') + 1])
    payload = json.loads(snapshot.read_text(encoding='utf-8'))
    Path('apply.json').write_text(json.dumps({'args': args, 'snapshot': payload}), encoding='utf-8')
    if Path('apply_error').exists(): raise SystemExit(int(Path('apply_error').read_text()))
else:
    raise SystemExit('Unexpected fixture command')
""",
            encoding="utf-8",
        )
        (directory / "notepad_stub.py").write_text(
            """import json
import sys
from pathlib import Path
Path('notepad_closed.json').write_text(json.dumps(sys.argv[1:]), encoding='utf-8')
""",
            encoding="utf-8",
        )
        (directory / "review_gate_stub.py").write_text(
            """import json
import sys
import time
from pathlib import Path
Path('review_waiting.json').write_text(json.dumps(sys.argv[1:]), encoding='utf-8')
deadline = time.monotonic() + 30
decision = Path('review_decision.txt')
while not decision.exists():
    if time.monotonic() >= deadline:
        raise SystemExit(90)
    time.sleep(0.05)
raise SystemExit(int(decision.read_text(encoding='ascii')))
""",
            encoding="utf-8",
        )

        (directory / "scripts" / "resultado_carga_weni.ps1").write_text(
            "fixture", encoding="ascii"
        )
        (directory / "result_stub.py").write_text(
            "import sys,time,json\nfrom pathlib import Path\n"
            "args=sys.argv[1:]\n"
            "if '-RecordOnly' in args: raise SystemExit(0)\n"
            "Path('result_waiting.json').write_text(json.dumps(args))\n"
            "while not Path('result_ack').exists(): time.sleep(.05)\n",
            encoding="utf-8",
        )
        batch = (REPOSITORY / "comandos/atualizar_weni_carga_completa.bat").read_text(
            encoding="utf-8"
        )
        # Production continues using CON. Only the isolated fixture accepts a pipe.
        batch = batch.replace(" < CON", "")
        self.assertIn('".venv\\Scripts\\python.exe"', batch)
        batch = batch.replace('".venv\\Scripts\\python.exe"', f'"{sys.executable}"')
        batch, note_count = re.subn(
            r'(?m)^start "" notepad\.exe (.+)$',
            lambda match: (
                f'start "" /b "{sys.executable}" "{directory / "notepad_stub.py"}" '
                + match.group(1)
            ),
            batch,
        )
        self.assertEqual(
            note_count, 1, "Notepad fixture must replace exactly one external call"
        )
        batch, gate_count = re.subn(
            r'(?m)^"%SystemRoot%\\System32\\WindowsPowerShell\\v1\.0\\powershell\.exe"[^\n]*confirmar_revisao_weni\.ps1[^\n]*$',
            lambda match: (
                f'"{sys.executable}" "{directory / "review_gate_stub.py"}" %CONFIRM_PART%'
            ),
            batch,
        )
        self.assertEqual(
            gate_count, 1, "Review fixture must replace exactly one external call"
        )
        batch = re.sub(
            r'(?m)^"%SystemRoot%\\System32\\WindowsPowerShell\\v1\.0\\powershell\.exe"[^\n]*resultado_carga_weni\.ps1"(.*)$',
            lambda match: (
                f'"{sys.executable}" "{directory / "result_stub.py"}"' + match.group(1)
            ),
            batch,
        )
        (directory / "comandos/atualizar_weni_carga_completa.bat").write_text(
            batch, encoding="utf-8"
        )

    def _run_batch_case(
        self,
        *,
        part: int,
        decision: int,
        closed_stdin=False,
        direct=False,
        shared_launcher=False,
        apply_code=0,
    ) -> None:
        with tempfile.TemporaryDirectory(prefix="nexoflow review ") as temporary:
            directory = Path(temporary)
            self._fixture(directory)
            if apply_code:
                (directory / "apply_error").write_text(str(apply_code))
            output_path = directory / "console.log"
            env = {**os.environ, "NEXOFLOW_LAUNCHER": "1"}
            env.pop("NEXOFLOW_WENI_SESSION", None)
            env.pop("NEXOFLOW_TERMINAL", None)
            if direct:
                env.pop("NEXOFLOW_LAUNCHER", None)
            elif not shared_launcher:
                env["NEXOFLOW_TERMINAL"] = "1"
            with output_path.open("wb") as output:
                process = subprocess.Popen(
                    [
                        os.environ.get("COMSPEC", "cmd.exe"),
                        "/d",
                        "/c",
                        "comandos/atualizar_weni_carga_completa.bat".replace("/", "\\"),
                    ],
                    cwd=directory,
                    stdin=subprocess.PIPE,
                    env=env,
                    stdout=output,
                    stderr=subprocess.STDOUT,
                )
                try:
                    process.stdin.write(f"{part}\r\n".encode("ascii"))
                    process.stdin.flush()
                    if closed_stdin:
                        process.stdin.close()
                    self._wait_until(
                        lambda: (
                            (directory / "review_waiting.json").exists()
                            and (directory / "notepad_closed.json").exists()
                        ),
                        process,
                        output_path,
                        "the human review gate after Notepad has closed",
                    )
                    time.sleep(0.2)
                    self.assertIsNone(
                        process.poll(), "Closing Notepad must not close the CMD"
                    )
                    self.assertFalse(
                        (directory / "apply.json").exists(),
                        "Apply must wait for approval",
                    )

                    suffix = f"_25_parte_{part}" if part else ""
                    snapshot_name = f"logs\\weni_update_review_full{suffix}.json"
                    review_name = f"weni_update_review_full{suffix}.txt"
                    note_args = json.loads(
                        (directory / "notepad_closed.json").read_text(encoding="utf-8")
                    )
                    self.assertEqual(len(note_args), 1)
                    self.assertEqual(Path(note_args[0]).name, review_name)
                    self.assertTrue(Path(note_args[0]).is_absolute())
                    gate_args = json.loads(
                        (directory / "review_waiting.json").read_text(encoding="utf-8")
                    )
                    self.assertEqual(gate_args, ["-Part", str(part)] if part else [])

                    dry_args = json.loads(
                        (directory / "dry_run.json").read_text(encoding="utf-8")
                    )
                    self.assertIn("--full", dry_args)
                    self.assertEqual(
                        dry_args[dry_args.index("--review-out") + 1], snapshot_name
                    )
                    if part:
                        self.assertEqual(
                            dry_args[dry_args.index("--quarter") + 1], str(part)
                        )
                    else:
                        self.assertNotIn("--quarter", dry_args)

                    pending = directory / "review_decision.pending"
                    pending.write_text(str(decision), encoding="ascii")
                    os.replace(pending, directory / "review_decision.txt")
                    expected_output = {
                        0: "[ETAPA 6/6]",
                        2: "Operação cancelada",
                        3: "Processo interrompido",
                    }[decision]
                    if decision == 0 and apply_code:
                        expected_output = f"Código de saída: {apply_code}"
                    self._wait_until(
                        lambda: (
                            expected_output
                            in output_path.read_text(encoding="utf-8", errors="replace")
                        ),
                        process,
                        output_path,
                        "the final diagnostic and console pause",
                    )
                    time.sleep(0.2)
                    self.assertIsNone(
                        process.poll(),
                        "The final diagnostic must remain visible until Enter",
                    )
                    if decision == 0:
                        applied = json.loads(
                            (directory / "apply.json").read_text(encoding="utf-8")
                        )
                        self.assertEqual(
                            applied["args"][
                                applied["args"].index("--require-review") + 1
                            ],
                            snapshot_name,
                        )
                        self.assertEqual(applied["snapshot"]["dry_run_args"], dry_args)
                        self.assertEqual(
                            applied["snapshot"]["fixture"], "frozen-review-snapshot"
                        )
                        self.assertIn("--full", applied["args"])
                        if part:
                            self.assertEqual(
                                applied["args"][applied["args"].index("--quarter") + 1],
                                str(part),
                            )
                        else:
                            self.assertNotIn("--quarter", applied["args"])
                    else:
                        self.assertFalse(
                            (directory / "apply.json").exists(),
                            "Cancellation or UI failure must not apply",
                        )
                    (directory / "result_ack").touch()
                    if direct or shared_launcher:
                        time.sleep(0.3)
                        self.assertIsNone(
                            process.poll(),
                            "A sessão deve permanecer aberta após fechar o aviso final",
                        )
                        process.stdin.write(b"\r\n")
                        process.stdin.flush()
                        time.sleep(0.2)
                        self.assertIsNone(
                            process.poll(), "ENTER não deve fechar a sessão concluída"
                        )
                        process.stdin.write(b"exit\r\n")
                        process.stdin.flush()
                    self.assertEqual(
                        process.wait(timeout=10),
                        apply_code if decision == 0 else decision,
                    )
                finally:
                    self._close_process(process)

    def test_missing_review_helper_fails_before_services_and_keeps_console_open(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(
            prefix="nexoflow missing review "
        ) as temporary:
            directory = Path(temporary)
            self._fixture(directory)
            (directory / "scripts" / "confirmar_revisao_weni.ps1").unlink()
            output_path = directory / "console.log"
            with output_path.open("wb") as output:
                process = subprocess.Popen(
                    [
                        os.environ.get("COMSPEC", "cmd.exe"),
                        "/d",
                        "/c",
                        "comandos/atualizar_weni_carga_completa.bat".replace("/", "\\"),
                    ],
                    cwd=directory,
                    stdin=subprocess.PIPE,
                    env={
                        **os.environ,
                        "NEXOFLOW_LAUNCHER": "1",
                        "NEXOFLOW_TERMINAL": "1",
                    },
                    stdout=output,
                    stderr=subprocess.STDOUT,
                )
                try:
                    process.stdin.write(b"0\r\n")
                    process.stdin.flush()
                    self._wait_until(
                        lambda: (
                            "[ERRO] Código de saída: 3"
                            in output_path.read_text(encoding="utf-8", errors="replace")
                        ),
                        process,
                        output_path,
                        "the missing-helper diagnostic and console pause",
                    )
                    time.sleep(0.2)
                    self.assertIsNone(
                        process.poll(),
                        "The missing-helper diagnostic must remain visible",
                    )
                    for marker in (
                        "dry_run.json",
                        "notepad_closed.json",
                        "review_waiting.json",
                        "apply.json",
                    ):
                        self.assertFalse(
                            (directory / marker).exists(),
                            f"Missing helper must prevent {marker}",
                        )
                    (directory / "result_ack").touch()
                    self.assertEqual(process.wait(timeout=10), 3)
                finally:
                    self._close_process(process)

    def test_closed_stdin_still_waits_for_final_result_acknowledgment(self):
        self._run_batch_case(part=0, decision=0, closed_stdin=True)

    def test_apply_failure_after_confirmation_waits_with_closed_stdin(self):
        self._run_batch_case(part=0, decision=0, closed_stdin=True, apply_code=9)

    def test_result_script_records_failure_and_waits_without_stdin(self):
        with tempfile.TemporaryDirectory(prefix="nexo result ") as temp:
            root = Path(temp)
            (root / "scripts").mkdir()
            (root / "scripts" / "resultado_carga_weni.ps1").write_bytes(
                (REPOSITORY / "scripts" / "resultado_carga_weni.ps1").read_bytes()
            )
            harness = root / "result.ps1"
            harness.write_text(
                '. "$PSScriptRoot/scripts/resultado_carga_weni.ps1" -ResultCode 9 -Stage ERRO -Snapshot "logs/test.json"\n'
                "function Show-WeniResult { param([string]$Message)\n"
                ' Set-Content -LiteralPath "$PSScriptRoot/ready" -Value $Message -Encoding UTF8\n'
                ' while (-not (Test-Path -LiteralPath "$PSScriptRoot/ack")) { Start-Sleep -Milliseconds 50 }\n}\n'
                "exit (Complete-WeniResult)\n",
                encoding="utf-8-sig",
            )
            out = root / "out.log"
            with out.open("wb") as output:
                ps = (
                    Path(os.environ["SystemRoot"])
                    / "System32/WindowsPowerShell/v1.0/powershell.exe"
                )
                process = subprocess.Popen(
                    [
                        str(ps),
                        "-NoLogo",
                        "-NoProfile",
                        "-STA",
                        "-ExecutionPolicy",
                        "Bypass",
                        "-File",
                        str(harness),
                    ],
                    cwd=root,
                    stdin=subprocess.DEVNULL,
                    stdout=output,
                    stderr=subprocess.STDOUT,
                )
                try:
                    self._wait_until(
                        lambda: (root / "ready").exists(),
                        process,
                        out,
                        "final result acknowledgment",
                    )
                    self.assertIsNone(process.poll())
                    log = (root / "logs" / "weni_full_launcher.log").read_text(
                        encoding="utf-8-sig"
                    )
                    self.assertIn("etapa=ERRO", log)
                    self.assertIn("código=9", log)
                    self.assertIn("-03:00", log)
                    (root / "ack").touch()
                    self.assertEqual(process.wait(timeout=15), 0)
                finally:
                    self._close_process(process)

    def test_direct_batch_session_stays_open_after_result(self):
        self._run_batch_case(part=0, decision=0, direct=True)

    def test_shared_launcher_flag_does_not_close_session_after_result(self):
        self._run_batch_case(part=0, decision=0, shared_launcher=True)

    def test_shared_launcher_keeps_error_and_cancellation_visible(self):
        for decision, apply_code in ((0, 9), (2, 0)):
            with self.subTest(decision=decision, apply_code=apply_code):
                self._run_batch_case(
                    part=0,
                    decision=decision,
                    apply_code=apply_code,
                    shared_launcher=True,
                )

    def test_incremental_launcher_stays_open_after_final_pause(self):
        for apply_code in (0, 9):
            with (
                self.subTest(apply_code=apply_code),
                tempfile.TemporaryDirectory(prefix="nexo incremental ") as temporary,
            ):
                directory = Path(temporary)
                self._fixture(directory)
                (directory / "secrets_config.py").write_text(
                    "RECOMPRA_ENABLED = False\n", encoding="ascii"
                )
                (directory / "app/run_update_weni.py").write_text(
                    "import sys\nfrom pathlib import Path\n"
                    "if '--apply' in sys.argv:\n"
                    "    Path('apply.json').write_text('synthetic')\n"
                    f"    raise SystemExit({apply_code})\n",
                    encoding="ascii",
                )
                batch = (REPOSITORY / "comandos/atualizar_weni.bat").read_text(
                    encoding="utf-8"
                )
                batch = batch.replace(" < CON", "").replace(
                    '".venv\\Scripts\\python.exe"', f'"{sys.executable}"'
                )
                batch, editor_count = re.subn(
                    r'(?m)^start "" notepad\.exe (.+)$',
                    lambda match, directory=directory: (
                        f'start "" /b "{sys.executable}" "{directory / "notepad_stub.py"}" '
                        + match.group(1)
                    ),
                    batch,
                )
                self.assertEqual(editor_count, 1)
                batch, count = re.subn(
                    r'(?m)^"%SystemRoot%\\System32\\WindowsPowerShell\\v1\.0\\powershell\.exe"[^\n]*confirmar_revisao_weni\.ps1[^\n]*$',
                    lambda match, directory=directory: (
                        f'"{sys.executable}" "{directory / "review_gate_stub.py"}"'
                    ),
                    batch,
                )
                self.assertEqual(count, 1)
                (directory / "comandos/atualizar_weni.bat").write_text(
                    batch, encoding="utf-8"
                )
                env = {**os.environ, "NEXOFLOW_LAUNCHER": "1"}
                env.pop("NEXOFLOW_WENI_SESSION", None)
                env.pop("NEXOFLOW_TERMINAL", None)
                output_path = directory / "console.log"
                with output_path.open("wb") as output:
                    process = subprocess.Popen(
                        [
                            os.environ.get("COMSPEC", "cmd.exe"),
                            "/d",
                            "/c",
                            "comandos/atualizar_weni.bat".replace("/", "\\"),
                        ],
                        cwd=directory,
                        env=env,
                        stdin=subprocess.PIPE,
                        stdout=output,
                        stderr=subprocess.STDOUT,
                    )
                    try:
                        self._wait_until(
                            lambda directory=directory: (
                                directory / "review_waiting.json"
                            ).exists(),
                            process,
                            output_path,
                            "incremental approval",
                        )
                        self._wait_until(
                            lambda directory=directory: (
                                directory / "notepad_closed.json"
                            ).exists(),
                            process,
                            output_path,
                            "incremental report opened",
                        )
                        opened = json.loads(
                            (directory / "notepad_closed.json").read_text(
                                encoding="utf-8"
                            )
                        )
                        self.assertEqual(
                            Path(opened[0]),
                            directory / "logs" / "weni_update_review.txt",
                        )
                        self.assertFalse((directory / "apply.json").exists())
                        pending = directory / "review_decision.pending"
                        pending.write_text("0")
                        os.replace(pending, directory / "review_decision.txt")
                        expected = (
                            "A aplicação foi interrompida"
                            if apply_code
                            else "NexoFlow Weni concluído com sucesso"
                        )
                        self._wait_until(
                            lambda expected=expected, output_path=output_path: (
                                expected
                                in output_path.read_text(
                                    encoding="utf-8", errors="replace"
                                )
                            ),
                            process,
                            output_path,
                            "incremental result",
                        )
                        process.stdin.write(b"\r\n")
                        process.stdin.flush()
                        time.sleep(0.3)
                        self.assertIsNone(
                            process.poll(), "PAUSE não deve encerrar a janela Weni"
                        )
                        process.stdin.write(b"exit\r\n")
                        process.stdin.flush()
                        self.assertEqual(process.wait(timeout=10), apply_code)
                    finally:
                        self._close_process(process)

    def test_full_load_waits_for_approval_and_applies_the_frozen_snapshot(self) -> None:
        self._run_batch_case(part=0, decision=0)

    def test_all_four_parts_wait_for_approval_and_apply_their_own_snapshot(
        self,
    ) -> None:
        for part in (1, 2, 3, 4):
            with self.subTest(part=part):
                self._run_batch_case(part=part, decision=0)

    def test_cancelled_review_keeps_console_open_and_returns_2(self) -> None:
        for part in (0, 2):
            with self.subTest(part=part):
                self._run_batch_case(part=part, decision=2)

    def test_failed_review_keeps_console_open_and_returns_3(self) -> None:
        for part in (0, 2):
            with self.subTest(part=part):
                self._run_batch_case(part=part, decision=3)


@unittest.skipUnless(os.name == "nt", "Requires Windows PowerShell")
class WeniReviewDialogWindowsTests(unittest.TestCase):
    """Test the actual PowerShell decision logic while replacing the GUI only."""

    def _run_dialog(
        self, expression: str, expected_code: int, *, part: str = ""
    ) -> None:
        script = REPOSITORY / "scripts" / "confirmar_revisao_weni.ps1"
        script_literal = str(script).replace("'", "''")
        with tempfile.TemporaryDirectory(prefix="nexoflow dialog ") as temporary:
            harness = Path(temporary) / "review_harness.ps1"
            harness.write_text(
                f". '{script_literal}'\n"
                "function Show-WeniReviewDialog {\n"
                "    param([string]$Message, [string]$Title)\n"
                f"    {expression}\n"
                "}\n"
                f"exit (Confirm-WeniReview -Part '{part}')\n",
                encoding="utf-8-sig",
            )
            result = subprocess.run(
                [
                    str(
                        Path(os.environ["SystemRoot"])
                        / "System32"
                        / "WindowsPowerShell"
                        / "v1.0"
                        / "powershell.exe"
                    ),
                    "-NoLogo",
                    "-NoProfile",
                    "-NonInteractive",
                    "-STA",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-File",
                    str(harness),
                ],
                capture_output=True,
                timeout=45,
                check=False,
            )
            self.assertEqual(
                expected_code, result.returncode, result.stdout + result.stderr
            )

    def test_exact_confirmation_and_trimmed_case_variant_are_accepted(self) -> None:
        for value in ("CARGA COMPLETA REVISADA", "  carga completa revisada  "):
            with self.subTest(response=value):
                self._run_dialog(f"return '{value}'", 0)

    def test_quarter_is_shown_in_the_real_review_message(self) -> None:
        self._run_dialog(
            "if ($Message -notlike '*Parte 2/4*') { throw 'Missing quarter in review message' }; "
            "return 'CARGA COMPLETA REVISADA'",
            0,
            part="2",
        )

    def test_empty_confirmation_is_cancelled(self) -> None:
        self._run_dialog("return ''", 2)

    def test_windows_powershell_decodes_accented_brazilian_portuguese(self) -> None:
        self._run_dialog(
            "if ($Title -cne 'NexoFlow Weni - Revisão Humana') { throw 'Incorrect title encoding' }; "
            "foreach ($text in @('relatório', 'revisão', 'NÃO', 'alteração')) { "
            "if (-not $Message.Contains($text)) { throw 'Incorrect message encoding' } }; "
            "return 'CARGA COMPLETA REVISADA'",
            0,
        )

    def test_inexact_confirmation_is_cancelled(self) -> None:
        self._run_dialog("return 'CARGA COMPLETA REVISADA depois'", 2)

    def test_dialog_exception_fails_closed(self) -> None:
        self._run_dialog("throw 'Synthetic dialog failure'", 3)


if __name__ == "__main__":
    unittest.main()
