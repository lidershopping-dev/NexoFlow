"""Recompra com fixtures sintéticas; nenhuma mensagem ou API real."""

import json
import tempfile
import unittest
from copy import deepcopy
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import Mock, patch

from recompra import (
    DAY_EXPRESSION,
    FIELDS,
    GROUP,
    OPT_OUT,
    REFUSAL_EXPRESSION,
    TZ,
    apply_review,
    load_state,
    make_review,
    propose,
    read_review,
    resolve_latest,
    validate_flow_guards,
    write_review,
)
from weni_update import (
    ContactUpdatePlan,
    GroupAction,
    _build_contact_plan,
    _execute_contact_plan,
)

NOW = datetime(2026, 10, 9, 13, 57, tzinfo=TZ)
STRUCTURE = {
    "group_uuid": "g-recompra",
    "opt_out_uuid": "g-out",
    "event_uuid": "event",
    "delivery_hour": -1,
    "event_signature": "signature",
    "errors": [],
}


def fixture(
    days=25, *, order_id="100", grouped=False, marketing="", status="2", payment="3"
):
    dt = NOW - timedelta(days=days)
    if days == 25:
        # Admission fixtures model a still-future D+25 event. Past/equal
        # deadlines are covered separately and must never authorize a POST.
        dt += timedelta(minutes=1)
    contact = {
        "uuid": "u1",
        "name": "Cliente Sintético",
        "urns": ["tel:+5591999999999"],
        "fields": {"cpf": "52998224725", "marketing_opt_in": marketing},
        "groups": [{"uuid": "g-recompra", "name": GROUP}] if grouped else [],
    }
    order = {
        "pedido_id": order_id,
        "pedido_chave": "FARMALIDER:" + order_id,
        "data_hora": dt.isoformat(),
        "cpf_normalizado": "52998224725",
        "farmalider_status_pedido": status,
        "farmalider_forma_pagamento": payment,
        "status_confirmacao": "CONFIRMADO_API",
        "status_validacao": "OK",
        "farmalider_cliente_id": "1",
        "valor_total_compra": "10",
    }
    items = [
        {
            "produto_nome": "Produto sintético, 30 cápsulas",
            "quantidade": "1",
            "valor_unitario": "10",
            "valor_total": "10",
        }
    ]
    return contact, order, items


