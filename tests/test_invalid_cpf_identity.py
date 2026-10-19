"""Synthetic customer-identity regressions for populated invalid CPFs."""

import unittest
from contextlib import nullcontext
from copy import deepcopy
from datetime import UTC, datetime
from unittest.mock import Mock, patch

from analytics import (
    ORDER_COLUMNS,
    ORDER_ITEM_COLUMNS,
    build_bi_order_rows,
    build_sales_metrics,
    normalize_contact,
)
from farmalider_client import FarmaliderClient, FarmaliderError, RequestPermit
from farmalider_integration import (
    cliente_link_status,
    resolve_contact_order,
    select_cliente_response,
    weni_customer_identity,
    weni_identity_guard,
)
from google_sheets import GoogleSheetsStore
from main import FarmaliderSettings, build_orders, validate_contacts
from weni_update import (
    PlanAuditError,
    _build_contact_plan,
    _prepare_frozen_plan_for_apply,
    audit_plans,
)

CAPTURED_AT = "2026-10-08T10:00:00-03:00"
VALID_CPF = "52998224725"
SETTINGS = FarmaliderSettings(True, "synthetic-token", False)


def customer_payload(
    identifier=101,
    *,
    name="Cliente Árvore",
    email="arvore@example.test",
    phone="91987654321",
    cpf=123,
    order_id=201,
):
    return {
        "cliente": {
            "id": identifier,
            "cpf": cpf,
            "nome": name,
            "email": email,
            "telefone": phone,
        },
        "ultimoPedido": {
            "id": order_id,
            "codCliente": identifier,
            "emissao": "2026-10-08T10:00:00",
            "status": 2,
            "formaPagamento": 1,
            "frete": 0,
        },
        "itens": [
            {
                "codPedido": order_id,
                "descricao": "Produto sintético",
                "quantidade": 1,
                "valor": 10,
                "total": 10,
            }
        ],
    }


def identity(
    *, name="Cliente Árvore", email="arvore@example.test", phone="91987654321"
):
    return {"nome": name, "email": email, "telefone": phone}


def weni_contact(
    uuid="synthetic-a",
    *,
    name="Cliente Árvore",
    email="arvore@example.test",
    phone="91987654321",
    cpf="123",
    pedido_id=None,
):
    fields = {"cpf": cpf, "nome": name, "email": email}
    if pedido_id is not None:
        fields["pedido_id"] = pedido_id
        fields["ultimo_pedido"] = {"pedido_id": pedido_id}
    return {
        "uuid": uuid,
        "name": name,
        "fields": fields,
        "urns": [f"tel:+55{phone}"] if phone else [],
    }


def farm_client(payload):
    farm = Mock()
    farm.get_cliente_by_cpf.return_value = payload
    farm.metrics_snapshot.return_value = {
        "total_requests": 1,
        "retries": 0,
        "rate_limits": 0,
        "server_errors": 0,
        "network_errors": 0,
        "max_response_ms": 1,
        "effective_delay_ms": 0,
    }
    candidates = payload if isinstance(payload, list) else [payload]
    orders = {
        str(candidate["ultimoPedido"]["id"]): {
            "capa": candidate["ultimoPedido"],
            "itens": candidate["itens"],
        }
        for candidate in candidates
        if isinstance(candidate, dict) and "ultimoPedido" in candidate
    }
    farm.get_pedido.side_effect = lambda order_id: orders.get(str(order_id))
    return farm


class MemoryWorksheet:
    """Keep the actual order upsert behavior observable without external calls."""

    def __init__(self, title, columns):
        self.title = title
        self.row_count = 1000
        self.values = [list(columns)]

    def get_all_values(self):
        return [row[:] for row in self.values]

    def col_values(self, column):
        return [row[column - 1] if len(row) >= column else "" for row in self.values]

    def update(self, *, range_name, values, **kwargs):
        start = int(range_name.split(":", 1)[0].lstrip("A")) - 1
        while len(self.values) < start + len(values):
            self.values.append([])
        for offset, row in enumerate(values):
            self.values[start + offset] = list(row)

    def append_rows(self, rows, **kwargs):
        self.values.extend([row[:] for row in rows])

    def batch_update(self, updates, **kwargs):
        for update in updates:
            row_number = int(update["range"].split(":", 1)[0].lstrip("A"))
            self.values[row_number - 1] = update["values"][0][:]


