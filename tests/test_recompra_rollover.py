"""Expiração de revisão à meia-noite, com relógio e serviços sintéticos."""

import json
import tempfile
import unittest
from contextlib import ExitStack
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import Mock, patch

import requests

import recompra
from run_recompra import main as run_recompra
from tests.test_recompra import STRUCTURE, fixture
from weni_client import WeniClient, WeniError

BEFORE = datetime(2026, 10, 9, 23, 59, 59, tzinfo=recompra.TZ)
AFTER = datetime(2026, 10, 10, 0, 0, 1, tzinfo=recompra.TZ)
EMPTY_STATE = {"schema": 1, "contacts": {}}


class MutableClock:
    def __init__(self, now=BEFORE):
        self.now = now

    def __call__(self):
        return self.now

    def cross_midnight(self):
        self.now = AFTER


class RecompraReviewDateTests(unittest.TestCase):
    def review(self):
        return recompra.make_review([], STRUCTURE, now=BEFORE, state=EMPTY_STATE)

    def test_current_review_uses_business_date_even_when_utc_day_already_changed(self):
        review = self.review()
        self.assertEqual(BEFORE.astimezone(UTC).date().isoformat(), "2026-10-10")
        recompra.ensure_review_current(review, BEFORE.astimezone(UTC))

    def test_previous_product_policy_review_is_rejected_even_with_valid_digest(self):
        review = self.review()
        review["policy"] = review["policy"].replace(
            "human_products_v3_", "human_products_v2_"
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "review.json"
            path.write_text(
                json.dumps({"review": review, "sha256": recompra.digest(review)}),
                encoding="utf-8",
            )
            with self.assertRaises(RuntimeError):
                recompra.read_review(path)

    def test_expired_review_has_specific_error_and_refresh_permission(self):
        self.assertEqual(recompra.REVIEW_EXPIRED_EXIT_CODE, 4)
        for allow_refresh in (True, False):
            with self.subTest(allow_refresh=allow_refresh):
                with self.assertRaises(recompra.ReviewExpiredError) as raised:
                    recompra.ensure_review_current(
                        self.review(), AFTER, allow_refresh=allow_refresh
                    )
                self.assertIsInstance(raised.exception, RuntimeError)
                self.assertIs(raised.exception.allow_refresh, allow_refresh)


class RecompraApplyRolloverTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.state_path = Path(temporary.name) / "state.json"
        self.audit_path = Path(temporary.name) / "audit.jsonl"
        self.clock = MutableClock()
        self.weni = Mock()
        self.farm = Mock()
        self.live = {}
        self.orders = {}
        self.hooks = {}

        def hook(name):
            callback = self.hooks.get(name)
            if callback:
                callback()

        def get_contact(*, uuid):
            result = deepcopy(self.live[uuid])
            hook("get:" + uuid)
            return result

        def update_fields(uid, fields):
            self.live[uid]["fields"].update(fields)
            hook("fields:" + uid)

        def group_action(uid, *, action, group):
            groups = self.live[uid]["groups"]
            if action == "remove":
                self.live[uid]["groups"] = [
                    value for value in groups if value["uuid"] != group
                ]
            else:
                groups.append({"uuid": group, "name": recompra.GROUP})
            hook(action + ":" + uid)

        def resolve(current, reads, captured_at, *, saved):
            result = deepcopy(self.orders[current["uuid"]])
            hook("resolve:" + current["uuid"])
            return result

        self.weni.get_contact.side_effect = get_contact
        self.weni.update_contact_fields.side_effect = update_fields
        self.weni.contact_group_action.side_effect = group_action
        self.resolve = resolve

    def prepare(self, *, grouped=False, days=24, count=1):
        plans = []
        for index in range(1, count + 1):
            contact, order, items = fixture(
                days, grouped=grouped, order_id=str(99 + index)
            )
            uid = f"u{index}"
            contact["uuid"] = uid
            self.live[uid] = deepcopy(contact)
            self.orders[uid] = (deepcopy(order), deepcopy(items))
            plans.append(recompra.propose(contact, order, items, STRUCTURE, now=BEFORE))
        return recompra.make_review(plans, STRUCTURE, now=BEFORE, state=EMPTY_STATE)

    def apply(self, review):
        with (
            patch("recompra.inspect_structure", return_value=STRUCTURE),
            patch("recompra.resolve_latest", side_effect=self.resolve),
        ):
            return recompra.apply_review(
                review,
                self.weni,
                self.farm,
                state_path=self.state_path,
                audit_path=self.audit_path,
                now_fn=self.clock,
            )

    def assert_expired(self, review, *, allow_refresh):
        with self.assertRaises(recompra.ReviewExpiredError) as raised:
            self.apply(review)
        self.assertIs(raised.exception.allow_refresh, allow_refresh)

    def assert_no_mutations(self):
        self.weni.contact_group_action.assert_not_called()
        self.weni.update_contact_fields.assert_not_called()
        self.assertFalse(self.state_path.exists())

    def test_expired_at_entry_stops_before_network_or_state_load(self):
        review = self.prepare()
        self.clock.cross_midnight()
        with (
            patch("recompra.inspect_structure") as inspect,
            patch("recompra.load_state") as load,
        ):
            self.assert_expired(review, allow_refresh=True)
        inspect.assert_not_called()
        load.assert_not_called()
        self.weni.get_contact.assert_not_called()
        self.assert_no_mutations()

    def test_transient_confirmation_file_lock_does_not_repeat_group_post(self):
        review = self.prepare()
        original = recompra.os.replace
        failures = []

        def replace(source, destination):
            payload = json.loads(Path(source).read_text(encoding="utf-8"))
            admitted = (
                payload.get("contacts", {})
                .get("u1", {})
                .get("orders", {})
                .get("100", {})
                .get("admitted")
            )
            if admitted and len(failures) < 2:
                failures.append(True)
                raise PermissionError(13, "bloqueio sintético")
            original(source, destination)

        with (
            patch("recompra.os.replace", side_effect=replace),
            patch("recompra.sleep"),
        ):
            result = self.apply(review)
        self.assertEqual(result["aplicados"], 1)
        self.assertEqual(len(failures), 2)
        self.weni.contact_group_action.assert_called_once_with(
            "u1", action="add", group=STRUCTURE["group_uuid"]
        )
        entry = recompra.load_state(self.state_path)["contacts"]["u1"]["orders"]["100"]
        self.assertTrue(entry["admitted"])
        self.assertFalse(entry["pending"])

    def test_persistent_intent_file_lock_blocks_remote_mutations(self):
        review = self.prepare()
        with (
            patch("recompra.os.replace", side_effect=PermissionError(13, "bloqueio")),
            patch("recompra.sleep"),
        ):
            with self.assertRaises(PermissionError):
                self.apply(review)
        self.assert_no_mutations()

    def test_persistent_confirmation_lock_preserves_pending_without_repeating_post(
        self,
    ):
        review = self.prepare()
        original = recompra.os.replace

        def replace(source, destination):
            payload = json.loads(Path(source).read_text(encoding="utf-8"))
            entry = (
                payload.get("contacts", {})
                .get("u1", {})
                .get("orders", {})
                .get("100", {})
            )
            if entry.get("admitted"):
                raise PermissionError(13, "bloqueio sintético")
            original(source, destination)

        with (
            patch("recompra.os.replace", side_effect=replace),
            patch("recompra.sleep"),
        ):
            with self.assertRaises(PermissionError):
                self.apply(review)
        self.weni.contact_group_action.assert_called_once_with(
            "u1", action="add", group=STRUCTURE["group_uuid"]
        )
        entry = recompra.load_state(self.state_path)["contacts"]["u1"]["orders"]["100"]
        self.assertTrue(entry["pending"])
        self.assertFalse(entry.get("admitted", False))

    def test_structure_inspection_crosses_midnight_before_contact_queries(self):
        review = self.prepare()

        def inspect(client):
            self.clock.cross_midnight()
            return STRUCTURE

        with (
            patch("recompra.inspect_structure", side_effect=inspect),
            patch("recompra.resolve_latest") as resolve,
        ):
            with self.assertRaises(recompra.ReviewExpiredError) as raised:
                recompra.apply_review(
                    review,
                    self.weni,
                    self.farm,
                    state_path=self.state_path,
                    audit_path=self.audit_path,
                    now_fn=self.clock,
                )
        self.assertTrue(raised.exception.allow_refresh)
        resolve.assert_not_called()
        self.weni.get_contact.assert_not_called()
        self.assert_no_mutations()

    def test_state_load_crosses_midnight_before_contact_queries(self):
        review = self.prepare()
        original = recompra.load_state

        def load(path):
            result = original(path)
            self.clock.cross_midnight()
            return result

        with patch("recompra.load_state", side_effect=load):
            self.assert_expired(review, allow_refresh=True)
        self.weni.get_contact.assert_not_called()
        self.assert_no_mutations()

    def test_initial_contact_get_crosses_midnight_without_resolving_order(self):
        review = self.prepare()
        self.hooks["get:u1"] = self.clock.cross_midnight
        with patch("recompra.resolve_latest") as resolve:
            with patch("recompra.inspect_structure", return_value=STRUCTURE):
                with self.assertRaises(recompra.ReviewExpiredError) as raised:
                    recompra.apply_review(
                        review,
                        self.weni,
                        self.farm,
                        state_path=self.state_path,
                        audit_path=self.audit_path,
                        now_fn=self.clock,
                    )
        self.assertTrue(raised.exception.allow_refresh)
        resolve.assert_not_called()
        self.assert_no_mutations()

    def test_order_resolution_crosses_midnight_before_any_state_or_post(self):
        review = self.prepare()
        self.hooks["resolve:u1"] = self.clock.cross_midnight
        self.assert_expired(review, allow_refresh=True)
        self.assert_no_mutations()

    def test_plan_calculation_crosses_midnight_before_persisting_intention(self):
        review = self.prepare()
        original = recompra.propose

        def propose(*args, **kwargs):
            result = original(*args, **kwargs)
            self.clock.cross_midnight()
            return result

        with patch("recompra.propose", side_effect=propose):
            self.assert_expired(review, allow_refresh=True)
        self.assert_no_mutations()

    def test_pending_checkpoint_crosses_midnight_blocks_all_posts_and_refresh(self):
        review = self.prepare()
        original = recompra.atomic_json

        def persist(path, state):
            original(path, state)
            self.clock.cross_midnight()

        with patch("recompra.atomic_json", side_effect=persist):
            self.assert_expired(review, allow_refresh=False)
        self.weni.update_contact_fields.assert_not_called()
        self.weni.contact_group_action.assert_not_called()
        state = recompra.load_state(self.state_path)
        self.assertTrue(state["contacts"]["u1"]["orders"]["100"]["pending"])

    def test_field_post_crosses_midnight_does_not_admit_contact(self):
        review = self.prepare()
        self.hooks["fields:u1"] = self.clock.cross_midnight
        self.assert_expired(review, allow_refresh=False)
        self.weni.update_contact_fields.assert_called_once()
        self.weni.contact_group_action.assert_not_called()
        state = recompra.load_state(self.state_path)
        self.assertTrue(state["contacts"]["u1"]["orders"]["100"]["pending"])

    def test_removal_post_crosses_midnight_blocks_replacement_fields_and_add(self):
        review = self.prepare(grouped=True, days=0)
        self.assertEqual(review["plans"][0]["action"], "replace")
        self.hooks["remove:u1"] = self.clock.cross_midnight
        self.assert_expired(review, allow_refresh=False)
        self.weni.update_contact_fields.assert_not_called()
        self.weni.contact_group_action.assert_called_once_with(
            "u1", action="remove", group=STRUCTURE["group_uuid"]
        )

    def test_final_get_crosses_midnight_blocks_admission(self):
        review = self.prepare()

        def fields_written():
            self.hooks["get:u1"] = self.clock.cross_midnight

        self.hooks["fields:u1"] = fields_written
        self.assert_expired(review, allow_refresh=False)
        self.weni.update_contact_fields.assert_called_once()
        self.weni.contact_group_action.assert_not_called()

    def test_successful_admission_after_midnight_is_saved_before_stop(self):
        review = self.prepare()
        self.hooks["add:u1"] = self.clock.cross_midnight
        self.assert_expired(review, allow_refresh=False)
        state = recompra.load_state(self.state_path)
        entry = state["contacts"]["u1"]["orders"]["100"]
        self.assertTrue(entry["admitted"])
        self.assertFalse(entry["pending"])
        self.assertEqual(entry["admitted_at"], AFTER.isoformat())
        audit = [
            json.loads(line)
            for line in self.audit_path.read_text(encoding="utf-8").splitlines()
        ]
        self.assertIn("APLICADO", [event["status"] for event in audit])
        self.assertNotIn(
            "FALHA_OU_RESULTADO_INDETERMINADO", [event["status"] for event in audit]
        )
        retry = recompra.propose(
            self.live["u1"],
            *self.orders["u1"],
            STRUCTURE,
            state["contacts"]["u1"],
            now=AFTER,
        )
        self.assertNotIn(retry["action"], ("add", "replace"))

    def test_day_changes_after_first_contact_is_saved_second_is_not_started(self):
        review = self.prepare(count=2)
        original = recompra.append_audit

        def audit(event, path):
            original(event, path)
            if event.get("status") == "APLICADO" and event.get("contact_uuid") == "u1":
                self.clock.cross_midnight()

        with patch("recompra.append_audit", side_effect=audit):
            self.assert_expired(review, allow_refresh=False)
        state = recompra.load_state(self.state_path)
        self.assertTrue(state["contacts"]["u1"]["orders"]["100"]["admitted"])
        self.assertNotIn("u2", state["contacts"])
        self.assertNotIn(
            "u2", [call.kwargs["uuid"] for call in self.weni.get_contact.call_args_list]
        )


class RecompraRunnerRolloverTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.review_path = Path(temporary.name) / "review.json"
        self.clock = MutableClock()
        self.weni = Mock()
        self.farm = Mock()
        self.review = recompra.make_review([], STRUCTURE, now=BEFORE, state=EMPTY_STATE)
        recompra.write_review(self.review, self.review_path)

    def patches(self):
        stack = ExitStack()
        self.addCleanup(stack.close)
        self.proxy = stack.enter_context(patch("proxy_config.configure_proxy"))
        stack.enter_context(patch("logging_config.configure_logging"))
        self.weni_factory = stack.enter_context(
            patch("weni_client.WeniClient", return_value=self.weni)
        )
        self.farm_factory = stack.enter_context(
            patch("weni_update._open_farmalider", return_value=self.farm)
        )
        self.inspect = stack.enter_context(
            patch("recompra.inspect_structure", return_value=STRUCTURE)
        )
        stack.enter_context(patch("recompra.now_local", side_effect=self.clock))
        stack.enter_context(
            patch("secrets_config.WENI_API_TOKEN", "TOKEN_SINTETICO_WENI", create=True)
        )
        stack.enter_context(patch("builtins.print"))
        return stack

    def assert_no_remote_work(self):
        self.proxy.assert_not_called()
        self.weni_factory.assert_not_called()
        self.farm_factory.assert_not_called()
        self.inspect.assert_not_called()

    def test_local_check_of_current_review_does_not_initialize_services(self):
        self.patches()
        self.assertEqual(
            run_recompra(["--check-review", "--require-review", str(self.review_path)]),
            0,
        )
        self.assert_no_remote_work()

    def test_local_check_of_expired_review_returns_refresh_code_without_services(self):
        self.clock.cross_midnight()
        self.patches()
        self.assertEqual(
            run_recompra(["--check-review", "--require-review", str(self.review_path)]),
            4,
        )
        self.assert_no_remote_work()

    def test_local_check_rejects_tampered_review_and_never_refreshes_invalid_data(self):
        data = json.loads(self.review_path.read_text(encoding="utf-8"))
        data["review"]["day"] = "2026-10-10"
        self.review_path.write_text(json.dumps(data), encoding="utf-8")
        self.patches()
        self.assertEqual(
            run_recompra(["--check-review", "--require-review", str(self.review_path)]),
            1,
        )
        self.assert_no_remote_work()

    def test_expired_apply_is_detected_locally_before_initializing_apis(self):
        self.clock.cross_midnight()
        self.patches()
        self.assertEqual(
            run_recompra(["--apply", "--require-review", str(self.review_path)]), 4
        )
        self.assert_no_remote_work()

    def test_apply_expiration_exit_code_distinguishes_partial_work(self):
        self.patches()
        for allow_refresh, expected in ((True, 4), (False, 1)):
            with self.subTest(allow_refresh=allow_refresh):
                error = recompra.ReviewExpiredError(
                    "2026-10-09", "2026-10-10", allow_refresh=allow_refresh
                )
                with patch("recompra.apply_review", side_effect=error):
                    self.assertEqual(
                        run_recompra(
                            ["--apply", "--require-review", str(self.review_path)]
                        ),
                        expected,
                    )
        self.assertEqual(self.weni.close.call_count, 2)
        self.assertEqual(self.farm.close.call_count, 2)

    def test_recompra_disables_automatic_retries_for_weni_mutations(self):
        self.patches()
        with patch("recompra.apply_review", return_value={}):
            self.assertEqual(
                run_recompra(["--apply", "--require-review", str(self.review_path)]), 0
            )
        self.weni_factory.assert_called_once_with(
            "TOKEN_SINTETICO_WENI", retry_mutations=False
        )

    def test_missing_review_returns_invalid_code_without_services(self):
        self.review_path.unlink()
        self.patches()
        self.assertEqual(
            run_recompra(["--check-review", "--require-review", str(self.review_path)]),
            1,
        )
        self.assert_no_remote_work()

    def test_indeterminate_api_failure_never_requests_automatic_refresh(self):
        self.patches()
        with patch(
            "recompra.apply_review",
            side_effect=RuntimeError("Estado preservado; inclusão incerta."),
        ):
            self.assertEqual(
                run_recompra(["--apply", "--require-review", str(self.review_path)]), 1
            )

    def test_full_dry_run_crossing_midnight_does_not_write_or_relabel_snapshot(self):
        stack = self.patches()
        contact, order, items = fixture(24)
        self.weni.iter_contacts.return_value = iter([contact])
        stack.enter_context(
            patch("recompra.load_state", return_value=deepcopy(EMPTY_STATE))
        )
        plans = [recompra.propose(contact, order, items, STRUCTURE, now=BEFORE)]

        def build(*args, **kwargs):
            self.assertEqual(kwargs["now"], BEFORE)
            self.clock.cross_midnight()
            return plans

        stack.enter_context(patch("recompra.build_plans", side_effect=build))
        writer = stack.enter_context(patch("recompra.write_review"))
        reviewer = stack.enter_context(
            patch("recompra.make_review", wraps=recompra.make_review)
        )
        self.assertEqual(
            run_recompra(
                ["--dry-run", "--full", "--review-out", str(self.review_path)]
            ),
            4,
        )
        writer.assert_not_called()
        for call in reviewer.call_args_list:
            self.assertEqual(call.kwargs["now"], BEFORE)
        self.assertEqual(recompra.read_review(self.review_path), self.review)
        self.weni.close.assert_called_once()
        self.farm.close.assert_called_once()

    def test_local_permission_error_is_not_reported_as_api_failure(self):
        self.patches()
        with (
            patch(
                "recompra.apply_review",
                side_effect=PermissionError(13, "conteúdo privado"),
            ) as apply,
            patch("builtins.print") as printer,
        ):
            self.assertEqual(
                run_recompra(["--apply", "--require-review", str(self.review_path)]), 1
            )
        apply.assert_called_once()
        output = " ".join(str(call.args[0]) for call in printer.call_args_list)
        self.assertIn("arquivo local", output)
        self.assertIn("errno=13", output)
        self.assertNotIn("Falha de API", output)
        self.assertNotIn("conteúdo privado", output)

    def test_requests_connection_error_is_not_reported_as_local_file_failure(self):
        self.patches()
        with (
            patch(
                "recompra.apply_review", side_effect=requests.ConnectionError("privado")
            ),
            patch("builtins.print") as printer,
        ):
            self.assertEqual(
                run_recompra(["--apply", "--require-review", str(self.review_path)]), 1
            )
        output = " ".join(str(call.args[0]) for call in printer.call_args_list)
        self.assertIn("Falha de API", output)
        self.assertNotIn("arquivo local", output)
        self.assertNotIn("privado", output)

    def test_dry_run_report_write_crossing_midnight_returns_expired_snapshot(self):
        stack = self.patches()
        self.weni.iter_contacts.return_value = iter([])
        stack.enter_context(
            patch("recompra.load_state", return_value=deepcopy(EMPTY_STATE))
        )
        stack.enter_context(patch("recompra.build_plans", return_value=[]))
        original = recompra.write_review

        def write(review, path):
            original(review, path)
            self.clock.cross_midnight()

        stack.enter_context(patch("recompra.write_review", side_effect=write))
        self.assertEqual(
            run_recompra(["--dry-run", "--review-out", str(self.review_path)]), 4
        )
        self.assertEqual(recompra.read_review(self.review_path)["day"], "2026-10-09")
        self.assertEqual(
            run_recompra(["--check-review", "--require-review", str(self.review_path)]),
            4,
        )

    def test_fresh_full_dry_run_after_expiry_recalculates_plans_for_new_day(self):
        stack = self.patches()
        contact, order, items = fixture(24)
        self.weni.iter_contacts.side_effect = lambda: iter([deepcopy(contact)])
        stack.enter_context(
            patch("recompra.load_state", return_value=deepcopy(EMPTY_STATE))
        )
        captured_days = []

        def build(contacts, farm, structure, state, *, now, workers, pagination_stats):
            captured_days.append(now.date().isoformat())
            plans = [
                recompra.propose(current, order, items, structure, now=now)
                for current in contacts
            ]
            if now == BEFORE:
                self.clock.cross_midnight()
            return plans

        stack.enter_context(patch("recompra.build_plans", side_effect=build))
        writer = stack.enter_context(
            patch("recompra.write_review", wraps=recompra.write_review)
        )
        arguments = ["--dry-run", "--full", "--review-out", str(self.review_path)]
        self.assertEqual(run_recompra(arguments), 4)
        self.assertEqual(run_recompra(arguments), 0)
        self.assertEqual(captured_days, ["2026-10-09", "2026-10-10"])
        writer.assert_called_once()
        renewed = recompra.read_review(self.review_path)
        self.assertEqual(renewed["day"], "2026-10-10")
        self.assertTrue(renewed["plans"][0]["due_today"])

    def test_weni_full_recompra_runner_preserves_expiry_code(self):
        import run_update_weni

        argv = [
            "app/run_update_weni.py",
            "--recompra",
            "--dry-run",
            "--full",
            "--review-out",
            str(self.review_path),
        ]
        with (
            patch("sys.argv", argv),
            patch("run_recompra.main", return_value=4) as runner,
        ):
            self.assertEqual(run_update_weni.main(), 4)
        runner.assert_called_once_with(
            ["--dry-run", "--full", "--review-out", str(self.review_path)]
        )


class RecompraBatchRolloverContractTests(unittest.TestCase):
    def setUp(self):
        self.path = (
            Path(__file__).resolve().parents[1] / "comandos/atualizar_recompra.bat"
        )
        self.batch = (
            self.path.read_text(encoding="utf-8")
            .replace("comandos\\", "comandos/")
            .replace("app\\", "app/")
        )
        self.lines = [line.strip() for line in self.batch.splitlines()]

    def test_review_refresh_returns_to_dry_run_before_reopening_and_reconfirmation(
        self,
    ):
        regeneration = self.batch.index(":gerar_revisao\n")
        dry_run = self.batch.index("app/run_recompra.py --dry-run")
        check = self.batch.index("app/run_recompra.py --check-review")
        report = self.batch.index('start "" notepad.exe')
        confirm = self.batch.index(' -Expected "RECOMPRA REVISADA"')
        apply = self.batch.index("app/run_recompra.py --apply")
        self.assertEqual(
            sorted((regeneration, dry_run, check, report, confirm, apply)),
            [regeneration, dry_run, check, report, confirm, apply],
        )
        renewal = self.batch.split("\n:renovar_revisao\n", 1)[1].split("\n:fim\n", 1)[0]
        self.assertIn("goto :gerar_revisao", renewal)
        self.assertIn('set "REVIEW_JSON=logs\\recompra_review_renovada_', renewal)
        self.assertIn('set "REVIEW_TXT=%REVIEW_JSON:.json=.txt%"', renewal)

    def test_only_safe_expiration_branches_to_refresh_and_it_is_bounded(self):
        refresh_branches = [
            line for line in self.lines if "goto :renovar_revisao" in line
        ]
        self.assertEqual(len(refresh_branches), 3)
        for branch in refresh_branches:
            self.assertEqual(branch, 'if "%RC%"=="4" goto :renovar_revisao')
        self.assertIn('set "REVIEW_REFRESHES=0"', self.lines)
        self.assertIn('if "%REVIEW_REFRESHES%"=="1" goto :fim', self.lines)
        self.assertIn('set "REVIEW_REFRESHES=1"', self.lines)

    def test_confirmation_failure_stops_before_apply_and_is_never_refreshed(self):
        confirm = next(
            index
            for index, line in enumerate(self.lines)
            if ' -Expected "RECOMPRA REVISADA"' in line
        )
        apply = next(
            index
            for index, line in enumerate(self.lines)
            if "app/run_recompra.py --apply" in line
        )
        between = self.lines[confirm + 1 : apply]
        self.assertIn('set "RC=%ERRORLEVEL%"', between)
        self.assertIn('if not "%RC%"=="0" goto :fim', between)
        self.assertFalse(any("goto :renovar_revisao" in line for line in between))

    def test_windows_batch_keeps_utf8_without_bom_and_all_jump_labels_exist(self):
        self.assertFalse(self.path.read_bytes().startswith(b"\xef\xbb\xbf"))
        self.assertIn("chcp 65001 >nul", self.lines)
        labels = {line[1:].casefold() for line in self.lines if line.startswith(":")}
        for line in self.lines:
            if "goto :" in line:
                self.assertIn(line.rsplit("goto :", 1)[1].casefold(), labels)


class RecompraMutationRetryTests(unittest.TestCase):
    @staticmethod
    def response(status=200, payload=None, *, invalid_json=False):
        result = requests.Response()
        result.status_code = status
        result.url = "https://example.invalid/synthetic"
        result.headers["Content-Type"] = "application/json"
        result._content = (
            b"invalid-json"
            if invalid_json
            else json.dumps(payload or {}).encode("utf-8")
        )
        return result

    def failure(self, kind):
        if kind in (429, 500):
            return self.response(kind)
        if kind == "timeout":
            return requests.ReadTimeout("Tempo de resposta sintético excedido")
        return self.response(invalid_json=True)

    def test_failed_posts_are_attempted_once_without_sleep_or_route_refresh(self):
        for kind in (429, 500, "timeout", "invalid_json"):
            with self.subTest(kind=kind):
                client = WeniClient("TOKEN_SINTETICO_WENI", retry_mutations=False)
                self.addCleanup(client.close)
                failure = self.failure(kind)
                with (
                    patch.object(
                        client.session, "request", side_effect=[failure]
                    ) as request,
                    patch("weni_client.time.sleep") as sleep,
                    patch("weni_client.refresh_network_route") as refresh,
                ):
                    with self.assertRaises(WeniError):
                        client.contact_group_action(
                            "u1", action="add", group=STRUCTURE["group_uuid"]
                        )
                request.assert_called_once()
                self.assertEqual(request.call_args.args[0], "POST")
                sleep.assert_not_called()
                refresh.assert_not_called()

    def test_get_still_retries_server_error_when_mutation_retries_are_disabled(self):
        client = WeniClient("TOKEN_SINTETICO_WENI", retry_mutations=False)
        self.addCleanup(client.close)
        with (
            patch.object(
                client.session,
                "request",
                side_effect=[self.response(500), self.response(payload={"ok": True})],
            ) as request,
            patch("weni_client.time.sleep") as sleep,
            patch("weni_client.refresh_network_route") as refresh,
        ):
            self.assertEqual(
                client._get_json("https://example.invalid/synthetic"), {"ok": True}
            )
        self.assertEqual(request.call_count, 2)
        self.assertEqual(
            [call.args[0] for call in request.call_args_list], ["GET", "GET"]
        )
        sleep.assert_called_once_with(1.0)
        refresh.assert_not_called()

    def test_default_client_keeps_legacy_post_retries(self):
        for kind in (429, 500, "timeout", "invalid_json"):
            with self.subTest(kind=kind):
                client = WeniClient("TOKEN_SINTETICO_WENI")
                self.addCleanup(client.close)
                self.assertTrue(client.retry_mutations)
                with (
                    patch.object(
                        client.session,
                        "request",
                        side_effect=[
                            self.failure(kind),
                            self.response(payload={"ok": True}),
                        ],
                    ) as request,
                    patch("weni_client.time.sleep") as sleep,
                    patch("weni_client.refresh_network_route") as refresh,
                ):
                    self.assertEqual(
                        client.contact_group_action(
                            "u1", action="add", group=STRUCTURE["group_uuid"]
                        ),
                        {"ok": True},
                    )
                self.assertEqual(request.call_count, 2)
                self.assertEqual(
                    [call.args[0] for call in request.call_args_list], ["POST", "POST"]
                )
                sleep.assert_called_once_with(1.0)
                if kind in {"timeout", "invalid_json"}:
                    refresh.assert_called_once()
                else:
                    refresh.assert_not_called()

    def test_lost_add_response_after_midnight_preserves_pending_without_another_post(
        self,
    ):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        state_path = Path(temporary.name) / "state.json"
        audit_path = Path(temporary.name) / "audit.jsonl"
        clock = MutableClock()
        contact, order, items = fixture(24)
        initial = recompra.propose(contact, order, items, STRUCTURE, now=BEFORE)
        contact["fields"].update(initial["fields"])
        plan = recompra.propose(contact, order, items, STRUCTURE, now=BEFORE)
        self.assertEqual(plan["fields"], {})
        self.assertEqual(plan["action"], "add")
        review = recompra.make_review([plan], STRUCTURE, now=BEFORE, state=EMPTY_STATE)
        client = WeniClient("TOKEN_SINTETICO_WENI", retry_mutations=False)
        self.addCleanup(client.close)

        def request(method, url, *, params, json, timeout, allow_redirects=False):
            self.assertFalse(allow_redirects)
            if method == "GET":
                return self.response(payload={"results": [deepcopy(contact)]})
            self.assertEqual(method, "POST")
            self.assertEqual(json["action"], "add")
            entry = recompra.load_state(state_path)["contacts"]["u1"]["orders"]["100"]
            self.assertTrue(entry["pending"])
            contact["groups"].append(
                {"uuid": STRUCTURE["group_uuid"], "name": recompra.GROUP}
            )
            clock.cross_midnight()
            raise requests.ReadTimeout("Resposta sintética perdida depois da admissão")

        with (
            patch.object(client.session, "request", side_effect=request) as network,
            patch("weni_client.time.sleep") as sleep,
            patch("weni_client.refresh_network_route") as refresh,
            patch("recompra.inspect_structure", return_value=STRUCTURE),
            patch("recompra.resolve_latest", return_value=(order, items)),
        ):
            with self.assertRaisesRegex(RuntimeError, "Estado preservado") as raised:
                recompra.apply_review(
                    review,
                    client,
                    Mock(),
                    state_path=state_path,
                    audit_path=audit_path,
                    now_fn=clock,
                )
        self.assertNotIsInstance(raised.exception, recompra.ReviewExpiredError)
        self.assertEqual(
            [call.args[0] for call in network.call_args_list], ["GET", "GET", "POST"]
        )
        sleep.assert_not_called()
        refresh.assert_not_called()
        saved = recompra.load_state(state_path)["contacts"]["u1"]
        self.assertTrue(saved["orders"]["100"]["pending"])
        self.assertFalse(saved["orders"]["100"].get("admitted", False))
        audit = [
            json.loads(line)
            for line in audit_path.read_text(encoding="utf-8").splitlines()
        ]
        self.assertEqual(
            [event["status"] for event in audit], ["FALHA_OU_RESULTADO_INDETERMINADO"]
        )
        retry = recompra.propose(contact, order, items, STRUCTURE, saved, now=AFTER)
        self.assertNotIn(retry["action"], ("add", "replace"))


if __name__ == "__main__":
    unittest.main()
