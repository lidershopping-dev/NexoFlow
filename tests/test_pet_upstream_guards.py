"""Cargas Weni/Planilhas não restauram produtos pet em campanha agendada."""

import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import Mock, patch

from delivery_tickets import _order_plan, _recompra_removals
from pedido_utils import serialize_weni_order_list
from weni_client import WeniError
from weni_update import (
    REQUIRED_ORDER_FIELDS,
    ContactUpdatePlan,
    UpdateStats,
    _build_contact_plan,
    _canonical_plan,
    _execute_contact_plan,
    _prepare_frozen_plan_for_apply,
    load_review_snapshot,
    write_review_snapshot,
)

NOW = "2026-10-09T13:00:00-03:00"
ORDER_DATE = "2026-10-06T10:00:00-03:00"
GROUP_NAME = "Recompra FarmaLíder"
GROUP_UUID = "recompra-synthetic"


def contact():
    return {
        "uuid": "contact-synthetic",
        "name": "Cliente Sintético",
        "urns": ["tel:+5591999999999"],
        "groups": [{"uuid": GROUP_UUID, "name": GROUP_NAME}],
        "fields": {
            "cpf": "52998224725",
            "pedido_id": "100",
            "pedido_data": "06/10/26 10:00:00",
            "pedido_produtos": "Produto humano anterior",
            "pedido_quantidade_itens": "1",
            "pedido_valor_produtos": "10",
            "pedido_valor": "10",
            "pedido_status": "2",
            "pedido_forma_pagamento": "3",
        },
    }


def purchase(name, category=""):
    return (
        {
            "pedido_id": "100",
            "data_hora": ORDER_DATE,
            "status_confirmacao": "CONFIRMADO_API",
            "status_validacao": "OK",
            "farmalider_status_pedido": "2",
            "farmalider_forma_pagamento": "3",
            "valor_total_compra": "10",
        },
        [
            {
                "produto_nome": name,
                "categoria": category,
                "quantidade": "1",
                "valor_unitario": "10",
                "valor_total": "10",
            }
        ],
    )


def build_plan(target, current, resolved):
    if target == "planilhas":
        return _order_plan(current, resolved, Mock(), NOW, REQUIRED_ORDER_FIELDS)
    with patch("weni_update.resolve_contact_order", return_value=resolved):
        return _build_contact_plan(
            current,
            context_messages=[],
            candidate_message_ids=set(),
            farm_client=Mock(),
            opt_out_group_name="Opt-Out LGPD",
            opt_out_group_uuid="lgpd-out-synthetic",
            captured_at=NOW,
            available_weni_fields=REQUIRED_ORDER_FIELDS,
        )


