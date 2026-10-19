"""Classificação cadastral sem descartar localizadores não zero reconhecidos pela API."""

import unittest
from copy import deepcopy
from unittest.mock import Mock

from farmalider_client import FarmaliderBadRequestError
from farmalider_integration import cpf_cadastro_status, resolve_contact_order
from main import FarmaliderSettings, validate_contacts

CAPTURED_AT = "2026-10-19T10:00:00-03:00"


def customer_payload(cpf):
    return {
        "cliente": {
            "id": 41,
            "cpf": cpf,
            "nome": "Felipe Sintético",
            "telefone": "85987654321",
        },
        "ultimoPedido": {
            "id": 74,
            "codCliente": 41,
            "emissao": "2026-10-18T10:00:00",
            "status": 2,
            "formaPagamento": 1,
            "frete": 0,
        },
        "itens": [
            {
                "codPedido": 74,
                "codProduto": 901,
                "descricao": "Curativo sintético",
                "quantidade": 1,
                "valor": 12,
                "total": 12,
            }
        ],
    }


def contact(cpf):
    return {
        "uuid": "cliente-sintetico",
        "name": "Felipe",
        "fields": {"cpf": cpf},
    }


def farm_client(cpf):
    api = Mock()
    payload = customer_payload(cpf)
    api.get_cliente_by_cpf.return_value = payload
    api.get_pedido.return_value = {
        "capa": payload["ultimoPedido"],
        "itens": payload["itens"],
    }
    return api


