"""Prazos do evento, registros sintéticos e relógio controlado, sem APIs."""

import json
import tempfile
import unittest
from copy import deepcopy
from datetime import timedelta
from pathlib import Path
from unittest.mock import Mock

import recompra
from tests import test_recompra_rollover as apply_fixtures
from tests.test_recompra import NOW, STRUCTURE, fixture


class RecompraDeadlineTests(unittest.TestCase):
    def prepare(self):
        helper = apply_fixtures.RecompraApplyRolloverTests("runTest")
        helper.setUp()
        self.addCleanup(helper.doCleanups)
        helper.clock.now = NOW
        current, order, items = fixture(25)
        helper.live["u1"] = deepcopy(current)
        helper.orders["u1"] = (deepcopy(order), deepcopy(items))
        plan = recompra.propose(current, order, items, STRUCTURE, now=NOW)
        review = recompra.make_review([plan], STRUCTURE, now=NOW)
        event_at = recompra.business_order_datetime(plan["event_at"])
        return helper, review, event_at

    def test_deadline_passes_before_apply_no_mutations_and_conflict(self):
        helper, review, event_at = self.prepare()
        helper.clock.now = event_at + timedelta(seconds=1)
        result = helper.apply(review)
        self.assertEqual(result["conflitos"], 1)
        helper.weni.update_contact_fields.assert_not_called()
        helper.weni.contact_group_action.assert_not_called()
        self.assertFalse(helper.state_path.exists())

    def test_long_scan_refreshes_deadlines_before_human_review(self):
        _, review, event_at = self.prepare()
        self.assertEqual(review["plans"][0]["action"], "add")
        recompra.finalize_review(review, event_at)
        plan = review["plans"][0]
        self.assertEqual(plan["action"], "")
        self.assertEqual(plan["reason"], "HORARIO_D25_ENCERRADO")
        self.assertFalse(plan["scheduled_future"])
        self.assertEqual(review["generated_at"], event_at.isoformat())

    def test_deadline_passes_during_fields_write_no_admission_and_no_pending(self):
        helper, review, event_at = self.prepare()
        helper.hooks["fields:u1"] = lambda: setattr(helper.clock, "now", event_at)
        result = helper.apply(review)
        self.assertEqual(result["conflitos"], 1)
        helper.weni.update_contact_fields.assert_called_once()
        helper.weni.contact_group_action.assert_not_called()
        entry = recompra.load_state(helper.state_path)["contacts"]["u1"]["orders"][
            "100"
        ]
        self.assertFalse(entry["pending"])
        self.assertFalse(entry.get("admitted", False))
        self.assertIn(
            "HORARIO_D25_ENCERRADO", helper.audit_path.read_text(encoding="utf-8")
        )

    def test_equal_and_past_event_never_authorize_new_admission(self):
        current, order, items = fixture(25)
        event = recompra.business_order_datetime(order["data_hora"]) + timedelta(
            days=25
        )
        for now in (event, event + timedelta(minutes=10)):
            with self.subTest(now=now):
                plan = recompra.propose(current, order, items, STRUCTURE, now=now)
                self.assertEqual(plan["reason"], "HORARIO_D25_ENCERRADO")
                self.assertEqual(plan["action"], "")
                self.assertEqual(plan["fields"]["pedido_data"], order["data_hora"])

    def test_already_grouped_same_reference_is_not_removed_just_for_time(self):
        current, order, items = fixture(25, grouped=True)
        current["fields"].update(
            pedido_data=order["data_hora"], pedido_produtos=items[0]["produto_nome"]
        )
        plan = recompra.propose(
            current, order, items, STRUCTURE, now=NOW + timedelta(hours=1)
        )
        self.assertFalse(plan["scheduled_future"])
        self.assertEqual(plan["action"], "")

    def test_past_new_reference_can_remove_previous_group_but_never_readd(self):
        current, order, items = fixture(25, grouped=True)
        current["fields"]["pedido_data"] = (NOW - timedelta(days=26)).isoformat()
        plan = recompra.propose(
            current, order, items, STRUCTURE, now=NOW + timedelta(hours=1)
        )
        self.assertEqual(plan["action"], "remove")
        self.assertEqual(plan["reason"], "HORARIO_D25_ENCERRADO")

    def test_explicit_delivery_hour_is_checked_in_business_timezone(self):
        current, order, items = fixture(25)
        plan = recompra.propose(
            current, order, items, {**STRUCTURE, "delivery_hour": 9}, now=NOW
        )
        self.assertEqual(plan["reason"], "HORARIO_D25_ENCERRADO")
        self.assertEqual(recompra.business_order_datetime(plan["event_at"]).hour, 9)

    def test_duplicate_review_uuids_are_rejected_before_remote_work(self):
        _, review, _ = self.prepare()
        review["plans"].append(deepcopy(review["plans"][0]))
        weni, farm = Mock(), Mock()
        with self.assertRaisesRegex(RuntimeError, "duplicado"):
            recompra.apply_review(review, weni, farm, now_fn=lambda: NOW)
        weni.assert_not_called()
        self.assertFalse(weni.method_calls)
        self.assertFalse(farm.method_calls)
        with self.assertRaisesRegex(RuntimeError, "duplicado"):
            recompra.make_review(review["plans"], STRUCTURE, now=NOW)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "review.json"
            path.write_text(
                json.dumps({"review": review, "sha256": recompra.digest(review)}),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(RuntimeError, "duplicado"):
                recompra.read_review(path)

    def test_zero_is_cadastral_error_without_api_call(self):
        current, _, _ = fixture(24)
        current["fields"]["cpf"] = "0"
        farm = Mock()
        order, items = recompra.resolve_latest(current, farm, NOW.isoformat())
        self.assertEqual(order["status_confirmacao"], "CPF_INVALIDO_CADASTRO")
        self.assertEqual(items, [])
        farm.get_cliente_by_cpf.assert_not_called()
        farm.get_pedido.assert_not_called()

    def test_review_summary_omits_unchanged_contact_details_and_saves_private_cadastro(
        self,
    ):
        current, _, _ = fixture(24)
        current["fields"]["cpf"] = "0"
        plan = recompra.propose(
            current,
            {"status_confirmacao": "CPF_INVALIDO_CADASTRO"},
            [],
            STRUCTURE,
            now=NOW,
        )
        review = recompra.make_review([plan], STRUCTURE, now=NOW)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "recompra_review.json"
            recompra.write_review(review, path)
            report = path.with_suffix(".txt").read_text(encoding="utf-8-sig")
            self.assertIn("CPF_INVALIDO_CADASTRO", report)
            self.assertNotIn("Contato u1 |", report)
            self.assertEqual(recompra.read_review(path), review)
            cadastro = json.loads(
                (
                    path.parent / "privado/recompra_review_cadastro_pendente.json"
                ).read_text(encoding="utf-8")
            )
            self.assertEqual(
                cadastro["contatos"],
                [
                    {
                        "contact_uuid": "u1",
                        "motivo": "CPF_INVALIDO_CADASTRO",
                        "cpf_original": "0",
                    }
                ],
            )
            self.assertEqual(cadastro["review_sha256"], recompra.digest(review))

    def test_api_sale_and_weni_cancellation_remain_blocked_with_explicit_warning(self):
        current, order, items = fixture(24)
        current["fields"]["pedido_status"] = "3"
        plan = recompra.propose(current, order, items, STRUCTURE, now=NOW)
        self.assertEqual(plan["reason"], "PEDIDO_STATUS_3_CANCELADO_WENI")
        self.assertNotIn(plan["action"], {"add", "replace"})
        self.assertTrue(
            any(
                warning.startswith("STATUS_WENI_DIVERGENTE_API:")
                for warning in plan["warnings"]
            )
        )
