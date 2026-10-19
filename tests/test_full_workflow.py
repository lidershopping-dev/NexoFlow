"""Fluxo integral: consultas parciais nunca autorizam a próxima aplicação."""

import os
import shutil
import subprocess
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import MagicMock, patch

from weni_update import ContactUpdatePlan, UpdateStats, _record_validated_order


class CompleteQueriesTests(unittest.TestCase):
    def test_unchanged_contacts_count_api_failures_but_not_business_exclusions(self):
        stats = UpdateStats()
        for status in (
            "ERRO_API",
            "ERRO_ITENS_PEDIDO_API",
            "API_DESABILITADA",
            "CPF_REJEITADO_API",
            "CPF_IDENTIDADE_DIVERGENTE",
        ):
            plan = ContactUpdatePlan(
                contact_uuid="synthetic", order_resolution_status=status
            )
            self.assertFalse(plan.has_changes)
            _record_validated_order(stats, plan)
        self.assertEqual(stats.orders_api_failed, 3)
        self.assertEqual(stats.orders_validated, 0)

    def test_weni_cli_finishes_with_pending_contacts_even_with_legacy_strict_flag(
        self,
    ):
        from run_update_weni import main

        for failure in ("orders_api_failed", "contacts_conflicted", "contacts_failed"):
            for strict in (False, True):
                with self.subTest(failure=failure, strict=strict), ExitStack() as stack:
                    stats = UpdateStats(**{failure: 1})
                    stack.enter_context(
                        patch(
                            "sys.argv",
                            [
                                "app/run_update_weni.py",
                                "--dry-run",
                                *(["--require-complete"] if strict else []),
                            ],
                        )
                    )
                    stack.enter_context(
                        patch("weni_update.run_update", return_value=stats)
                    )
                    stack.enter_context(
                        patch(
                            "run_update_weni.configure_proxy", return_value=MagicMock()
                        )
                    )
                    stack.enter_context(patch("logging_config.configure_logging"))
                    stack.enter_context(
                        patch(
                            "weni_dedup.run_with_dedup",
                            side_effect=lambda **kw: (
                                kw["update"](set(), None),
                                {
                                    "proposed": 0,
                                    "pending": 0,
                                    "deleted": 0,
                                    "skipped": 0,
                                },
                            ),
                        )
                    )
                    stack.enter_context(patch("builtins.print"))
                    self.assertEqual(main(), 0)

    def test_planilhas_keeps_review_but_blocks_api_errors_before_application(self):
        from run_delivery_tickets import main

        for outcome, expected in (
            ("ERRO_ITENS_PEDIDO_API", 3),
            ("CPF_IDENTIDADE_DIVERGENTE", 0),
        ):
            with self.subTest(outcome=outcome), ExitStack() as stack:
                stack.enter_context(
                    patch(
                        "sys.argv",
                        [
                            "app/run_delivery_tickets.py",
                            "--dry-run",
                            "--planilhas",
                            "--require-complete",
                        ],
                    )
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
                stack.enter_context(patch("weni_client.WeniClient"))
                stack.enter_context(patch("weni_update._open_farmalider"))
                stack.enter_context(
                    patch("weni_update._available_weni_fields", return_value=set())
                )
                stack.enter_context(
                    patch(
                        "delivery_tickets.reconcile_tickets",
                        return_value=(
                            [],
                            {
                                "tickets": [{"resultado_validacao": outcome}],
                                "orders": [],
                            },
                        ),
                    )
                )
                writer = stack.enter_context(
                    patch("delivery_tickets.write_delivery_review")
                )
                apply = stack.enter_context(
                    patch("delivery_tickets.apply_delivery_review")
                )
                stack.enter_context(patch("logging_config.configure_logging"))
                stack.enter_context(patch("proxy_config.configure_proxy"))
                stack.enter_context(patch("builtins.print"))
                self.assertEqual(main(), expected)
                writer.assert_called_once()
                apply.assert_not_called()

    def test_recompra_blocks_partial_queries_without_mutation(self):
        from run_recompra import main

        for reason, diagnostic, expected in (
            ("ERRO_API", "ERRO_API", 3),
            ("RECUSA_MARKETING", "ERRO_API", 3),
            ("CPF_REJEITADO_API", "CPF_REJEITADO_API", 0),
        ):
            with self.subTest(reason=reason), ExitStack() as stack:
                stack.enter_context(patch("weni_client.WeniClient"))
                stack.enter_context(patch("weni_update._open_farmalider"))
                stack.enter_context(
                    patch("recompra.inspect_structure", return_value={"errors": []})
                )
                stack.enter_context(patch("recompra.load_state", return_value={}))
                stack.enter_context(
                    patch(
                        "recompra.build_plans",
                        return_value=[
                            {"reason": reason, "order_resolution_status": diagnostic}
                        ],
                    )
                )
                stack.enter_context(patch("recompra.make_review", return_value={}))
                stack.enter_context(patch("recompra.ensure_review_current"))
                stack.enter_context(patch("recompra.finalize_review"))
                writer = stack.enter_context(patch("recompra.write_review"))
                apply = stack.enter_context(patch("recompra.apply_review"))
                stack.enter_context(patch("logging_config.configure_logging"))
                stack.enter_context(patch("proxy_config.configure_proxy"))
                stack.enter_context(patch("builtins.print"))
                self.assertEqual(main(["--dry-run", "--require-complete"]), expected)
                writer.assert_called_once()
                apply.assert_not_called()

    def test_recompra_preserves_query_error_when_refusal_overrides_reason(self):
        from recompra import propose

        structure = {"group_uuid": "synthetic-group", "opt_out_uuid": "synthetic-out"}
        with patch("recompra.refused", return_value=True):
            plan = propose(
                {"uuid": "synthetic", "fields": {}},
                {"status_confirmacao": "ERRO_API"},
                [],
                structure,
            )
        self.assertEqual(plan["reason"], "RECUSA_MARKETING")
        self.assertEqual(plan["order_resolution_status"], "ERRO_API")


@unittest.skipUnless(os.name == "nt", "Requires Windows CMD")
class FullWorkflowWindowsTests(unittest.TestCase):
    children = (
        "comandos/atualizar_weni_carga_completa.bat",
        "comandos/atualizar_weni_planilhas.bat",
        "comandos/executar_analytics_planilhas.bat",
        "comandos/executar_carga_completa.bat",
        "comandos/atualizar_recompra.bat",
        "comandos/executar_manual.bat",
    )

    def invoke(self, *, failing_stage=None, result_code=0, missing_import=False):
        with tempfile.TemporaryDirectory(prefix="NexoFlow workflow ") as temporary:
            root = Path(temporary)
            (root / "comandos").mkdir()
            shutil.copyfile(
                Path(__file__).resolve().parents[1] / "comandos/atualizar_tudo.bat",
                root / "comandos/atualizar_tudo.bat",
            )
            python = root / ".venv/Scripts/python.exe"
            python.parent.mkdir(parents=True)
            python.touch()  # Preflight only; no Python or APIs run in this test.
            (root / "importacoes").mkdir()
            for name in ("Conversas_Delivery.xlsx", "Contatos_Delivery.xlsx"):
                if not missing_import:
                    (root / "importacoes" / name).touch()
            for index, name in enumerate(self.children):
                code = result_code if index == failing_stage else 0
                (root / name).write_text(
                    f'@echo off\necho {index}:%*:%NEXOFLOW_FULL_WORKFLOW%:%FORCE_FULL_SYNC%:%FULL_SYNC_QUARTER%>>"%~dp0..\\calls.txt"\nexit /b {code}\n',
                    encoding="utf-8",
                )
            env = {**os.environ, "FORCE_FULL_SYNC": "1", "FULL_SYNC_QUARTER": "2"}
            result = subprocess.run(
                ["cmd.exe", "/d", "/c", "comandos\\atualizar_tudo.bat"],
                cwd=root,
                env=env,
                input="\n",
                text=True,
                encoding="utf-8",
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=30,
            )
            calls = (
                (root / "calls.txt").read_text(encoding="utf-8").splitlines()
                if (root / "calls.txt").exists()
                else []
            )
            return result, calls

    def test_success_order_full_mode_and_incremental_environment(self):
        result, calls = self.invoke()
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(
            calls,
            [
                f"{index}:{'--fluxo-completo' if index in (0, 3) else ''}:1::"
                for index in range(6)
            ],
        )
        self.assertIn("Looker Studio", result.stdout)

    def test_failures_cancellations_and_partial_runs_stop_at_each_stage(self):
        for stage in range(6):
            for code in (1, 2, 3, 4):
                with self.subTest(stage=stage, code=code):
                    result, calls = self.invoke(failing_stage=stage, result_code=code)
                    self.assertEqual(result.returncode, code, result.stdout)
                    self.assertEqual(len(calls), stage + 1)
                    self.assertNotIn("[OK] As cinco etapas", result.stdout)

    def test_missing_exports_block_all_operations(self):
        result, calls = self.invoke(missing_import=True)
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertEqual(calls, [])
