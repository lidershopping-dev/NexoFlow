"""Consolidação segura de páginas Weni com dados inteiramente sintéticos."""

import os
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import Mock, patch

from recompra import (
    audit_plan,
    build_plans,
    iter_prioritized_contacts,
    plan_needs_apply,
)
from tests.test_recompra import NOW, STRUCTURE, fixture


def contact(uid, cpf="52998224725"):
    result, _, _ = fixture()
    result["uuid"] = uid
    result["fields"]["cpf"] = cpf
    return result


class RecompraPaginationTests(unittest.TestCase):
    def test_marketing_refusal_removes_without_any_farmalider_queries(self):
        for marketing, opt_out in (("recusado", False), ("true", True)):
            with self.subTest(marketing=marketing, opt_out=opt_out):
                current, _, _ = fixture(grouped=True, marketing=marketing)
                if opt_out:
                    current["groups"].append(
                        {"uuid": STRUCTURE["opt_out_uuid"], "name": "Opt-Out Marketing"}
                    )
                before = deepcopy(current)
                farm = Mock()
                plans = build_plans(
                    [current], farm, STRUCTURE, {"contacts": {}}, now=NOW
                )
                self.assertEqual(len(plans), 1)
                plan = plans[0]
                self.assertEqual(plan["reason"], "RECUSA_MARKETING")
                self.assertEqual(plan["action"], "remove")
                self.assertEqual(plan["fields"], {})
                self.assertIsNone(plan["purchase"])
                self.assertEqual(plan["order_resolution_status"], "")
                self.assertEqual(farm.mock_calls, [])
                self.assertEqual(current, before)

    def test_marketing_refusal_outside_group_needs_no_api_or_application(self):
        current, _, _ = fixture(marketing="não")
        farm = Mock()
        plans = build_plans([current], farm, STRUCTURE, {"contacts": {}}, now=NOW)
        self.assertEqual(plans[0]["reason"], "RECUSA_MARKETING")
        self.assertFalse(plan_needs_apply(plans[0]))
        self.assertEqual(farm.mock_calls, [])

    def test_identical_uuid_is_resolved_once_and_counted_before_the_api(self):
        original = contact("sintetico")
        reordered = dict(reversed(list(original.items())))
        contacts = [original, deepcopy(original), reordered]
        before = deepcopy(contacts)
        farm = Mock()
        farm.get_cliente_by_cpf.return_value = None
        stats = {}
        plans = build_plans(
            contacts,
            farm,
            STRUCTURE,
            {"contacts": {}},
            now=NOW,
            pagination_stats=stats,
        )
        self.assertEqual([plan["contact_uuid"] for plan in plans], ["sintetico"])
        farm.get_cliente_by_cpf.assert_called_once_with("52998224725")
        farm.get_pedido.assert_not_called()
        self.assertEqual(contacts, before)
        self.assertEqual(
            stats,
            {
                "contatos_recebidos": 3,
                "uuids_unicos": 1,
                "duplicatas_identicas": 2,
                "repeticoes_divergentes": 0,
                "uuids_divergentes": 0,
            },
        )

    def test_late_conflict_never_resolves_either_identity_and_other_contacts_continue(
        self,
    ):
        first = contact("divergente")
        later = deepcopy(first)
        later["fields"]["cpf"] = "11111111111"
        later["name"] = "Outra identidade sintética"
        healthy = [contact(f"normal-{index}") for index in range(9)]
        stats = {}
        source_complete = False

        def source():
            nonlocal source_complete
            yield first
            yield from healthy
            yield later
            yield deepcopy(first)
            source_complete = True

        def resolve(current, *_args):
            self.assertTrue(source_complete)
            self.assertNotEqual(current["uuid"], "divergente")
            return None, []

        saved = {
            "contacts": {
                "divergente": {
                    "purchase": {"pedido_id": "100"},
                    "orders": {"100": {"pending": True}},
                }
            }
        }
        with patch(
            "recompra._resolve_checked_contact", side_effect=resolve
        ) as resolver:
            plans = build_plans(
                source(),
                Mock(),
                STRUCTURE,
                saved,
                now=NOW,
                workers=4,
                pagination_stats=stats,
            )
        self.assertEqual(resolver.call_count, len(healthy))
        self.assertEqual(len(plans), len(healthy) + 1)
        blocked = next(plan for plan in plans if plan["contact_uuid"] == "divergente")
        self.assertEqual(blocked["reason"], "CONTATO_DUPLICADO_DIVERGENTE")
        self.assertEqual(blocked["action"], "")
        self.assertEqual(blocked["fields"], {})
        self.assertIsNone(blocked["purchase"])
        self.assertEqual(blocked["order_id"], "")
        self.assertEqual(blocked["guard"]["fields"]["cpf"], "")
        self.assertEqual(blocked["guard"]["name"], "")
        self.assertFalse(plan_needs_apply(blocked, saved["contacts"]["divergente"]))
        audit_plan(blocked)
        self.assertEqual(stats["uuids_divergentes"], 1)
        self.assertEqual(stats["repeticoes_divergentes"], 1)
        self.assertEqual(stats["duplicatas_identicas"], 1)

    def test_any_conflicting_guard_field_blocks_the_uuid_without_api(self):
        for key, value in (
            ("name", "Outro primeiro nome"),
            ("urns", ["tel:+5591888888888"]),
            ("groups", [{"uuid": "g-out", "name": "Opt-Out Marketing"}]),
            ("fields", {"cpf": "52998224725", "marketing_opt_in": "recusado"}),
        ):
            with self.subTest(key=key):
                first = contact("divergente")
                second = deepcopy(first)
                second[key] = value
                farm = Mock()
                plans = build_plans(
                    [first, second], farm, STRUCTURE, {"contacts": {}}, now=NOW
                )
                self.assertEqual(len(plans), 1)
                self.assertEqual(plans[0]["reason"], "CONTATO_DUPLICADO_DIVERGENTE")
                self.assertFalse(plan_needs_apply(plans[0]))
                self.assertEqual(farm.mock_calls, [])

    def test_cpf_priority_and_first_position_are_preserved_for_unique_uuids(self):
        source = [
            contact("zero", "0"),
            contact("vazio", ""),
            contact("valido-1"),
            contact("invalido", "11111111111"),
            contact("valido-2"),
        ]
        source.insert(1, deepcopy(source[2]))
        actual = list(iter_prioritized_contacts(iter(source)))
        self.assertEqual(
            [entry["uuid"] for entry in actual],
            ["valido-1", "valido-2", "zero", "invalido", "vazio"],
        )

    def test_missing_uuid_blocks_all_api_reads_even_after_valid_contacts(self):
        farm = Mock()
        with self.assertRaisesRegex(RuntimeError, "sem UUID"):
            build_plans(
                [contact("valido"), {"fields": {"cpf": "52998224725"}}],
                farm,
                STRUCTURE,
                {"contacts": {}},
                now=NOW,
            )
        self.assertEqual(farm.mock_calls, [])

    def test_source_failure_removes_private_spool_without_starting_the_api(self):
        directories = []
        real_directory = tempfile.TemporaryDirectory

        def directory(*args, **kwargs):
            result = real_directory(*args, **kwargs)
            directories.append(Path(result.name))
            return result

        def source():
            yield contact("valido")
            raise RuntimeError("Falha sintética ao receber página posterior")

        farm = Mock()
        with (
            patch("recompra.tempfile.TemporaryDirectory", side_effect=directory),
            self.assertRaisesRegex(RuntimeError, "página posterior"),
        ):
            build_plans(source(), farm, STRUCTURE, {"contacts": {}}, now=NOW)
        self.assertEqual(farm.mock_calls, [])
        self.assertEqual(len(directories), 1)
        self.assertFalse(directories[0].exists())

    @unittest.skipIf(os.name == "nt", "Permissões POSIX não se aplicam ao Windows.")
    def test_sqlite_spool_has_private_permissions_before_receiving_customer_data(self):
        import sqlite3

        connect = sqlite3.connect
        observed = []

        def inspect(path):
            path = Path(path)
            observed.append(path)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(path.parent.stat().st_mode & 0o777, 0o700)
            return connect(path)

        with patch("recompra.sqlite3.connect", side_effect=inspect):
            self.assertEqual(
                len(list(iter_prioritized_contacts([contact("unico")]))), 1
            )
        self.assertEqual(len(observed), 1)
        self.assertFalse(observed[0].exists())


if __name__ == "__main__":
    unittest.main()
