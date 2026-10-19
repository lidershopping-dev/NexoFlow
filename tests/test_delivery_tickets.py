import json
import tempfile
import unittest
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from delivery_tickets import (
    apply_delivery_review,
    build_delivery_order_records,
    reconcile_tickets,
    select_tickets,
    ticket_order_link,
    write_delivery_review,
)
from weni_chats_client import WeniChatsClient, WeniChatsError
from weni_update import REQUIRED_ORDER_FIELDS

CONTACT = "10000000-0000-0000-0000-000000000001"
TICKET = "20000000-0000-0000-0000-000000000001"
NOW = "2026-10-08T16:00:00+00:00"


def room(**updates):
    result = {
        "uuid": TICKET,
        "contact": {"uuid": "internal-id", "external_id": CONTACT},
        "tags": [{"name": "Pedido"}, {"name": "Pedido finalizado"}],
        "user": {"name": "Atendente sintético"},
        "is_active": False,
        "created_on": "2026-10-08T12:00:00Z",
        "ended_at": "2026-10-08T15:00:00Z",
    }
    result.update(updates)
    return result


def contact(**updates):
    result = {
        "uuid": CONTACT,
        "name": "Cliente Árvore",
        "fields": {
            "cpf": "123",
            "nome": "Cliente Árvore",
            "email": "arvore@example.test",
        },
        "urns": ["tel:+5591987654321"],
    }
    result.update(updates)
    return result


def farm(payload=None):
    payload = payload or {
        "cliente": {
            "id": 101,
            "cpf": 123,
            "nome": "Cliente Árvore",
            "email": "arvore@example.test",
            "telefone": "91987654321",
        },
        "ultimoPedido": {
            "id": 201,
            "codCliente": 101,
            "emissao": "2026-06-01T10:00:00",
            "status": 2,
            "formaPagamento": 1,
            "frete": 0,
        },
        "itens": [
            {
                "codPedido": 201,
                "descricao": "Produto sintético",
                "quantidade": 2,
                "valor": 10,
                "total": 20,
            }
        ],
    }
    client = Mock()
    client.get_cliente_by_cpf.return_value = payload
    client.get_pedido.return_value = {
        "capa": payload["ultimoPedido"],
        "itens": payload["itens"],
    }
    return client


def selection(rooms=None):
    return select_tickets(rooms or [room()], cutoff=datetime.fromisoformat(NOW))[0]


