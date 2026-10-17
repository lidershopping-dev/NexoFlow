import json
import tempfile
import unittest
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import Mock, patch

from analytics import parse_ultimo_pedido
from pedido_utils import parse_weni_order_list
from weni_update import (
    ContactUpdatePlan,
    GroupAction,
    PlanAuditError,
    UpdateStats,
    UpdateWindow,
    _build_contact_plan,
    _load_state,
    _prepare_frozen_plan_for_apply,
    _resolve_required_groups,
    _save_state,
    _track_open_order,
    audit_plans,
    load_review_snapshot,
    run_update,
    verify_review_snapshot,
    write_review_snapshot,
)


class WeniUpdateTests(unittest.TestCase):
    def setUp(self) -> None:
        # Testes unitários nunca devem consumir o checkpoint/estado operacional
        # real de C:\NexoFlow\logs. Cada teste de run_update começa sem
        # conflitos pendentes herdados de uma execução anterior.
        self._load_state_patcher = patch(
            "weni_update._load_state",
            side_effect=lambda *args, **kwargs: {},
        )
        self._load_state_patcher.start()
        self.addCleanup(self._load_state_patcher.stop)

    def _required_weni_fields(self) -> list[dict[str, str]]:
        keys = [
            "contact_urn_whatsapp",
            "contact_urn_instagram",
            "contact_urn_facebook",
            "contact_canal_principal",
            "pedido_id",
            "pedido_data",
            "pedido_status",
            "pedido_produtos",
            "pedido_quantidade_itens",
            "pedido_valor_produtos",
            "pedido_valor",
            "pedido_forma_pagamento",
        ]
        return [{"key": key} for key in keys]

    def _purchase_contact(self, *, include_raw: bool = True) -> dict:
        fields = {"cpf": "52998224725"}
        if include_raw:
            fields["ultimo_pedido"] = json.dumps(
                {
                    "pedido_id": "123",
                    "data_hora": "2026-10-03T12:00:00Z",
                    "valor_total_compra": 25.5,
                    "produtos": [
                        {
                            "nome": "Produto A",
                            "quantidade": 2,
                            "valor_unitario": 10.0,
                            "valor_total": 20.0,
                        },
                        {
                            "nome": "Produto B",
                            "quantidade": 1,
                            "valor_unitario": 5.5,
                            "valor_total": 5.5,
                        },
                    ],
                }
            )
        return {"uuid": "u-pedido", "fields": fields}

    def _purchase_api(self) -> Mock:
        farm = Mock()
        farm.get_cliente_by_cpf.return_value = {
            "id": "99",
            "cpf": "52998224725",
            "nome": "Cliente Teste",
            "ultimoPedido": {
                "id": "123",
                "codCliente": "99",
                "emissao": "2026-10-03T12:00:00Z",
                "status": 2,
                "formaPagamento": 2,
                "frete": 0,
            },
            "itens": [
                {
                    "codPedido": "123",
                    "codProduto": "9001",
                    "descricao": "Produto A",
                    "quantidade": 2,
                    "valor": 10.0,
                    "total": 20.0,
                },
                {
                    "codPedido": "123",
                    "codProduto": "9002",
                    "descricao": "Produto B",
                    "quantidade": 1,
                    "valor": 5.5,
                    "total": 5.5,
                },
            ],
        }
        farm.get_pedido.return_value = {
            "capa": {
                "id": "123",
                "codCliente": "99",
                "emissao": "2026-10-03T12:00:00Z",
                "status": 2,
                "formaPagamento": 2,
                "cancelamento": "",
                "frete": 0,
            },
            "itens": [
                {
                    "codPedido": "123",
                    "codProduto": "9001",
                    "descricao": "Produto A",
                    "quantidade": 2,
                    "valor": 10.0,
                    "total": 20.0,
                },
                {
                    "codPedido": "123",
                    "codProduto": "9002",
                    "descricao": "Produto B",
                    "quantidade": 1,
                    "valor": 5.5,
                    "total": 5.5,
                },
            ],
        }
        return farm

    def _purchase_plan(
        self,
        contact: dict,
        farm: Mock | None = None,
        *,
        captured_at: str = "2026-10-03T12:01:00Z",
        available_weni_fields: set[str] | None = None,
    ) -> ContactUpdatePlan:
        return _build_contact_plan(
            contact,
            context_messages=[],
            candidate_message_ids=set(),
            farm_client=farm,
            opt_out_group_name="Opt-Out LGPD",
            opt_out_group_uuid="g-out",
            captured_at=captured_at,
            available_weni_fields=available_weni_fields,
        )

    def _incremental_purchase_client(self, contact: dict) -> Mock:
        client = Mock()
        client.list_fields.return_value = [
            {"key": "contact_urn_whatsapp"},
            {"key": "contact_urn_instagram"},
            {"key": "contact_urn_facebook"},
            {"key": "contact_canal_principal"},
            {"key": "pedido_id"},
            {"key": "pedido_data"},
            {"key": "pedido_status"},
            {"key": "pedido_forma_pagamento"},
            {"key": "pedido_produtos"},
            {"key": "pedido_quantidade_itens"},
            {"key": "pedido_valor_produtos"},
            {"key": "pedido_valor"},
            {"key": "ultima_compra"},
        ]
        client.list_groups.return_value = [
            {"uuid": "g-in", "name": "Opt-In LGPD"},
            {"uuid": "g-out", "name": "Opt-Out LGPD"},
        ]
        client.list_contacts.return_value = [contact]
        client.list_messages.return_value = []
        return client

    def test_open_order_tracking_keeps_only_non_terminal_statuses(self) -> None:
        tracked: set[str] = set()
        _track_open_order(tracked, "u1", "Pendente de pagamento")
        self.assertEqual(tracked, {"u1"})
        _track_open_order(tracked, "u1", "-1")
        self.assertEqual(tracked, {"u1"})
        _track_open_order(tracked, "u1", "Em processamento")
        self.assertEqual(tracked, {"u1"})
        _track_open_order(tracked, "u1", "Em trânsito")
        self.assertEqual(tracked, {"u1"})
        _track_open_order(tracked, "u1", "Concluído")
        self.assertEqual(tracked, set())
        _track_open_order(tracked, "u1", "4")
        self.assertEqual(tracked, {"u1"})
        _track_open_order(tracked, "u1", "-2")
        self.assertEqual(tracked, {"u1"})
        _track_open_order(tracked, "u1", "Em processamento")
        _track_open_order(tracked, "u1", "Cancelado")
        self.assertEqual(tracked, set())

    def test_required_groups_are_resolved_with_one_api_listing(self) -> None:
        client = Mock()
        client.list_groups.return_value = [
            {"uuid": "g-in", "name": "Opt-In LGPD"},
            {"uuid": "g-out", "name": "Opt-Out LGPD"},
        ]
        result = _resolve_required_groups(
            client,
            ("Opt-In LGPD", "Opt-Out LGPD"),
        )
        self.assertEqual(result["Opt-In LGPD"], (True, "g-in"))
        self.assertEqual(result["Opt-Out LGPD"], (True, "g-out"))
        client.list_groups.assert_called_once_with()

    def test_audit_accepts_safe_age_plan(self) -> None:
        plan = ContactUpdatePlan(
            contact_uuid="u1",
            fields={"idade": 27, "maior_de_18_anos": "sim"},
        )
        audit_plans([plan], opt_out_group_available=True)

    def test_audit_rejects_inconsistent_age(self) -> None:
        plan = ContactUpdatePlan(
            contact_uuid="u1",
            fields={"idade": 17, "maior_de_18_anos": "sim"},
        )
        with self.assertRaises(PlanAuditError):
            audit_plans([plan], opt_out_group_available=True)

    def test_audit_rejects_purchase_without_confirmation(self) -> None:
        plan = ContactUpdatePlan(
            contact_uuid="u1",
            fields={"ultima_compra": "2026-10-03 12:00:00"},
            purchase_confirmed=False,
        )
        with self.assertRaises(PlanAuditError):
            audit_plans([plan], opt_out_group_available=True)

    def test_audit_rejects_malformed_fields_even_for_a_validated_order(self) -> None:
        malformed_fields = (
            {"pedido_data": "data inválida"},
            {"pedido_valor": -1},
            {"pedido_valor": float("nan")},
            {"pedido_valor": float("inf")},
            {"pedido_quantidade_itens": "0"},
            {"pedido_quantidade_itens": "-1"},
            {"pedido_produtos": ""},
            {"pedido_valor_produtos": "10, inválido"},
            {"pedido_status": "Finalizado"},
            {"pedido_forma_pagamento": "Pix"},
            {"pedido_forma_pagamento": "-1"},
            {"pedido_forma_pagamento": "2147483648"},
        )
        for fields in malformed_fields:
            with self.subTest(fields=fields):
                plan = ContactUpdatePlan(
                    contact_uuid="u-pedido",
                    fields=fields,
                    order_validated=True,
                )
                with self.assertRaises(PlanAuditError):
                    audit_plans([plan], opt_out_group_available=True)

    def test_audit_requires_optout_group_when_group_action_exists(self) -> None:
        plan = ContactUpdatePlan(
            contact_uuid="u1",
            fields={"opt_in_lgpd": "recusado"},
            group_actions=[
                GroupAction(
                    "add",
                    "Opt-Out LGPD",
                    "teste",
                    group_name="Opt-Out LGPD",
                )
            ],
        )
        with self.assertRaises(PlanAuditError):
            audit_plans(
                [plan],
                opt_out_group_available=False,
                opt_in_group_available=True,
            )

    def test_audit_requires_optin_group_when_group_action_exists(self) -> None:
        plan = ContactUpdatePlan(
            contact_uuid="u1",
            fields={"opt_in_lgpd": "ativo"},
            group_actions=[
                GroupAction(
                    "add",
                    "Opt-In LGPD",
                    "teste",
                    group_name="Opt-In LGPD",
                )
            ],
        )
        with self.assertRaisesRegex(PlanAuditError, "Opt-In LGPD"):
            audit_plans(
                [plan],
                opt_out_group_available=True,
                opt_in_group_available=False,
            )

    def test_build_plan_adds_refused_contact_to_optout(self) -> None:
        contact = {
            "uuid": "u1",
            "urns": ["whatsapp:5591999999999"],
            "groups": [],
            "fields": {},
        }
        messages = [
            {
                "id": "q1",
                "created_on": "2026-10-03T12:00:00Z",
                "direction": "out",
                "text": "Você aceita o tratamento de dados conforme a LGPD?",
            },
            {
                "id": "a1",
                "created_on": "2026-10-03T12:00:01Z",
                "direction": "in",
                "text": "Não",
            },
        ]
        plan = _build_contact_plan(
            contact,
            context_messages=messages,
            candidate_message_ids={"a1"},
            farm_client=None,
            opt_out_group_name="Opt-Out LGPD",
            opt_out_group_uuid="g-out",
            captured_at="2026-10-03T12:01:00Z",
            opt_in_group_name="Opt-In LGPD",
            opt_in_group_uuid="g-in",
        )
        self.assertEqual(plan.fields["opt_in_lgpd"], "recusado")
        self.assertEqual(plan.fields["contact_urn"], "whatsapp:5591999999999")
        actions = {(a.action, a.group_name, a.group) for a in plan.group_actions}
        self.assertEqual(actions, {("add", "Opt-Out LGPD", "g-out")})
        self.assertEqual(plan.fields["contact_urn_whatsapp"], "whatsapp:5591999999999")
        self.assertEqual(plan.fields["contact_canal_principal"], "whatsapp")

    def test_build_plan_multichannel_writes_specific_urns_and_principal(self) -> None:
        contact = {
            "uuid": "u-multi",
            "urns": ["instagram:222", "facebook:333", "whatsapp:111"],
            "groups": [],
            "fields": {},
        }
        plan = _build_contact_plan(
            contact,
            context_messages=[],
            candidate_message_ids=set(),
            farm_client=None,
            opt_out_group_name="Opt-Out LGPD",
            opt_out_group_uuid="g-out",
            captured_at="2026-10-03T12:01:00Z",
            opt_in_group_name="Opt-In LGPD",
            opt_in_group_uuid="g-in",
        )
        self.assertEqual(plan.fields["contact_urn"], "whatsapp:111")
        self.assertEqual(plan.fields["contact_urn_whatsapp"], "whatsapp:111")
        self.assertEqual(plan.fields["contact_urn_instagram"], "instagram:222")
        self.assertEqual(plan.fields["contact_urn_facebook"], "facebook:333")
        self.assertEqual(plan.fields["contact_canal_principal"], "whatsapp")
        self.assertEqual(plan.contact_channel, "whatsapp")

    def test_build_plan_instagram_only_does_not_invent_whatsapp(self) -> None:
        contact = {
            "uuid": "u-instagram",
            "urns": ["instagram:999"],
            "groups": [],
            "fields": {},
        }
        plan = _build_contact_plan(
            contact,
            context_messages=[],
            candidate_message_ids=set(),
            farm_client=None,
            opt_out_group_name="Opt-Out LGPD",
            opt_out_group_uuid="g-out",
            captured_at="2026-10-03T12:01:00Z",
            opt_in_group_name="Opt-In LGPD",
            opt_in_group_uuid="g-in",
        )
        self.assertEqual(plan.fields["contact_urn"], "instagram:999")
        self.assertEqual(plan.fields["contact_urn_instagram"], "instagram:999")
        self.assertEqual(plan.fields["contact_canal_principal"], "instagram")
        self.assertNotIn("contact_urn_whatsapp", plan.fields)
        self.assertNotIn("contact_urn_facebook", plan.fields)

    def test_build_plan_facebook_messenger_is_classified_as_facebook(self) -> None:
        contact = {
            "uuid": "u-facebook",
            "urns": ["messenger:abc"],
            "groups": [],
            "fields": {},
        }
        plan = _build_contact_plan(
            contact,
            context_messages=[],
            candidate_message_ids=set(),
            farm_client=None,
            opt_out_group_name="Opt-Out LGPD",
            opt_out_group_uuid="g-out",
            captured_at="2026-10-03T12:01:00Z",
            opt_in_group_name="Opt-In LGPD",
            opt_in_group_uuid="g-in",
        )
        self.assertEqual(plan.fields["contact_urn"], "messenger:abc")
        self.assertEqual(plan.fields["contact_urn_facebook"], "messenger:abc")
        self.assertEqual(plan.fields["contact_canal_principal"], "facebook")

    def test_audit_rejects_wrong_channel_specific_urn(self) -> None:
        plan = ContactUpdatePlan(
            contact_uuid="u-bad-channel",
            fields={"contact_urn_instagram": "whatsapp:5591999999999"},
        )
        with self.assertRaisesRegex(PlanAuditError, "contact_urn_instagram"):
            audit_plans(
                [plan], opt_out_group_available=True, opt_in_group_available=True
            )

    def test_build_plan_active_syncs_optin_and_optout(self) -> None:
        contact = {
            "uuid": "u1",
            "groups": [{"uuid": "g-out", "name": "Opt-Out LGPD"}],
            "fields": {"opt_in_lgpd": "ativo"},
        }
        plan = _build_contact_plan(
            contact,
            context_messages=[],
            candidate_message_ids=set(),
            farm_client=None,
            opt_out_group_name="Opt-Out LGPD",
            opt_out_group_uuid="g-out",
            captured_at="2026-10-03T12:01:00Z",
            opt_in_group_name="Opt-In LGPD",
            opt_in_group_uuid="g-in",
        )
        actions = {(a.action, a.group_name, a.group) for a in plan.group_actions}
        self.assertEqual(
            actions,
            {
                ("add", "Opt-In LGPD", "g-in"),
                ("remove", "Opt-Out LGPD", "g-out"),
            },
        )
        self.assertEqual(plan.group_actions[0].action, "add")
        self.assertEqual(plan.group_actions[0].group_name, "Opt-In LGPD")

    def test_build_plan_refused_syncs_optout_and_optin(self) -> None:
        contact = {
            "uuid": "u1",
            "groups": [{"uuid": "g-in", "name": "Opt-In LGPD"}],
            "fields": {"opt_in_lgpd": "recusado"},
        }
        plan = _build_contact_plan(
            contact,
            context_messages=[],
            candidate_message_ids=set(),
            farm_client=None,
            opt_out_group_name="Opt-Out LGPD",
            opt_out_group_uuid="g-out",
            captured_at="2026-10-03T12:01:00Z",
            opt_in_group_name="Opt-In LGPD",
            opt_in_group_uuid="g-in",
        )
        actions = {(a.action, a.group_name, a.group) for a in plan.group_actions}
        self.assertEqual(
            actions,
            {
                ("remove", "Opt-In LGPD", "g-in"),
                ("add", "Opt-Out LGPD", "g-out"),
            },
        )
        self.assertEqual(plan.group_actions[0].action, "add")
        self.assertEqual(plan.group_actions[0].group_name, "Opt-Out LGPD")

    def test_build_plan_consistent_lgpd_groups_need_no_action(self) -> None:
        for value, group_uuid, group_name in (
            ("ativo", "g-in", "Opt-In LGPD"),
            ("recusado", "g-out", "Opt-Out LGPD"),
        ):
            with self.subTest(value=value):
                contact = {
                    "uuid": "u1",
                    "groups": [{"uuid": group_uuid, "name": group_name}],
                    "fields": {"opt_in_lgpd": value},
                }
                plan = _build_contact_plan(
                    contact,
                    context_messages=[],
                    candidate_message_ids=set(),
                    farm_client=None,
                    opt_out_group_name="Opt-Out LGPD",
                    opt_out_group_uuid="g-out",
                    captured_at="2026-10-03T12:01:00Z",
                    opt_in_group_name="Opt-In LGPD",
                    opt_in_group_uuid="g-in",
                )
                self.assertEqual(plan.group_actions, [])

    def test_build_plan_updates_blank_cpf_from_explicit_message(self) -> None:
        contact = {
            "uuid": "u1",
            "fields": {"cpf": ""},
        }
        messages = [
            {
                "id": "m1",
                "created_on": "2026-10-03T12:00:00Z",
                "direction": "in",
                "text": "Meu CPF é 529.982.247-25",
            }
        ]
        plan = _build_contact_plan(
            contact,
            context_messages=messages,
            candidate_message_ids={"m1"},
            farm_client=None,
            opt_out_group_name="Opt-Out LGPD",
            opt_out_group_uuid="g1",
            captured_at="2026-10-03T12:01:00Z",
        )
        self.assertEqual(plan.fields["cpf"], "52998224725")
        self.assertEqual(plan.previous["cpf"], "")
        self.assertEqual(plan.evidence["cpf"]["message_id"], "m1")
        audit_plans([plan], opt_out_group_available=True)

    def test_build_plan_replaces_old_cpf_with_most_recent_customer_cpf(self) -> None:
        contact = {
            "uuid": "u1",
            "fields": {"cpf": "52998224725"},
        }
        messages = [
            {
                "id": "m2",
                "created_on": "2026-10-03T12:10:00Z",
                "direction": "in",
                "text": "Meu CPF correto é 168.995.350-09",
            }
        ]
        plan = _build_contact_plan(
            contact,
            context_messages=messages,
            candidate_message_ids={"m2"},
            farm_client=None,
            opt_out_group_name="Opt-Out LGPD",
            opt_out_group_uuid="g1",
            captured_at="2026-10-03T12:11:00Z",
        )
        self.assertEqual(plan.previous["cpf"], "52998224725")
        self.assertEqual(plan.fields["cpf"], "16899535009")
        audit_plans([plan], opt_out_group_available=True)

    def test_same_cpf_with_different_format_is_not_rewritten(self) -> None:
        contact = {
            "uuid": "u1",
            "fields": {"cpf": "529.982.247-25"},
        }
        messages = [
            {
                "id": "m1",
                "created_on": "2026-10-03T12:00:00Z",
                "direction": "in",
                "text": "CPF: 52998224725",
            }
        ]
        plan = _build_contact_plan(
            contact,
            context_messages=messages,
            candidate_message_ids={"m1"},
            farm_client=None,
            opt_out_group_name="Opt-Out LGPD",
            opt_out_group_uuid="g1",
            captured_at="2026-10-03T12:01:00Z",
        )
        self.assertNotIn("cpf", plan.fields)

    def test_audit_rejects_invalid_cpf(self) -> None:
        plan = ContactUpdatePlan(contact_uuid="u1", fields={"cpf": "11111111111"})
        with self.assertRaisesRegex(PlanAuditError, "CPF inválido"):
            audit_plans([plan], opt_out_group_available=True)

    def test_state_is_written_atomically_and_loaded(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            payload = {"last_success_utc": "2026-10-03T12:00:00Z"}
            _save_state(payload, path)
            self.assertEqual(_load_state(path), payload)

    def test_confirmed_purchase_populates_flat_weni_contract(self) -> None:
        contact = self._purchase_contact(include_raw=False)
        farm = self._purchase_api()
        plan = self._purchase_plan(contact, farm)
        self.assertTrue(plan.order_validated)
        self.assertTrue(plan.purchase_confirmed)
        self.assertEqual(plan.fields["pedido_id"], "123")
        self.assertEqual(plan.fields["pedido_data"], "03/10/26 09:00:00")
        self.assertEqual(plan.fields["pedido_status"], "2")
        self.assertEqual(plan.fields["pedido_forma_pagamento"], "2")
        self.assertEqual(plan.fields["pedido_produtos"], "Produto A, Produto B")
        self.assertEqual(plan.fields["pedido_quantidade_itens"], "2, 1")
        self.assertEqual(plan.fields["pedido_valor_produtos"], "10, 5.5")
        self.assertEqual(plan.fields["pedido_valor"], 25.5)
        self.assertEqual(plan.fields["ultima_compra"], "2026-10-03T09:00:00-03:00")
        audit_plans([plan], opt_out_group_available=True)

    def test_api_first_ignores_legacy_ultimo_pedido(self) -> None:
        contact = self._purchase_contact(include_raw=True)
        raw = json.loads(contact["fields"]["ultimo_pedido"])
        raw["pedido_id"] = "999"
        raw["produtos"][0]["nome"] = "Produto antigo"
        contact["fields"]["ultimo_pedido"] = json.dumps(raw)
        farm = self._purchase_api()
        plan = self._purchase_plan(contact, farm)
        self.assertEqual(plan.fields["pedido_id"], "123")
        self.assertEqual(plan.fields["pedido_produtos"], "Produto A, Produto B")
        farm.get_cliente_by_cpf.assert_called_once_with("52998224725")
        farm.get_pedido.assert_called_once_with("123")

    def test_descricao_is_product_name_and_codproduto_is_not(self) -> None:
        contact = self._purchase_contact(include_raw=False)
        farm = self._purchase_api()
        farm.get_pedido.return_value["itens"][0]["codProduto"] = "NOME ERRADO"
        farm.get_pedido.return_value["itens"][0]["descricao"] = "Nome oficial"
        plan = self._purchase_plan(contact, farm)
        self.assertEqual(plan.fields["pedido_produtos"], "Nome oficial, Produto B")
        self.assertNotIn("NOME ERRADO", plan.fields["pedido_produtos"])

    def test_missing_description_blocks_order_write(self) -> None:
        contact = self._purchase_contact(include_raw=False)
        farm = self._purchase_api()
        for payload in (
            farm.get_cliente_by_cpf.return_value,
            farm.get_pedido.return_value,
        ):
            for item in payload["itens"]:
                item.pop("descricao", None)
        plan = self._purchase_plan(contact, farm)
        self.assertFalse(plan.order_validated)
        self.assertFalse(plan.purchase_confirmed)
        self.assertFalse(
            set(plan.fields)
            & {
                "pedido_id",
                "pedido_data",
                "pedido_produtos",
                "pedido_quantidade_itens",
                "pedido_valor_produtos",
                "pedido_valor",
                "pedido_status",
                "pedido_forma_pagamento",
            }
        )

    def test_status_is_written_as_official_code(self) -> None:
        for status in (-2, -1, 0, 1, 2, 3, 4):
            with self.subTest(status=status):
                contact = self._purchase_contact(include_raw=False)
                farm = self._purchase_api()
                farm.get_pedido.return_value["capa"]["status"] = status
                farm.get_cliente_by_cpf.return_value["ultimoPedido"]["status"] = status
                plan = self._purchase_plan(contact, farm)
                self.assertEqual(plan.fields["pedido_status"], str(status))
                audit_plans([plan], opt_out_group_available=True)

    def test_payment_is_written_as_official_code(self) -> None:
        for payment in (1, 2, 3, 4, 5, 6):
            with self.subTest(payment=payment):
                contact = self._purchase_contact(include_raw=False)
                farm = self._purchase_api()
                farm.get_pedido.return_value["capa"]["formaPagamento"] = payment
                plan = self._purchase_plan(contact, farm)
                self.assertEqual(plan.fields["pedido_forma_pagamento"], str(payment))

    def test_unknown_status_from_api_does_not_validate_or_write_order(self) -> None:
        contact = self._purchase_contact(include_raw=False)
        farm = self._purchase_api()
        farm.get_pedido.return_value["capa"]["status"] = 99
        farm.get_cliente_by_cpf.return_value["ultimoPedido"]["status"] = 99
        plan = self._purchase_plan(contact, farm)
        self.assertFalse(plan.order_validated)
        self.assertFalse(plan.purchase_confirmed)
        self.assertFalse(
            set(plan.fields)
            & {
                "pedido_id",
                "pedido_data",
                "pedido_status",
                "pedido_produtos",
                "pedido_quantidade_itens",
                "pedido_valor_produtos",
                "pedido_valor",
                "pedido_forma_pagamento",
                "ultima_compra",
            }
        )

    def test_product_commas_and_quotes_round_trip_into_analytics(self) -> None:
        contact = self._purchase_contact(include_raw=False)
        farm = self._purchase_api()
        names = ["Produto 0,5 MG", 'Produto "Especial", 30 CPR']
        for payload in (
            farm.get_cliente_by_cpf.return_value,
            farm.get_pedido.return_value,
        ):
            for item, name in zip(payload["itens"], names, strict=False):
                item["descricao"] = name
        plan = self._purchase_plan(contact, farm)
        audit_plans([plan], opt_out_group_available=True)
        self.assertEqual(parse_weni_order_list(plan.fields["pedido_produtos"]), names)
        applied = deepcopy(contact)
        applied["fields"].update(plan.fields)
        order, items = parse_ultimo_pedido(applied, "2026-10-03T13:00:00Z")
        self.assertEqual([item["produto_nome"] for item in items], names)
        self.assertEqual([item["quantidade"] for item in items], ["2", "1"])
        self.assertEqual([item["valor_unitario"] for item in items], ["10.00", "5.50"])
        self.assertEqual(order["valor_total_compra"], "25.50")
        self.assertEqual(self._purchase_plan(applied, farm).fields, {})

    def test_undocumented_numeric_payment_does_not_block_valid_order(self) -> None:
        for payment in (7, 9):
            with self.subTest(payment=payment):
                contact = self._purchase_contact(include_raw=False)
                farm = self._purchase_api()
                farm.get_pedido.return_value["capa"]["formaPagamento"] = payment
                with patch("weni_update.logging.warning") as warning:
                    plan = self._purchase_plan(contact, farm)
                warning.assert_not_called()
                self.assertTrue(plan.order_validated)
                self.assertEqual(plan.order_payment_code, str(payment))
                self.assertEqual(plan.fields["pedido_forma_pagamento"], str(payment))
                audit_plans([plan], opt_out_group_available=True)
                self.assertEqual(plan.fields["pedido_valor"], 25.5)

    def test_zero_payment_cancels_order_without_warning_or_latest_purchase(
        self,
    ) -> None:
        for status in (-2, -1, 0, 1, 2, 3, 4):
            with self.subTest(status=status):
                contact = self._purchase_contact(include_raw=False)
                contact["fields"]["ultima_compra"] = "2026-10-01T10:00:00-03:00"
                farm = self._purchase_api()
                farm.get_cliente_by_cpf.return_value["ultimoPedido"].update(
                    status=status,
                    formaPagamento=0,
                )
                farm.get_pedido.return_value["capa"].update(
                    status=status,
                    formaPagamento=0,
                )
                with patch("weni_update.logging.warning") as warning:
                    plan = self._purchase_plan(contact, farm)
                warning.assert_not_called()
                self.assertTrue(plan.order_validated)
                self.assertFalse(plan.purchase_confirmed)
                self.assertEqual(plan.order_payment_code, "0")
                self.assertEqual(plan.fields["pedido_forma_pagamento"], "Cancelado")
                self.assertEqual(plan.fields["pedido_status"], "3")
                self.assertNotIn("ultima_compra", plan.fields)
                audit_plans([plan], opt_out_group_available=True)
                applied = deepcopy(contact)
                applied["fields"].update(plan.fields)
                self.assertEqual(self._purchase_plan(applied, farm).fields, {})

    def test_audit_rejects_cancelled_payment_without_cancelled_order_status(
        self,
    ) -> None:
        for status in (None, "-1", "0", "1", "2"):
            with self.subTest(status=status):
                plan = ContactUpdatePlan(
                    contact_uuid="u-order",
                    order_validated=True,
                    fields={"pedido_forma_pagamento": "CANCELADO"},
                    guards={"pedido_status": status},
                )
                with self.assertRaises(PlanAuditError):
                    audit_plans([plan], opt_out_group_available=True)

    def test_cancelled_payment_only_change_keeps_order_status_guard(self) -> None:
        contact = self._purchase_contact(include_raw=False)
        farm = self._purchase_api()
        farm.get_pedido.return_value["capa"].update(status=3, formaPagamento=0)
        farm.get_cliente_by_cpf.return_value["ultimoPedido"].update(
            status=3,
            formaPagamento=0,
        )
        existing = self._purchase_plan(contact, farm)
        contact["fields"].update(existing.fields)
        contact["fields"]["pedido_forma_pagamento"] = "0"
        plan = self._purchase_plan(contact, farm)
        self.assertEqual(plan.fields, {"pedido_forma_pagamento": "Cancelado"})
        self.assertEqual(plan.guards["pedido_status"], "3")
        audit_plans([plan], opt_out_group_available=True)
        plan.guards["pedido_status"] = "2"
        with self.assertRaises(PlanAuditError):
            audit_plans([plan], opt_out_group_available=True)

    def test_partial_apply_cancelled_status_keeps_pending_payment_auditable(
        self,
    ) -> None:
        plan = ContactUpdatePlan(
            contact_uuid="u-order",
            order_validated=True,
            fields={"pedido_status": "3", "pedido_forma_pagamento": "CANCELADO"},
            previous={"pedido_status": "0", "pedido_forma_pagamento": "0"},
            guards={"pedido_status": "0", "pedido_forma_pagamento": "0"},
        )
        original_guards = deepcopy(plan.guards)
        audit_plans([plan], opt_out_group_available=True)
        prepared, conflict = _prepare_frozen_plan_for_apply(
            plan,
            {
                "uuid": "u-order",
                "fields": {
                    "pedido_status": "3",
                    "pedido_forma_pagamento": "0",
                },
            },
            opt_in_group_name="Opt-In",
            opt_in_group_uuid="",
            opt_out_group_name="Opt-Out",
            opt_out_group_uuid="",
        )
        self.assertEqual(conflict, "")
        self.assertIsNotNone(prepared)
        self.assertEqual(prepared.fields, {"pedido_forma_pagamento": "CANCELADO"})
        self.assertEqual(prepared.guards["pedido_status"], "3")
        self.assertEqual(prepared.guards["pedido_forma_pagamento"], "0")
        audit_plans([prepared], opt_out_group_available=True)
        self.assertEqual(plan.guards, original_guards)
        self.assertEqual(plan.fields["pedido_status"], "3")

    def test_partial_apply_completed_status_keeps_pending_latest_purchase_auditable(
        self,
    ) -> None:
        previous_purchase = "2026-10-01T09:00:00-03:00"
        proposed_purchase = "2026-10-03T09:00:00-03:00"
        plan = ContactUpdatePlan(
            contact_uuid="u-order",
            order_validated=True,
            purchase_confirmed=True,
            fields={"pedido_status": "2", "ultima_compra": proposed_purchase},
            previous={"pedido_status": "-1", "ultima_compra": previous_purchase},
            guards={"pedido_status": "-1", "pedido_forma_pagamento": "6"},
        )
        original_guards = deepcopy(plan.guards)
        audit_plans([plan], opt_out_group_available=True)
        prepared, conflict = _prepare_frozen_plan_for_apply(
            plan,
            {
                "uuid": "u-order",
                "fields": {
                    "pedido_status": "2",
                    "pedido_forma_pagamento": "6",
                    "ultima_compra": previous_purchase,
                },
            },
            opt_in_group_name="Opt-In",
            opt_in_group_uuid="",
            opt_out_group_name="Opt-Out",
            opt_out_group_uuid="",
        )
        self.assertEqual(conflict, "")
        self.assertIsNotNone(prepared)
        self.assertEqual(prepared.fields, {"ultima_compra": proposed_purchase})
        self.assertEqual(prepared.guards["pedido_status"], "2")
        self.assertEqual(prepared.guards["pedido_forma_pagamento"], "6")
        audit_plans([prepared], opt_out_group_available=True)
        self.assertEqual(plan.guards, original_guards)
        self.assertEqual(plan.fields["pedido_status"], "2")

    def test_audit_rejects_old_snapshot_with_unpaid_order_in_unchanged_fields(
        self,
    ) -> None:
        for fields in (
            {"pedido_quantidade_itens": "2, 1"},
            {"ultima_compra": "2026-10-03T09:00:00-03:00"},
        ):
            with self.subTest(fields=fields):
                plan = ContactUpdatePlan(
                    contact_uuid="u-order",
                    order_validated=True,
                    purchase_confirmed=True,
                    fields=fields,
                    guards={
                        "pedido_forma_pagamento": "0",
                        "pedido_status": "2",
                        "pedido_produtos": "A, B",
                        "pedido_quantidade_itens": "2, 1",
                        "pedido_valor_produtos": "10, 20",
                    },
                )
                with self.assertRaises(PlanAuditError):
                    audit_plans([plan], opt_out_group_available=True)

    def test_unknown_payment_summary_counts_unchanged_and_cancelled_orders_without_writes(
        self,
    ) -> None:
        started = datetime(2026, 10, 3, 12, 1, tzinfo=UTC)
        contact = self._purchase_contact(include_raw=False)
        farm = self._purchase_api()
        farm.get_cliente_by_cpf.return_value["ultimoPedido"].update(
            status=3,
            formaPagamento=9,
        )
        farm.get_pedido.return_value["capa"].update(status=3, formaPagamento=9)
        pending = self._purchase_plan(contact, farm)
        unchanged = deepcopy(contact)
        unchanged["uuid"] = "u-unchanged"
        unchanged["fields"].update(pending.fields)
        duplicate = deepcopy(contact)
        duplicate["uuid"] = "u-duplicate"
        client = self._incremental_purchase_client(contact)
        client.list_contacts.return_value = [contact, duplicate, unchanged]
        current_by_uuid = {
            item["uuid"]: item for item in client.list_contacts.return_value
        }
        client.get_contact.side_effect = lambda *, uuid: deepcopy(current_by_uuid[uuid])

        with (
            tempfile.TemporaryDirectory() as tmp,
            patch("weni_update.WeniClient", return_value=client),
            patch("weni_update._open_farmalider", return_value=farm),
            patch(
                "weni_update.resolve_window",
                return_value=UpdateWindow(
                    started_at=started,
                    after=None,
                    full=True,
                ),
            ),
            patch("weni_update._append_audit"),
            patch("weni_update._save_state"),
        ):
            review = Path(tmp) / "review.json"
            with patch("weni_update.logging.info") as log_info:
                result = run_update(apply=False, full=True, review_out=review)
            logs = [call.args[0] % call.args[1:] for call in log_info.call_args_list]
            self.assertEqual(result.orders_validated, 3)
            self.assertEqual(result.plans_with_changes, 2)
            self.assertEqual(result.purchase_confirmed, 0)
            self.assertEqual(result.payment_unmapped_codes, {"9": 3})
            self.assertEqual(result.contacts_updated, 0)
            client.update_contact_fields.assert_not_called()
            client.contact_group_action.assert_not_called()
            payment_messages = [
                line for line in logs if "Descrição no Analytics:" in line
            ]
            self.assertEqual(len(payment_messages), 1)
            self.assertIn("Não identificado (código 9)", payment_messages[0])
            self.assertTrue(
                any(
                    "3 contato(s) com pedido validado" in line
                    and "Nenhuma gravação foi iniciada" in line
                    for line in logs
                )
            )
            self.assertTrue(all("52998224725" not in line for line in logs))
            snapshot = json.loads(review.read_text(encoding="utf-8"))
            self.assertEqual(snapshot["summary"]["orders_validated"], 3)
            self.assertEqual(snapshot["summary"]["payment_unmapped_codes"], {"9": 3})
            self.assertTrue(
                all("order_payment_code" not in plan for plan in snapshot["plans"])
            )
            loaded, _, _ = load_review_snapshot(full=True, json_path=review)
            self.assertEqual(len(loaded), 2)
            applied = run_update(apply=True, full=True, require_review=review)
            self.assertEqual(applied.orders_validated, 3)
            self.assertEqual(applied.payment_unmapped_codes, {"9": 3})
            self.assertEqual(applied.contacts_updated, 2)
            self.assertEqual(client.update_contact_fields.call_count, 2)
            for call in client.update_contact_fields.call_args_list:
                self.assertEqual(call.args[1]["pedido_forma_pagamento"], "9")
                self.assertEqual(call.args[1]["pedido_status"], "3")
                self.assertNotIn("ultima_compra", call.args[1])

    def test_audit_rejects_misalignment_when_only_one_list_changes(self) -> None:
        plan = ContactUpdatePlan(
            contact_uuid="u-order",
            order_validated=True,
            fields={"pedido_quantidade_itens": "1"},
            guards={"pedido_produtos": "A, B", "pedido_valor_produtos": "10, 20"},
        )
        with self.assertRaisesRegex(
            PlanAuditError, "produtos=2; quantidades=1; valores=2"
        ):
            audit_plans([plan], opt_out_group_available=True)

    def test_audit_reads_wrapped_unchanged_fields_when_one_list_changes(self) -> None:
        plan = ContactUpdatePlan(
            contact_uuid="u-order",
            order_validated=True,
            fields={"pedido_quantidade_itens": "2, 1"},
            guards={
                "pedido_produtos": {"value": '"Produto 0,5 MG", B'},
                "pedido_valor_produtos": {"value": "10, 20"},
            },
        )
        audit_plans([plan], opt_out_group_available=True)

    def test_audit_rejects_malformed_csv_and_empty_item_slots(self) -> None:
        for products, quantities, values in (
            ('"Produto 0,5 MG', "1", "10"),
            ("A, , B", "1, 1, 1", "10, 20, 30"),
            ("A, B", "1, ", "10, 20"),
            ("A, B", "1, 1", "10, "),
        ):
            with self.subTest(products=products, quantities=quantities, values=values):
                plan = ContactUpdatePlan(
                    contact_uuid="u-order",
                    order_validated=True,
                    fields={
                        "pedido_produtos": products,
                        "pedido_quantidade_itens": quantities,
                        "pedido_valor_produtos": values,
                    },
                )
                with self.assertRaises(PlanAuditError):
                    audit_plans([plan], opt_out_group_available=True)

    def test_cancelled_order_updates_snapshot_without_latest_purchase(self) -> None:
        contact = self._purchase_contact(include_raw=False)
        contact["fields"]["ultima_compra"] = "2026-10-01T10:00:00-03:00"
        farm = self._purchase_api()
        farm.get_pedido.return_value["capa"]["status"] = 3
        plan = self._purchase_plan(contact, farm)
        self.assertTrue(plan.order_validated)
        self.assertFalse(plan.purchase_confirmed)
        self.assertEqual(plan.fields["pedido_status"], "3")
        self.assertNotIn("ultima_compra", plan.fields)
        audit_plans([plan], opt_out_group_available=True)

    def test_configured_sales_states_advance_latest_purchase(self) -> None:
        for status in (-2, -1, 0, 1, 2, 3, 4):
            with self.subTest(status=status):
                contact = self._purchase_contact(include_raw=False)
                contact["fields"]["ultima_compra"] = "2026-10-01T10:00:00-03:00"
                farm = self._purchase_api()
                farm.get_pedido.return_value["capa"]["status"] = status
                plan = self._purchase_plan(contact, farm)
                self.assertEqual(
                    plan.purchase_confirmed, status in (-2, -1, 0, 4, 1, 2)
                )
                if status in (-2, -1, 0, 4, 1, 2):
                    self.assertEqual(
                        plan.fields["ultima_compra"],
                        "2026-10-03T09:00:00-03:00",
                    )
                else:
                    self.assertNotIn("ultima_compra", plan.fields)

    def test_open_states_are_saved_queued_and_use_configured_sales_policy(self) -> None:
        for status in (-2, -1, 0, 4, 1):
            with self.subTest(status=status):
                started = datetime(2026, 10, 3, 12, 1, tzinfo=UTC)
                contact = self._purchase_contact(include_raw=False)
                contact["fields"]["ultima_compra"] = "2026-10-01T10:00:00-03:00"
                farm = self._purchase_api()
                farm.get_pedido.return_value["capa"]["status"] = status
                farm.get_cliente_by_cpf.return_value["ultimoPedido"]["status"] = status
                client = self._incremental_purchase_client(contact)
                client.get_contact.return_value = deepcopy(contact)
                with (
                    patch("weni_update.WeniClient", return_value=client),
                    patch("weni_update._open_farmalider", return_value=farm),
                    patch(
                        "weni_update.resolve_window",
                        return_value=UpdateWindow(
                            started_at=started,
                            after=started,
                            full=False,
                        ),
                    ),
                    patch("weni_update._append_audit"),
                    patch("weni_update._save_state") as save_mock,
                ):
                    result = run_update(apply=True, full=False)
                client.list_contacts.assert_called_once_with(after=None)
                farm.get_cliente_by_cpf.assert_called()
                self.assertEqual(result.orders_validated, 1)
                self.assertEqual(
                    result.purchase_confirmed, int(status in (-2, -1, 0, 4, 1, 2))
                )
                self.assertEqual(result.contacts_updated, 1)
                saved_fields = client.update_contact_fields.call_args.args[1]
                self.assertEqual(saved_fields["pedido_status"], str(status))
                if status in (-2, -1, 0, 4, 1, 2):
                    self.assertIn("ultima_compra", saved_fields)
                else:
                    self.assertNotIn("ultima_compra", saved_fields)
                self.assertEqual(
                    save_mock.call_args.args[0]["open_order_uuids"],
                    ["u-pedido"],
                )

    def test_freight_is_not_added_to_pedido_valor(self) -> None:
        contact = self._purchase_contact(include_raw=False)
        farm = self._purchase_api()
        farm.get_pedido.return_value["capa"]["frete"] = 50
        plan = self._purchase_plan(contact, farm)
        self.assertEqual(plan.fields["pedido_valor"], 25.5)

    def test_pedido_data_uses_business_display_format(self) -> None:
        contact = self._purchase_contact(include_raw=False)
        farm = self._purchase_api()
        farm.get_pedido.return_value["capa"]["emissao"] = "2026-10-04T01:30:00Z"
        plan = self._purchase_plan(contact, farm)
        self.assertEqual(plan.fields["pedido_data"], "03/10/26 22:30:00")
        self.assertEqual(plan.fields["ultima_compra"], "2026-10-03T22:30:00-03:00")

    def test_same_order_corrects_future_weni_date_and_current_status(self) -> None:
        for current_id in ("123", "000123", {"value": "123"}):
            for status in (2, 3):
                with self.subTest(current_id=current_id, status=status):
                    contact = self._purchase_contact(include_raw=False)
                    contact["fields"].update(
                        pedido_id=current_id,
                        pedido_data="04-10-2026 12:21",
                        pedido_status="1",
                    )
                    farm = self._purchase_api()
                    farm.get_pedido.return_value["capa"]["status"] = status
                    plan = self._purchase_plan(contact, farm)
                    self.assertTrue(plan.order_validated)
                    self.assertEqual(plan.fields["pedido_data"], "03/10/26 09:00:00")
                    self.assertEqual(plan.fields["pedido_status"], str(status))
                    self.assertEqual(plan.purchase_confirmed, status == 2)
                    audit_plans([plan], opt_out_group_available=True)

    def test_different_or_unknown_order_preserves_newer_weni_date(self) -> None:
        for current_id in ("456", "", "123.0", True):
            with self.subTest(current_id=current_id):
                contact = self._purchase_contact(include_raw=False)
                contact["fields"].update(
                    pedido_id=current_id,
                    pedido_data="04-10-2026 12:21",
                    pedido_status="1",
                )
                plan = self._purchase_plan(contact, self._purchase_api())
                self.assertFalse(plan.order_validated)
                self.assertFalse(plan.purchase_confirmed)
                self.assertFalse(
                    any(name.startswith("pedido_") for name in plan.fields)
                )

    def test_latest_api_order_replaces_older_weni_order(self) -> None:
        contact = self._purchase_contact(include_raw=False)
        contact["fields"].update(pedido_id="456", pedido_data="18-09-2026 12:21")
        farm = self._purchase_api()
        farm.get_pedido.return_value["capa"]["emissao"] = "2026-10-09T12:21:00"
        plan = self._purchase_plan(contact, farm)
        self.assertTrue(plan.order_validated)
        self.assertEqual(plan.fields["pedido_id"], "123")
        self.assertEqual(plan.fields["pedido_data"], "09/10/26 12:21:00")
        farm.get_pedido.assert_called_once_with("123")

    def test_missing_optional_weni_order_field_is_skipped(self) -> None:
        contact = self._purchase_contact(include_raw=False)
        farm = self._purchase_api()
        available = {
            "pedido_id",
            "pedido_data",
            "pedido_status",
            "pedido_produtos",
            "pedido_quantidade_itens",
            "pedido_valor",
            "ultima_compra",
        }
        plan = self._purchase_plan(contact, farm, available_weni_fields=available)
        self.assertNotIn("pedido_valor_produtos", plan.fields)
        self.assertNotIn("pedido_forma_pagamento", plan.fields)
        self.assertEqual(plan.fields["pedido_status"], "2")

    def test_audit_rejects_misaligned_item_lists(self) -> None:
        plan = ContactUpdatePlan(
            contact_uuid="u-order",
            order_validated=True,
            fields={
                "pedido_produtos": "A, B",
                "pedido_quantidade_itens": "1",
                "pedido_valor_produtos": "10, 20",
            },
        )
        with self.assertRaisesRegex(PlanAuditError, "desalinhadas"):
            audit_plans([plan], opt_out_group_available=True)

    def test_audit_rejects_invalid_status_and_payment_codes(self) -> None:
        for fields in (
            {"pedido_status": "5"},
            {"pedido_status": "-3"},
            {"pedido_status": "8"},
            {"pedido_status": "9"},
            {"pedido_forma_pagamento": "1.5"},
            {"pedido_forma_pagamento": "NaN"},
        ):
            with self.subTest(fields=fields):
                plan = ContactUpdatePlan(
                    contact_uuid="u-order",
                    order_validated=True,
                    fields=fields,
                )
                with self.assertRaises(PlanAuditError):
                    audit_plans([plan], opt_out_group_available=True)

    def test_order_plan_is_idempotent_after_apply(self) -> None:
        contact = self._purchase_contact(include_raw=False)
        farm = self._purchase_api()
        first = self._purchase_plan(contact, farm)
        applied = deepcopy(contact)
        applied["fields"].update(first.fields)
        second = self._purchase_plan(applied, farm, captured_at="2026-10-03T13:01:00Z")
        self.assertTrue(second.order_validated)
        self.assertEqual(second.fields, {})
        self.assertEqual(second.evidence, {})

    def test_api_identity_mismatch_produces_no_order_writes(self) -> None:
        contact = self._purchase_contact(include_raw=False)
        farm = self._purchase_api()
        farm.get_pedido.return_value["capa"]["codCliente"] = "100"
        plan = self._purchase_plan(contact, farm)
        self.assertFalse(plan.order_validated)
        self.assertFalse(
            set(plan.fields)
            & {
                "pedido_id",
                "pedido_data",
                "pedido_produtos",
                "pedido_quantidade_itens",
                "pedido_valor_produtos",
                "pedido_valor",
                "pedido_status",
                "pedido_forma_pagamento",
            }
        )

    def test_legacy_order_without_api_does_not_write_transactional_fields(self) -> None:
        plan = self._purchase_plan(self._purchase_contact(), farm=None)
        self.assertFalse(plan.order_validated)
        self.assertFalse(plan.purchase_confirmed)
        self.assertFalse(
            set(plan.fields)
            & {
                "pedido_id",
                "pedido_data",
                "pedido_produtos",
                "pedido_quantidade_itens",
                "pedido_valor_produtos",
                "pedido_valor",
                "pedido_status",
                "pedido_forma_pagamento",
                "ultima_compra",
            }
        )

    def test_incremental_order_apply_rechecks_identity_and_queues_conflict(
        self,
    ) -> None:
        started = datetime(2026, 10, 3, 12, 1, tzinfo=UTC)
        contact = self._purchase_contact(include_raw=False)
        client = self._incremental_purchase_client(contact)
        current = deepcopy(contact)
        current["fields"]["cpf"] = "11144477735"
        client.get_contact.return_value = current
        with (
            patch("weni_update.WeniClient", return_value=client),
            patch("weni_update._open_farmalider", return_value=self._purchase_api()),
            patch(
                "weni_update.resolve_window",
                return_value=UpdateWindow(
                    started_at=started,
                    after=started,
                    full=False,
                ),
            ),
            patch("weni_update._append_audit") as audit_mock,
            patch("weni_update._save_state") as save_mock,
        ):
            result = run_update(apply=True, full=False)
        self.assertEqual(result.contacts_conflicted, 1)
        self.assertEqual(result.contacts_updated, 0)
        client.update_contact_fields.assert_not_called()
        self.assertEqual(
            save_mock.call_args.args[0]["pending_conflict_uuids"],
            ["u-pedido"],
        )
        self.assertEqual(audit_mock.call_args.args[0]["status"], "conflict_skipped")

    def test_incremental_apply_skips_already_written_order_fields(self) -> None:
        started = datetime(2026, 10, 3, 12, 1, tzinfo=UTC)
        contact = self._purchase_contact(include_raw=False)
        farm = self._purchase_api()
        planned = self._purchase_plan(contact, farm)
        current = deepcopy(contact)
        current["fields"].update(planned.fields)
        client = self._incremental_purchase_client(contact)
        client.get_contact.return_value = current
        with (
            patch("weni_update.WeniClient", return_value=client),
            patch("weni_update._open_farmalider", return_value=farm),
            patch(
                "weni_update.resolve_window",
                return_value=UpdateWindow(
                    started_at=started,
                    after=started,
                    full=False,
                ),
            ),
            patch("weni_update._append_audit") as audit_mock,
            patch("weni_update._save_state") as save_mock,
        ):
            result = run_update(apply=True, full=False)
        self.assertEqual(result.contacts_already_satisfied, 1)
        self.assertEqual(result.contacts_conflicted, 0)
        client.update_contact_fields.assert_not_called()
        self.assertEqual(save_mock.call_args.args[0]["pending_conflict_uuids"], [])
        self.assertEqual(audit_mock.call_args.args[0]["status"], "already_satisfied")

    def test_review_snapshot_contains_human_readable_changes(self) -> None:
        plan = ContactUpdatePlan(
            contact_uuid="u1",
            contact_name="Maria",
            contact_urn="whatsapp:5591999999999",
            fields={"opt_in_lgpd": "recusado", "cpf": "52998224725", "idade": 27},
            previous={"opt_in_lgpd": "ativo", "cpf": "", "idade": None},
            evidence={
                "opt_in_lgpd": {
                    "message_id": "m1",
                    "created_on": "2026-10-03T12:00:00Z",
                    "reason": "recusa explícita",
                }
            },
            group_actions=[
                GroupAction(
                    "add",
                    "g1",
                    "opt_in_lgpd recusado",
                    group_name="Opt-Out LGPD",
                )
            ],
        )
        stats = UpdateStats(
            messages_incoming=5,
            contacts_considered=1,
            plans_with_changes=1,
            field_changes=3,
            group_actions=1,
        )
        with tempfile.TemporaryDirectory() as tmp:
            json_path = Path(tmp) / "review.json"
            _, txt_path, digest = write_review_snapshot(
                [plan],
                stats,
                full=True,
                json_path=json_path,
                captured_at_utc="2026-10-03T12:00:00Z",
            )
            text = txt_path.read_text(encoding="utf-8-sig")
            self.assertIn("Maria", text)
            self.assertIn("ativo  ->  recusado", text)
            self.assertIn("cpf: <vazio>  ->  52998224725", text)
            self.assertIn("Contatos com CPF detectado/alterado: 1", text)
            self.assertIn("ADICIONAR em Opt-Out LGPD", text)
            self.assertIn("Inclusões em Opt-Out LGPD: 1", text)
            self.assertIn("Inclusões em Opt-In LGPD: 0", text)
            self.assertIn(digest, text)
            self.assertIn("plano fica CONGELADO", text)
            loaded, payload, loaded_digest = load_review_snapshot(
                full=True,
                json_path=json_path,
            )
            self.assertEqual(len(loaded), 1)
            self.assertEqual(payload["captured_at_utc"], "2026-10-03T12:00:00Z")
            self.assertEqual(loaded_digest, digest)
            self.assertEqual(
                verify_review_snapshot([plan], full=True, json_path=json_path), digest
            )

    def test_review_snapshot_blocks_changed_plan(self) -> None:
        plan = ContactUpdatePlan(
            contact_uuid="u1", fields={"idade": 27}, previous={"idade": None}
        )
        stats = UpdateStats(
            contacts_considered=1, plans_with_changes=1, field_changes=1
        )
        with tempfile.TemporaryDirectory() as tmp:
            json_path = Path(tmp) / "review.json"
            write_review_snapshot(
                [plan],
                stats,
                full=True,
                json_path=json_path,
                captured_at_utc="2026-10-03T12:00:00Z",
            )
            changed = ContactUpdatePlan(
                contact_uuid="u1",
                fields={"idade": 28},
                previous={"idade": None},
            )
            with self.assertRaisesRegex(PlanAuditError, "plano mudou"):
                verify_review_snapshot([changed], full=True, json_path=json_path)

    @patch("weni_update._save_state")
    @patch("weni_update._append_audit")
    @patch("weni_update.WeniClient")
    def test_full_apply_uses_frozen_snapshot_without_recalculating_history(
        self,
        client_cls,
        append_audit_mock,
        save_state_mock,
    ) -> None:
        plan = ContactUpdatePlan(
            contact_uuid="u1",
            contact_name="Maria",
            fields={"idade": 27},
            previous={"idade": None},
        )
        stats = UpdateStats(
            contacts_considered=1, plans_with_changes=1, field_changes=1
        )
        with tempfile.TemporaryDirectory() as tmp:
            review = Path(tmp) / "review.json"
            write_review_snapshot(
                [plan],
                stats,
                full=True,
                json_path=review,
                captured_at_utc="2026-10-03T12:00:00Z",
            )

            client = client_cls.return_value
            client.list_fields.return_value = self._required_weni_fields()

            def list_groups(name=None):
                groups = [
                    {"uuid": "g-in", "name": "Opt-In LGPD"},
                    {"uuid": "g-out", "name": "Opt-Out LGPD"},
                ]
                if name is None:
                    return groups
                return [item for item in groups if item["name"] == name]

            client.list_groups.side_effect = list_groups
            client.get_contact.return_value = {
                "uuid": "u1",
                "groups": [],
                "fields": {},
            }

            result = run_update(
                apply=True,
                full=True,
                require_review=review,
            )

            self.assertEqual(result.contacts_updated, 1)
            self.assertEqual(result.contacts_conflicted, 0)
            client.list_messages.assert_not_called()
            client.list_contacts.assert_not_called()
            client.update_contact_fields.assert_called_once_with("u1", {"idade": 27})
            save_state_mock.assert_called_once()
            saved = save_state_mock.call_args.args[0]
            self.assertEqual(saved["last_success_utc"], "2026-10-03T12:00:00Z")
            append_audit_mock.assert_called_once()

    @patch("weni_update._save_state")
    @patch("weni_update._append_audit")
    @patch("weni_update.WeniClient")
    def test_full_apply_skips_only_changed_contact_and_continues(
        self,
        client_cls,
        append_audit_mock,
        save_state_mock,
    ) -> None:
        plans = [
            ContactUpdatePlan(
                contact_uuid="u1",
                contact_name="Mudou",
                fields={"idade": 27},
                previous={"idade": None},
            ),
            ContactUpdatePlan(
                contact_uuid="u2",
                contact_name="Estável",
                fields={"idade": 31},
                previous={"idade": None},
            ),
        ]
        stats = UpdateStats(
            contacts_considered=2, plans_with_changes=2, field_changes=2
        )
        with tempfile.TemporaryDirectory() as tmp:
            review = Path(tmp) / "review.json"
            write_review_snapshot(
                plans,
                stats,
                full=True,
                json_path=review,
                captured_at_utc="2026-10-03T12:00:00Z",
            )

            client = client_cls.return_value
            client.list_fields.return_value = self._required_weni_fields()
            client.list_groups.side_effect = lambda name=None: (
                [
                    {"uuid": "g-in", "name": "Opt-In LGPD"},
                    {"uuid": "g-out", "name": "Opt-Out LGPD"},
                ]
                if name is None
                else [
                    {"uuid": "g-in" if name == "Opt-In LGPD" else "g-out", "name": name}
                ]
            )

            def get_contact(*, uuid=None, urn=None):
                if uuid == "u1":
                    return {"uuid": "u1", "groups": [], "fields": {"idade": 29}}
                if uuid == "u2":
                    return {"uuid": "u2", "groups": [], "fields": {}}
                return None

            client.get_contact.side_effect = get_contact

            result = run_update(
                apply=True,
                full=True,
                require_review=review,
            )

            self.assertEqual(result.contacts_conflicted, 1)
            self.assertEqual(result.contacts_updated, 1)
            client.update_contact_fields.assert_called_once_with("u2", {"idade": 31})
            save_state_mock.assert_called_once()
            self.assertEqual(append_audit_mock.call_count, 2)

    @patch("weni_update._save_state")
    @patch("weni_update._append_audit")
    @patch("weni_update._open_farmalider", return_value=None)
    @patch("weni_update.resolve_window")
    @patch("weni_update.WeniClient")
    def test_apply_syncs_active_lgpd_to_optin_and_removes_optout(
        self,
        client_cls,
        resolve_window_mock,
        _open_farmalider_mock,
        append_audit_mock,
        save_state_mock,
    ) -> None:
        from datetime import datetime

        started = datetime(2026, 10, 3, 12, 0, tzinfo=UTC)
        resolve_window_mock.return_value = UpdateWindow(
            started_at=started,
            after=started,
            full=False,
        )
        client = client_cls.return_value
        client.list_fields.return_value = self._required_weni_fields()

        def list_groups(name=None):
            groups = [
                {"uuid": "g-in", "name": "Opt-In LGPD"},
                {"uuid": "g-out", "name": "Opt-Out LGPD"},
            ]
            if name is None:
                return groups
            return [item for item in groups if item["name"] == name]

        client.list_groups.side_effect = list_groups
        client.list_messages.return_value = []
        client.list_contacts.return_value = [
            {
                "uuid": "u1",
                "groups": [{"uuid": "g-out", "name": "Opt-Out LGPD"}],
                "fields": {"opt_in_lgpd": "ativo"},
            }
        ]

        stats = run_update(apply=True, full=False)

        self.assertEqual(stats.group_actions, 2)
        self.assertEqual(stats.contacts_updated, 1)
        client.contact_group_action.assert_any_call("u1", action="add", group="g-in")
        client.contact_group_action.assert_any_call(
            "u1", action="remove", group="g-out"
        )
        self.assertEqual(client.contact_group_action.call_count, 2)
        client.update_contact_fields.assert_not_called()
        save_state_mock.assert_called_once()
        append_audit_mock.assert_called_once()

    @patch("weni_update._save_state")
    @patch("weni_update._append_audit")
    @patch("weni_update._open_farmalider", return_value=None)
    @patch("weni_update.resolve_window")
    @patch("weni_update.WeniClient")
    def test_dry_run_never_writes_or_advances_checkpoint(
        self,
        client_cls,
        resolve_window_mock,
        _open_farmalider_mock,
        append_audit_mock,
        save_state_mock,
    ) -> None:
        from datetime import datetime

        started = datetime(2026, 10, 3, 12, 0, tzinfo=UTC)
        resolve_window_mock.return_value = UpdateWindow(
            started_at=started,
            after=started,
            full=False,
        )
        client = client_cls.return_value
        client.list_fields.return_value = self._required_weni_fields()
        client.list_groups.return_value = [{"uuid": "g1", "name": "Opt-Out LGPD"}]
        client.list_messages.return_value = []
        client.list_contacts.return_value = [
            {
                "uuid": "u1",
                "urns": ["whatsapp:5591999999999"],
                "groups": [],
                "fields": {},
            }
        ]

        stats = run_update(apply=False, full=False)

        self.assertEqual(stats.plans_with_changes, 1)
        client.update_contact_fields.assert_not_called()
        client.contact_group_action.assert_not_called()
        save_state_mock.assert_not_called()
        append_audit_mock.assert_called_once()

    @patch("weni_update._save_state")
    @patch("weni_update._append_audit")
    @patch("weni_update._open_farmalider", return_value=None)
    @patch("weni_update.resolve_window")
    @patch("weni_update.WeniClient")
    def test_full_apply_without_review_is_blocked_before_write(
        self,
        client_cls,
        resolve_window_mock,
        _open_farmalider_mock,
        append_audit_mock,
        save_state_mock,
    ) -> None:
        from datetime import datetime

        started = datetime(2026, 10, 3, 12, 0, tzinfo=UTC)
        resolve_window_mock.return_value = UpdateWindow(
            started_at=started,
            after=None,
            full=True,
        )
        client = client_cls.return_value
        client.list_fields.return_value = self._required_weni_fields()
        client.list_groups.return_value = [{"uuid": "g1", "name": "Opt-Out LGPD"}]
        client.list_messages.return_value = []
        client.list_contacts.return_value = [
            {
                "uuid": "u1",
                "urns": ["whatsapp:5591999999999"],
                "groups": [],
                "fields": {},
            }
        ]

        with self.assertRaisesRegex(PlanAuditError, "snapshot de revisão humana"):
            run_update(apply=True, full=True)

        client.update_contact_fields.assert_not_called()
        client.contact_group_action.assert_not_called()
        save_state_mock.assert_not_called()
        append_audit_mock.assert_not_called()

    @patch("weni_update._save_state")
    @patch("weni_update._append_audit")
    @patch("weni_update._open_farmalider", return_value=None)
    @patch("weni_update.resolve_window")
    @patch("weni_update.WeniClient")
    def test_apply_failure_keeps_contact_pending_and_continues(
        self,
        client_cls,
        resolve_window_mock,
        _open_farmalider_mock,
        append_audit_mock,
        save_state_mock,
    ) -> None:
        from datetime import datetime

        started = datetime(2026, 10, 3, 12, 0, tzinfo=UTC)
        resolve_window_mock.return_value = UpdateWindow(
            started_at=started,
            after=started,
            full=False,
        )
        client = client_cls.return_value
        client.list_fields.return_value = self._required_weni_fields()
        client.list_groups.return_value = [{"uuid": "g1", "name": "Opt-Out LGPD"}]
        client.list_messages.return_value = []
        client.list_contacts.return_value = [
            {
                "uuid": "u1",
                "urns": ["whatsapp:5591999999999"],
                "groups": [],
                "fields": {},
            }
        ]
        client.list_contacts.return_value.append(
            {
                "uuid": "u2",
                "urns": ["whatsapp:5591988888888"],
                "groups": [],
                "fields": {},
            }
        )
        client.update_contact_fields.side_effect = [RuntimeError("falha simulada"), {}]

        result = run_update(apply=True, full=False)
        self.assertEqual(result.contacts_failed, 1)
        self.assertEqual(result.contacts_updated, 1)
        self.assertEqual(client.update_contact_fields.call_count, 2)
        saved = save_state_mock.call_args.args[0]
        self.assertEqual(saved["pending_conflict_uuids"], ["u1"])
        self.assertEqual(saved["last_success_utc"], "2026-10-03T12:00:00Z")
        self.assertEqual(append_audit_mock.call_count, 2)

    @patch("weni_update._save_state")
    @patch("weni_update._append_audit")
    @patch("weni_update._open_farmalider", return_value=None)
    @patch("weni_update.WeniClient")
    def test_incremental_retry_recovers_messages_older_than_checkpoint(
        self, client_cls, _farm, _audit, save_state
    ):
        from datetime import datetime

        started = datetime(2026, 10, 3, 12, tzinfo=UTC)
        client = client_cls.return_value
        client.list_fields.return_value = self._required_weni_fields()
        client.list_groups.return_value = [{"uuid": "g1", "name": "Opt-Out LGPD"}]
        client.list_contacts.return_value = [
            {
                "uuid": "u1",
                "urns": ["whatsapp:5591999999999"],
                "groups": [],
                "fields": {},
            }
        ]
        old = {
            "id": "old-in",
            "direction": "in",
            "text": "Tenho 37 anos",
            "created_on": "2026-10-01T12:00:00Z",
        }
        client.list_messages.side_effect = lambda **kw: (
            [old] if kw.get("contact") == "u1" else []
        )
        with (
            patch(
                "weni_update._load_state",
                return_value={"pending_conflict_uuids": ["u1"]},
            ),
            patch(
                "weni_update.resolve_window",
                return_value=UpdateWindow(
                    started_at=started, after=started, full=False
                ),
            ),
        ):
            result = run_update(apply=True)
        self.assertEqual(result.contacts_updated, 1)
        self.assertEqual(client.update_contact_fields.call_args.args[1]["idade"], 37)
        client.list_messages.assert_any_call(contact="u1", before=started)
        self.assertEqual(save_state.call_args.args[0]["pending_conflict_uuids"], [])

    @patch("weni_update._save_state")
    @patch("weni_update._append_audit")
    @patch("weni_update.WeniClient")
    def test_frozen_apply_failure_preserves_pending_and_applies_other_contact(
        self, client_cls, _audit, save_state
    ):
        client = client_cls.return_value
        client.list_fields.return_value = self._required_weni_fields()
        client.list_groups.return_value = [{"uuid": "g1", "name": "Opt-Out LGPD"}]
        client.get_contact.side_effect = lambda **kw: {
            "uuid": kw["uuid"],
            "fields": {},
            "urns": [],
            "groups": [],
        }
        plans = [
            ContactUpdatePlan(contact_uuid=uid, fields={"idade": 37})
            for uid in ("u1", "u2")
        ]
        client.update_contact_fields.side_effect = [RuntimeError("falha simulada"), {}]
        payload = {"summary": {}, "captured_at_utc": "2026-10-03T12:00:00Z"}
        with (
            patch(
                "weni_update.load_review_snapshot",
                return_value=(plans, payload, "hash"),
            ),
            patch("weni_update.audit_plans"),
            patch(
                "weni_update._prepare_frozen_plan_for_apply",
                side_effect=lambda plan, *args, **kw: (plan, ""),
            ),
        ):
            result = run_update(
                apply=True, full=True, require_review=Path("sintetico.json")
            )
        self.assertEqual(result.contacts_failed, 1)
        self.assertEqual(result.contacts_updated, 1)
        self.assertEqual(client.update_contact_fields.call_count, 2)
        self.assertEqual(save_state.call_args.args[0]["pending_conflict_uuids"], ["u1"])

    @patch("weni_update._save_state")
    @patch("weni_update._append_audit")
    @patch("weni_update._open_farmalider", return_value=None)
    @patch("weni_update.WeniClient")
    def test_failed_pending_contact_lookup_does_not_stop_healthy_contacts(
        self, client_cls, _farm, _audit, save_state
    ):
        from datetime import datetime

        started = datetime(2026, 10, 3, 12, tzinfo=UTC)
        client = client_cls.return_value
        client.list_fields.return_value = self._required_weni_fields()
        client.list_groups.return_value = [{"uuid": "g1", "name": "Opt-Out LGPD"}]
        client.list_contacts.return_value = [
            {
                "uuid": "u2",
                "urns": ["whatsapp:5591999999999"],
                "groups": [],
                "fields": {},
            }
        ]
        client.list_messages.return_value = []
        client.get_contact.side_effect = RuntimeError("falha simulada")
        with (
            patch(
                "weni_update._load_state",
                return_value={"pending_conflict_uuids": ["u1"]},
            ),
            patch(
                "weni_update.resolve_window",
                return_value=UpdateWindow(
                    started_at=started, after=started, full=False
                ),
            ),
        ):
            result = run_update(apply=True)
        self.assertEqual(result.contacts_failed, 1)
        self.assertEqual(result.contacts_updated, 1)
        self.assertEqual(save_state.call_args.args[0]["pending_conflict_uuids"], ["u1"])
        client.get_contact.assert_called_once_with(uuid="u1")

    @patch("weni_update._save_state")
    @patch("weni_update._append_audit")
    @patch("weni_update.WeniClient")
    def test_frozen_snapshot_skips_verified_deduplicated_contact_without_conflict(
        self, client_cls, _audit, save_state
    ):
        client = client_cls.return_value
        client.list_fields.return_value = self._required_weni_fields()
        client.list_groups.return_value = [{"uuid": "g1", "name": "Opt-Out LGPD"}]
        client.get_contact.return_value = {
            "uuid": "u2",
            "fields": {},
            "urns": [],
            "groups": [],
        }
        plans = [
            ContactUpdatePlan(contact_uuid=uid, fields={"idade": 37})
            for uid in ("u1", "u2")
        ]
        payload = {"summary": {}, "captured_at_utc": "2026-10-03T12:00:00Z"}
        with (
            patch(
                "weni_update.load_review_snapshot",
                return_value=(plans, payload, "hash"),
            ),
            patch("weni_update.audit_plans"),
            patch(
                "weni_update._prepare_frozen_plan_for_apply",
                side_effect=lambda plan, *args, **kw: (plan, ""),
            ),
        ):
            result = run_update(
                apply=True,
                full=True,
                require_review=Path("sintetico.json"),
                deduplicated_uuids={"u1"},
            )
        self.assertEqual(result.contacts_failed, 0)
        self.assertEqual(result.contacts_conflicted, 0)
        self.assertEqual(result.contacts_updated, 1)
        client.get_contact.assert_called_once_with(uuid="u2")
        client.update_contact_fields.assert_called_once_with("u2", {"idade": 37})

    @patch("weni_update._open_farmalider", return_value=None)
    @patch("weni_update.resolve_window")
    @patch("weni_update.WeniClient")
    def test_missing_order_fields_blocks_before_contact_reads(
        self, client_cls, resolve_window_mock, _open_farmalider_mock
    ) -> None:
        from datetime import datetime

        started = datetime(2026, 10, 3, 12, 0, tzinfo=UTC)
        resolve_window_mock.return_value = UpdateWindow(
            started_at=started,
            after=started,
            full=False,
        )
        client = client_cls.return_value
        client.list_fields.return_value = [
            item
            for item in self._required_weni_fields()
            if item["key"] != "pedido_valor_produtos"
        ]

        with self.assertRaisesRegex(PlanAuditError, "pedido_valor_produtos"):
            run_update(apply=False, full=False)

        client.list_messages.assert_not_called()
        client.list_contacts.assert_not_called()
        client.update_contact_fields.assert_not_called()
        client.contact_group_action.assert_not_called()

    @patch("weni_update._open_farmalider", return_value=None)
    @patch("weni_update.resolve_window")
    @patch("weni_update.WeniClient")
    def test_missing_multichannel_fields_blocks_before_contact_reads(
        self, client_cls, resolve_window_mock, _open_farmalider_mock
    ) -> None:
        from datetime import datetime

        started = datetime(2026, 10, 3, 12, 0, tzinfo=UTC)
        resolve_window_mock.return_value = UpdateWindow(
            started_at=started,
            after=started,
            full=False,
        )
        client = client_cls.return_value
        client.list_fields.return_value = [
            {"key": "contact_urn_whatsapp"},
            {"key": "contact_canal_principal"},
        ]

        with self.assertRaisesRegex(PlanAuditError, "contact_urn_facebook"):
            run_update(apply=False, full=False)

        client.list_messages.assert_not_called()
        client.list_contacts.assert_not_called()
        client.update_contact_fields.assert_not_called()
        client.contact_group_action.assert_not_called()


if __name__ == "__main__":
    unittest.main()


class GoogleAdsWeniUpdateTests(unittest.TestCase):
    def _contact(self, fields=None):
        return {
            "uuid": "u-google",
            "urns": ["whatsapp:5591999999999"],
            "groups": [],
            "fields": fields or {},
        }

    def _available_fields(self):
        from weni_update import ALLOWED_WRITE_FIELDS

        return set(ALLOWED_WRITE_FIELDS)

    def test_google_ads_pmax_populates_first_and_last_without_changing_urn(self):
        message = {
            "id": "g1",
            "created_on": "2026-10-03T12:00:00Z",
            "direction": "in",
            "urn": "whatsapp:5591999999999",
            "metadata": {
                "source_url": (
                    "https://example.com/?utm_source=google&utm_medium=cpc"
                    "&utm_campaign_type=performance_max&gclid=GCLID123"
                )
            },
        }
        plan = _build_contact_plan(
            self._contact(),
            context_messages=[message],
            candidate_message_ids={"g1"},
            farm_client=None,
            opt_out_group_name="Opt-Out LGPD",
            opt_out_group_uuid="g-out",
            captured_at="2026-10-03T12:01:00Z",
            opt_in_group_name="Opt-In LGPD",
            opt_in_group_uuid="g-in",
            available_weni_fields=self._available_fields(),
        )
        self.assertEqual(plan.fields["contact_urn"], "whatsapp:5591999999999")
        self.assertEqual(plan.fields["primeiro_anuncio_origem"], "Google Ads")
        self.assertEqual(plan.fields["ultimo_anuncio_origem"], "Google Ads")
        self.assertEqual(plan.fields["primeiro_anuncio_clique_id"], "GCLID123")
        self.assertEqual(plan.fields["ultimo_anuncio_clique_id"], "GCLID123")
        self.assertEqual(
            plan.fields["primeiro_anuncio_tipo_campanha"], "Performance Max"
        )
        self.assertEqual(plan.fields["ultimo_anuncio_tipo_campanha"], "Performance Max")
        self.assertEqual(plan.fields["primeiro_anuncio_canal_destino"], "whatsapp")
        self.assertEqual(plan.fields["ultimo_anuncio_canal_destino"], "whatsapp")

    def test_google_ads_does_not_replace_existing_first_ad(self):
        fields = {
            "primeiro_anuncio_origem": "Meta Ads",
            "primeiro_anuncio_id": "META-1",
            "primeiro_anuncio_data": "2026-10-01 10:00:00",
        }
        message = {
            "id": "g2",
            "created_on": "2026-10-03T12:00:00Z",
            "direction": "in",
            "urn": "messenger:abc",
            "metadata": {
                "source_url": "https://example.com/?utm_source=google&utm_medium=paid_search&gclid=G2"
            },
        }
        plan = _build_contact_plan(
            self._contact(fields),
            context_messages=[message],
            candidate_message_ids={"g2"},
            farm_client=None,
            opt_out_group_name="Opt-Out LGPD",
            opt_out_group_uuid="g-out",
            captured_at="2026-10-03T12:01:00Z",
            opt_in_group_name="Opt-In LGPD",
            opt_in_group_uuid="g-in",
            available_weni_fields=self._available_fields(),
        )
        self.assertNotIn("primeiro_anuncio_origem", plan.fields)
        self.assertNotIn("primeiro_anuncio_id", plan.fields)
        self.assertEqual(plan.fields["ultimo_anuncio_origem"], "Google Ads")
        self.assertEqual(plan.fields["ultimo_anuncio_canal_destino"], "facebook")

    def test_newer_google_ads_updates_last_ad_but_older_does_not(self):
        fields = {
            "ultimo_anuncio_origem": "Meta Ads",
            "ultimo_anuncio_id": "META-2",
            "ultimo_anuncio_data": "2026-10-03 11:00:00",
        }
        older = {
            "id": "g-old",
            "created_on": "2026-10-03T10:00:00Z",
            "direction": "in",
            "urn": "whatsapp:1",
            "metadata": {
                "source_url": "https://example.com/?gclid=OLD&utm_source=google&utm_medium=cpc"
            },
        }
        plan_old = _build_contact_plan(
            self._contact(fields),
            context_messages=[older],
            candidate_message_ids={"g-old"},
            farm_client=None,
            opt_out_group_name="Opt-Out LGPD",
            opt_out_group_uuid="g-out",
            captured_at="2026-10-03T12:01:00Z",
            opt_in_group_name="Opt-In LGPD",
            opt_in_group_uuid="g-in",
            available_weni_fields=self._available_fields(),
        )
        self.assertNotIn("ultimo_anuncio_origem", plan_old.fields)

        newer = dict(older)
        newer["id"] = "g-new"
        newer["created_on"] = "2026-10-03T12:00:00Z"
        newer["metadata"] = {
            "source_url": "https://example.com/?gclid=NEW&utm_source=google&utm_medium=cpc"
        }
        plan_new = _build_contact_plan(
            self._contact(fields),
            context_messages=[newer],
            candidate_message_ids={"g-new"},
            farm_client=None,
            opt_out_group_name="Opt-Out LGPD",
            opt_out_group_uuid="g-out",
            captured_at="2026-10-03T12:01:00Z",
            opt_in_group_name="Opt-In LGPD",
            opt_in_group_uuid="g-in",
            available_weni_fields=self._available_fields(),
        )
        self.assertEqual(plan_new.fields["ultimo_anuncio_origem"], "Google Ads")
        self.assertEqual(plan_new.fields["ultimo_anuncio_clique_id"], "NEW")

    def test_optional_google_fields_are_skipped_when_not_created_in_weni(self):
        base_fields = {
            "contact_urn_whatsapp",
            "contact_urn_instagram",
            "contact_urn_facebook",
            "contact_canal_principal",
            "contact_urn",
            "primeiro_anuncio_origem",
            "primeiro_anuncio_id",
            "primeiro_anuncio_titulo",
            "primeiro_anuncio_url",
            "primeiro_anuncio_clique_id",
            "primeiro_anuncio_data",
            "ultimo_anuncio_origem",
            "ultimo_anuncio_id",
            "ultimo_anuncio_titulo",
            "ultimo_anuncio_url",
            "ultimo_anuncio_clique_id",
            "ultimo_anuncio_data",
        }
        message = {
            "id": "g3",
            "created_on": "2026-10-03T12:00:00Z",
            "direction": "in",
            "urn": "instagram:123",
            "metadata": {
                "source_url": "https://example.com/?gclid=G3&utm_source=google&utm_medium=display"
            },
        }
        contact = {
            "uuid": "u-instagram-google",
            "urns": ["instagram:123"],
            "groups": [],
            "fields": {},
        }
        plan = _build_contact_plan(
            contact,
            context_messages=[message],
            candidate_message_ids={"g3"},
            farm_client=None,
            opt_out_group_name="Opt-Out LGPD",
            opt_out_group_uuid="g-out",
            captured_at="2026-10-03T12:01:00Z",
            opt_in_group_name="Opt-In LGPD",
            opt_in_group_uuid="g-in",
            available_weni_fields=base_fields,
        )
        self.assertEqual(plan.fields["ultimo_anuncio_origem"], "Google Ads")
        self.assertNotIn("ultimo_anuncio_tipo_campanha", plan.fields)
        self.assertNotIn("ultimo_anuncio_canal_destino", plan.fields)


# NexoFlow 1.0