class CpfCadastroStatusTests(unittest.TestCase):
    def test_absent_value_is_distinct_from_placeholder(self):
        for value in (None, "", " \t", {"value": None}, {"value": ""}):
            with self.subTest(value=value):
                self.assertEqual(cpf_cadastro_status(value), "CPF_NAO_INFORMADO")

    def test_placeholder_or_malformed_value_is_rejected_locally(self):
        for value in (
            0,
            "0",
            "00000000000",
            "000.000.000-00",
            {"value": "0"},
            True,
            123.0,
            "sem CPF",
            "CPF 52998224725",
            "52998224725x",
            "529982247250",
            "-",
            "１２３",
            ["52998224725"],
        ):
            with self.subTest(value=value):
                self.assertEqual(cpf_cadastro_status(value), "CPF_INVALIDO_CADASTRO")

    def test_nonzero_invalid_checksum_still_allows_official_identity_validation(self):
        for value in (
            "123",
            "11111111111",
            "52998224724",
            "529.982.247-24",
            "52998224725",
            "191",
            {"value": "191"},
        ):
            with self.subTest(value=value):
                self.assertEqual(cpf_cadastro_status(value), "")

    def test_zero_never_queries_or_uses_stale_order_locator_and_preserves_original(
        self,
    ):
        for value in (0, "0", "000.000.000-00", {"value": "0"}):
            with self.subTest(value=value):
                entry = contact(value)
                entry["fields"]["pedido_id"] = "74"
                entry["fields"]["ultimo_pedido"] = {"pedido_id": "74"}
                before = deepcopy(entry)
                api = farm_client(0)
                order, items = resolve_contact_order(
                    entry, farm_client=api, captured_at=CAPTURED_AT
                )
                self.assertEqual(order["status_confirmacao"], "CPF_INVALIDO_CADASTRO")
                self.assertEqual(order["cpf_normalizado"].strip("0"), "")
                self.assertEqual(items, [])
                self.assertEqual(entry, before)
                api.get_cliente_by_cpf.assert_not_called()
                api.get_pedido.assert_not_called()

    def test_malformed_value_is_not_silently_stripped_into_another_persons_cpf(self):
        api = farm_client("52998224725")
        order, items = resolve_contact_order(
            contact("CPF 52998224725"), farm_client=api, captured_at=CAPTURED_AT
        )
        self.assertEqual(order["status_confirmacao"], "CPF_INVALIDO_CADASTRO")
        self.assertEqual(order["cpf"], "CPF 52998224725")
        self.assertEqual(items, [])
        api.get_cliente_by_cpf.assert_not_called()
        api.get_pedido.assert_not_called()

    def test_zero_restored_valid_cpf_ignores_display_name_mismatch(self):
        api = farm_client("00000000191")
        entry = contact("191")
        entry["name"] = "✨"
        order, items = resolve_contact_order(
            entry, farm_client=api, captured_at=CAPTURED_AT
        )
        api.get_cliente_by_cpf.assert_called_once_with("00000000191")
        self.assertEqual(order["status_confirmacao"], "CONFIRMADO_API")
        self.assertEqual(order["status_validacao"], "OK")
        self.assertEqual(len(items), 1)
        self.assertEqual(entry["fields"]["cpf"], "191")

    def test_nonzero_invalid_cpf_is_confirmed_by_first_name(self):
        api = farm_client("123")
        order, items = resolve_contact_order(
            contact("123"), farm_client=api, captured_at=CAPTURED_AT
        )
        api.get_cliente_by_cpf.assert_called_once_with("123")
        self.assertEqual(order["status_validacao"], "OK")
        self.assertEqual(len(items), 1)

    def test_nonzero_invalid_cpf_is_confirmed_by_local_phone(self):
        api = farm_client("123")
        entry = contact("123")
        entry["name"] = "✨"
        entry["urns"] = ["tel:+5591987654321"]
        order, items = resolve_contact_order(
            entry, farm_client=api, captured_at=CAPTURED_AT
        )
        api.get_cliente_by_cpf.assert_called_once_with("123")
        self.assertEqual(order["status_validacao"], "OK")
        self.assertEqual(len(items), 1)

    def test_http_rejection_keeps_existing_official_rejection_classification(self):
        api = Mock()
        api.get_cliente_by_cpf.side_effect = FarmaliderBadRequestError("400 sintético")
        order, items = resolve_contact_order(
            contact("123"), farm_client=api, captured_at=CAPTURED_AT
        )
        api.get_cliente_by_cpf.assert_called_once_with("123")
        self.assertEqual(order["status_confirmacao"], "CPF_REJEITADO_API")
        self.assertEqual(items, [])

    def test_analytics_classifies_bad_cadastro_without_query_or_reusing_customer_id(
        self,
    ):
        originals = ["0", "000.000.000-00", "sem CPF", "52998224725x", ""]
        rows = [
            {
                "contact_uuid": f"cliente-sintetico-{index}",
                "cpf": value,
                "farmalider_cliente_id": "41",
                "farmalider_cliente_nome": "Nome anterior",
            }
            for index, value in enumerate(originals)
        ]
        api = Mock()
        statuses = {}
        stats = validate_contacts(
            rows,
            farm_client=api,
            farm_settings=FarmaliderSettings(True, "token-sintetico", False),
            sync_time=CAPTURED_AT,
            existing_by_uuid={row["contact_uuid"]: deepcopy(row) for row in rows},
            reuse_existing=True,
            cliente_lookup_status_by_cpf=statuses,
        )
        self.assertEqual([row["cpf"] for row in rows], originals)
        self.assertEqual(
            [row["farmalider_cliente_status"] for row in rows],
            ["CPF_INVALIDO_CADASTRO"] * 4 + ["CPF_NAO_INFORMADO"],
        )
        self.assertTrue(all(row["farmalider_cliente_id"] == "" for row in rows))
        self.assertTrue(all(row["farmalider_cliente_nome"] == "" for row in rows))
        self.assertTrue(
            all(row["farmalider_cliente_consultado_em"] == "" for row in rows)
        )
        self.assertEqual(statuses["0"], "CPF_INVALIDO_CADASTRO")
        self.assertEqual(stats.invalid_registration, 4)
        self.assertEqual(stats.unique_queries, 0)
        self.assertEqual(stats.confirmed, 0)
        self.assertEqual(stats.api_errors, 0)
        self.assertEqual(stats.cache_reused, 0)
        api.get_cliente_by_cpf.assert_not_called()