class DeliveryTicketsTests(unittest.TestCase):
    def reconcile(self, tickets=None, contacts=None, client=None, order_id_field=""):
        return reconcile_tickets(
            tickets or selection(),
            contacts or [contact()],
            client or farm(),
            captured_at=NOW,
            available_fields=REQUIRED_ORDER_FIELDS,
            order_id_field=order_id_field,
        )

    def test_exact_tags_closed_human_and_flows_external_id(self):
        rooms = [
            room(),
            room(),
            room(
                uuid="20000000-0000-0000-0000-000000000002",
                tags=[{"name": " PEDIDO   FINALIZADO "}],
            ),
            room(
                uuid="20000000-0000-0000-0000-000000000003", tags=[{"name": "Pedidos"}]
            ),
            room(uuid="20000000-0000-0000-0000-000000000004", ended_at=None),
            room(uuid="20000000-0000-0000-0000-000000000005", user=None),
            room(
                uuid="20000000-0000-0000-0000-000000000006", contact={"uuid": CONTACT}
            ),
        ]
        selected, rejected = select_tickets(rooms, cutoff=datetime.fromisoformat(NOW))
        self.assertEqual(len(selected), 2)
        self.assertTrue(all(ticket["contact_uuid"] == CONTACT for ticket in selected))
        self.assertEqual(sum(rejected.values()), 3)

    def test_deleted_order_tag_is_exact_legacy_alias(self):
        for tag, expected in [
            ("Pedido is deleted", True),
            (" PEDIDO  IS   DELETED ", True),
            ("Pedido_is_deleted_2026-09-29 11:53:51.275814+00:00", True),
            ("PEDIDO_is_deleted_2026-09-29T13:04:10.754801+00:00", True),
            ("Pedido_is_deleted_2026-09-29T13:04:10Z", True),
            ("Pedido_is_deleted_2026-09-29 13:04:10", False),
            ("Pedido_is_deleted_2026-02-30T13:04:10Z", False),
            ("Pedido_is_deleted_2026-09-29T13:04:10+99:00", False),
            ("Pedido_is_deleted_2026-09-29T13:04:10Z extra", False),
            ("Pedido_is_deleted_invalido", False),
            ("Pedido_is_deleted_", False),
            ("Orcamento_is_deleted_2026-09-29 11:53:51.275814+00:00", False),
            ("Pedido cancelado_is_deleted_2026-09-29T13:04:10Z", False),
            ("Pedido is deleted extra", False),
            ("Pedidos is deleted", False),
            ("Pedido cancelado", False),
        ]:
            with self.subTest(tag=tag):
                self.assertEqual(
                    bool(selection([room(tags=[{"name": tag}])])), expected
                )

    def test_cutoff_and_dates_without_timezone_are_not_guessed(self):
        for ended in ["2026-10-09T00:00:00Z", "2026-10-08T15:00:00", "invalid"]:
            self.assertFalse(selection([room(ended_at=ended)]))

    def test_two_tickets_one_customer_one_order_and_real_emission(self):
        client = farm()
        tickets = selection([room(), room(uuid="20000000-0000-0000-0000-000000000002")])
        plans, data = self.reconcile(tickets=tickets, client=client)
        self.assertEqual(client.get_cliente_by_cpf.call_count, 1)
        self.assertEqual(len(plans), 1)
        self.assertEqual(len(data["orders"]), 1)
        self.assertEqual(len(data["items"]), 1)
        self.assertEqual(len(data["tickets"]), 2)
        self.assertEqual(data["orders"][0]["data"], "2026-06-01")
        self.assertEqual(plans[0].fields["pedido_data"], "01/06/26 10:00:00")
        self.assertTrue(
            all(
                row["vinculo_atendimento"] == "FORA_DO_PERIODO_DO_ATENDIMENTO"
                for row in data["tickets"]
            )
        )
        rows = build_delivery_order_records(
            data["tickets"], data["orders"], updated_at=NOW
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["quantidade_atendimentos"], 2)
        self.assertEqual(rows[0]["pedido_valor"], 20.0)
        self.assertEqual(rows[0]["vinculo_atendimento"], "SEM_VINCULO_COMPROVADO")

    def test_invalid_cpf_different_customer_cannot_confirm_order(self):
        bad = contact(
            name="Outra Pessoa",
            fields={"cpf": "123", "email": "other@example.test"},
            urns=[],
        )
        plans, data = self.reconcile(contacts=[bad])
        self.assertFalse(plans[0].has_changes)
        self.assertEqual(data["orders"], [])
        self.assertEqual(
            data["tickets"][0]["resultado_validacao"], "CPF_IDENTIDADE_DIVERGENTE"
        )

    def test_valid_cpfs_before_invalid_and_no_identity_shared_by_invalid_cpf(self):
        valid = contact(
            uuid="10000000-0000-0000-0000-000000000002", fields={"cpf": "52998224725"}
        )
        tickets = selection(
            [
                room(),
                room(
                    uuid="20000000-0000-0000-0000-000000000002",
                    contact={"external_id": valid["uuid"]},
                ),
            ]
        )
        with patch(
            "delivery_tickets.resolve_contact_order", return_value=(None, [])
        ) as resolver:
            self.reconcile(tickets=tickets, contacts=[contact(), valid])
        self.assertEqual(
            [call.args[0]["fields"]["cpf"] for call in resolver.call_args_list],
            ["52998224725", "123"],
        )

    def test_no_overwrite_of_newer_weni_order_but_history_export_kept(self):
        newer = contact()
        newer["fields"]["pedido_data"] = "02/06/26 10:00:00"
        plans, data = self.reconcile(contacts=[newer])
        self.assertFalse(plans[0].has_changes)
        self.assertEqual(len(data["orders"]), 1)

    def test_delivery_sale_flag_respects_current_status_and_identity_provenance(self):
        _, data = self.reconcile()
        for status, payment, expected in [
            (4, 1, 1),
            (1, 1, 1),
            (2, 1, 1),
            (-2, 1, 1),
            (-1, 1, 1),
            (0, 1, 1),
            (3, 1, 0),
            (2, 0, 0),
        ]:
            with self.subTest(status=status, payment=payment):
                order = dict(
                    data["orders"][0],
                    farmalider_status_pedido=str(status),
                    farmalider_forma_pagamento=str(payment),
                )
                rows = build_delivery_order_records(
                    data["tickets"], [order], updated_at=NOW
                )
                self.assertEqual(rows[0]["venda_realizada"], expected)
        order = dict(data["orders"][0], origem_pedido="FARMALIDER_PEDIDO_API")
        rows = build_delivery_order_records(data["tickets"], [order], updated_at=NOW)
        self.assertEqual(rows[0]["venda_realizada"], 0)

    def test_ticket_id_and_proximity_are_distinct_evidence(self):
        plans, data = self.reconcile()
        order = data["orders"][0]
        ticket = selection()[0]
        ticket["custom_fields"] = {"pedido_id": "201"}
        self.assertEqual(
            ticket_order_link(ticket, order), "FORA_DO_PERIODO_DO_ATENDIMENTO"
        )
        self.assertEqual(
            ticket_order_link(ticket, order, order_id_field="pedido_id"),
            "CONFIRMADO_POR_ID",
        )
        ticket["custom_fields"] = {"pedido_id": "202"}
        self.assertEqual(
            ticket_order_link(ticket, order, order_id_field="pedido_id"),
            "OUTRO_PEDIDO_NO_TICKET",
        )
        ticket["custom_fields"] = {}
        ticket["aberto_em"] = "2026-06-01T12:00:00Z"
        ticket["encerrado_em"] = "2026-06-01T14:00:00Z"
        self.assertEqual(
            ticket_order_link(ticket, order), "COMPATIVEL_COM_PERIODO_SEM_ID"
        )

    def test_snapshot_conflicting_identity_never_writes_weni_or_exports_success(self):
        plans, delivery = self.reconcile()
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "review.json"
            write_delivery_review(plans, delivery, path)
            changed = contact()
            changed["fields"]["email"] = "changed@example.test"
            weni = Mock()
            weni.get_contact.return_value = changed
            weni.list_fields.return_value = [
                {"key": key} for key in REQUIRED_ORDER_FIELDS
            ]
            store = Mock()
            store.get_control.return_value = ""
            for method in (
                store.upsert_contacts_detailed,
                store.upsert_orders_detailed,
                store.upsert_order_items_detailed,
            ):
                method.return_value.final_records = []
            store.upsert_pharmacy_orders.return_value = (
                store.upsert_orders_detailed.return_value,
                store.upsert_order_items_detailed.return_value,
            )
            store.reconcile_pharmacy_scope.return_value.orders = []
            store.reconcile_pharmacy_scope.return_value.items = []
            store.reconcile_pharmacy_scope.return_value.contacts = []
            result = apply_delivery_review(path, weni, store_factory=lambda: store)
            self.assertEqual(result["conflicts"], 1)
            weni.update_contact_fields.assert_not_called()
            self.assertEqual(store.upsert_contacts_detailed.call_args.args[0], [])
            self.assertEqual(store.upsert_pharmacy_orders.call_args.args[0], [])
            self.assertEqual(store.upsert_delivery_tickets.call_args.args[0], [])
            store.replace_looker_tables.assert_called_once()
            store.close.assert_called_once_with()

    def test_legacy_ultima_compra_is_omitted_and_same_snapshot_resumes(self):
        plans, delivery = self.reconcile()
        self.assertNotIn("ultima_compra", plans[0].fields)
        delivery["target"] = "weni"
        plans[0].fields["ultima_compra"] = delivery["orders"][0]["data_hora"]
        plans[0].previous["ultima_compra"] = None
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "review.json"
            write_delivery_review(plans, delivery, path)
            original = path.read_bytes()
            live = contact()
            weni = Mock()
            weni.list_fields.return_value = [
                {"key": key} for key in REQUIRED_ORDER_FIELDS
            ]
            weni.get_contact.side_effect = lambda **kwargs: deepcopy(live)

            def update(uid, fields):
                self.assertNotIn("ultima_compra", fields)
                live["fields"].update(fields)

            weni.update_contact_fields.side_effect = update
            result = apply_delivery_review(path, weni, target="weni")
            self.assertEqual(result["conflicts"], 0)
            count = weni.update_contact_fields.call_count
            apply_delivery_review(path, weni, target="weni")
            self.assertEqual(weni.update_contact_fields.call_count, count)
            self.assertEqual(path.read_bytes(), original)

    def test_unknown_snapshot_field_blocks_before_any_contact_write(self):
        from weni_update import PlanAuditError

        plans, delivery = self.reconcile()
        delivery["target"] = "weni"
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "review.json"
            write_delivery_review(plans, delivery, path)
            weni = Mock()
            weni.list_fields.return_value = [
                {"key": key} for key in REQUIRED_ORDER_FIELDS if key != "pedido_valor"
            ]
            with self.assertRaises(PlanAuditError):
                apply_delivery_review(path, weni, target="weni")
            weni.get_contact.assert_not_called()
            weni.update_contact_fields.assert_not_called()

    def test_weni_failure_identifies_stage_without_contact_uuid(self):
        from weni_client import WeniError

        plans, delivery = self.reconcile()
        delivery["target"] = "weni"
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "review.json"
            write_delivery_review(plans, delivery, path)
            weni = Mock()
            weni.list_fields.return_value = [
                {"key": key} for key in REQUIRED_ORDER_FIELDS
            ]
            weni.get_contact.return_value = contact()
            weni.update_contact_fields.side_effect = WeniError(
                "HTTP=400; requisição rejeitada"
            )
            with self.assertRaises(WeniError) as caught:
                apply_delivery_review(path, weni, target="weni")
            self.assertIn("GRAVAR_CAMPOS", str(caught.exception))
            self.assertIn("HTTP=400", str(caught.exception))
            self.assertNotIn(plans[0].contact_uuid, str(caught.exception))

    def _campaign_contact(self):
        current = contact()
        current["groups"] = [
            {"uuid": "recompra-synthetic", "name": "Recompra FarmaLíder"}
        ]
        current["fields"].update(
            pedido_data="01/05/26 10:00:00", marketing_opt_in="ativo"
        )
        return current

    def _apply_weni_review(self, plans, delivery, current, weni=None):
        weni = weni or Mock()
        weni.list_fields.return_value = [{"key": key} for key in REQUIRED_ORDER_FIELDS]
        weni.get_contact.return_value = current
        delivery["target"] = "weni"
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "review.json"
            write_delivery_review(plans, delivery, path)
            result = apply_delivery_review(path, weni, target="weni")
        return result, weni

    def test_new_order_removes_old_campaign_before_updating_fields(self):
        current = self._campaign_contact()
        current["groups"].append(dict(current["groups"][0]))
        plans, delivery = self.reconcile(contacts=[current])
        self.assertEqual(
            [(action.action, action.group) for action in plans[0].group_actions],
            [("remove", "recompra-synthetic")],
        )
        result, weni = self._apply_weni_review(plans, delivery, current)
        self.assertEqual(result["conflicts"], 0)
        weni.contact_group_action.assert_called_once_with(
            CONTACT, action="remove", group="recompra-synthetic"
        )
        calls = [call[0] for call in weni.mock_calls]
        self.assertLess(
            calls.index("contact_group_action"), calls.index("update_contact_fields")
        )
        self.assertNotIn(
            "marketing_opt_in", weni.update_contact_fields.call_args.args[1]
        )

    def test_cancelled_same_date_order_removes_campaign(self):
        current = self._campaign_contact()
        current["fields"]["pedido_data"] = "01/06/26 10:00:00"
        client = farm()
        client.get_pedido.return_value["capa"]["formaPagamento"] = 0
        plans, delivery = self.reconcile(contacts=[current], client=client)
        self.assertNotIn("pedido_data", plans[0].fields)
        self.assertFalse(plans[0].purchase_confirmed)
        _, weni = self._apply_weni_review(plans, delivery, current)
        weni.contact_group_action.assert_called_once_with(
            CONTACT, action="remove", group="recompra-synthetic"
        )

    def test_removal_failure_does_not_update_fields_or_retry_post(self):
        from weni_client import WeniError

        current = self._campaign_contact()
        plans, delivery = self.reconcile(contacts=[current])
        weni = Mock()
        weni.contact_group_action.side_effect = WeniError("HTTP=500")
        with self.assertRaises(WeniError):
            self._apply_weni_review(plans, delivery, current, weni)
        weni.contact_group_action.assert_called_once()
        weni.update_contact_fields.assert_not_called()

    def test_delivery_entrypoint_disables_automatic_mutation_retries(self):
        from run_delivery_tickets import main

        with (
            patch(
                "sys.argv",
                [
                    "app/run_delivery_tickets.py",
                    "--apply",
                    "--require-review",
                    "synthetic.json",
                ],
            ),
            patch.dict(
                "sys.modules",
                {
                    "secrets_config": SimpleNamespace(
                        WENI_API_TOKEN="synthetic-token",
                        GOOGLE_SPREADSHEET_ID="synthetic-sheet",
                    )
                },
            ),
            patch("weni_client.WeniClient") as weni_factory,
            patch(
                "delivery_tickets.apply_delivery_review", return_value={"conflicts": 0}
            ),
            patch("logging_config.configure_logging"),
            patch("builtins.print"),
            patch("proxy_config.configure_proxy"),
        ):
            self.assertEqual(main(), 0)
        weni_factory.assert_called_once_with("synthetic-token", retry_mutations=False)

    def test_partial_apply_resumes_without_repeating_completed_group_removal(self):
        from weni_client import WeniError

        current = self._campaign_contact()
        plans, delivery = self.reconcile(contacts=[current])
        delivery["target"] = "weni"
        weni = Mock()
        weni.list_fields.return_value = [{"key": key} for key in REQUIRED_ORDER_FIELDS]
        weni.get_contact.side_effect = lambda **kwargs: deepcopy(current)
        weni.contact_group_action.side_effect = lambda *args, **kwargs: current.update(
            groups=[]
        )
        weni.update_contact_fields.side_effect = WeniError("HTTP=500")
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "review.json"
            write_delivery_review(plans, delivery, path)
            with self.assertRaises(WeniError):
                apply_delivery_review(path, weni, target="weni")
            self.assertEqual(current["groups"], [])
            weni.update_contact_fields.side_effect = lambda uid, fields: current[
                "fields"
            ].update(fields)
            result = apply_delivery_review(path, weni, target="weni")
        self.assertEqual(result["conflicts"], 0)
        weni.contact_group_action.assert_called_once()
        self.assertEqual(current["fields"]["pedido_data"], "01/06/26 10:00:00")

    def test_same_recent_sale_does_not_remove_campaign_for_other_order_fields(self):
        current = self._campaign_contact()
        current["fields"]["pedido_data"] = "06/10/26 10:00:00"
        client = farm()
        client.get_pedido.return_value["capa"]["emissao"] = "2026-10-06T10:00:00"
        plans, _ = self.reconcile(contacts=[current], client=client)
        self.assertTrue(plans[0].fields)
        self.assertNotIn("pedido_data", plans[0].fields)
        self.assertEqual(plans[0].group_actions, [])

    def test_marketing_consent_changed_after_removal_review_blocks_apply(self):
        current = self._campaign_contact()
        plans, delivery = self.reconcile(contacts=[current])
        current["fields"]["marketing_opt_in"] = "recusado"
        result, weni = self._apply_weni_review(plans, delivery, current)
        self.assertEqual(result["conflicts"], 1)
        weni.contact_group_action.assert_not_called()
        weni.update_contact_fields.assert_not_called()

    def test_campaign_admission_after_review_blocks_unreviewed_reference_change(self):
        original = contact()
        plans, delivery = self.reconcile(contacts=[original])
        current = deepcopy(original)
        current["groups"] = self._campaign_contact()["groups"]
        result, weni = self._apply_weni_review(plans, delivery, current)
        self.assertEqual(result["conflicts"], 1)
        weni.contact_group_action.assert_not_called()
        weni.update_contact_fields.assert_not_called()

    def test_analytics_target_does_not_remove_campaign(self):
        current = self._campaign_contact()
        plans, delivery = self.reconcile(contacts=[current])
        delivery["target"] = "analytics"
        weni, store = Mock(), Mock()
        weni.list_fields.return_value = [{"key": key} for key in REQUIRED_ORDER_FIELDS]
        weni.get_contact.return_value = current
        store.get_control.return_value = ""
        for method in (
            store.upsert_contacts_detailed,
            store.upsert_orders_detailed,
            store.upsert_order_items_detailed,
        ):
            method.return_value.final_records = []
        store.upsert_pharmacy_orders.return_value = (
            store.upsert_orders_detailed.return_value,
            store.upsert_order_items_detailed.return_value,
        )
        store.reconcile_pharmacy_scope.return_value.orders = []
        store.reconcile_pharmacy_scope.return_value.items = []
        store.reconcile_pharmacy_scope.return_value.contacts = []
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "review.json"
            write_delivery_review(plans, delivery, path)
            apply_delivery_review(
                path, weni, store_factory=lambda: store, target="analytics"
            )
        weni.contact_group_action.assert_not_called()
        weni.update_contact_fields.assert_not_called()

    def test_snapshot_delivery_data_tampering_blocks_before_external_calls(self):
        plans, delivery = self.reconcile()
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "review.json"
            payload = write_delivery_review(plans, delivery, path)
            payload["delivery"]["orders"][0]["valor_total_compra"] = "999"
            path.write_text(json.dumps(payload), encoding="utf-8")
            weni, factory = Mock(), Mock()
            with self.assertRaises(ValueError):
                apply_delivery_review(path, weni, store_factory=factory)
            weni.list_fields.assert_not_called()
            factory.assert_not_called()

    def test_historical_ticket_id_verified_against_same_customer(self):
        tickets = selection([room(custom_fields={"pedido_id": "199"})])
        client = farm()
        latest = client.get_pedido.return_value
        historical = deepcopy(latest)
        historical["capa"]["id"] = 199
        historical["capa"]["emissao"] = "2026-05-01T10:00:00"
        historical["itens"][0]["codPedido"] = 199
        client.get_pedido.side_effect = lambda pid: (
            historical if str(pid) == "199" else latest
        )
        plans, data = self.reconcile(
            tickets=tickets, client=client, order_id_field="pedido_id"
        )
        self.assertEqual(data["orders"][0]["pedido_id"], "199")
        self.assertEqual(data["orders"][0]["data"], "2026-05-01")
        self.assertEqual(data["tickets"][0]["vinculo_atendimento"], "CONFIRMADO_POR_ID")
        self.assertEqual(plans[0].fields["pedido_id"], "201")
        historical["capa"]["codCliente"] = 999
        _, data = self.reconcile(
            tickets=tickets, client=client, order_id_field="pedido_id"
        )
        self.assertEqual(data["orders"], [])
        self.assertEqual(
            data["tickets"][0]["resultado_validacao"], "PEDIDO_CLIENTE_DIVERGENTE_API"
        )


