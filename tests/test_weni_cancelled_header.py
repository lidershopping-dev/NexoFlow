"""Um cancelamento oficial validado retira a agenda mesmo sem itens."""

import unittest
from copy import deepcopy
from unittest.mock import Mock

from farmalider_integration import apply_pedido_api_confirmation
from tests.test_pedido_resolution import client, cliente_payload, pedido_payload
from tests.test_recompra import GROUP, NOW, fixture
from weni_update import (
    PURCHASE_FIELDS,
    _build_contact_plan,
    _execute_contact_plan,
    _prepare_frozen_plan_for_apply,
    audit_plans,
)


class WeniCancelledHeaderTests(unittest.TestCase):
    def setUp(self):
        self.contact, _, _ = fixture(grouped=True)
        self.contact["fields"].update(
            pedido_id="123",
            pedido_data="06/10/26 10:30:00",
            pedido_status="2",
            contact_urn="tel:+5591999999999",
            contact_canal_principal="outro",
        )
        self.customer = cliente_payload()
        self.customer["itens"] = []
        self.pedido = pedido_payload()
        self.pedido["itens"] = []
        self.farm = client(cliente=self.customer, pedido=self.pedido)

    def build(self):
        return _build_contact_plan(
            self.contact,
            context_messages=[],
            candidate_message_ids=set(),
            farm_client=self.farm,
            opt_out_group_name="Opt-Out LGPD",
            opt_out_group_uuid="g-out",
            captured_at=NOW.isoformat(),
        )

    def prepare(self, plan, contact=None):
        return _prepare_frozen_plan_for_apply(
            plan,
            self.contact if contact is None else contact,
            opt_in_group_name="Opt-In LGPD",
            opt_in_group_uuid="g-in",
            opt_out_group_name="Opt-Out LGPD",
            opt_out_group_uuid="g-out",
        )

    def test_verified_cancelled_incomplete_order_removes_only_old_agenda(self):
        for signal in (
            {"status": 0},
            {"status": 2},
            {"status": 3},
            {"formaPagamento": 0},
            {"cancelamento": "2026-10-06T10:31:00"},
        ):
            with self.subTest(signal=signal):
                self.setUp()
                self.pedido["capa"].update(signal)
                plan = self.build()
                self.assertEqual(plan.purchase_confirmation, "CANCELADO_API")
                self.assertEqual(plan.order_resolution_status, "CANCELADO_API")
                self.assertFalse(plan.order_validated)
                self.assertFalse(plan.purchase_confirmed)
                self.assertFalse(set(plan.fields) & PURCHASE_FIELDS)
                self.assertEqual(
                    [
                        (action.action, action.group_name)
                        for action in plan.group_actions
                    ],
                    [("remove", GROUP)],
                )
                for key in (
                    "cpf",
                    "pedido_id",
                    "pedido_data",
                    "pedido_status",
                    "ultimo_pedido",
                ):
                    self.assertIn(key, plan.guards)
                audit_plans([plan], opt_out_group_available=True)
                prepared, conflict = self.prepare(plan)
                self.assertEqual(conflict, "")
                self.assertIsNotNone(prepared)
                weni = Mock()
                _execute_contact_plan(weni, prepared)
                weni.contact_group_action.assert_called_once_with(
                    "u1", action="remove", group="g-recompra"
                )
                weni.update_contact_fields.assert_not_called()

    def test_header_with_wrong_order_or_customer_cannot_confirm_cancellation(self):
        for key, value in (("id", 456), ("codCliente", 456)):
            with self.subTest(key=key):
                self.setUp()
                self.pedido["capa"].update({key: value, "status": 3})
                plan = self.build()
                self.assertNotEqual(plan.purchase_confirmation, "CANCELADO_API")
                self.assertFalse(set(plan.fields) & PURCHASE_FIELDS)
                self.assertEqual(plan.group_actions, [])

    def test_invalid_cpf_cancel_requires_matching_name_and_contact_identity(self):
        self.contact["fields"].update(cpf="123", email="cliente@example.invalid")
        self.customer["cliente"].update(
            cpf="123", nome="Cliente Sintético", email="cliente@example.invalid"
        )
        self.pedido["capa"]["status"] = 3
        plan = self.build()
        self.assertEqual(plan.purchase_confirmation, "CANCELADO_API")
        self.assertIn("__invalid_cpf_identity", plan.guards)
        self.assertIn("__native_urns", plan.guards)
        self.assertFalse(set(plan.fields) & PURCHASE_FIELDS)
        changed = deepcopy(self.contact)
        changed["name"] = "Outro Cliente"
        prepared, conflict = self.prepare(plan, changed)
        self.assertIsNone(prepared)
        self.assertIn("identificar", conflict)

        self.customer["cliente"]["nome"] = "Outro Cliente"
        plan = self.build()
        self.assertNotEqual(plan.purchase_confirmation, "CANCELADO_API")
        self.assertEqual(plan.group_actions, [])

    def test_changed_order_guard_blocks_frozen_cancellation(self):
        self.pedido["capa"]["status"] = 3
        plan = self.build()
        for key, value in (
            ("cpf", "16899535009"),
            ("pedido_id", "456"),
            ("pedido_data", "09/10/26 10:00:00"),
        ):
            with self.subTest(key=key):
                changed = deepcopy(self.contact)
                changed["fields"][key] = value
                prepared, conflict = self.prepare(plan, changed)
                self.assertIsNone(prepared)
                self.assertIn(key, conflict)

    def test_removed_group_is_idempotent_and_never_readded(self):
        self.pedido["capa"]["status"] = 3
        plan = self.build()
        changed = deepcopy(self.contact)
        changed["groups"] = []
        prepared, conflict = self.prepare(plan, changed)
        self.assertEqual(conflict, "")
        self.assertIsNotNone(prepared)
        self.assertEqual(prepared.group_actions, [])
        weni = Mock()
        _execute_contact_plan(weni, prepared)
        weni.contact_group_action.assert_not_called()
        weni.update_contact_fields.assert_not_called()

    def test_order_removal_failure_has_one_attempt_and_no_partial_update(self):
        self.pedido["capa"]["status"] = 3
        plan = self.build()
        weni = Mock()
        weni.contact_group_action.side_effect = RuntimeError("Falha sintética")
        with self.assertRaises(RuntimeError):
            _execute_contact_plan(weni, plan)
        self.assertEqual(weni.contact_group_action.call_count, 1)
        weni.update_contact_fields.assert_not_called()

    def test_header_proof_is_reset_before_revalidation(self):
        order = {"pedido_id": "123", "_farmalider_cabecalho_verificado": "1"}
        self.pedido["capa"].update(codCliente=456, status=3)
        result, _ = apply_pedido_api_confirmation(
            order, [], self.pedido, linked_cliente_id="99", confirmed_at=NOW.isoformat()
        )
        self.assertNotIn("_farmalider_cabecalho_verificado", result)
        self.assertEqual(result["status_confirmacao"], "PEDIDO_CLIENTE_DIVERGENTE_API")

    def test_locator_without_linked_customer_has_no_ownership_proof(self):
        self.pedido["capa"]["status"] = 3
        result, _ = apply_pedido_api_confirmation(
            {"pedido_id": "123"},
            [],
            self.pedido,
            linked_cliente_id="",
            confirmed_at=NOW.isoformat(),
            allow_locator_without_customer=True,
        )
        self.assertNotIn("_farmalider_cabecalho_verificado", result)
        self.assertEqual(result["status_validacao"], "ERRO")


if __name__ == "__main__":
    unittest.main()