class InvalidCpfIdentityTests(unittest.TestCase):
    def test_invalid_cpf_accepts_matching_name_or_phone_but_not_email_alone(self):
        payload = customer_payload()
        cases = [
            ({}, "CPF_IDENTIDADE_INSUFICIENTE"),
            (identity(email="", phone=""), "CPF_INVALIDO_ENCONTRADO"),
            (identity(name="", email=""), "CPF_INVALIDO_ENCONTRADO"),
            (identity(name="🌻"), "CPF_INVALIDO_ENCONTRADO"),
            (
                identity(email="other@example.test", phone="92987654321"),
                "CPF_INVALIDO_ENCONTRADO",
            ),
            (identity(name="", phone=""), "CPF_IDENTIDADE_INSUFICIENTE"),
            (
                identity(name="Outro cliente", phone="92911112222"),
                "CPF_IDENTIDADE_DIVERGENTE",
            ),
        ]
        for local, status in cases:
            with self.subTest(identity=local):
                self.assertEqual(
                    cliente_link_status("123", payload, weni_identity=local)[0], status
                )

    def test_invalid_cpf_phone_matching_accepts_local_number_and_different_ddd(self):
        for phone in ("987654321", "92987654321", "+55 (92) 98765-4321"):
            with self.subTest(phone=phone):
                self.assertEqual(
                    cliente_link_status(
                        "123",
                        customer_payload(),
                        weni_identity=identity(name="🌻", email="", phone=phone),
                    )[0],
                    "CPF_INVALIDO_ENCONTRADO",
                )
        self.assertEqual(
            cliente_link_status(
                "123",
                customer_payload(phone="987654321"),
                weni_identity=identity(name="🌻", email="", phone="91987654321"),
            )[0],
            "CPF_INVALIDO_ENCONTRADO",
        )

    def test_first_name_matching_ignores_accents_symbols_and_surnames(self):
        self.assertEqual(
            cliente_link_status(
                "123",
                customer_payload(name="Felipe Balcão", phone=""),
                weni_identity=identity(
                    name="💫 FÉLIPE Outro Sobrenome", email="", phone=""
                ),
            )[0],
            "CPF_INVALIDO_ENCONTRADO",
        )
        result, selected = select_cliente_response(
            "123",
            [
                customer_payload(name="Felipe Um", phone=""),
                customer_payload(102, name="Felipe Dois", phone=""),
            ],
            weni_identity=identity(name="Felipe", email="", phone=""),
        )
        self.assertEqual(result[0], "CPF_IDENTIDADE_AMBIGUA")
        self.assertIsNone(selected)

    def test_first_name_matching_removes_internal_symbols_without_resolving_ambiguity(
        self,
    ):
        candidates = [
            customer_payload(name="Felipe Um", phone=""),
            customer_payload(102, name="Felipe Dois", phone=""),
        ]
        for name in ("Felipe", "F.e.l.i.p.e", "F💙elipe", "💫 *FÉLIPE* 💫"):
            with self.subTest(name=name):
                local = identity(name=name, email="", phone="")
                self.assertEqual(
                    cliente_link_status("123", candidates[0], weni_identity=local)[0],
                    "CPF_INVALIDO_ENCONTRADO",
                )
                result, selected = select_cliente_response(
                    "123", candidates, weni_identity=local
                )
                self.assertEqual(result[0], "CPF_IDENTIDADE_AMBIGUA")
                self.assertIsNone(selected)

    def test_first_name_matching_preserves_space_and_hyphen_boundaries(self):
        for name in ("💫 *ANA*-Maria", "✨ Ana Maria", "💫 123 Ana-Maria"):
            with self.subTest(name=name):
                local = identity(name=name, email="", phone="")
                self.assertEqual(
                    cliente_link_status(
                        "123",
                        customer_payload(name="Ana Balcão", phone=""),
                        weni_identity=local,
                    )[0],
                    "CPF_INVALIDO_ENCONTRADO",
                )
                self.assertEqual(
                    cliente_link_status(
                        "123",
                        customer_payload(name="Anamaria Balcão", phone=""),
                        weni_identity=local,
                    )[0],
                    "CPF_IDENTIDADE_DIVERGENTE",
                )

    def test_email_or_phone_each_suffices_with_the_same_name(self):
        for weni_identity in [identity(phone=""), identity(email="")]:
            with self.subTest(identity=weni_identity):
                self.assertEqual(
                    cliente_link_status(
                        "123", customer_payload(), weni_identity=weni_identity
                    ),
                    ("CPF_INVALIDO_ENCONTRADO", "101", "Cliente Árvore"),
                )

    def test_display_name_can_match_api_when_custom_name_is_expanded(self):
        local = identity(name="Cliente Ferreira Souza")
        local["weni_name"] = "CLIENTE F SOUZA"
        payload = customer_payload(name="Cliente F Souza")
        self.assertEqual(
            cliente_link_status("123", payload, weni_identity=local)[0],
            "CPF_INVALIDO_ENCONTRADO",
        )
        local["email"] = "other@example.test"
        local["telefone"] = "11911112222"
        self.assertEqual(
            cliente_link_status("123", payload, weni_identity=local)[0],
            "CPF_INVALIDO_ENCONTRADO",
        )
        local["nome"] = "Outra Pessoa"
        local["weni_name"] = "Outra Pessoa"
        self.assertEqual(
            cliente_link_status("123", payload, weni_identity=local)[0],
            "CPF_IDENTIDADE_DIVERGENTE",
        )

    def test_display_name_used_for_identity_is_guarded_against_changes(self):
        contact = {
            "name": "Cliente F Souza",
            "fields": {"nome": "Cliente Ferreira Souza"},
            "urns": ["tel:91987654321"],
        }
        before = weni_identity_guard(contact)
        contact["name"] = "Outra Pessoa"
        self.assertNotEqual(before, weni_identity_guard(contact))

    def test_name_and_email_comparison_normalizes_case_accents_and_spaces(self):
        result = cliente_link_status(
            "00000000123",
            customer_payload(),
            weni_identity=identity(
                name="  CLIENTE   ARVORE  ", email="  ARVORE@EXAMPLE.TEST  ", phone=""
            ),
        )
        self.assertEqual(result[0:2], ("CPF_INVALIDO_ENCONTRADO", "101"))

    def test_phone_ignores_brazil_country_and_ddd_but_preserves_local_digits(self):
        for phone in ["+55 (91) 98765-4321", "91987654321"]:
            with self.subTest(phone=phone):
                self.assertEqual(
                    cliente_link_status(
                        "123",
                        customer_payload(),
                        weni_identity=identity(email="", phone=phone),
                    )[0],
                    "CPF_INVALIDO_ENCONTRADO",
                )
        for api_phone, weni_phone in [
            ("92911112222", "91987654321"),
            ("9198765432", "91998765432"),
        ]:
            with self.subTest(api_phone=api_phone, weni_phone=weni_phone):
                self.assertEqual(
                    cliente_link_status(
                        "123",
                        customer_payload(email="", phone=api_phone),
                        weni_identity=identity(
                            name="Nome diferente", email="", phone=weni_phone
                        ),
                    )[0],
                    "CPF_IDENTIDADE_DIVERGENTE",
                )

    def test_first_name_suffices_but_email_alone_does_not_establish_identity(self):
        self.assertEqual(
            cliente_link_status(
                "123",
                customer_payload(),
                weni_identity=identity(name="Cliente", phone=""),
            )[0],
            "CPF_INVALIDO_ENCONTRADO",
        )
        result = cliente_link_status(
            "123",
            customer_payload(email="same", phone=""),
            weni_identity=identity(name="Outro cliente", email="same", phone=""),
        )
        self.assertNotEqual(result[0], "CPF_INVALIDO_ENCONTRADO")
        self.assertEqual(result[1:], ("", ""))

    def test_selects_matching_customer_in_each_supported_candidate_shape(self):
        wrong = customer_payload(
            102,
            name="Outra Palmeira",
            email="palmeira@example.test",
            phone="92911112222",
            order_id=202,
        )
        wanted = customer_payload()
        for payload in [
            [wrong, wanted],
            [wrong["cliente"], wanted["cliente"]],
            {"clientes": [wrong, wanted]},
            {"clientes": [wrong["cliente"], wanted["cliente"]]},
            {"cliente": [wrong["cliente"], wanted["cliente"]]},
        ]:
            with self.subTest(shape=payload):
                result, selected = select_cliente_response(
                    "123", payload, weni_identity=identity()
                )
                self.assertEqual(result[0:2], ("CPF_INVALIDO_ENCONTRADO", "101"))
                self.assertIsInstance(selected, dict)
                selected_customer = selected.get("cliente", selected)
                self.assertEqual(str(selected_customer["id"]), "101")

    def test_no_matching_candidate_blocks_even_with_other_customers_present(self):
        result, selected = select_cliente_response(
            "123",
            [customer_payload(), customer_payload(102, order_id=202)],
            weni_identity=identity(name="Pessoa ausente", phone="92911112222"),
        )
        self.assertEqual(result, ("CPF_IDENTIDADE_DIVERGENTE", "", ""))
        self.assertIsNone(selected)

    def test_two_distinct_matching_customers_are_ambiguous(self):
        result, selected = select_cliente_response(
            "123",
            [customer_payload(), customer_payload(102, order_id=202)],
            weni_identity=identity(),
        )
        self.assertEqual(result, ("CPF_IDENTIDADE_AMBIGUA", "", ""))
        self.assertIsNone(selected)

    def test_conflicting_snapshots_for_same_customer_fail_closed(self):
        result, selected = select_cliente_response(
            "123",
            [customer_payload(), customer_payload(order_id=202)],
            weni_identity=identity(),
        )
        self.assertNotEqual(result[0], "CPF_INVALIDO_ENCONTRADO")
        self.assertEqual(result[1:], ("", ""))
        self.assertIsNone(selected)

    def test_invalid_matching_still_requires_same_cpf_and_positive_customer_id(self):
        for payload in [customer_payload(cpf=1), customer_payload(identifier=0)]:
            with self.subTest(customer=payload["cliente"]):
                result, selected = select_cliente_response(
                    "123", payload, weni_identity=identity()
                )
                self.assertNotEqual(result[0], "CPF_INVALIDO_ENCONTRADO")
                self.assertEqual(result[1:], ("", ""))
                self.assertIsNone(selected)

    def test_valid_cpf_keeps_mathematical_cpf_identity_behavior(self):
        self.assertEqual(
            cliente_link_status(
                VALID_CPF,
                customer_payload(cpf=VALID_CPF),
                weni_identity=identity(name="🌻", email="other@example.test", phone=""),
            ),
            ("CPF_CONFIRMADO", "101", "Cliente Árvore"),
        )

    def test_raw_weni_wrapped_fields_and_whatsapp_urn_are_used(self):
        contact = {
            "uuid": "synthetic-wrapped",
            "name": "Nome do canal",
            "fields": {
                "cpf": {"value": "123"},
                "nome": {"value": "Cliente Árvore"},
                "email": {"value": ""},
            },
            "urns": ["whatsapp:+5591987654321", "instagram:synthetic-id"],
        }
        farm = farm_client(customer_payload(email=""))
        order, items = resolve_contact_order(
            contact, farm_client=farm, captured_at=CAPTURED_AT
        )
        self.assertEqual(order["status_validacao"], "OK")
        self.assertEqual(order["farmalider_cliente_id"], "101")
        self.assertEqual(len(items), 1)

    def test_invalid_identity_guard_detects_native_phone_change_despite_saved_phone(
        self,
    ):
        contact = weni_contact(email="")
        contact["fields"]["telefone"] = "91987654321"
        changed = deepcopy(contact)
        changed["urns"] = ["tel:+5592987654321"]
        self.assertNotEqual(weni_identity_guard(contact), weni_identity_guard(changed))

    def test_native_whatsapp_phone_takes_precedence_over_stale_saved_channel(self):
        contact = weni_contact(email="", phone="")
        contact["urns"] = ["whatsapp:+5591987654321"]
        contact["fields"]["contact_urn_whatsapp"] = "whatsapp:+5592911112222"
        payload = [
            customer_payload(
                102, name="Outro cliente", email="", phone="92911112222", order_id=202
            ),
            customer_payload(email=""),
        ]
        farm = farm_client(payload)
        order, items = resolve_contact_order(
            contact, farm_client=farm, captured_at=CAPTURED_AT
        )
        self.assertIsNotNone(order)
        self.assertEqual(order["status_validacao"], "OK")
        self.assertEqual(order["farmalider_cliente_id"], "101")
        self.assertEqual(items[0]["pedido_id"], "201")
        row = normalize_contact(contact, CAPTURED_AT)
        validate_contacts(
            [row], farm_client=farm, farm_settings=SETTINGS, sync_time=CAPTURED_AT
        )
        self.assertEqual(row["farmalider_cliente_id"], "101")

    def test_shared_cpf_raw_response_is_resolved_per_contact_for_customers_and_orders(
        self,
    ):
        contacts = [
            weni_contact(),
            weni_contact(
                "synthetic-b",
                name="Outra Palmeira",
                email="palmeira@example.test",
                phone="92911112222",
            ),
            weni_contact(
                "synthetic-c",
                name="Pessoa ausente",
                email="absent@example.test",
                phone="93911113333",
            ),
        ]
        payload = [
            customer_payload(
                102,
                name="Outra Palmeira",
                email="palmeira@example.test",
                phone="92911112222",
                order_id=202,
            ),
            customer_payload(),
        ]
        original_payload = deepcopy(payload)
        farm = farm_client(payload)
        rows = [normalize_contact(contact, CAPTURED_AT) for contact in contacts]
        responses, lookup_statuses = {}, {}
        stats = validate_contacts(
            rows,
            farm_client=farm,
            farm_settings=SETTINGS,
            sync_time=CAPTURED_AT,
            cliente_responses_by_cpf=responses,
            cliente_lookup_status_by_cpf=lookup_statuses,
        )
        self.assertEqual(
            [row["farmalider_cliente_id"] for row in rows], ["101", "102", ""]
        )
        self.assertEqual(
            rows[2]["farmalider_cliente_status"], "CPF_IDENTIDADE_DIVERGENTE"
        )
        self.assertEqual(stats.unique_queries, 1)
        self.assertEqual(stats.confirmed, 2)
        orders, items, _ = build_orders(
            contacts,
            rows_by_uuid={row["contact_uuid"]: row for row in rows},
            farm_client=farm,
            sync_time=CAPTURED_AT,
            cliente_responses_by_cpf=responses,
            cliente_lookup_status_by_cpf=lookup_statuses,
        )
        confirmed = {
            order["contact_uuid"]: order
            for order in orders
            if order.get("status_validacao") == "OK"
        }
        self.assertEqual(set(confirmed), {"synthetic-a", "synthetic-b"})
        self.assertEqual(confirmed["synthetic-a"]["pedido_id"], "201")
        self.assertEqual(confirmed["synthetic-b"]["pedido_id"], "202")
        self.assertEqual(
            {item["contact_uuid"] for item in items}, {"synthetic-a", "synthetic-b"}
        )
        farm.get_cliente_by_cpf.assert_called_once_with("123")
        self.assertEqual(payload, original_payload)
        self.assertTrue(all(contact["fields"]["cpf"] == "123" for contact in contacts))

    def test_raw_contact_identity_map_preserves_ambiguity_hidden_by_normalized_phone(
        self,
    ):
        contact = weni_contact(email="", phone="")
        contact["fields"].update(telefone="91987654321", celular="92911112222")
        row = normalize_contact(contact, CAPTURED_AT)
        self.assertNotIn("celular", row)
        payload = [
            customer_payload(email=""),
            customer_payload(
                102, name="Outro cliente", email="", phone="92911112222", order_id=202
            ),
        ]
        farm = farm_client(payload)
        validate_contacts(
            [row],
            farm_client=farm,
            farm_settings=SETTINGS,
            sync_time=CAPTURED_AT,
            identity_by_uuid={row["contact_uuid"]: weni_customer_identity(contact)},
        )
        self.assertEqual(row["farmalider_cliente_status"], "CPF_IDENTIDADE_AMBIGUA")
        self.assertEqual(row["farmalider_cliente_id"], "")
        order, items = resolve_contact_order(
            contact, farm_client=farm, captured_at=CAPTURED_AT
        )
        self.assertEqual(order["status_confirmacao"], row["farmalider_cliente_status"])
        self.assertEqual(items, [])

    def test_selected_envelope_keeps_its_own_snapshot(self):
        payload = [
            customer_payload(
                102,
                name="Outra Palmeira",
                email="palmeira@example.test",
                phone="92911112222",
                order_id=202,
            ),
            customer_payload(),
        ]
        farm = farm_client(payload)
        farm.get_pedido.return_value = None
        farm.get_pedido.side_effect = None
        order, items = resolve_contact_order(
            weni_contact(), farm_client=farm, captured_at=CAPTURED_AT
        )
        self.assertEqual(order["pedido_id"], "201")
        self.assertEqual(order["farmalider_cliente_id"], "101")
        self.assertEqual(items[0]["pedido_id"], "201")

    def test_customer_list_never_borrows_an_outer_order_snapshot(self):
        envelope = customer_payload()
        payload = {
            "cliente": [envelope["cliente"]],
            "ultimoPedido": envelope["ultimoPedido"],
            "itens": envelope["itens"],
        }
        farm = farm_client(payload)
        order, items = resolve_contact_order(
            weni_contact(), farm_client=farm, captured_at=CAPTURED_AT
        )
        self.assertEqual(items, [])
        self.assertTrue(order is None or order.get("status_validacao") != "OK")
        farm.get_pedido.assert_not_called()

    def test_invalid_identity_failure_does_not_use_weni_order_locators(self):
        for payload, status in [
            (
                customer_payload(name="Outro cliente", phone="92911112222"),
                "CPF_IDENTIDADE_DIVERGENTE",
            ),
            (
                [customer_payload(), customer_payload(102, order_id=202)],
                "CPF_IDENTIDADE_AMBIGUA",
            ),
        ]:
            with self.subTest(status=status):
                farm = farm_client(payload)
                order, items = resolve_contact_order(
                    weni_contact(pedido_id="201"),
                    farm_client=farm,
                    captured_at=CAPTURED_AT,
                    linked_cliente_id="101",
                )
                self.assertEqual(order["status_confirmacao"], status)
                self.assertEqual(items, [])
                farm.get_pedido.assert_not_called()

    def test_404_and_500_do_not_recover_invalid_cpf_using_stale_customer_or_locator(
        self,
    ):
        for failure in [None, FarmaliderError("synthetic HTTP 500")]:
            with self.subTest(failure=type(failure).__name__):
                farm = farm_client(customer_payload())
                if isinstance(failure, Exception):
                    farm.get_cliente_by_cpf.side_effect = failure
                else:
                    farm.get_cliente_by_cpf.return_value = None
                order, items = resolve_contact_order(
                    weni_contact(pedido_id="201"),
                    farm_client=farm,
                    captured_at=CAPTURED_AT,
                    linked_cliente_id="101",
                )
                self.assertNotEqual(order["status_validacao"], "OK")
                self.assertEqual(items, [])
                farm.get_pedido.assert_not_called()

    def test_known_invalid_cpf_lookup_failure_never_repeats_lookup_or_uses_locator(
        self,
    ):
        for status in ["CPF_NAO_ENCONTRADO", "ERRO_API", "CPF_REJEITADO_API"]:
            with self.subTest(status=status):
                farm = farm_client(customer_payload())
                order, items = resolve_contact_order(
                    weni_contact(pedido_id="201"),
                    farm_client=farm,
                    captured_at=CAPTURED_AT,
                    linked_cliente_id="101",
                    cliente_lookup_status=status,
                )
                self.assertNotEqual(order["status_validacao"], "OK")
                self.assertEqual(items, [])
                farm.get_cliente_by_cpf.assert_not_called()
                farm.get_pedido.assert_not_called()

    def test_recent_invalid_persistent_result_is_revalidated(self):
        row = normalize_contact(weni_contact(), CAPTURED_AT)
        existing = dict(
            row,
            farmalider_cliente_status="CPF_INVALIDO_ENCONTRADO",
            farmalider_cliente_id="999",
            farmalider_cliente_nome="Cliente anterior",
            farmalider_cliente_consultado_em=datetime.now(UTC).isoformat(),
        )
        farm = farm_client(customer_payload())
        stats = validate_contacts(
            [row],
            farm_client=farm,
            farm_settings=SETTINGS,
            sync_time=CAPTURED_AT,
            existing_by_uuid={row["contact_uuid"]: existing},
            reuse_existing=True,
        )
        farm.get_cliente_by_cpf.assert_called_once_with("123")
        self.assertEqual(stats.cache_reused, 0)
        self.assertEqual(row["farmalider_cliente_id"], "101")

    def test_api_error_never_preserves_unverified_invalid_customer_id(self):
        for old_status in ["CPF_INVALIDO_ENCONTRADO", "ERRO_API"]:
            with self.subTest(old_status=old_status):
                row = normalize_contact(weni_contact(), CAPTURED_AT)
                existing = dict(
                    row,
                    farmalider_cliente_status=old_status,
                    farmalider_cliente_id="999",
                    farmalider_cliente_nome="Cliente anterior",
                    farmalider_cliente_consultado_em=CAPTURED_AT,
                )
                farm = farm_client(customer_payload())
                farm.get_cliente_by_cpf.side_effect = FarmaliderError(
                    "synthetic HTTP 500"
                )
                stats = validate_contacts(
                    [row],
                    farm_client=farm,
                    farm_settings=SETTINGS,
                    sync_time=CAPTURED_AT,
                    existing_by_uuid={row["contact_uuid"]: existing},
                )
                self.assertEqual(row["farmalider_cliente_status"], "ERRO_API")
                self.assertEqual(row["farmalider_cliente_id"], "")
                self.assertEqual(stats.prior_details_retained, 0)

    def test_legacy_invalid_cpf_confirmation_is_excluded_from_sales_and_bi(self):
        contact = weni_contact()
        order, items = resolve_contact_order(
            contact,
            farm_client=farm_client(customer_payload()),
            captured_at=CAPTURED_AT,
        )
        legacy_order = dict(order, origem_pedido="FARMALIDER_PEDIDO_API")
        self.assertEqual(legacy_order["status_confirmacao"], "CONFIRMADO_API")
        self.assertEqual(legacy_order["status_validacao"], "OK")
        self.assertEqual(build_sales_metrics([legacy_order], items), [])
        self.assertEqual(
            build_bi_order_rows(
                [normalize_contact(contact, CAPTURED_AT)],
                [legacy_order],
                items,
                updated_at=CAPTURED_AT,
            ),
            [],
        )

    def test_identity_verified_invalid_orders_count_for_all_official_discovery_paths(
        self,
    ):
        for source in ["pedido", "cpf-fallback", "weni-locator"]:
            with self.subTest(source=source):
                contact = weni_contact(pedido_id="201")
                payload = customer_payload()
                official_order = {
                    "capa": deepcopy(payload["ultimoPedido"]),
                    "itens": deepcopy(payload["itens"]),
                }
                if source == "weni-locator":
                    del payload["ultimoPedido"]
                    del payload["itens"]
                farm = farm_client(payload)
                if source == "cpf-fallback":
                    farm.get_pedido.side_effect = None
                    farm.get_pedido.return_value = None
                elif source == "weni-locator":
                    farm.get_pedido.side_effect = None
                    farm.get_pedido.return_value = official_order
                order, items = resolve_contact_order(
                    contact, farm_client=farm, captured_at=CAPTURED_AT
                )
                self.assertEqual(order["status_validacao"], "OK")
                self.assertTrue(order["origem_pedido"].endswith("_IDENTIDADE_VALIDADA"))
                metrics = build_sales_metrics([order], items)
                self.assertEqual(len(metrics), 1)
                self.assertEqual(metrics[0][1:4], [1, 1, 1])
                self.assertEqual(metrics[0][5], "10.00")
                self.assertEqual(
                    len(
                        build_bi_order_rows(
                            [normalize_contact(contact, CAPTURED_AT)],
                            [order],
                            items,
                            updated_at=CAPTURED_AT,
                        )
                    ),
                    1,
                )

    def test_persisted_legacy_order_and_new_identity_failure_stay_excluded_from_bi(
        self,
    ):
        contact = weni_contact()
        farm = farm_client(customer_payload())
        old_order, old_items = resolve_contact_order(
            contact, farm_client=farm, captured_at=CAPTURED_AT
        )
        old_order["origem_pedido"] = "FARMALIDER_PEDIDO_API"
        store = GoogleSheetsStore.__new__(GoogleSheetsStore)
        store.orders = MemoryWorksheet("Pedidos", ORDER_COLUMNS)
        store.order_items = MemoryWorksheet("Itens", ORDER_ITEM_COLUMNS)
        store._read_with_retry = lambda label, operation: operation()
        store._write_with_retry = lambda label, operation: operation()
        first = store.upsert_orders_detailed([old_order])
        first_items = store.upsert_order_items_detailed(old_items)
        contact["fields"]["nome"] = "Pessoa ausente"
        contact["name"] = "Pessoa ausente"
        contact["urns"] = ["tel:92911112222"]
        failure, failure_items = resolve_contact_order(
            contact, farm_client=farm, captured_at=CAPTURED_AT
        )
        self.assertEqual(failure["status_confirmacao"], "CPF_IDENTIDADE_DIVERGENTE")
        self.assertEqual(failure_items, [])
        final = store.upsert_orders_detailed([failure])
        self.assertEqual(len(final.final_records), 2)
        historical = next(
            row
            for row in final.final_records
            if row["pedido_chave"] == old_order["pedido_chave"]
        )
        self.assertEqual(historical, first.final_records[0])
        self.assertEqual(
            build_sales_metrics(final.final_records, first_items.final_records), []
        )
        self.assertEqual(
            build_bi_order_rows(
                [normalize_contact(contact, CAPTURED_AT)],
                final.final_records,
                first_items.final_records,
                updated_at=CAPTURED_AT,
            ),
            [],
        )

    def test_invalid_frozen_plan_rejects_name_email_and_native_phone_drift(self):
        contact = weni_contact()
        plan = _build_contact_plan(
            contact,
            context_messages=[],
            candidate_message_ids=set(),
            farm_client=farm_client(customer_payload()),
            captured_at=CAPTURED_AT,
            opt_out_group_name="Opt-Out LGPD",
            opt_out_group_uuid="synthetic-group",
        )
        self.assertTrue(plan.order_validated)
        self.assertIn("__invalid_cpf_identity", plan.guards)
        self.assertIn("__native_urns", plan.guards)
        audit_plans([plan], opt_out_group_available=True)
        for drift in ["nome", "email", "native-phone"]:
            with self.subTest(drift=drift):
                changed = deepcopy(contact)
                if drift == "nome":
                    changed["fields"]["nome"] = "Cliente ausente"
                elif drift == "email":
                    changed["fields"]["email"] = "different@example.test"
                else:
                    changed["urns"] = ["tel:+5592987654321"]
                prepared, conflict = _prepare_frozen_plan_for_apply(
                    plan,
                    changed,
                    opt_in_group_name="Opt-In LGPD",
                    opt_in_group_uuid="",
                    opt_out_group_name="Opt-Out LGPD",
                    opt_out_group_uuid="synthetic-group",
                )
                self.assertIsNone(prepared)
                self.assertTrue(conflict)
        prepared, conflict = _prepare_frozen_plan_for_apply(
            plan,
            contact,
            opt_in_group_name="Opt-In LGPD",
            opt_in_group_uuid="",
            opt_out_group_name="Opt-Out LGPD",
            opt_out_group_uuid="synthetic-group",
        )
        self.assertIsNotNone(prepared)
        self.assertEqual(conflict, "")

    def test_old_invalid_frozen_plan_without_identity_guard_is_not_auditable(self):
        contact = weni_contact()
        plan = _build_contact_plan(
            contact,
            context_messages=[],
            candidate_message_ids=set(),
            farm_client=farm_client(customer_payload()),
            captured_at=CAPTURED_AT,
            opt_out_group_name="Opt-Out LGPD",
            opt_out_group_uuid="synthetic-group",
        )
        legacy_plan = deepcopy(plan)
        del legacy_plan.guards["__invalid_cpf_identity"]
        with self.assertRaises(PlanAuditError):
            audit_plans([legacy_plan], opt_out_group_available=True)

    def test_customer_transport_accepts_candidate_lists_but_order_transport_rejects_them(
        self,
    ):
        client = FarmaliderClient("synthetic-token")
        response = Mock(status_code=200, headers={})
        response.json.return_value = [customer_payload()]
        try:
            with (
                patch.object(client.session, "get", return_value=response),
                patch.object(
                    client,
                    "_request_slot",
                    side_effect=lambda _: nullcontext(RequestPermit(0, 4)),
                ),
            ):
                self.assertEqual(client.get_cliente_by_cpf("123"), [customer_payload()])
                with self.assertRaises(FarmaliderError):
                    client.get_pedido("201")
                # O contrato de pedido é objeto ou ausência; nunca lista.
                pedido_payload = {"capa": {"id": 201}, "itens": []}
                response.json.return_value = pedido_payload
                self.assertIs(client.get_pedido("201"), pedido_payload)
                response.status_code = 404
                response.json.reset_mock()
                self.assertIsNone(client.get_pedido("201"))
                response.json.assert_not_called()
        finally:
            client.close()


if __name__ == "__main__":
    unittest.main()