class WeniChatsClientTests(unittest.TestCase):
    def client(self):
        return WeniChatsClient(
            "https://chats.example.test", "30000000-0000-0000-0000-000000000001"
        )

    def test_rejects_flows_token_and_authenticated_base_url(self):
        with self.assertRaises(WeniChatsError):
            WeniChatsClient("https://chats.example.test", "flows-token")
        with self.assertRaises(WeniChatsError):
            WeniChatsClient(
                "https://user:secret@chats.example.test",
                "30000000-0000-0000-0000-000000000001",
            )

    def test_pagination_collects_all_pages_and_deduplicates_rooms(self):
        client = self.client()
        first, second = Mock(status_code=200), Mock(status_code=200)
        first.json.return_value = {"results": [room()], "next": "?cursor=next"}
        second.json.return_value = {"results": [room()], "next": None}
        client.session.get = Mock(side_effect=[first, second])
        result = client.list_closed_rooms(cutoff=datetime.fromisoformat(NOW))
        self.assertEqual(len(result), 1)
        self.assertEqual(client.session.get.call_count, 2)
        self.assertEqual(
            client.session.get.call_args_list[0].kwargs["params"]["is_active"], "false"
        )

    def test_cross_origin_pagination_blocked_before_token_sent(self):
        client = self.client()
        first = Mock(status_code=200)
        first.json.return_value = {
            "results": [room()],
            "next": "https://other.example.test/v2/external/rooms_metrics/",
        }
        client.session.get = Mock(return_value=first)
        with self.assertRaises(WeniChatsError):
            client.list_closed_rooms(cutoff=datetime.fromisoformat(NOW))
        self.assertEqual(client.session.get.call_count, 1)

    def test_http_401_does_not_retry_or_print_response_body(self):
        client = self.client()
        client.session.get = Mock(
            return_value=Mock(status_code=401, text="secret private body")
        )
        with self.assertRaises(WeniChatsError) as error:
            client.list_closed_rooms(cutoff=datetime.fromisoformat(NOW))
        self.assertNotIn("secret", str(error.exception))
        self.assertEqual(client.session.get.call_count, 1)
