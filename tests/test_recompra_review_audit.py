"""Resumo privado/agregado da recompra com dados inteiramente sintéticos."""

import json
import unittest
from copy import deepcopy

from recompra_review_audit import build_review_audit, public_review_lines


def plan(uid, *, action="", fields=None, reason="JANELA_0_A_25", **kwargs):
    return {
        "contact_uuid": uid,
        "action": action,
        "fields": fields or {},
        "reason": reason,
        "guard": {"fields": {}, "groups": []},
        "within_window": reason == "JANELA_0_A_25",
        "scheduled_future": True,
        "campaign_products": [],
        "warnings": [],
        **kwargs,
    }


class RecompraReviewAuditTests(unittest.TestCase):
    def test_actions_and_field_updates_are_explicit_and_do_not_double_count_contacts(
        self,
    ):
        plans = [
            plan("synthetic-add", action="add", fields={"pedido_data": "new"}),
            plan("synthetic-remove", action="remove", reason="RECUSA_MARKETING"),
            plan("synthetic-replace", action="replace", fields={"pedido_data": "new"}),
            plan("synthetic-update", fields={"pedido_produtos": "SYNTHETIC_PRODUCT"}),
            plan("synthetic-noop"),
        ]
        untouched = deepcopy(plans)
        report = build_review_audit(iter(plans))
        self.assertEqual(
            report["actions"],
            {"add": 1, "remove": 1, "replace": 1, "none": 2, "unknown": 0},
        )
        self.assertEqual(report["fields_updates"], 3)
        self.assertEqual(report["planned_changes"], 4)
        self.assertEqual(report["reviewed_unique"], 5)
        self.assertEqual(report["within_window"], 4)
        self.assertEqual(plans, untouched)

    def test_identical_uuid_is_counted_once_and_divergence_is_explicit(self):
        first = plan("synthetic-a", action="add")
        second = plan("synthetic-b", reason="MAIS_DE_25_DIAS")
        changed = deepcopy(second)
        changed["action"] = "remove"
        report = build_review_audit([first, deepcopy(first), second, changed])
        self.assertEqual(report["reviewed_rows"], 4)
        self.assertEqual(report["reviewed_unique"], 2)
        self.assertEqual(report["duplicate_rows"], 2)
        self.assertEqual(report["conflicting_duplicate_uuids"], 1)
        self.assertEqual(report["actions"]["add"], 1)
        self.assertEqual(report["actions"]["remove"], 0)
        self.assertNotIn("synthetic-a", json.dumps(report))
        self.assertNotIn("synthetic-b", json.dumps(report))

    def test_marketing_values_do_not_infer_authorization_from_empty_fields(self):
        values = ("", " ATIVO ", True, "Não", False, "0", "SYNTHETIC_UNKNOWN")
        plans = []
        for index, value in enumerate(values):
            plans.append(
                plan(
                    f"synthetic-{index}",
                    guard={
                        "fields": {"marketing_opt_in": value},
                        "groups": ["synthetic-opt-out"] if index == 2 else [],
                    },
                )
            )
        report = build_review_audit(
            plans, structure={"opt_out_uuid": "synthetic-opt-out"}
        )
        self.assertEqual(
            report["marketing_within_window"],
            {
                "empty": 1,
                "affirmative_value": 2,
                "refused_value": 3,
                "other_value": 1,
                "opt_out_group": 1,
                "opt_out_with_affirmative_value": 1,
            },
        )
        lines = "\n".join(
            public_review_lines(
                {"plans": plans, "structure": {"opt_out_uuid": "synthetic-opt-out"}}
            )
        )
        self.assertIn("Campo vazio não comprova consentimento", lines)
        self.assertIn("não representa mensagens enviadas", lines)
        self.assertNotIn("SYNTHETIC_UNKNOWN", lines)
        self.assertNotIn("synthetic-opt-out", lines)

    def test_opt_out_name_and_mapping_groups_are_counted_even_without_structure(self):
        plans = [
            plan(
                "synthetic-a",
                reason="RECUSA_MARKETING",
                guard={
                    "fields": {"marketing_opt_in": "true"},
                    "groups": [{"name": "Opt-Out Marketing"}],
                },
            ),
            plan(
                "synthetic-b",
                reason="RECUSA_MARKETING",
                guard={
                    "fields": {"marketing_opt_in": "ativo"},
                    "groups": ["Opt-Out Marketing"],
                },
            ),
        ]
        report = build_review_audit(plans)
        self.assertEqual(report["marketing"]["opt_out_with_affirmative_value"], 2)
        self.assertEqual(report["marketing_within_window"]["affirmative_value"], 0)

    def test_pending_counts_separate_invalid_cadastro_and_actual_api_rejection(self):
        report = build_review_audit(
            [
                plan("synthetic-zero", reason="CPF_INVALIDO_CADASTRO"),
                plan("synthetic-http", reason="CPF_REJEITADO_API"),
                plan("synthetic-later", reason="PEDIDO_POSTERIOR_PENDENTE_API"),
                plan("synthetic-http-error", reason="ERRO_ITENS_PEDIDO_API"),
                plan("synthetic-no-order", reason="SEM_PEDIDO_CONFIRMADO"),
                plan("synthetic-cancelled", reason="PEDIDO_STATUS_3_CANCELADO"),
            ]
        )
        self.assertEqual(report["cpf_cadastro_pending"], 1)
        self.assertEqual(
            report["pending_reasons"],
            {
                "CPF_INVALIDO_CADASTRO": 1,
                "CPF_REJEITADO_API": 1,
                "ERRO_ITENS_PEDIDO_API": 1,
                "PEDIDO_POSTERIOR_PENDENTE_API": 1,
            },
        )

    def test_product_categories_are_observed_without_changing_eligibility(self):
        report = build_review_audit(
            [
                plan(
                    "synthetic-a",
                    campaign_products=[
                        {"produto_nome": "SYNTHETIC_MEDICINE", "categoria": ""},
                        {"produto_nome": "SYNTHETIC_CREAM", "categoria": "Humano"},
                        {"produto_nome": "SYNTHETIC_CAPSULE"},
                    ],
                ),
                plan(
                    "synthetic-old",
                    reason="MAIS_DE_25_DIAS",
                    campaign_products=[{"produto_nome": "SYNTHETIC_OLD_PRODUCT"}],
                ),
            ]
        )
        self.assertEqual(report["products"], {"rows": 4, "category_empty": 3})
        self.assertEqual(
            report["products_within_window"], {"rows": 3, "category_empty": 2}
        )
        self.assertEqual(report["within_window"], 1)
        self.assertNotIn("SYNTHETIC_MEDICINE", json.dumps(report))

    def test_warning_classes_and_unknown_reasons_never_echo_free_text_or_identity(self):
        plans = [
            plan(
                "SYNTHETIC_UUID_PRIVATE",
                reason="SYNTHETIC_CUSTOMER_PRIVATE",
                guard={
                    "fields": {
                        "cpf": "SYNTHETIC_CPF_PRIVATE",
                        "nome": "SYNTHETIC_NAME_PRIVATE",
                        "marketing_opt_in": "SYNTHETIC_MARKETING_PRIVATE",
                    },
                    "groups": [],
                },
                warnings=[
                    "STATUS_WENI_DIVERGENTE_API: SYNTHETIC_PRIVATE_PAYLOAD",
                    "STATUS_WENI_DIVERGENTE_API: SYNTHETIC_SECOND_PAYLOAD",
                    "SYNTHETIC_UNCLASSIFIED_PRIVATE",
                ],
            )
        ]
        report = build_review_audit(plans)
        self.assertEqual(report["reasons"], {"OUTRO_MOTIVO": 1})
        self.assertEqual(
            report["warning_classes"],
            {"OUTRO_AVISO": 1, "STATUS_WENI_DIVERGENTE_API": 1},
        )
        self.assertNotIn("SYNTHETIC_", json.dumps(report))
        lines = "\n".join(public_review_lines({"plans": plans}))
        self.assertNotIn("SYNTHETIC_", lines)
        self.assertIn("STATUS_WENI_DIVERGENTE_API=1", lines)

    def test_expired_admission_is_flagged_without_turning_summary_into_authorization(
        self,
    ):
        reviewed = plan("synthetic-a", action="add", scheduled_future=False)
        report = build_review_audit([reviewed])
        self.assertEqual(report["admissions_without_future_event"], 1)
        lines = "\n".join(public_review_lines({"plans": [reviewed]}))
        self.assertIn("sem evento futuro: 1", lines)
        self.assertIn("não autoriza sua aplicação", lines)
        self.assertEqual(reviewed["action"], "add")

    def test_empty_review_and_missing_uuid_remain_visible(self):
        empty = build_review_audit([])
        self.assertEqual(empty["reviewed_unique"], 0)
        self.assertEqual(empty["marketing"]["empty"], 0)
        self.assertTrue(public_review_lines({"plans": []}))
        malformed = build_review_audit([plan(""), plan("")])
        self.assertEqual(malformed["missing_uuid"], 2)
        self.assertEqual(malformed["reviewed_unique"], 2)
        self.assertEqual(malformed["duplicate_rows"], 0)


if __name__ == "__main__":
    unittest.main()
