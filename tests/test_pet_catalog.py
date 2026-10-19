"""Catálogo parcial: correspondência exata, sem rede nem dados de clientes."""

import json
import unittest
from copy import deepcopy
from unittest.mock import patch

from pet_catalog import CATALOG_SOURCE_ROWS, catalog_contains
from pharmacy_scope import filter_pharmacy_order, sanitize_contact_order
from product_scope import PHARMACY_SCOPE_VERSION, is_animal_product
from recompra import PRODUCT_POLICY, audit_plan, propose
from tests.test_pharmacy_scope import fixture as scope_fixture
from tests.test_recompra import NOW, STRUCTURE, fixture


class PetCatalogTests(unittest.TestCase):
    def test_reference_matches_abbreviation_and_unknown_display_name(self):
        for field in ("produto_codigo", "codigo", "codProduto"):
            with self.subTest(field=field):
                self.assertTrue(
                    is_animal_product({field: "32909403", "nome": "Produto X"})
                )
        self.assertTrue(is_animal_product({"produto_codigo": 32909403}))
        self.assertTrue(is_animal_product({"nome": "EASOTIC SUSPENSAO 10ML"}))
        self.assertTrue(is_animal_product({"nome": "MICRO LAC 15G"}))

    def test_name_matching_tolerates_accents_spaces_and_case(self):
        self.assertTrue(catalog_contains({"nome": "  MICRO\u00a0LÁC  15g  "}))
        self.assertTrue(
            catalog_contains(
                {"nome": "Suspensão Otológica para Cães Virbac EasOtic - 10ml"}
            )
        )

    def test_only_reference_namespace_and_full_name_are_matched(self):
        for field in ("id", "produto_id", "sku_id", "ID do SKU", "ean"):
            with self.subTest(field=field):
                self.assertFalse(
                    catalog_contains({field: "32909403", "nome": "Produto humano"})
                )
        for name in ("Micro", "Micro Lac", "Micro Lac 150g", "Micro Lac 15g outro"):
            with self.subTest(name=name):
                self.assertFalse(catalog_contains({"nome": name}))
        self.assertFalse(catalog_contains({"codigo": "32909403-extra"}))
        self.assertFalse(catalog_contains({}))

    def test_partial_catalog_does_not_replace_other_evidence(self):
        self.assertFalse(catalog_contains({"nome": "Produto novo"}))
        self.assertTrue(
            is_animal_product({"nome": "Produto novo", "categoria": "Veterinário"})
        )
        for name in ("ACNASE CREME", "SINUSTRAT", "Frasco PET 300ml", "Paracetamol"):
            self.assertFalse(is_animal_product({"nome": name}))
        self.assertEqual(CATALOG_SOURCE_ROWS, 8769)

    def test_veterinary_brands_in_abbreviated_names_are_excluded(self):
        for name in (
            "MACROGARD PASTA 60G",
            "MACROGARD SAMLL SIZE C/30 CPR",
            "MacroGard Pet Small Size 30 comprimidos",
            "BECORDIL 5MG C/30 CPR",
        ):
            with self.subTest(name=name):
                self.assertTrue(is_animal_product({"produto_nome": name}))
                current, order, items = fixture(24, grouped=True)
                items[0]["produto_nome"] = name
                plan = propose(current, order, items, STRUCTURE, now=NOW)
                self.assertEqual(plan["reason"], "PEDIDO_SOMENTE_PRODUTOS_ANIMAIS")
                self.assertEqual(plan["action"], "remove")

    def test_character_bandage_is_human_without_overriding_pet_evidence(self):
        name = "CURATIVO TOPZ PATRULHA CANINA C/25"
        self.assertFalse(is_animal_product({"nome": name}))
        self.assertFalse(
            is_animal_product(
                {"nome": "Curativo Topz Baby Patrulha Canina 10 unidades"}
            )
        )
        for item in (
            {"nome": name, "categoria": "Veterinário"},
            {"nome": name, "produto_codigo": "32909403"},
            {"nome": name + " uso animal"},
            {"nome": "Coleira Patrulha Canina"},
        ):
            self.assertTrue(is_animal_product(item))
        with patch("product_scope.catalog_contains", return_value=True):
            self.assertTrue(is_animal_product({"nome": name}))

    def test_mixed_order_removes_veterinary_brand_preserving_human_bandage_value(self):
        order, items, _ = scope_fixture()
        items[0]["produto_nome"] = "CURATIVO TOPZ PATRULHA CANINA C/25"
        items[1]["produto_nome"] = "MACROGARD PASTA 60G"
        filtered, retained = filter_pharmacy_order(order, items)
        self.assertEqual(retained, [items[0]])
        self.assertEqual(filtered["valor_total_faturamento"], "18.00")
        self.assertEqual(filtered["farmalider_frete"], "0.00")
        current, purchase, campaign_items = fixture(24)
        campaign_items[0]["produto_nome"] = items[0]["produto_nome"]
        campaign_items.append(
            {**campaign_items[0], "produto_nome": items[1]["produto_nome"]}
        )
        plan = propose(current, purchase, campaign_items, STRUCTURE, now=NOW)
        self.assertEqual(plan["action"], "add")
        self.assertNotIn("MACROGARD", plan["fields"]["pedido_produtos"])
        self.assertIn("PATRULHA CANINA", plan["fields"]["pedido_produtos"])

    def test_pet_order_by_code_is_excluded_and_personal_fields_preserved(self):
        order, items, contact = scope_fixture(pet_only=True)
        raw = json.loads(contact["ultimo_pedido"])
        raw["produtos"][0].update(nome="Produto X", codigo="32909403")
        contact["ultimo_pedido"] = json.dumps(raw)
        contact["pedido_produtos"] = "Produto X"
        items[0].update(produto_nome="Produto X", produto_codigo="32909403")
        filtered, retained = filter_pharmacy_order(order, items)
        self.assertIsNone(filtered)
        self.assertEqual(retained, [])
        clean = sanitize_contact_order(contact)
        self.assertEqual(clean["pedido_produtos"], "")
        self.assertEqual(clean["cpf"], contact["cpf"])
        self.assertEqual(clean["opt_in_lgpd"], "Recusado")
        self.assertEqual(contact["pedido_produtos"], "Produto X")

    def test_mixed_order_by_code_preserves_only_discounted_human_value(self):
        order, items, _ = scope_fixture()
        items[1].update(produto_nome="Produto X", produto_codigo="32909403")
        before = deepcopy(items)
        filtered, retained = filter_pharmacy_order(order, items)
        self.assertIsNotNone(filtered)
        self.assertEqual(retained, [items[0]])
        self.assertEqual(filtered["valor_total_faturamento"], "18.00")
        self.assertEqual(filtered["farmalider_frete"], "0.00")
        self.assertEqual(items, before)

    def test_recompra_policy_invalidates_reviews_without_catalog(self):
        self.assertNotEqual(PHARMACY_SCOPE_VERSION, "human_products_v1")
        self.assertIn(PHARMACY_SCOPE_VERSION, PRODUCT_POLICY)

    def test_recompra_removes_pet_by_code_before_admission(self):
        contact, order, items = fixture(24, grouped=True)
        items[0].update(produto_nome="Produto X", produto_codigo="32909403")
        plan = propose(contact, order, items, STRUCTURE, now=NOW)
        self.assertEqual(plan["action"], "remove")
        self.assertEqual(plan["reason"], "PEDIDO_SOMENTE_PRODUTOS_ANIMAIS")

    def test_frozen_campaign_keeps_and_rechecks_product_reference(self):
        contact, order, items = fixture(24)
        items[0].update(produto_nome="Paracetamol", produto_codigo="99999999")
        plan = propose(contact, order, items, STRUCTURE, now=NOW)
        self.assertEqual(plan["campaign_products"][0]["produto_codigo"], "99999999")
        audit_plan(plan)
        plan["campaign_products"][0]["produto_codigo"] = "32909403"
        with self.assertRaises(RuntimeError):
            audit_plan(plan)


if __name__ == "__main__":
    unittest.main()
