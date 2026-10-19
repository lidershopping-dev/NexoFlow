"""Conflitos privados e retomada sem repetir mutações já persistidas."""

import hashlib
import json
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import Mock

from delivery_tickets import (
    apply_delivery_review,
    reconcile_tickets,
    write_delivery_review,
)
from farmalider_integration import weni_identity_guard
from tests.test_delivery_tickets import CONTACT, NOW, contact, farm, selection
from weni_client import WeniError
from weni_update import REQUIRED_ORDER_FIELDS


class DeliveryConflictTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "delivery_weni_review.json"
        self.current = contact()
        self.weni = Mock()
        self.weni.list_fields.return_value = [
            {"key": key} for key in REQUIRED_ORDER_FIELDS
        ]
        self.weni.get_contact.side_effect = lambda **kwargs: deepcopy(self.current)

        def update(uid, fields):
            self.current["fields"].update(fields)
            return deepcopy(self.current)

        self.weni.update_contact_fields.side_effect = update

    def review(self, *, source=None, edit=None):
        plans, data = reconcile_tickets(
            selection(),
            [source or deepcopy(self.current)],
            farm(),
            captured_at=NOW,
            available_fields=REQUIRED_ORDER_FIELDS,
        )
        data["target"] = "weni"
        if edit:
            edit(plans, data)
        write_delivery_review(plans, data, self.path)
        return plans, data

    def apply(self):
        return apply_delivery_review(self.path, self.weni, target="weni")

    def report(self, result):
        return json.loads(Path(result["conflict_report"]).read_text(encoding="utf-8"))

    def assert_conflict(self, result, reason, field):
        self.assertEqual(result["conflicts"], 1)
        self.assertEqual(result["contacts_accepted"], 0)
        self.assertIn(reason, result["conflict_reasons"])
        report = self.report(result)
        self.assertEqual(len(report["conflitos"]), 1)
        conflict = report["conflitos"][0]
        self.assertEqual(conflict["contact_uuid"], CONTACT)
        self.assertIn(field, conflict["campos"])
        self.assertTrue(report["valores_ocultos"])
        self.weni.update_contact_fields.assert_not_called()
        self.weni.contact_group_action.assert_not_called()
        return conflict

    def test_field_conflict_reports_private_uuid_and_redacted_comparison(self):
        self.review()
        self.current["fields"]["pedido_produtos"] = "Produto particular posterior"
        with self.assertLogs(level="WARNING") as captured:
            result = self.apply()
        conflict = self.assert_conflict(result, "CAMPO_ALTERADO", "pedido_produtos")
        comparison = next(
            item
            for item in conflict["comparacoes"]
            if item["campo"] == "pedido_produtos"
        )
        self.assertNotEqual(
            comparison["esperado"]["assinatura"],
            comparison["encontrado"]["assinatura"],
        )
        private = Path(result["conflict_report"]).read_text(encoding="utf-8")
        logs = "\n".join(captured.output)
        self.assertIn(hashlib.sha256(CONTACT.encode()).hexdigest()[:12], logs)
        self.assertNotIn(CONTACT, logs)
        for text in (logs, private):
            for sensitive in (
                "Produto particular posterior",
                "Cliente Árvore",
                "arvore@example.test",
                "+5591987654321",
            ):
                self.assertNotIn(sensitive, text)
        self.assertEqual(
            Path(result["conflict_report"]),
            self.path.parent / "privado" / "delivery_weni_review_conflitos.json",
        )

    def test_missing_contact_is_distinguished_without_overwriting_anything(self):
        self.review()
        self.current = None
        self.assert_conflict(self.apply(), "CONTATO_NAO_ENCONTRADO", "__contato")

    def test_changed_guard_names_the_source_without_logging_its_value(self):
        self.review()
        self.current["fields"]["cpf"] = "98765432100"
        result = self.apply()
        self.assert_conflict(result, "FONTE_ALTERADA", "cpf")
        self.assertNotIn(
            "98765432100", Path(result["conflict_report"]).read_text(encoding="utf-8")
        )

    def test_identity_change_is_a_diagnostic_reason(self):
        self.review()
        self.current["fields"]["email"] = "novo@example.test"
        self.assert_conflict(
            self.apply(), "IDENTIDADE_ALTERADA", "__invalid_cpf_identity"
        )

    def test_native_urn_guard_reports_native_change_separately(self):
        def guard(plans, data):
            plans[0].guards["__native_urns"] = list(self.current["urns"])

        self.review(edit=guard)
        self.current["urns"] = ["tel:+5511999887766"]
        result = self.apply()
        reason = next(iter(result["conflict_reasons"]))
        self.assertIn("URN_NATIVA_ALTERADA", reason)
        self.assert_conflict(result, reason, "__native_urns")

    def test_same_snapshot_already_applied_does_not_repeat_fields(self):
        self.review()
        original = self.path.read_bytes()
        first = self.apply()
        self.assertEqual(first["contacts_accepted"], 1)
        self.weni.update_contact_fields.assert_called_once()
        second = self.apply()
        self.assertEqual(second["contacts_accepted"], 1)
        self.assertEqual(second["conflicts"], 0)
        self.weni.update_contact_fields.assert_called_once()
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(self.report(second)["conflitos"], [])

    def test_post_committed_before_network_failure_is_not_replayed(self):
        self.review()

        def commit_then_timeout(uid, fields):
            self.current["fields"].update(fields)
            raise WeniError("Resposta perdida após o POST sintético")

        self.weni.update_contact_fields.side_effect = commit_then_timeout
        with self.assertRaises(WeniError):
            self.apply()
        self.weni.update_contact_fields.assert_called_once()
        result = self.apply()
        self.assertEqual(result["conflicts"], 0)
        self.weni.update_contact_fields.assert_called_once()

    def test_partially_persisted_field_post_resumes_only_remaining_fields(self):
        plans, _ = self.review()
        already_saved = next(iter(plans[0].fields))
        self.assertGreater(len(plans[0].fields), 1)

        def partially_commit(uid, fields):
            if self.weni.update_contact_fields.call_count == 1:
                self.current["fields"][already_saved] = fields[already_saved]
                raise WeniError("Resposta sintética interrompida")
            self.assertNotIn(already_saved, fields)
            self.current["fields"].update(fields)
            return deepcopy(self.current)

        self.weni.update_contact_fields.side_effect = partially_commit
        with self.assertRaises(WeniError):
            self.apply()
        self.assertEqual(self.apply()["conflicts"], 0)
        self.assertEqual(self.weni.update_contact_fields.call_count, 2)
        self.apply()
        self.assertEqual(self.weni.update_contact_fields.call_count, 2)

    def test_uncertain_group_removal_is_read_before_resume_and_not_replayed(self):
        self.current["fields"].update(
            pedido_data="01/05/26 10:00:00", marketing_opt_in="ativo"
        )
        self.current["groups"] = [
            {
                "uuid": "30000000-0000-0000-0000-000000000001",
                "name": "Recompra FarmaLíder",
            }
        ]
        self.review()

        def remove_then_timeout(uid, *, action, group):
            self.assertEqual(action, "remove")
            self.current["groups"] = []
            raise WeniError("Resposta da remoção perdida")

        self.weni.contact_group_action.side_effect = remove_then_timeout
        with self.assertRaises(WeniError):
            self.apply()
        self.weni.contact_group_action.assert_called_once()
        self.weni.update_contact_fields.assert_not_called()
        result = self.apply()
        self.assertEqual(result["conflicts"], 0)
        self.weni.contact_group_action.assert_called_once()
        self.weni.update_contact_fields.assert_called_once()
        self.apply()
        self.weni.contact_group_action.assert_called_once()
        self.weni.update_contact_fields.assert_called_once()

    def test_resolving_conflict_clears_last_attempt_report_without_editing_snapshot(
        self,
    ):
        original = deepcopy(self.current)
        self.review()
        frozen = self.path.read_bytes()
        self.current["fields"]["email"] = "alterado@example.test"
        self.assertEqual(self.apply()["conflicts"], 1)
        self.current = original
        resumed = self.apply()
        self.assertEqual(resumed["conflicts"], 0)
        self.assertEqual(self.report(resumed)["conflitos"], [])
        archived = list(
            (self.path.parent / "privado" / "historico_conflitos").glob("*.json")
        )
        self.assertEqual(len(archived), 1)
        previous = json.loads(archived[0].read_text(encoding="utf-8"))
        self.assertEqual(previous["conflitos"][0]["contact_uuid"], CONTACT)
        self.assertEqual(previous["conflitos"][0]["motivo"], "IDENTIDADE_ALTERADA")
        self.assertEqual(self.path.read_bytes(), frozen)

    def test_old_zero_cpf_snapshot_blocks_only_its_unsafe_order_link(self):
        def old_snapshot(plans, data):
            plans[0].guards["cpf"] = "0"
            older = contact()
            older["fields"]["cpf"] = "0"
            plans[0].guards["__invalid_cpf_identity"] = weni_identity_guard(older)

        self.review(edit=old_snapshot)
        self.current["fields"]["cpf"] = "0"
        self.assert_conflict(self.apply(), "CPF_INVALIDO_CADASTRO", "cpf")

    def test_zero_cpf_without_frozen_order_remains_readonly_diagnostic(self):
        self.current["fields"]["cpf"] = "0"
        plans, data = self.review()
        self.assertEqual(data["orders"], [])
        self.assertFalse(plans[0].has_changes)
        result = self.apply()
        self.assertEqual(result["contacts_accepted"], 1)
        self.assertEqual(result["conflicts"], 0)
        self.weni.update_contact_fields.assert_not_called()

    def test_absent_cpf_is_missing_locator_and_does_not_assert_no_purchase(self):
        self.current["fields"].pop("cpf")
        _, data = self.review()
        self.assertEqual(data["summary"], {"CPF_NAO_INFORMADO": 1})
        self.assertEqual(data["tickets"][0]["resultado_validacao"], "CPF_NAO_INFORMADO")
        self.assertEqual(data["orders"], [])

    def test_sheets_failure_never_reports_completed_application(self):
        plans, data = self.review()
        data["target"] = "both"
        write_delivery_review(plans, data, self.path)
        store_factory = Mock(side_effect=RuntimeError("Sheets sintético indisponível"))
        with self.assertRaises(RuntimeError):
            apply_delivery_review(
                self.path, self.weni, target="both", store_factory=store_factory
            )
        report = json.loads(
            (
                self.path.parent / "privado" / "delivery_weni_review_conflitos.json"
            ).read_text(encoding="utf-8")
        )
        self.assertTrue(report["avaliacao_contatos_concluida"])
        self.assertFalse(report["aplicacao_concluida"])


if __name__ == "__main__":
    unittest.main()