class PetUpstreamGuardTests(unittest.TestCase):
    def test_same_date_pet_restore_removes_campaign_before_full_item_write(self):
        for target in ("weni", "planilhas"):
            with self.subTest(target=target):
                plan = build_plan(
                    target,
                    contact(),
                    purchase("ALIM GATO GRANPLUS FIL FG/ARR 3KG +++"),
                )
                self.assertNotIn("pedido_data", plan.fields)
                self.assertEqual(
                    [(a.action, a.group) for a in plan.group_actions],
                    [("remove", GROUP_UUID)],
                )
                client = Mock()
                _execute_contact_plan(client, plan)
                self.assertEqual(
                    [call[0] for call in client.method_calls],
                    ["contact_group_action", "update_contact_fields"],
                )
                self.assertEqual(
                    client.update_contact_fields.call_args.args[1]["pedido_produtos"],
                    "ALIM GATO GRANPLUS FIL FG/ARR 3KG +++",
                )
                self.assertNotIn("marketing_opt_in", plan.fields)

    def test_opaque_product_with_animal_category_removes_campaign(self):
        for target in ("weni", "planilhas"):
            with self.subTest(target=target):
                plan = build_plan(
                    target, contact(), purchase("Produto opaco 10 mg", "Veterinários")
                )
                self.assertTrue(plan.order_has_animal_products)
                self.assertNotIn("pedido_data", plan.fields)
                self.assertEqual(plan.group_actions[0].group, GROUP_UUID)

    def test_human_and_packaging_pet_do_not_cancel_same_date_campaign(self):
        for name in (
            "CURATIVO TOPZ PATRULHA CANINA C/25",
            "Shampoo hidratante para humanos 200 ml",
            "Protetor solar embalagem PET 100 ml",
            "Creme para hidratação 200 g",
        ):
            for target in ("weni", "planilhas"):
                with self.subTest(name=name, target=target):
                    plan = build_plan(target, contact(), purchase(name))
                    self.assertNotIn("pedido_data", plan.fields)
                    self.assertFalse(plan.order_has_animal_products)
                    self.assertEqual(plan.group_actions, [])

    def test_new_veterinary_brands_remove_group_in_weni_and_spreadsheet_loads(self):
        for name in ("MACROGARD PASTA 60G", "BECORDIL 5MG C/30 CPR"):
            for target in ("weni", "planilhas"):
                with self.subTest(name=name, target=target):
                    plan = build_plan(target, contact(), purchase(name))
                    self.assertTrue(plan.order_has_animal_products)
                    self.assertEqual(plan.group_actions[0].action, "remove")
                    self.assertEqual(plan.group_actions[0].group, GROUP_UUID)

    def test_unchanged_pet_products_do_not_introduce_unreviewed_group_change(self):
        for target in ("weni", "planilhas"):
            with self.subTest(target=target):
                current = contact()
                current["fields"]["pedido_produtos"] = "BISC CAES PREMIER 250G"
                plan = build_plan(target, current, purchase("BISC CAES PREMIER 250G"))
                self.assertNotIn("pedido_produtos", plan.fields)
                self.assertEqual(plan.group_actions, [])

    def test_csv_pet_item_and_punctuation_are_preserved_in_upstream_data(self):
        current = contact()
        names = ["Creme humano, 30 g", "PETISCO P/CAES SPIN PET 50G"]
        plan = ContactUpdatePlan(
            current["uuid"],
            fields={"pedido_produtos": serialize_weni_order_list(names)},
        )
        actions = _recompra_removals(plan, current, NOW)
        self.assertEqual(
            [(a.action, a.group) for a in actions], [("remove", GROUP_UUID)]
        )
        self.assertEqual(
            plan.fields["pedido_produtos"], serialize_weni_order_list(names)
        )

    def test_group_removal_failure_blocks_product_write_and_does_not_retry(self):
        for target in ("weni", "planilhas"):
            with self.subTest(target=target):
                plan = build_plan(target, contact(), purchase("ALIM CAES 2KG"))
                client = Mock()
                client.contact_group_action.side_effect = WeniError("Falha sintética")
                with self.assertRaises(WeniError):
                    _execute_contact_plan(client, plan)
                client.contact_group_action.assert_called_once_with(
                    "contact-synthetic", action="remove", group=GROUP_UUID
                )
                client.update_contact_fields.assert_not_called()

    def test_category_evidence_survives_review_and_preparation(self):
        current = contact()
        plan = build_plan(
            "planilhas", current, purchase("Produto opaco 10 mg", "Pet Shop")
        )
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "review.json"
            write_review_snapshot(
                [plan], UpdateStats(), full=True, json_path=path, captured_at_utc=NOW
            )
            reloaded, _, _ = load_review_snapshot(full=True, json_path=path)
        self.assertTrue(reloaded[0].order_has_animal_products)
        self.assertEqual(_canonical_plan(reloaded[0]), _canonical_plan(plan))
        prepared, error = _prepare_frozen_plan_for_apply(
            reloaded[0],
            deepcopy(current),
            opt_in_group_name="",
            opt_in_group_uuid="",
            opt_out_group_name="",
            opt_out_group_uuid="",
        )
        self.assertEqual(error, "")
        self.assertIsNotNone(prepared)
        self.assertTrue(prepared.order_has_animal_products)
        self.assertEqual(
            [(a.action, a.group) for a in _recompra_removals(prepared, current, NOW)],
            [("remove", GROUP_UUID)],
        )

    def test_non_animal_plan_keeps_previous_canonical_schema(self):
        plan = build_plan("planilhas", contact(), purchase("Creme humano 30 g"))
        self.assertNotIn("order_has_animal_products", _canonical_plan(plan))

    def test_admission_after_review_blocks_unreviewed_pet_restore(self):
        for target in ("weni", "planilhas"):
            for name, category in (
                ("ALIM CAES 2KG", ""),
                ("Produto opaco 10 mg", "Pet Shop"),
            ):
                with self.subTest(target=target, name=name):
                    initial = contact()
                    initial["groups"] = []
                    plan = build_plan(target, initial, purchase(name, category))
                    self.assertEqual(plan.group_actions, [])
                    prepared, error = _prepare_frozen_plan_for_apply(
                        plan,
                        contact(),
                        opt_in_group_name="",
                        opt_in_group_uuid="",
                        opt_out_group_name="",
                        opt_out_group_uuid="",
                    )
                    self.assertIsNone(prepared)
                    self.assertIn("Recompra mudou após a revisão", error)

    def test_replaced_campaign_uuid_requires_reviewing_new_removal(self):
        plan = build_plan("weni", contact(), purchase("ALIM CAES 2KG"))
        changed = contact()
        changed["groups"][0]["uuid"] = "different-campaign-synthetic"
        prepared, error = _prepare_frozen_plan_for_apply(
            plan,
            changed,
            opt_in_group_name="",
            opt_in_group_uuid="",
            opt_out_group_name="",
            opt_out_group_uuid="",
        )
        self.assertIsNone(prepared)
        self.assertIn("Recompra mudou após a revisão", error)


if __name__ == "__main__":
    unittest.main()
