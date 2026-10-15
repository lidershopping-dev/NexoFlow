"""Frete representado na lista de itens não é mercadoria nem receita duplicada."""

import json
import unittest
from copy import deepcopy

from analytics import normalize_contact
from farmalider_integration import apply_pedido_api_confirmation, resolve_contact_order
from main import build_orders
from pharmacy_scope import filter_pharmacy_order
from tests.test_pedido_resolution import (
    CAPTURED_AT,
    client,
    cliente_payload,
    contact,
    item,
    pedido_payload,
)


class FreightItemTests(unittest.TestCase):
    def payload(self):
        payload = pedido_payload()
        payload["itens"].append(
            item(code=0, description=None, value="9.99", total="9.99")
        )
        return payload

    def confirm(self, payload):
        return apply_pedido_api_confirmation(
            {"pedido_id": "123", "origem_pedido": "FARMALIDER_PEDIDO_API"},
            [],
            payload,
            linked_cliente_id="99",
            confirmed_at=CAPTURED_AT,
        )

    def test_matching_freight_is_counted_once_and_excluded_from_product_snapshot(self):
        payload = self.payload()
        original = deepcopy(payload)
        for code, description in ((0, None), ("0", ""), (" 0 ", "  ")):
            with self.subTest(code=code, description=description):
                payload["itens"][1].update(codProduto=code, descricao=description)
                order, items = self.confirm(payload)
                self.assertEqual(order["status_confirmacao"], "CONFIRMADO_API")
                self.assertEqual(order["valor_total_compra"], "38.90")
                self.assertEqual(order["valor_total_api_itens"], "38.90")
                self.assertEqual(order["farmalider_frete"], "9.99")
                self.assertEqual(order["valor_total_faturamento"], "48.89")
                self.assertEqual(order["quantidade_itens"], "1")
                self.assertEqual(order["quantidade_itens_api"], "1")
                self.assertEqual(order["quantidade_produtos"], "1")
                self.assertEqual(len(items), 1)
                self.assertEqual(items[0]["produto_codigo"], "9001")
                self.assertEqual(
                    len(json.loads(order["ultimo_pedido_raw"])["produtos"]), 1
                )
        payload["itens"][1].update(codProduto=0, descricao=None)
        self.assertEqual(payload, original)

    def test_order_get_and_customer_snapshot_use_the_same_freight_rule(self):
        payload = self.payload()
        customer = cliente_payload()
        customer["itens"] = deepcopy(payload["itens"])
        for dedicated in (payload, None):
            with self.subTest(dedicated=dedicated is not None):
                order, items = resolve_contact_order(
                    contact(),
                    farm_client=client(cliente=customer, pedido=dedicated),
                    captured_at=CAPTURED_AT,
                )
                self.assertEqual(order["status_confirmacao"], "CONFIRMADO_API")
                self.assertEqual(order["valor_total_faturamento"], "48.89")
                self.assertEqual(len(items), 1)

    def test_analytics_build_does_not_mark_valid_freight_as_a_blocking_error(self):
        payload = self.payload()
        customer = cliente_payload()
        customer["itens"] = deepcopy(payload["itens"])
        person = contact()
        orders, items, stats = build_orders(
            [person],
            rows_by_uuid={person["uuid"]: normalize_contact(person, CAPTURED_AT)},
            farm_client=client(cliente=customer, pedido=payload),
            sync_time=CAPTURED_AT,
            cliente_responses_by_cpf={person["fields"]["cpf"]: customer},
        )
        self.assertEqual(stats.api_errors, 0)
        self.assertEqual(stats.failure_reasons, {})
        self.assertEqual(stats.confirmed_api, 1)
        self.assertEqual(len(items), 1)
        self.assertEqual(orders[0]["valor_total_faturamento"], "48.89")

    def test_multiple_freight_rows_must_match_header_in_total(self):
        payload = self.payload()
        payload["itens"][1].update(valor="4.00", total="4.00")
        payload["itens"].insert(
            0, item(code=0, description=None, value="5.99", total="5.99")
        )
        order, items = self.confirm(payload)
        self.assertEqual(order["farmalider_frete"], "9.99")
        self.assertEqual(order["valor_total_faturamento"], "48.89")
        self.assertEqual(len(items), 1)

    def test_freight_mismatch_or_missing_header_stays_an_error(self):
        for header in (None, "", "0", "5.00"):
            with self.subTest(header=header):
                payload = self.payload()
                payload["capa"]["frete"] = header
                order, items = self.confirm(payload)
                self.assertEqual(order["status_confirmacao"], "ERRO_ITENS_PEDIDO_API")
                self.assertEqual(items, [])

    def test_wrong_order_owner_and_malformed_freight_values_still_fail(self):
        for change, expected in (
            ({"codPedido": 456}, "ITEM_PEDIDO_DIVERGENTE_API"),
            ({"quantidade": 0}, "ERRO_ITENS_PEDIDO_API"),
            ({"valor": -1}, "ERRO_ITENS_PEDIDO_API"),
            ({"total": "NaN"}, "ERRO_ITENS_PEDIDO_API"),
            ({"codProduto": False}, "ERRO_ITENS_PEDIDO_API"),
        ):
            with self.subTest(change=change):
                payload = self.payload()
                payload["itens"][1].update(change)
                order, items = self.confirm(payload)
                self.assertEqual(order["status_confirmacao"], expected)
                self.assertEqual(items, [])
        payload = self.payload()
        payload["capa"]["codCliente"] = 456
        self.assertEqual(
            self.confirm(payload)[0]["status_confirmacao"],
            "PEDIDO_CLIENTE_DIVERGENTE_API",
        )

    def test_product_with_missing_name_is_not_freight_just_because_price_matches(self):
        for code in (9002, None, "", "0000"):
            with self.subTest(code=code):
                payload = self.payload()
                payload["itens"][1]["codProduto"] = code
                order, _ = self.confirm(payload)
                self.assertEqual(order["status_confirmacao"], "ERRO_ITENS_PEDIDO_API")
        payload = self.payload()
        payload["itens"][1]["descricao"] = "Produto real com código zero"
        order, items = self.confirm(payload)
        self.assertEqual(order["quantidade_produtos"], "2")
        self.assertEqual(len(items), 2)

    def test_freight_without_any_product_is_treated_as_cancelled(self):
        payload = self.payload()
        payload["itens"] = payload["itens"][1:]
        order, items = self.confirm(payload)
        self.assertEqual(order["status_confirmacao"], "CANCELADO_API")
        self.assertEqual(order["status_validacao"], "CANCELADO_SEM_ITENS")
        self.assertEqual(items, [])

    def test_pet_mixed_order_keeps_only_human_items_without_freight(self):
        payload = self.payload()
        payload["itens"].append(
            item(code=9002, description="Ração para cães", value="20.00", total="20.00")
        )
        order, items = self.confirm(payload)
        retained, products = filter_pharmacy_order(order, items)
        self.assertIsNotNone(retained)
        self.assertEqual(retained["valor_total_compra"], "38.90")
        self.assertEqual(retained["valor_total_faturamento"], "38.90")
        self.assertEqual(retained["farmalider_frete"], "0.00")
        self.assertEqual(len(products), 1)