class RecompraRulesTests(unittest.TestCase):
    def test_window_0_24_25_26_365_and_future(self):
        for days in (0, 24, 25, 26, 365, -1):
            with self.subTest(days=days):
                c, o, i = fixture(days)
                plan = propose(c, o, i, STRUCTURE, now=NOW)
                self.assertEqual(plan["action"] == "add", 0 <= days <= 25)
                self.assertEqual(plan["due_today"], days == 25)
                self.assertFalse(set(plan["fields"]) - FIELDS)
                if days < 0:
                    self.assertEqual(plan["fields"], {})

    def test_remove_expired_group_26_and_365(self):
        for days in (26, 365):
            c, o, i = fixture(days, grouped=True)
            self.assertEqual(propose(c, o, i, STRUCTURE, now=NOW)["action"], "remove")

    def test_empty_opt_in_allowed_without_modifying_marketing(self):
        c, o, i = fixture(marketing="")
        plan = propose(c, o, i, STRUCTURE, now=NOW)
        self.assertEqual(plan["action"], "add")
        self.assertNotIn("marketing_opt_in", plan["fields"])
        self.assertNotIn("ultima_compra", plan["fields"])
        self.assertIn("pedido_data", plan["fields"])
        self.assertEqual(plan["fields"]["pedido_data"], o["data_hora"])
        self.assertNotIn("ultimo_pedido", plan["fields"])
        self.assertNotIn("ultimo_pedido", plan["guard"]["fields"])
        self.assertEqual(json.loads(plan["purchase"]["snapshot"])["pedido_id"], "100")

    def test_all_recusals_and_group_opt_out_take_precedence(self):
        for value in ("recusado", "Não", "nao", "negado", "  NÃO  ", "false", "0"):
            c, o, i = fixture(grouped=True, marketing=value)
            self.assertEqual(propose(c, o, i, STRUCTURE, now=NOW)["action"], "remove")
        c, o, i = fixture(grouped=True, marketing="ativo")
        c["groups"].append({"uuid": "g-out", "name": OPT_OUT})
        self.assertEqual(
            propose(c, o, i, STRUCTURE, now=NOW)["reason"], "RECUSA_MARKETING"
        )

    def test_documented_non_cancelled_states_are_sales_for_recompra(self):
        for status in ("-2", "-1", "0", "4", "1", "2"):
            with self.subTest(status=status):
                c, o, items = fixture(status=status)
                plan = propose(c, o, items, STRUCTURE, now=NOW)
                self.assertNotEqual(plan["reason"], "SEM_PEDIDO_CONFIRMADO")

    def test_missing_date_products_identity_cancelled_and_pending(self):
        for status in ("3",):
            c, o, i = fixture(grouped=True, status=status)
            p = propose(c, o, i, STRUCTURE, now=NOW)
            self.assertEqual(p["action"], "remove")
            self.assertEqual(p["fields"], {})
        for transform in ("date", "items", "payment", "identity"):
            c, o, i = fixture(grouped=True)
            if transform == "date":
                o["data_hora"] = ""
            if transform == "items":
                i = []
            if transform == "payment":
                o["farmalider_forma_pagamento"] = "0"
            if transform == "identity":
                o.update(cpf_normalizado="0", origem_pedido="FARMALIDER_PEDIDO_API")
            self.assertEqual(propose(c, o, i, STRUCTURE, now=NOW)["action"], "remove")

    def test_existing_sales_statuses_4_1_2_are_used(self):
        for status in ("4", "1", "2"):
            c, o, i = fixture(status=status)
            self.assertEqual(propose(c, o, i, STRUCTURE, now=NOW)["action"], "add")

    def test_old_order_cannot_replace_newer_confirmed_purchase(self):
        c, o, i = fixture(25, grouped=True)
        c["fields"]["pedido_data"] = (NOW - timedelta(days=5)).isoformat()
        c["fields"]["pedido_status"] = "2"
        p = propose(c, o, i, STRUCTURE, now=NOW)
        self.assertEqual(p["reason"], "PEDIDO_ANTIGO_BLOQUEADO")
        self.assertEqual(p["fields"], {})

    def test_new_order_removes_old_event_before_replacing_reference(self):
        c, o, i = fixture(0, order_id="200", grouped=True)
        c["fields"]["pedido_data"] = (NOW - timedelta(days=24)).isoformat()
        saved = {"orders": {"100": {"admitted": True}}}
        p = propose(c, o, i, STRUCTURE, saved, now=NOW)
        self.assertEqual(p["action"], "replace")
        self.assertEqual(p["order_id"], "200")
        self.assertFalse(p["due_today"])

    def test_recompra_writes_only_date_and_products_not_analytics_fields(self):
        c, o, i = fixture(0, order_id="200", grouped=True)
        c["fields"].update(
            {
                "pedido_id": "100",
                "pedido_valor": "999",
                "pedido_quantidade_itens": "9",
                "pedido_status": "1",
            }
        )
        p = propose(c, o, i, STRUCTURE, now=NOW)
        self.assertEqual(set(p["fields"]), {"pedido_data", "pedido_produtos"})
        self.assertEqual(p["fields"]["pedido_data"], o["data_hora"])
        self.assertEqual(p["order_id"], "200")
        self.assertNotIn("ultimo_pedido", p["guard"]["fields"])

    def test_incomplete_item_blocks_admission_and_order_writes(self):
        c, o, i = fixture(24, grouped=True)
        i[0]["quantidade"] = ""
        p = propose(c, o, i, STRUCTURE, now=NOW)
        self.assertEqual(p["reason"], "PEDIDO_INCOMPLETO")
        self.assertEqual(p["action"], "remove")
        self.assertEqual(p["fields"], {})

    def test_ultima_compra_is_ignored_even_when_it_conflicts(self):
        c, o, i = fixture(24)
        c["fields"]["ultima_compra"] = (NOW + timedelta(days=10)).isoformat()
        p = propose(c, o, i, STRUCTURE, now=NOW)
        self.assertEqual(p["action"], "add")
        self.assertNotIn("ultima_compra", p["fields"])
        self.assertNotIn("ultima_compra", p["guard"]["fields"])

    def test_same_order_never_reenters_after_flow_removes_group(self):
        c, o, i = fixture(24)
        for record in ({"admitted": True}, {"pending": True}):
            p = propose(c, o, i, STRUCTURE, {"orders": {"100": record}}, now=NOW)
            self.assertEqual(p["action"], "")
            self.assertEqual(p["reason"], "PEDIDO_JA_ADMITIDO_OU_INDETERMINADO")

    def test_calendar_day_in_belem_differs_from_utc(self):
        c, o, i = fixture()
        o["data_hora"] = "2026-09-15T01:00:00Z"
        p = propose(c, o, i, STRUCTURE, now=NOW)
        self.assertTrue(p["due_today"])
        self.assertTrue(
            p["fields"]["pedido_data"].startswith("2026-09-14T22:00:00-03:00")
        )

    def test_past_event_is_visible_and_does_not_change_reference_to_now(self):
        c, o, i = fixture()
        o["data_hora"] = "2026-09-14T08:00:00-03:00"
        p = propose(c, o, i, STRUCTURE, now=NOW)
        self.assertTrue(p["due_today"])
        self.assertFalse(p["scheduled_future"])
        self.assertEqual(p["fields"]["pedido_data"], o["data_hora"])


