"""Provas sintéticas: rede bloqueada pelo bootstrap da suíte."""

import json
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import Mock, patch

import weni_dedup as dedup
from dedup_io import DedupHTTPClient, digest, save_private
from dedup_sheets import diagnostic_order, requests_for, verify_transaction
from farmalider_client import FarmaliderError
from weni_client import WeniClient, WeniError

CAN = "11111111-1111-4111-8111-111111111111"
DUP = "22222222-2222-4222-8222-222222222222"
OTHER = "33333333-3333-4333-8333-333333333333"
CPF = "52998224725"


def table(headers, rows, sheet=1):
    return {
        "id": sheet,
        "book": "book",
        "headers": headers,
        "cells": {
            str(i): [{"userEnteredValue": {"stringValue": str(v)}} for v in row]
            for i, row in enumerate(rows, 2)
        },
    }


class Fixtures(unittest.TestCase):
    def setUp(self):
        self.canonical = {
            "uuid": CAN,
            "name": "🙂",
            "urns": ["whatsapp:+5591999998888"],
            "fields": {"cpf": CPF},
            "groups": [],
        }
        self.duplicate = {
            "uuid": DUP,
            "name": "Outro perfil",
            "urns": ["whatsapp:+5591988887777"],
            "fields": {"cpf": CPF, "pedido_id": "123"},
            "groups": [],
        }
        self.customer = {
            "id": 99,
            "nome": "Cliente oficial",
            "cpf": CPF,
            "celular": "91999998888",
        }
        self.order = {"capa": {"id": 123, "codCliente": 99}}
        self.contacts = [self.canonical, self.duplicate]
        self.tables = {
            "Base_Contatos": table(
                ["contact_uuid", "cpf", "opt_in_lgpd"], [[CAN, CPF, ""], [DUP, CPF, ""]]
            ),
            "Pedidos_FarmaLider": table(
                [
                    "pedido_chave",
                    "pedido_id",
                    "contact_uuid",
                    "telefone",
                    "cliente",
                    "cpf",
                    "cpf_normalizado",
                    "farmalider_cliente_id",
                    "farmalider_cod_cliente",
                    "valor_total_compra",
                    "quantidade_itens",
                    "farmalider_frete",
                    "status_confirmacao",
                ],
                [
                    [
                        "api-123",
                        "123",
                        DUP,
                        "old",
                        "Antigo",
                        CPF,
                        CPF,
                        "99",
                        "99",
                        "129.90",
                        "2",
                        "9.99",
                        "CONFIRMADO_API",
                    ]
                ],
                2,
            ),
            "Itens_Pedidos_FarmaLider": table(
                [
                    "item_chave",
                    "pedido_chave",
                    "pedido_id",
                    "contact_uuid",
                    "telefone",
                    "cliente",
                    "produto",
                    "quantidade",
                    "valor",
                ],
                [
                    [
                        "item-1",
                        "api-123",
                        "123",
                        DUP,
                        "old",
                        "Antigo",
                        "Produto humano",
                        "2",
                        "60",
                    ]
                ],
                3,
            ),
            "Historico_Alteracoes": table(
                ["contact_uuid", "pedido_id", "farmalider_cliente_id", "cpf"],
                [[DUP, "123", "99", CPF]],
                4,
            ),
        }
        self.tables["Itens_Pedidos_FarmaLider"]["ledger_orders"] = {"api-123": "123"}
        self.store = Mock()
        self.store.layout.values.return_value = [
            ["dashboard", "hub"],
            ["operacao", "ops"],
            ["historico", "hist"],
        ]
        self.store.capture.return_value = (self.tables, {"123"})
        from dedup_sheets import DedupSheets

        self.store.context_for.side_effect = DedupSheets.context_for
        self.farm = Mock()
        self.farm.get_cliente_by_cpf.return_value = self.customer
        self.farm.get_pedido.return_value = self.order
        self.weni = Mock()
        self.current = {c["uuid"]: deepcopy(c) for c in self.contacts}
        self.weni.get_contact.side_effect = lambda **kw: deepcopy(
            self.current.get(kw["uuid"])
        )
        self.weni.delete_contact.side_effect = lambda uid: self.current.pop(uid)

    def prepare(self, **kw):
        return dedup.prepare(self.contacts, self.farm, lambda: self.store, **kw)

    def pair(self):
        return self.prepare()["pairs"][0]

    def test_preview_confirms_owner_without_any_mutation(self):
        result = self.prepare()
        self.assertEqual(len(result["pairs"]), 1)
        self.assertFalse(result["pending"])
        self.store.cleanup.assert_not_called()
        self.farm.get_pedido.assert_called_once_with("123")
        self.assertEqual(result["pairs"][0]["canonical_uuid"], CAN)

    def test_without_orders_still_requires_cpf_and_unique_native_phone(self):
        self.duplicate["fields"].pop("pedido_id")
        self.tables = {"Base_Contatos": self.tables["Base_Contatos"]}
        self.store.capture.return_value = (self.tables, set())
        self.assertEqual(len(self.prepare()["pairs"]), 1)
        self.farm.get_pedido.assert_not_called()

    def test_generic_cpf_never_calls_api_or_opens_sheets(self):
        for c in self.contacts:
            c["fields"]["cpf"] = "0"
        self.assertFalse(self.prepare()["pairs"])
        self.farm.get_cliente_by_cpf.assert_not_called()
        self.store.capture.assert_not_called()

    def test_same_phone_outside_cpf_group_prevents_deletion(self):
        other = deepcopy(self.canonical)
        other["uuid"] = OTHER
        other["fields"]["cpf"] = "16899535009"
        self.contacts.append(other)
        self.assertFalse(self.prepare()["pairs"])

    def test_quarter_only_proposes_duplicates_inside_selected_scope(self):
        self.assertFalse(self.prepare(scope={CAN})["pairs"])
        self.assertEqual(len(self.prepare(scope={DUP})["pairs"]), 1)

    def test_customer_or_order_failure_is_a_pending_case(self):
        self.farm.get_cliente_by_cpf.side_effect = FarmaliderError("sintético")
        self.assertFalse(self.prepare()["pairs"])
        self.farm.get_cliente_by_cpf.side_effect = None
        self.farm.get_pedido.side_effect = FarmaliderError("sintético")
        self.assertFalse(self.prepare()["pairs"])

    def test_other_historical_customer_or_missing_ledger_blocks(self):
        self.tables["Historico_Alteracoes"]["cells"]["2"][2]["userEnteredValue"][
            "stringValue"
        ] = "100"
        self.assertFalse(self.prepare()["pairs"])
        self.tables["Historico_Alteracoes"]["cells"]["2"][2]["userEnteredValue"][
            "stringValue"
        ] = "99"
        self.store.capture.return_value = (self.tables, set())
        self.assertFalse(self.prepare()["pairs"])

    def test_cell_only_identity_repair_preserves_money_status_products(self):
        pair = self.pair()
        before = deepcopy(self.tables)
        requests, _ = requests_for(self.tables, [pair], {CAN: self.canonical})
        for name in ("Pedidos_FarmaLider", "Itens_Pedidos_FarmaLider"):
            for r in requests[name]:
                self.assertIn("updateCells", r)
                col = r["updateCells"]["range"]["startColumnIndex"]
                self.assertIn(
                    self.tables[name]["headers"][col],
                    {
                        "contact_uuid",
                        "telefone",
                        "cliente",
                        "cpf",
                        "cpf_normalizado",
                        "farmalider_cliente_id",
                    },
                )
        self.assertEqual(before, self.tables)

    def test_item_key_cannot_reassign_items_from_another_order(self):
        self.tables["Itens_Pedidos_FarmaLider"]["ledger_orders"] = {"api-123": "456"}
        result = self.prepare()
        self.assertFalse(result["pairs"])
        self.assertIn("Item não corresponde", result["pending"][0]["reason"])
        self.weni.delete_contact.assert_not_called()

    def test_formula_or_validation_on_identity_blocks_before_deletion(self):
        self.tables["Pedidos_FarmaLider"]["cells"]["2"][3] = {
            "userEnteredValue": {"formulaValue": '="' + DUP + '"'}
        }
        result = self.prepare()
        self.assertFalse(result["pairs"])
        self.assertIn("fórmula", result["pending"][0]["reason"])

    def test_refusal_is_never_changed_to_positive_consent(self):
        self.duplicate["fields"]["opt_in_lgpd"] = "recusado"
        pair = self.pair()
        canonical = deepcopy(self.canonical)
        canonical["fields"]["opt_in_lgpd"] = "recusado"
        requests, _ = requests_for(self.tables, [pair], {CAN: canonical})
        update = requests["Base_Contatos"][0]["updateCells"]
        self.assertEqual(
            update["rows"][0]["values"][0]["userEnteredValue"],
            {"stringValue": "recusado"},
        )

    def test_review_tampering_and_mode_mismatch_are_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "review.json"
            path.write_text("{}")
            payload = dedup.write_review(
                path, self.prepare(), full=True, quarter=None, campaign_id=""
            )
            self.assertEqual(
                dedup.load_review(path, full=True, quarter=None)["sha256"],
                payload["sha256"],
            )
            with self.assertRaises(ValueError):
                dedup.load_review(path, full=False, quarter=None)
            altered = json.loads(dedup.companion(path).read_text())
            altered["pairs"][0]["canonical_uuid"] = OTHER
            save_private(dedup.companion(path), altered)
            with self.assertRaises(ValueError):
                dedup.load_review(path, full=True, quarter=None)

    def test_apply_interrupts_then_deletes_then_cleans_sources_and_bi(self):
        pair = self.pair()
        events = []
        self.weni.interrupt_contact_flows.side_effect = lambda uid: events.append(
            "interrupt"
        )

        def delete(uid):
            events.append("delete")
            self.current.pop(uid)

        self.weni.delete_contact.side_effect = delete
        self.store.cleanup.side_effect = lambda *a: events.append("sheets")
        with (
            tempfile.TemporaryDirectory() as folder,
            patch("dedup_bi.refresh", side_effect=lambda *a: events.append("bi")),
        ):
            removed, skipped = dedup.apply_batch(
                [pair], self.weni, self.farm, self.store, self.contacts, {}, folder
            )
            state = json.loads((Path(folder) / "state.json").read_text())
        self.assertEqual(events, ["interrupt", "delete", "sheets", "bi"])
        self.assertEqual(removed, {DUP})
        self.assertEqual(skipped, 0)
        self.assertTrue(state["done"])

    def test_recompra_membership_is_removed_before_contact_deletion(self):
        self.duplicate["groups"] = [{"uuid": "rec", "name": "Recompra FarmaLíder"}]
        self.current[DUP]["groups"] = deepcopy(self.duplicate["groups"])
        pair = self.pair()
        events = []
        self.weni.interrupt_contact_flows.side_effect = lambda uid: events.append(
            "interrupt"
        )
        self.weni.contact_group_action.side_effect = lambda *a, **kw: events.append(
            "remove"
        )

        def delete(uid):
            events.append("delete")
            self.current.pop(uid)

        self.weni.delete_contact.side_effect = delete
        with tempfile.TemporaryDirectory() as folder, patch("dedup_bi.refresh"):
            dedup.apply_batch(
                [pair],
                self.weni,
                self.farm,
                self.store,
                self.contacts,
                {"rec": "Recompra FarmaLíder"},
                folder,
            )
        self.assertEqual(events, ["interrupt", "remove", "delete"])
        self.weni.contact_group_action.assert_called_once_with(
            DUP, action="remove", group="rec"
        )

    def test_apply_rechecks_owner_and_does_not_delete_after_order_error(self):
        pair = self.pair()
        self.farm.get_pedido.side_effect = FarmaliderError("sintético")
        with tempfile.TemporaryDirectory() as folder:
            removed, skipped = dedup.apply_batch(
                [pair], self.weni, self.farm, self.store, self.contacts, {}, folder
            )
        self.assertFalse(removed)
        self.assertEqual(skipped, 1)
        self.weni.delete_contact.assert_not_called()
        self.store.cleanup.assert_not_called()

    def test_uncertain_delete_only_reconciles_never_sends_twice(self):
        pair = self.pair()
        self.weni.delete_contact.side_effect = RuntimeError("timeout")
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaises(RuntimeError):
                dedup.apply_batch(
                    [pair], self.weni, self.farm, self.store, self.contacts, {}, folder
                )
            self.weni.delete_contact.reset_mock()
            with self.assertRaisesRegex(RuntimeError, "não será repetida"):
                dedup.apply_batch(
                    [pair], self.weni, self.farm, self.store, self.contacts, {}, folder
                )
            self.weni.delete_contact.assert_not_called()
            self.current.pop(DUP)
            with patch("dedup_bi.refresh"):
                removed, _ = dedup.apply_batch(
                    [pair], self.weni, self.farm, self.store, self.contacts, {}, folder
                )
            self.assertEqual(removed, {DUP})
            self.weni.delete_contact.assert_not_called()

    def test_rejected_delete_can_be_revalidated_on_next_execution(self):
        pair = self.pair()
        self.weni.delete_contact.side_effect = WeniError(
            "sem permissão", status_code=403
        )
        with tempfile.TemporaryDirectory() as folder, patch("dedup_bi.refresh"):
            with self.assertRaises(WeniError):
                dedup.apply_batch(
                    [pair], self.weni, self.farm, self.store, self.contacts, {}, folder
                )
            state = json.loads((Path(folder) / "state.json").read_text())
            self.assertEqual(state["deletions"][DUP], "rejected")
            self.weni.delete_contact.side_effect = lambda uid: self.current.pop(uid)
            removed, _ = dedup.apply_batch(
                [pair], self.weni, self.farm, self.store, self.contacts, {}, folder
            )
            self.assertEqual(removed, {DUP})
            self.assertEqual(self.weni.delete_contact.call_count, 2)

    def test_sheets_failure_is_resumed_after_weni_deletion(self):
        pair = self.pair()
        self.store.cleanup.side_effect = RuntimeError("timeout")
        with tempfile.TemporaryDirectory() as folder, patch("dedup_bi.refresh"):
            with self.assertRaises(RuntimeError):
                dedup.apply_batch(
                    [pair], self.weni, self.farm, self.store, self.contacts, {}, folder
                )
            self.store.cleanup.side_effect = None
            self.weni.delete_contact.reset_mock()
            removed, _ = dedup.apply_batch(
                [pair], self.weni, self.farm, self.store, self.contacts, {}, folder
            )
            self.assertEqual(removed, {DUP})
            self.weni.delete_contact.assert_not_called()

    def test_new_refusal_after_review_preserves_contact_without_deleting(self):
        pair = self.pair()
        self.current[DUP]["fields"]["marketing_opt_in"] = "recusado"
        with tempfile.TemporaryDirectory() as folder:
            removed, skipped = dedup.apply_batch(
                [pair], self.weni, self.farm, self.store, self.contacts, {}, folder
            )
        self.assertFalse(removed)
        self.assertEqual(skipped, 1)
        self.weni.delete_contact.assert_not_called()

    def test_native_readback_checks_keys_and_preserves_financial_cells(self):
        pair = self.pair()
        requests, _ = requests_for(self.tables, [pair], {CAN: self.canonical})
        body = [r for rs in requests.values() for r in rs]
        entry = {
            "backup": deepcopy(self.tables),
            "requests": body,
            "requests_sha256": digest(body),
        }
        current = deepcopy(self.tables)
        for request in body:
            if "updateCells" not in request:
                continue
            q = request["updateCells"]
            t = next(t for t in current.values() if t["id"] == q["range"]["sheetId"])
            t["cells"][str(q["range"]["startRowIndex"] + 1)][
                q["range"]["startColumnIndex"]
            ]["userEnteredValue"] = q["rows"][0]["values"][0]["userEnteredValue"]
        for title in ("Base_Contatos", "Historico_Alteracoes"):
            current[title]["cells"] = {
                n: cs
                for n, cs in current[title]["cells"].items()
                if cs[0].get("userEnteredValue", {}).get("stringValue") != DUP
            }
        verify_transaction(entry, current, {DUP})
        current["Pedidos_FarmaLider"]["cells"]["2"][9]["userEnteredValue"] = {
            "stringValue": "999"
        }
        with self.assertRaisesRegex(RuntimeError, "valor original"):
            verify_transaction(entry, current, {DUP})
        entry["requests_sha256"] = "alterado"
        with self.assertRaisesRegex(ValueError, "alterado"):
            verify_transaction(entry, current, {DUP})

    def test_existing_marketing_refusal_also_removes_inconsistent_recompra(self):
        canonical = self.current[CAN]
        canonical["fields"]["marketing_opt_in"] = "recusado"
        canonical["groups"] = [{"uuid": "rec", "name": "Recompra FarmaLíder"}]
        self.weni.contact_group_action.side_effect = lambda uid, **kw: self.current[
            uid
        ]["groups"].clear()
        kept = dedup.preserve_refusals(
            self.weni,
            deepcopy(canonical),
            self.duplicate,
            {"rec": "Recompra FarmaLíder"},
        )
        self.assertEqual(kept["groups"], [])
        self.weni.contact_group_action.assert_called_once_with(
            CAN, action="remove", group="rec"
        )
        self.weni.update_contact_fields.assert_not_called()

    def test_routine_cleans_before_advancing_normal_weni_checkpoint(self):
        from weni_update import UpdateStats, write_review_snapshot

        with tempfile.TemporaryDirectory() as folder:
            review = Path(folder) / "review.json"
            write_review_snapshot([], UpdateStats(), full=True, json_path=review)
            dedup.write_review(
                review, self.prepare(), full=True, quarter=None, campaign_id=""
            )
            self.weni.list_contacts.return_value = deepcopy(self.contacts)
            self.weni.list_groups.return_value = []

            def update(removed, contacts):
                self.assertEqual(removed, {DUP})
                self.assertNotIn(DUP, self.current)
                self.store.cleanup.assert_called_once()
                return UpdateStats()

            with (
                patch.object(dedup, "DIRECTORY", Path(folder) / "journal"),
                patch.object(dedup, "WeniClient", return_value=self.weni),
                patch.object(dedup, "DedupSheets", return_value=self.store),
                patch("weni_update._open_farmalider", return_value=self.farm),
                patch("dedup_bi.refresh"),
            ):
                _, result = dedup.run_with_dedup(
                    apply=True,
                    full=True,
                    quarter=None,
                    review_path=review,
                    update=update,
                )
            self.assertEqual(result["deleted"], 1)

    def test_routine_failure_keeps_cleanup_pending_but_continues_normal_load(self):
        from weni_update import UpdateStats, write_review_snapshot

        with tempfile.TemporaryDirectory() as folder:
            review = Path(folder) / "review.json"
            write_review_snapshot([], UpdateStats(), full=True, json_path=review)
            dedup.write_review(
                review, self.prepare(), full=True, quarter=None, campaign_id=""
            )
            self.weni.list_contacts.return_value = deepcopy(self.contacts)
            self.weni.list_groups.return_value = []
            self.store.cleanup.side_effect = RuntimeError("timeout")
            update = Mock()
            with (
                patch.object(dedup, "DIRECTORY", Path(folder) / "journal"),
                patch.object(dedup, "WeniClient", return_value=self.weni),
                patch.object(dedup, "DedupSheets", return_value=self.store),
                patch("weni_update._open_farmalider", return_value=self.farm),
            ):
                _, result = dedup.run_with_dedup(
                    apply=True,
                    full=True,
                    quarter=None,
                    review_path=review,
                    update=update,
                )
            update.assert_called_once_with({DUP}, None)
            self.assertEqual(result["failed_batches"], 1)
            states = list((Path(folder) / "journal").glob("*/lote-*/state.json"))
            self.assertEqual(len(states), 1)
            state = json.loads(states[0].read_text())
            self.assertFalse(state["done"])
            self.assertEqual(state["deleted"], [DUP])

    def test_normal_snapshot_corruption_blocks_before_any_delete(self):
        from weni_update import UpdateStats, write_review_snapshot

        with tempfile.TemporaryDirectory() as folder:
            review = Path(folder) / "review.json"
            write_review_snapshot([], UpdateStats(), full=True, json_path=review)
            dedup.write_review(
                review, self.prepare(), full=True, quarter=None, campaign_id=""
            )
            payload = json.loads(review.read_text())
            payload["plan_sha256"] = "alterado"
            save_private(review, payload)
            with (
                patch.object(dedup, "DIRECTORY", Path(folder) / "journal"),
                patch.object(dedup, "WeniClient") as client,
            ):
                with self.assertRaises(RuntimeError):
                    dedup.run_with_dedup(
                        apply=True,
                        full=True,
                        quarter=None,
                        review_path=review,
                        update=Mock(),
                    )
            client.assert_not_called()


