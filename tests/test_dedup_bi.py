"""Testes herméticos do reparo privado: nenhum acesso a APIs/credenciais."""

import json
import tempfile
import unittest
from copy import deepcopy
from datetime import date
from pathlib import Path
from unittest.mock import Mock, patch

import dedup_bi as repair


class RefreshGuardTests(unittest.TestCase):
    def test_financial_change_is_blocked_per_customer(self):
        old = [
            [
                "cliente_chave",
                "cliente",
                "pedidos_identificados",
                "faturamento_identificado",
                "atualizado_em",
            ],
            ["c1", "Nome antigo", 2, 99.9, "antes"],
        ]
        desired = [old[0], ["c1", "Nome correto", 2, 100.0, "depois"]]
        with self.assertRaisesRegex(ValueError, "fora do reparo"):
            repair.diff_table("BI_Clientes", old, desired, removed_uuids=set())

    def test_customer_name_repair_keeps_money_and_generates_cell_only_updates(self):
        old = [
            [
                "cliente_chave",
                "cliente",
                "pedidos_identificados",
                "faturamento_identificado",
                "atualizado_em",
            ],
            ["c1", "Nome antigo", 2, 99.9, "antes"],
        ]
        new = [old[0], ["c1", "Nome correto", 2, 99.9, "depois"]]
        before = deepcopy(old)
        plan = repair.diff_table("BI_Clientes", old, new, removed_uuids=set())
        self.assertEqual(old, before)
        self.assertEqual(
            {u["header"] for u in plan["updates"]}, {"cliente", "atualizado_em"}
        )
        self.assertEqual(plan["after"][1][3], 99.9)
        plan["sheet_id"] = 123
        requests = repair.requests_for({"tables": {"BI_Clientes": plan}})
        self.assertTrue(
            all(r["updateCells"]["fields"] == "userEnteredValue" for r in requests)
        )
        self.assertNotIn("99.9", str(requests))

    def test_timestamp_alone_does_not_rewrite_untouched_rows(self):
        old = [["cliente_chave", "cliente", "atualizado_em"], ["c1", "Nome", "antes"]]
        new = [old[0], ["c1", "Nome", "depois"]]
        plan = repair.diff_table("BI_Clientes", old, new, removed_uuids=set())
        self.assertEqual(plan["updates"], [])
        self.assertEqual(plan["before_hash"], plan["after_hash"])

    def test_retired_contact_hash_only_can_be_deleted(self):
        retired, kept = (
            repair.opaque_key("contato", "old"),
            repair.opaque_key("contato", "keep"),
        )
        old = [
            ["contato_chave", "nome", "atualizado_em"],
            [retired, "Duplicado", "antes"],
            [kept, "Correto", "antes"],
        ]
        new = [old[0], old[2]]
        plan = repair.diff_table("BI_Publico", old, new, removed_uuids={"old"})
        self.assertEqual(plan["deletions"], [2])
        self.assertEqual(plan["after"], new)
        with self.assertRaisesRegex(ValueError, "sem UUID"):
            repair.diff_table("BI_Publico", old, new, removed_uuids={"different"})

    def test_delete_dimension_descends_after_value_updates(self):
        keys = [repair.opaque_key("contato", u) for u in ("old1", "keep", "old2")]
        old = [
            ["contato_chave", "nome", "atualizado_em"],
            [keys[0], "Duplicado1", "antes"],
            [keys[1], "Nome antigo", "antes"],
            [keys[2], "Duplicado2", "antes"],
        ]
        new = [old[0], [keys[1], "Correto", "depois"]]
        plan = repair.diff_table("BI_Publico", old, new, removed_uuids={"old1", "old2"})
        self.assertEqual(plan["deletions"], [4, 2])
        self.assertEqual(plan["after"], new)
        plan["sheet_id"] = 1
        requests = repair.requests_for({"tables": {"BI_Publico": plan}})
        self.assertEqual(
            [
                r["deleteDimension"]["range"]["startIndex"]
                for r in requests
                if "deleteDimension" in r
            ],
            [3, 1],
        )
        self.assertIn("updateCells", requests[0])

    def test_historical_snapshots_are_not_recalculated(self):
        d1 = (date(2026, 10, 14) - date(1899, 12, 30)).days
        d2 = d1 + 1
        old = [
            [
                "data",
                "contatos_total",
                "pedidos_total",
                "faturamento_total",
                "atualizado_em",
            ],
            [d1, 100, 10, 1000, "antigo"],
            [d2, 110, 11, 1100, "antes"],
        ]
        new = [old[0], [d2, 109, 11, 1100, "depois"]]
        plan = repair.diff_table("BI_Resumo_Diario", old, new, removed_uuids=set())
        self.assertEqual(plan["after"][1], old[1])
        self.assertEqual({u["row"] for u in plan["updates"]}, {3})
        self.assertEqual(plan["deletions"], [])

    def test_summary_financial_change_and_missing_order_fact_are_blocked(self):
        old = [
            [
                "data",
                "contatos_total",
                "pedidos_total",
                "faturamento_total",
                "atualizado_em",
            ],
            [45000, 100, 10, 1000, "antes"],
        ]
        new = [old[0], [45000, 99, 10, 999, "depois"]]
        with self.assertRaisesRegex(ValueError, "financeiros"):
            repair.diff_table("BI_Resumo_Diario", old, new, removed_uuids=set())
        before = [
            ["pedido_chave", "cliente", "pedido_valor", "atualizado_em"],
            ["o1", "Pessoa", 100, "antes"],
        ]
        with self.assertRaisesRegex(ValueError, "financeiros"):
            repair.diff_table("BI_Pedidos", before, [before[0]], removed_uuids={"old"})

    def test_unchanged_numeric_cells_and_iso_dates_compare_equal(self):
        old = [
            ["pedido_chave", "data", "pedido_valor", "cliente", "atualizado_em"],
            ["o1", 46310, 100, "Pessoa", "antes"],
        ]
        # _typed_bi_rows produces the same date serial; a serial can also be a float.
        new = [old[0], ["o1", 46310.0, 100.0, "Pessoa", "depois"]]
        self.assertEqual(
            repair.diff_table("BI_Pedidos", old, new, removed_uuids=set())["updates"],
            [],
        )

    def test_new_financial_keys_and_duplicate_keys_are_blocked(self):
        old = [["pedido_chave", "cliente", "pedido_valor"], ["o1", "Pessoa", 100]]
        with self.assertRaisesRegex(ValueError, "Novas chaves"):
            repair.diff_table(
                "BI_Pedidos", old, old + [["o2", "Pessoa", 100]], removed_uuids=set()
            )
        with self.assertRaisesRegex(ValueError, "duplicada"):
            repair.diff_table("BI_Pedidos", old + [old[1]], old, removed_uuids=set())

    def test_removal_parser_and_nonfinite_numbers(self):
        self.assertEqual(repair.removed_set([{"duplicate_uuid": "old"}]), {"old"})
        self.assertEqual(repair.removed_set({"removed_uuids": ["old"]}), {"old"})
        with self.assertRaises(ValueError):
            repair.cell_value(float("inf"))
        self.assertEqual(
            repair.cell_value("=não executar"), {"stringValue": "=não executar"}
        )

    def test_numeric_hash_survives_integer_readback_and_preserves_text(self):
        self.assertEqual(
            repair.digest([["valor"], [100.0]]), repair.digest([["valor"], [100]])
        )
        self.assertNotEqual(
            repair.digest([["valor"], ["100"]]), repair.digest([["valor"], [100]])
        )
        self.assertNotEqual(
            repair.digest([["valor"], [True]]), repair.digest([["valor"], [1]])
        )

    def test_delivery_removal_requires_explicit_affected_order_key(self):
        old = [["pedido_chave", "pedido_valor", "atualizado_em"], ["o1", 99.9, "antes"]]
        with self.assertRaisesRegex(ValueError, "Delivery sem pedido"):
            repair.diff_table(
                "BI_Delivery_Pedidos", old, [old[0]], removed_uuids={"old"}
            )
        plan = repair.diff_table(
            "BI_Delivery_Pedidos",
            old,
            [old[0]],
            removed_uuids={"old"},
            allowed_delivery_removals={"o1"},
        )
        self.assertEqual(plan["deletions"], [2])

    def test_uncertain_send_is_never_repeated_even_when_old_state_remains_visible(self):
        current = {title: [["chave"], ["antes"]] for title in repair.TARGET_TITLES}
        plan = {
            "hub": "hub",
            "operations": "ops",
            "controls_hash": repair.digest([]),
            "sources_hash": repair.digest({}),
            "tables": {
                title: {
                    "before_hash": repair.digest(current[title]),
                    "after_hash": repair.digest([["chave"], ["depois"]]),
                }
                for title in repair.TARGET_TITLES
            },
        }
        hub = Mock()
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "bi_plan.json"
            path.write_text(json.dumps(plan), encoding="utf-8")
            (Path(folder) / "bi_receipt.json").write_text(
                '{"state":"sending"}', encoding="utf-8"
            )
            store = Mock()
            store.layout.dashboard, store.layout.operations = "hub", "ops"
            store.hub = hub
            store.books = {"ops": Mock()}
            with patch.object(repair, "read_state", return_value=({}, current, [], {})):
                with self.assertRaisesRegex(RuntimeError, "reenvio"):
                    repair.refresh(store, {"old"}, set(), folder)
        hub.batch_update.assert_not_called()

    def test_builder_uses_existing_cutoff_and_rejects_remaining_source_uuid(self):
        sources = {title: [["contact_uuid"]] for title in repair.SOURCE_TITLES}
        current = {
            "BI_Resumo_Diario": [["data"], ["2026-10-15"]],
            "BI_Qualidade": [["historico_observado_desde"], ["2026-10-10"]],
        }
        current.update(
            {
                title: list([repair.SCHEMAS[title]])
                for title in repair.DETAIL_TITLES
                if title != "BI_Qualidade"
            }
        )
        current["BI_Qualidade"] = [repair.SCHEMAS["BI_Qualidade"]]
        current.update(
            {
                "BI_Pedidos": [repair.BI_ORDERS_COLUMNS],
                "BI_Delivery_Pedidos": [repair.DELIVERY_ORDER_COLUMNS],
                "BI_Resumo_Diario": [repair.BI_SUMMARY_COLUMNS, ["2026-10-15"]],
                "BI_Canais": [repair.BI_CHANNEL_COLUMNS],
                "BI_Conversao": [repair.BI_CONVERSION_COLUMNS],
            }
        )
        desired, cutoff = repair.prepare_targets(
            sources,
            current,
            {},
            removed_uuids={"old"},
            updated_at="2026-10-16T00:00:00+00:00",
        )
        self.assertEqual(cutoff, "2026-10-15")
        self.assertEqual(set(desired), set(repair.TARGET_TITLES))
        sources["Base_Contatos"].append(["old"])
        with self.assertRaisesRegex(ValueError, "ainda consta"):
            repair.prepare_targets(
                sources,
                current,
                {},
                removed_uuids={"old"},
                updated_at="2026-10-16T00:00:00+00:00",
            )


if __name__ == "__main__":
    unittest.main()