class RecompraApplyTests(unittest.TestCase):
    def execute(self, c, o, i, *, failure=False, changed=False, clock=None):
        now = clock or (lambda: NOW)
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        path = Path(temp.name) / "state.json"
        audit = Path(temp.name) / "audit.jsonl"
        state = load_state(path)
        plan = propose(c, o, i, STRUCTURE, now=NOW)
        review = make_review([plan], STRUCTURE, now=NOW, state=state)
        client = Mock()
        live = deepcopy(c)

        def get_contact(**kwargs):
            return deepcopy(live)

        def update(uid, fields):
            live["fields"].update(fields)

        def group(uid, action, group):
            if failure and action == "add":
                raise RuntimeError("Resposta perdida")
            if action == "remove":
                live["groups"] = [g for g in live["groups"] if g["uuid"] != group]
            else:
                live["groups"].append({"uuid": group, "name": GROUP})

        client.get_contact.side_effect = get_contact
        client.update_contact_fields.side_effect = update
        client.contact_group_action.side_effect = group
        if changed:
            live["fields"]["marketing_opt_in"] = "recusado"
        with (
            patch("recompra.inspect_structure", return_value=STRUCTURE),
            patch("recompra.resolve_latest", return_value=(o, i)),
        ):
            if failure:
                with self.assertRaises(RuntimeError):
                    apply_review(
                        review,
                        client,
                        Mock(),
                        state_path=path,
                        audit_path=audit,
                        now_fn=now,
                    )
            else:
                result = apply_review(
                    review,
                    client,
                    Mock(),
                    state_path=path,
                    audit_path=audit,
                    now_fn=now,
                )
                self.assertEqual(result.get("conflitos", 0), 1 if changed else 0)
        return client, load_state(path), live, review, path

    def test_reviewed_apply_updates_then_admits_and_persists_across_restart(self):
        c, o, i = fixture(24)
        client, state, live, _, path = self.execute(c, o, i)
        self.assertTrue(state["contacts"]["u1"]["orders"]["100"]["admitted"])
        self.assertEqual(load_state(path), state)
        calls = [call[0] for call in client.method_calls]
        self.assertLess(
            calls.index("update_contact_fields"), calls.index("contact_group_action")
        )
        live["groups"] = []
        self.assertEqual(
            propose(live, o, i, STRUCTURE, state["contacts"]["u1"], now=NOW)["action"],
            "",
        )

    def test_uncertain_post_never_retries_admission_automatically(self):
        c, o, i = fixture(24)
        client, state, live, _, _ = self.execute(c, o, i, failure=True)
        self.assertTrue(state["contacts"]["u1"]["orders"]["100"]["pending"])
        self.assertEqual(
            propose(live, o, i, STRUCTURE, state["contacts"]["u1"], now=NOW)["action"],
            "",
        )

    def test_concurrent_refusal_skips_all_posts(self):
        c, o, i = fixture(24)
        client, _, _, _, _ = self.execute(c, o, i, changed=True)
        client.update_contact_fields.assert_not_called()
        client.contact_group_action.assert_not_called()

    def test_old_review_and_missing_fields_block_before_posts(self):
        c, o, i = fixture()
        review = make_review([propose(c, o, i, STRUCTURE, now=NOW)], STRUCTURE, now=NOW)
        client = Mock()
        with self.assertRaises(RuntimeError):
            apply_review(review, client, Mock(), now_fn=lambda: NOW + timedelta(days=1))
        with patch(
            "recompra.inspect_structure",
            return_value={**STRUCTURE, "errors": ["pedido_data ausente"]},
        ):
            with self.assertRaises(RuntimeError):
                apply_review(review, client, Mock(), now_fn=lambda: NOW)
        client.update_contact_fields.assert_not_called()
        client.contact_group_action.assert_not_called()

    def test_revised_plan_tampering_blocks_application(self):
        c, o, i = fixture()
        review = make_review([propose(c, o, i, STRUCTURE, now=NOW)], STRUCTURE, now=NOW)
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "review.json"
            write_review(review, path)
            self.assertEqual(read_review(path), review)
            data = json.loads(path.read_text())
            data["review"]["plans"][0]["fields"]["marketing_opt_in"] = "ativo"
            path.write_text(json.dumps(data))
            with self.assertRaises(RuntimeError):
                read_review(path)

    def test_corrupt_state_cannot_reset_duplicate_protection(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "state.json"
            path.write_text("broken")
            with self.assertRaises(RuntimeError):
                load_state(path)

    def test_main_weni_removes_old_campaign_group_before_field_write(self):
        client = Mock()
        plan = ContactUpdatePlan(
            "u1",
            fields={"pedido_data": "2026-10-09T13:57:00-03:00"},
            group_actions=[GroupAction("remove", "g-recompra", "nova compra", GROUP)],
        )
        _execute_contact_plan(client, plan)
        self.assertEqual(
            [c[0] for c in client.method_calls],
            ["contact_group_action", "update_contact_fields"],
        )

    def test_main_weni_without_campaign_keeps_other_group_behavior(self):
        client = Mock()
        plan = ContactUpdatePlan(
            "u1",
            fields={"idade": 37},
            group_actions=[GroupAction("add", "g-lgpd", "ativo", "Opt-In LGPD")],
        )
        _execute_contact_plan(client, plan)
        self.assertEqual(
            [c[0] for c in client.method_calls],
            ["update_contact_fields", "contact_group_action"],
        )


class RecompraIntegrationTests(unittest.TestCase):
    def test_existing_weni_planner_proposes_expiry_removal_without_other_group_changes(
        self,
    ):
        c, o, i = fixture(26, grouped=True)
        c["fields"]["pedido_data"] = o["data_hora"]
        plan = _build_contact_plan(
            c,
            context_messages=[],
            candidate_message_ids=set(),
            farm_client=None,
            opt_out_group_name="Opt-Out LGPD",
            opt_out_group_uuid="g-lgpd-out",
            captured_at=NOW.isoformat(),
        )
        self.assertEqual(
            [(a.action, a.group_name) for a in plan.group_actions], [("remove", GROUP)]
        )
        self.assertNotIn("marketing_opt_in", plan.fields)

    def test_failure_removing_old_schedule_blocks_new_date_write(self):
        client = Mock()
        client.contact_group_action.side_effect = RuntimeError("falha")
        plan = ContactUpdatePlan(
            "u1",
            fields={"pedido_data": NOW.isoformat()},
            group_actions=[GroupAction("remove", "g-recompra", "referencia", GROUP)],
        )
        with self.assertRaises(RuntimeError):
            _execute_contact_plan(client, plan)
        client.update_contact_fields.assert_not_called()

    def test_group_presence_does_not_allow_same_order_to_be_rescheduled(self):
        c, o, i = fixture(24, grouped=True)
        c["fields"]["pedido_data"] = (NOW - timedelta(days=23)).isoformat()
        p = propose(
            c, o, i, STRUCTURE, {"orders": {"100": {"admitted": True}}}, now=NOW
        )
        self.assertEqual(p["action"], "remove")

    def test_invalid_cpf_selection_uses_identity_per_contact_even_when_lookup_cached(
        self,
    ):
        c, o, i = fixture()
        c["fields"]["cpf"] = "123"
        cache = {}
        farm = Mock()
        farm.get_cliente_by_cpf.return_value = {"clientes": []}
        with patch(
            "recompra.select_cliente_response",
            return_value=(("CPF_IDENTIDADE_DIVERGENTE", "", ""), None),
        ) as selector:
            self.assertEqual(
                resolve_latest(c, farm, NOW.isoformat(), cache),
                ({"status_confirmacao": "CPF_IDENTIDADE_DIVERGENTE"}, []),
            )
            second = deepcopy(c)
            second["uuid"] = "u2"
            second["name"] = "Outro Cliente Sintético"
            self.assertEqual(
                resolve_latest(second, farm, NOW.isoformat(), cache),
                ({"status_confirmacao": "CPF_IDENTIDADE_DIVERGENTE"}, []),
            )
            self.assertEqual(farm.get_cliente_by_cpf.call_count, 1)
            self.assertEqual(selector.call_count, 2)
            self.assertNotEqual(
                selector.call_args_list[0].kwargs["weni_identity"],
                selector.call_args_list[1].kwargs["weni_identity"],
            )

    def test_flow_route_audit_rejects_bypass_even_if_guard_words_are_present(self):
        nodes = []
        specs = [
            ("@contact.groups", "has_group", ["g-out"], False),
            (REFUSAL_EXPRESSION, "has_phrase", ["MKT_RECUSADO"], False),
            (DAY_EXPRESSION, "has_phrase", ["D25_OK"], True),
            ("@fields.pedido_produtos", "has_text", [], True),
        ]
        for index, (operand, kind, args, accept) in enumerate(specs):
            nodes.append(
                {
                    "uuid": str(index),
                    "actions": [],
                    "router": {
                        "operand": operand,
                        "cases": [
                            {
                                "type": kind,
                                "arguments": args,
                                "category_uuid": "matched",
                            }
                        ],
                        "categories": [
                            {"uuid": "matched", "exit_uuid": "matched-exit"},
                            {"uuid": "default", "exit_uuid": "default-exit"},
                        ],
                        "default_category_uuid": "default",
                    },
                    "exits": [
                        {
                            "uuid": "matched-exit",
                            "destination_uuid": str(index + 1) if accept else "reject",
                        },
                        {
                            "uuid": "default-exit",
                            "destination_uuid": "reject" if accept else str(index + 1),
                        },
                    ],
                }
            )
        nodes.append(
            {
                "uuid": "4",
                "actions": [
                    {
                        "type": "send_msg",
                        "templating": {"template": {"name": "recompra"}},
                    }
                ],
                "exits": [],
            }
        )
        nodes.append(
            {
                "uuid": "reject",
                "actions": [{"type": "remove_contact_groups"}],
                "exits": [{"uuid": "end", "destination_uuid": None}],
            }
        )
        flow = {"nodes": nodes}
        self.assertTrue(validate_flow_guards(flow, "g-out"))
        unsafe = deepcopy(flow)
        unsafe["nodes"][2]["exits"][1]["destination_uuid"] = "4"
        self.assertFalse(validate_flow_guards(unsafe, "g-out"))


class RecompraRuntimeDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.state_path = Path(temp.name) / "recompra_state.json"
        self.audit_path = Path(temp.name) / "recompra_audit.jsonl"

    def test_conflict_after_fields_before_group_add_does_not_block_future_review(self):
        from recompra import apply_review

        contact, order, items = fixture(24)
        state = load_state(self.state_path)
        plan = propose(contact, order, items, STRUCTURE, now=NOW)
        review = make_review([plan], STRUCTURE, now=NOW, state=state)
        original = deepcopy(contact)
        changed = deepcopy(contact)
        changed["fields"].update(plan["fields"])
        changed["fields"]["marketing_opt_in"] = "recusado"
        weni = Mock()
        weni.get_contact.side_effect = [original, changed]
        with (
            patch("recompra.inspect_structure", return_value=STRUCTURE),
            patch("recompra.resolve_latest", return_value=(order, items)),
        ):
            result = apply_review(
                review,
                weni,
                Mock(),
                state_path=self.state_path,
                audit_path=self.audit_path,
                now_fn=lambda: NOW,
            )
        self.assertEqual(result, {"conflitos": 1})
        weni.update_contact_fields.assert_called_once()
        weni.contact_group_action.assert_not_called()
        saved = load_state(self.state_path)["contacts"]["u1"]
        self.assertFalse(saved["orders"]["100"]["pending"])
        self.assertFalse(saved["orders"]["100"].get("admitted", False))
        # Nova revisão é necessária, mas não há veto por suposto POST perdido.
        eligible = deepcopy(changed)
        eligible["fields"]["marketing_opt_in"] = ""
        self.assertEqual(
            propose(eligible, order, items, STRUCTURE, saved, now=NOW)["action"], "add"
        )
        events = [
            json.loads(line)
            for line in self.audit_path.read_text(encoding="utf-8").splitlines()
        ]
        self.assertEqual(events[0]["status"], "CONFLITO_PRE_INCLUSAO")

    def test_uncertain_add_preserves_pending_and_records_safe_stage(self):
        from recompra import apply_review

        contact, order, items = fixture(24)
        state = load_state(self.state_path)
        plan = propose(contact, order, items, STRUCTURE, now=NOW)
        review = make_review([plan], STRUCTURE, now=NOW, state=state)
        current = deepcopy(contact)
        weni = Mock()

        def get_contact(*, uuid):
            return deepcopy(current)

        def update_fields(uid, fields):
            current["fields"].update(fields)

        weni.get_contact.side_effect = get_contact
        weni.update_contact_fields.side_effect = update_fields
        weni.contact_group_action.side_effect = RuntimeError(
            "SENSITIVE_FAKE_ERROR_PAYLOAD"
        )
        with (
            patch("recompra.inspect_structure", return_value=STRUCTURE),
            patch("recompra.resolve_latest", return_value=(order, items)),
        ):
            with self.assertRaisesRegex(RuntimeError, "adicionar_grupo") as caught:
                apply_review(
                    review,
                    weni,
                    Mock(),
                    state_path=self.state_path,
                    audit_path=self.audit_path,
                    now_fn=lambda: NOW,
                )
        self.assertNotIn("SENSITIVE_FAKE_ERROR_PAYLOAD", str(caught.exception))
        weni.contact_group_action.assert_called_once()
        self.assertTrue(
            load_state(self.state_path)["contacts"]["u1"]["orders"]["100"]["pending"]
        )
        events = [
            json.loads(line)
            for line in self.audit_path.read_text(encoding="utf-8").splitlines()
        ]
        self.assertEqual(events[0]["status"], "FALHA_OU_RESULTADO_INDETERMINADO")
        self.assertEqual(events[0]["stage"], "adicionar_grupo")
        self.assertEqual(events[0]["error_type"], "RuntimeError")
        self.assertTrue(events[0]["origin"])
        self.assertNotIn("SENSITIVE_FAKE_ERROR_PAYLOAD", json.dumps(events[0]))

    def test_diagnose_state_is_read_only_and_handles_legacy_audit(self):
        from recompra import atomic_json, diagnose_state

        payload = {
            "schema": 1,
            "contacts": {
                "u1": {"orders": {"100": {"pending": True, "admitted": False}}},
                "u2": {"orders": {"200": {"pending": False, "admitted": True}}},
            },
        }
        atomic_json(self.state_path, payload)
        self.audit_path.write_text(
            json.dumps(
                {"contact_uuid": "u1", "status": "FALHA_OU_RESULTADO_INDETERMINADO"}
            )
            + "\n",
            encoding="utf-8",
        )
        before_state = self.state_path.read_bytes()
        before_audit = self.audit_path.read_bytes()
        result = diagnose_state(self.state_path, self.audit_path)
        self.assertTrue(result["somente_leitura"])
        self.assertEqual(result["contatos_com_estado"], 2)
        self.assertEqual(result["inclusoes_pendentes_ou_indeterminadas"], 1)
        self.assertEqual(result["admissoes_confirmadas_localmente"], 1)
        self.assertEqual(
            result["eventos_auditoria"]["FALHA_OU_RESULTADO_INDETERMINADO"], 1
        )
        self.assertEqual(
            result["ultimas_pendencias_auditoria"][0]["contact_uuid"], "u1"
        )
        self.assertEqual(self.state_path.read_bytes(), before_state)
        self.assertEqual(self.audit_path.read_bytes(), before_audit)

    def test_ultimo_pedido_altered_or_invalid_is_ignored(self):
        from recompra import FIELDS

        contact, order, items = fixture(24)
        baseline = propose(contact, order, items, STRUCTURE, now=NOW)
        contact["fields"]["ultimo_pedido"] = "{JSON INVÁLIDO " + "X" * 2000
        amended = propose(contact, order, items, STRUCTURE, now=NOW)
        self.assertEqual(amended, baseline)
        self.assertEqual(FIELDS, {"pedido_data", "pedido_produtos"})
        self.assertNotIn("ultimo_pedido", amended["guard"]["fields"])
        self.assertNotIn("ultimo_pedido", amended["fields"])

    def test_ultimo_pedido_change_between_gets_does_not_block_admission(self):
        from recompra import apply_review

        contact, order, items = fixture(24)
        contact["fields"]["ultimo_pedido"] = "{JSON inválido}"
        state = load_state(self.state_path)
        plan = propose(contact, order, items, STRUCTURE, now=NOW)
        review = make_review([plan], STRUCTURE, now=NOW, state=state)
        current = deepcopy(contact)
        weni = Mock()
        times = {"get": 0}

        def get_contact(*, uuid):
            times["get"] += 1
            result = deepcopy(current)
            if times["get"] >= 2:
                result["fields"]["ultimo_pedido"] = "ALTERADO POR OUTRA INTEGRAÇÃO"
            return result

        weni.get_contact.side_effect = get_contact
        weni.update_contact_fields.side_effect = lambda uid, fields: current[
            "fields"
        ].update(fields)
        weni.contact_group_action.side_effect = lambda uid, *, action, group: current[
            "groups"
        ].append({"uuid": group, "name": GROUP})
        with (
            patch("recompra.inspect_structure", return_value=STRUCTURE),
            patch("recompra.resolve_latest", return_value=(order, items)),
        ):
            result = apply_review(
                review,
                weni,
                Mock(),
                state_path=self.state_path,
                audit_path=self.audit_path,
                now_fn=lambda: NOW,
            )
        self.assertEqual(result["aplicados"], 1)
        weni.contact_group_action.assert_called_once()
        self.assertNotIn("ultimo_pedido", weni.update_contact_fields.call_args.args[1])
        self.assertTrue(
            load_state(self.state_path)["contacts"]["u1"]["orders"]["100"]["admitted"]
        )

    def test_order_resolver_does_not_pass_ultimo_pedido_to_reused_service(self):
        contact, order, items = fixture(24)
        contact["fields"]["ultimo_pedido"] = '{"pedido_id":"999999"}'
        payload = {"cliente": {"id": 1}, "ultimoPedido": {"id": "100"}}
        farm = Mock()
        farm.get_cliente_by_cpf.return_value = payload
        from recompra import resolve_latest

        with (
            patch(
                "recompra.select_cliente_response",
                return_value=(("CPF_CONFIRMADO", "1", ""), payload),
            ),
            patch(
                "recompra.resolve_contact_order", return_value=(order, items)
            ) as resolver,
        ):
            result = resolve_latest(contact, farm, NOW.isoformat())
        self.assertEqual(result, (order, items))
        self.assertEqual(resolver.call_count, 1)
        self.assertNotIn("ultimo_pedido", resolver.call_args.args[0]["fields"])