class TransportTests(unittest.TestCase):
    def test_invalid_uuid_cannot_select_delete_and_mutations_require_single_attempt(
        self,
    ):
        weni = WeniClient("sintético", retry_mutations=False)
        with patch.object(weni, "_request_json") as request:
            for value in (
                "",
                None,
                CAN.replace("-", ""),
                "00000000-0000-0000-0000-000000000000",
            ):
                with self.assertRaises(ValueError):
                    weni.delete_contact(value)
            request.assert_not_called()
            weni.delete_contact(DUP)
            self.assertEqual(request.call_args.args[0], "DELETE")
        weni.close()
        weni = WeniClient("sintético")
        with self.assertRaises(ValueError):
            weni.delete_contact(DUP)
        weni.close()

    def test_mutating_google_request_is_not_retried(self):
        client = object.__new__(DedupHTTPClient)
        with patch(
            "gspread.http_client.HTTPClient.request",
            side_effect=RuntimeError("timeout"),
        ) as call:
            with self.assertRaises(RuntimeError):
                client.request("post", "url")
            call.assert_called_once()

    def test_diagnostic_exclusion_never_accepts_real_orders_or_freight(self):
        row = {
            "pedido_id": "",
            "status_confirmacao": "CPF_IDENTIDADE_DIVERGENTE",
            "origem_pedido": "FARMALIDER_CLIENTE_ULTIMO_PEDIDO",
        }
        self.assertTrue(diagnostic_order(row))
        self.assertFalse(diagnostic_order(dict(row, pedido_id="123")))
        self.assertFalse(diagnostic_order(dict(row, farmalider_frete="9.99")))
