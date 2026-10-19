"""Otimizações de recompra com fixtures sintéticas, sem chamadas externas."""

import tempfile
import unittest
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import ExitStack
from copy import deepcopy
from pathlib import Path
from threading import Barrier, Event, Lock
from unittest.mock import Mock, patch

from analytics import cpf_query_priority
from config import FARMALIDER_MAX_WORKERS
from farmalider_client import FarmaliderBadRequestError
from recompra import (
    RecompraReadCache,
    apply_review,
    atomic_json,
    build_plans,
    load_state,
    make_review,
    plan_needs_apply,
    propose,
    resolve_latest,
)
from run_recompra import build_parser
from run_recompra import main as run_recompra
from tests.test_recompra import NOW, STRUCTURE, fixture

TIMEOUT = 10


def synthetic_contact(index, cpf="52998224725"):
    contact, _, _ = fixture()
    contact["uuid"] = f"synthetic-{index}"
    contact["fields"]["cpf"] = cpf
    return contact


class RecompraReadCacheTests(unittest.TestCase):
    def test_concurrent_cpf_lookup_single_flight_and_mutation_isolation(self):
        consumers = 6
        started = Event()
        release = Event()
        waiting = Event()
        entry_barrier = Barrier(consumers)
        waiter_lock = Lock()
        waiter_count = 0
        payload = {
            "clientes": [{"cliente": {"nome": "Cliente Sintético"}, "itens": [1]}]
        }

        class ObservedFuture(Future):
            def result(self, timeout=None):
                nonlocal waiter_count
                with waiter_lock:
                    waiter_count += 1
                    if waiter_count == consumers - 1:
                        waiting.set()
                return super().result(timeout=timeout)

        def fetch(_cpf):
            started.set()
            if not release.wait(TIMEOUT):
                raise RuntimeError("Consulta sintética não foi liberada.")
            return payload

        farm = Mock()
        farm.get_cliente_by_cpf.side_effect = fetch
        cache = RecompraReadCache(farm)

        def consume():
            entry_barrier.wait(TIMEOUT)
            return cache.get_cliente_by_cpf("52998224725")

        with (
            patch("recompra.Future", ObservedFuture),
            ThreadPoolExecutor(max_workers=consumers) as pool,
        ):
            futures = [pool.submit(consume) for _ in range(consumers)]
            try:
                self.assertTrue(started.wait(TIMEOUT))
                self.assertTrue(waiting.wait(TIMEOUT))
                self.assertEqual(farm.get_cliente_by_cpf.call_count, 1)
            finally:
                release.set()
            results = [future.result(TIMEOUT) for future in futures]

        results[0]["clientes"][0]["cliente"]["nome"] = "Modificado"
        results[0]["clientes"][0]["itens"].append(2)
        self.assertEqual(results[1], payload)
        self.assertEqual(cache.get_cliente_by_cpf("52998224725"), payload)
        self.assertEqual(cache.metrics(), {"consultas": 1, "reusos": 6, "entradas": 1})

    def test_order_lookup_deduplicates_numeric_and_text_id_and_separates_routes(self):
        farm = Mock()
        farm.get_pedido.return_value = {"itens": [{"descricao": "Produto Sintético"}]}
        farm.get_cliente_by_cpf.return_value = {"cliente": {"id": 7}}
        cache = RecompraReadCache(farm)
        first = cache.get_pedido(7)
        first["itens"][0]["descricao"] = "Modificado"
        self.assertEqual(cache.get_pedido("7"), farm.get_pedido.return_value)
        self.assertEqual(
            cache.get_cliente_by_cpf("7"), farm.get_cliente_by_cpf.return_value
        )
        farm.get_pedido.assert_called_once_with(7)
        farm.get_cliente_by_cpf.assert_called_once_with("7")

    def test_cache_is_bounded_and_retains_recently_used_entries(self):
        farm = Mock()
        farm.get_cliente_by_cpf.side_effect = lambda cpf: {"cpf": cpf}
        cache = RecompraReadCache(farm, max_entries=2)
        for cpf in ("first", "second", "first", "third", "first"):
            cache.get_cliente_by_cpf(cpf)
            self.assertLessEqual(cache.metrics()["entradas"], 2)
        self.assertEqual(farm.get_cliente_by_cpf.call_count, 3)
        cache.get_cliente_by_cpf("second")
        self.assertEqual(farm.get_cliente_by_cpf.call_count, 4)
        self.assertEqual(cache.metrics()["entradas"], 2)

    def test_transient_error_does_not_persist_and_next_read_retries(self):
        farm = Mock()
        farm.get_cliente_by_cpf.side_effect = [
            RuntimeError("falha transitória"),
            {"cliente": {"id": 1}},
        ]
        cache = RecompraReadCache(farm)
        with self.assertRaisesRegex(RuntimeError, "transitória"):
            cache.get_cliente_by_cpf("52998224725")
        self.assertEqual(cache.metrics()["entradas"], 0)
        self.assertEqual(
            cache.get_cliente_by_cpf("52998224725"), {"cliente": {"id": 1}}
        )
        self.assertEqual(farm.get_cliente_by_cpf.call_count, 2)

    def test_bad_request_remains_cached_for_this_run_only(self):
        farm = Mock()
        farm.get_cliente_by_cpf.side_effect = FarmaliderBadRequestError("CPF rejeitado")
        cache = RecompraReadCache(farm)
        for _ in range(2):
            with self.assertRaises(FarmaliderBadRequestError):
                cache.get_cliente_by_cpf("0")
        farm.get_cliente_by_cpf.assert_called_once_with("0")
        with self.assertRaises(FarmaliderBadRequestError):
            RecompraReadCache(farm).get_cliente_by_cpf("0")
        self.assertEqual(farm.get_cliente_by_cpf.call_count, 2)

    def test_failed_owner_cannot_remove_replacement_future_after_eviction(self):
        replacement_started = Event()
        consumer_observed = Event()
        release = Event()
        lock = Lock()
        calls = 0
        retries = []
        farm = Mock()
        farm.get_pedido.return_value = {"id": "other"}
        cache = RecompraReadCache(farm, max_entries=1)

        def fetch(cpf):
            nonlocal calls
            with lock:
                calls += 1
                current = calls
            if current == 1:
                raise RuntimeError("Falha transitória sintética")
            if current == 2:
                replacement_started.set()
                if not release.wait(TIMEOUT):
                    raise RuntimeError("Retry sintético não foi liberado.")
            else:
                # Uma regressão cria esta terceira consulta: sinaliza sem
                # bloquear para que a asserção detecte a duplicação.
                consumer_observed.set()
            return {"cpf": cpf}

        farm.get_cliente_by_cpf.side_effect = fetch
        with ThreadPoolExecutor(max_workers=2) as pool:

            class RacingFuture(Future):
                def set_exception(self, exception):
                    super().set_exception(exception)
                    # A Future velha já terminou: a outra rota a expulsa.
                    cache.get_pedido("other")
                    retries.append(pool.submit(cache.get_cliente_by_cpf, "0"))
                    if not replacement_started.wait(TIMEOUT):
                        raise AssertionError("A Future substituta não começou.")

                def result(self, timeout=None):
                    if not self.done():
                        consumer_observed.set()
                    return super().result(timeout=timeout)

            with patch("recompra.Future", RacingFuture):
                try:
                    with self.assertRaisesRegex(RuntimeError, "Falha transitória"):
                        cache.get_cliente_by_cpf("0")
                    third = pool.submit(cache.get_cliente_by_cpf, "0")
                    self.assertTrue(consumer_observed.wait(TIMEOUT))
                    self.assertEqual(farm.get_cliente_by_cpf.call_count, 2)
                finally:
                    release.set()
                self.assertEqual(retries[0].result(TIMEOUT), {"cpf": "0"})
                self.assertEqual(third.result(TIMEOUT), {"cpf": "0"})
        self.assertEqual(cache.metrics(), {"consultas": 3, "reusos": 1, "entradas": 1})

    def test_invalid_cpf_cache_never_shares_selected_identity_or_order(self):
        contacts = []
        payload = {"clientes": []}
        for index, name in enumerate(
            ("Ana Cliente Sintética", "Bia Cliente Sintética"), start=1
        ):
            contact = synthetic_contact(index, "123")
            contact["name"] = name
            contact["urns"] = []
            contact["fields"]["email"] = f"cliente{index}@example.test"
            contacts.append(contact)
            payload["clientes"].append(
                {
                    "cliente": {
                        "id": index,
                        "cpf": "123",
                        "nome": name,
                        "email": f"cliente{index}@example.test",
                    },
                    "ultimoPedido": {"id": str(index * 100), "codCliente": index},
                }
            )
        farm = Mock()
        farm.get_cliente_by_cpf.return_value = payload
        cache = RecompraReadCache(farm)

        def resolve(contact, **kwargs):
            index = kwargs["linked_cliente_id"]
            _, order, items = fixture(order_id=str(int(index) * 100))
            order["farmalider_cliente_id"] = index
            return order, items

        with patch("recompra.resolve_contact_order", side_effect=resolve) as resolver:
            first, _ = resolve_latest(contacts[0], cache, NOW.isoformat())
            second, _ = resolve_latest(contacts[1], cache, NOW.isoformat())
            divergent = deepcopy(contacts[1])
            divergent["fields"]["email"] = "unrelated@example.test"
            divergent["name"] = "Outra pessoa"
            divergent["fields"]["nome"] = "Outra pessoa"
            rejected, items = resolve_latest(divergent, cache, NOW.isoformat())
        self.assertEqual((first["pedido_id"], second["pedido_id"]), ("100", "200"))
        self.assertEqual(rejected["status_confirmacao"], "CPF_IDENTIDADE_DIVERGENTE")
        self.assertEqual(items, [])
        self.assertEqual(resolver.call_count, 2)
        farm.get_cliente_by_cpf.assert_called_once_with("123")


class RecompraPlanBuildTests(unittest.TestCase):
    def test_progress_uses_large_batches_and_reports_the_final_partial_batch(self):
        contacts = [synthetic_contact(index) for index in range(2050)]
        with (
            patch("recompra.resolve_latest", return_value=(None, [])),
            patch("recompra.logging.info") as info,
        ):
            plans = build_plans(contacts, Mock(), STRUCTURE, {"contacts": {}}, now=NOW)
        self.assertEqual(len(plans), 2050)
        progress_counts = [
            call.args[1]
            for call in info.call_args_list
            if "velocidade=" in call.args[0]
        ]
        self.assertEqual(progress_counts, [1000, 2000])
        self.assertEqual(info.call_args.args[1], 2050)
        self.assertIn("duração=", info.call_args.args[0])

    def test_queries_run_in_parallel_with_no_more_than_four_workers(self):
        barrier = Barrier(5)
        release = Event()
        lock = Lock()
        active = started = maximum = 0

        def resolve(*args, **kwargs):
            nonlocal active, started, maximum
            with lock:
                active += 1
                started += 1
                maximum = max(maximum, active)
                first_wave = started <= 4
            try:
                if first_wave:
                    barrier.wait(TIMEOUT)
                    if not release.wait(TIMEOUT):
                        raise RuntimeError("Workers sintéticos não foram liberados.")
                return None, []
            finally:
                with lock:
                    active -= 1

        contacts = [synthetic_contact(index) for index in range(16)]
        with (
            patch("recompra.resolve_latest", side_effect=resolve),
            ThreadPoolExecutor(max_workers=1) as coordinator,
        ):
            future = coordinator.submit(
                build_plans,
                contacts,
                Mock(),
                STRUCTURE,
                load_state(Path("absent-state-synthetic.json")),
                now=NOW,
                workers=4,
            )
            try:
                barrier.wait(TIMEOUT)
                with lock:
                    self.assertEqual(active, 4)
            finally:
                release.set()
            plans = future.result(TIMEOUT)
        self.assertEqual(maximum, 4)
        self.assertEqual(len(plans), 16)
        self.assertEqual(
            {plan["contact_uuid"] for plan in plans},
            {contact["uuid"] for contact in contacts},
        )

    def test_all_valid_cpfs_complete_before_invalid_including_zero(self):
        cpfs = [
            "0",
            "",
            "123",
            "52998224725",
            "11111111111",
            "52998224725",
            "52998224725",
        ]
        contacts = [synthetic_contact(index, cpf) for index, cpf in enumerate(cpfs)]
        lock = Lock()
        completed_valid = set()
        invalid_seen = []
        expected_valid = {
            contact["uuid"]
            for contact in contacts
            if cpf_query_priority(contact["fields"]["cpf"]) == 0
        }

        def resolve(contact, *args, **kwargs):
            priority = cpf_query_priority(contact["fields"]["cpf"])
            with lock:
                if priority:
                    self.assertEqual(completed_valid, expected_valid)
                    invalid_seen.append(contact["fields"]["cpf"])
                else:
                    completed_valid.add(contact["uuid"])
            return None, []

        with patch("recompra.resolve_latest", side_effect=resolve):
            plans = build_plans(
                contacts, Mock(), STRUCTURE, {"contacts": {}}, now=NOW, workers=4
            )
        self.assertEqual(len(plans), len(contacts))
        self.assertIn("0", invalid_seen)
        self.assertEqual(
            [cpf_query_priority(plan["guard"]["fields"]["cpf"]) for plan in plans],
            [0, 0, 0, 1, 1, 1, 2],
        )

    def test_source_is_consolidated_before_any_contact_queries_begin(self):
        source_finished = Event()

        def source():
            for index in range(8):
                yield synthetic_contact(index)
            yield synthetic_contact(8)
            source_finished.set()

        def resolve(*args, **kwargs):
            self.assertTrue(
                source_finished.is_set(),
                "Consulta iniciada antes de descartar UUIDs divergentes em páginas posteriores.",
            )
            return None, []

        with patch("recompra.resolve_latest", side_effect=resolve):
            plans = build_plans(
                source(), Mock(), STRUCTURE, {"contacts": {}}, now=NOW, workers=4
            )
        self.assertTrue(source_finished.is_set())
        self.assertEqual(len(plans), 9)

    def test_temporary_spool_is_removed_after_worker_failure(self):
        temporary_directory = tempfile.TemporaryDirectory
        created = []

        def directory(*args, **kwargs):
            result = temporary_directory(*args, **kwargs)
            created.append(Path(result.name))
            return result

        contacts = [synthetic_contact("invalid", "0")] + [
            synthetic_contact(index) for index in range(8)
        ]
        with (
            patch("recompra.tempfile.TemporaryDirectory", side_effect=directory),
            patch(
                "recompra.resolve_latest",
                side_effect=RuntimeError("worker sintético falhou"),
            ),
        ):
            with self.assertRaisesRegex(RuntimeError, "worker sintético"):
                build_plans(
                    contacts, Mock(), STRUCTURE, {"contacts": {}}, now=NOW, workers=4
                )
        self.assertEqual(len(created), 1)
        self.assertFalse(created[0].exists())

    def test_invalid_worker_count_is_rejected_before_any_contact_is_read(self):
        contacts = Mock()
        for workers in (0, 5):
            with self.subTest(workers=workers), self.assertRaises(ValueError):
                build_plans(
                    contacts,
                    Mock(),
                    STRUCTURE,
                    {"contacts": {}},
                    now=NOW,
                    workers=workers,
                )
        self.assertEqual(contacts.mock_calls, [])


class RecompraApplyOptimizationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.state_path = Path(self.temp.name) / "state.json"
        self.audit_path = Path(self.temp.name) / "audit.jsonl"

    def existing_plan(self, *, grouped=False, record=None):
        contact, order, items = fixture(24, grouped=grouped)
        initial = propose(contact, order, items, STRUCTURE, now=NOW)
        contact["fields"].update(initial["fields"])
        saved = {
            "purchase": initial["purchase"],
            "orders": {"100": record} if record is not None else {},
        }
        state = {"schema": 1, "contacts": {"u1": saved}}
        atomic_json(self.state_path, state)
        plan = propose(contact, order, items, STRUCTURE, saved, now=NOW)
        self.assertEqual(plan["fields"], {})
        return contact, order, items, saved, state, plan

    def apply(self, review, weni, farm):
        with patch("recompra.inspect_structure", return_value=STRUCTURE):
            return apply_review(
                review,
                weni,
                farm,
                state_path=self.state_path,
                audit_path=self.audit_path,
                now_fn=lambda: NOW,
            )

    def test_no_work_skips_contact_and_order_reads_posts_and_state_writes(self):
        _, _, _, saved, state, plan = self.existing_plan(record={"admitted": True})
        self.assertFalse(plan_needs_apply(plan, saved))
        review = make_review([plan], STRUCTURE, now=NOW, state=state)
        weni, farm = Mock(), Mock()
        with (
            patch("recompra.atomic_json") as writer,
            patch("recompra.resolve_latest") as resolver,
        ):
            self.assertEqual(self.apply(review, weni, farm), {"sem_mudanca": 1})
        self.assertEqual(weni.mock_calls, [])
        self.assertEqual(farm.mock_calls, [])
        resolver.assert_not_called()
        writer.assert_not_called()

    def test_apply_progress_uses_large_batches_and_keeps_the_final_summary(self):
        _, _, _, saved, state, plan = self.existing_plan(record={"admitted": True})
        plans = []
        for index in range(2050):
            uid = f"contato-ficticio-{index}"
            current = deepcopy(plan)
            current["contact_uuid"] = uid
            plans.append(current)
            state["contacts"][uid] = deepcopy(saved)
        atomic_json(self.state_path, state)
        review = make_review(plans, STRUCTURE, now=NOW, state=state)
        with patch("recompra.logging.info") as info:
            result = self.apply(review, Mock(), Mock())
        self.assertEqual(result, {"sem_mudanca": 2050})
        progress_counts = [
            call.args[1]
            for call in info.call_args_list
            if "contatos=%s/%s" in call.args[0]
        ]
        self.assertEqual(progress_counts, [1000, 2000])
        self.assertEqual(info.call_args.args[1], 2050)

    def test_no_purchase_plan_preserves_saved_purchase_without_reads_or_writes(self):
        contact, order, items = fixture(26)
        saved_purchase = propose(contact, order, items, STRUCTURE, now=NOW)["purchase"]
        saved = {"purchase": saved_purchase, "orders": {"100": {"admitted": True}}}
        state = {"schema": 1, "contacts": {"u1": saved}}
        atomic_json(self.state_path, state)
        before = self.state_path.read_bytes()
        plan = propose(contact, None, [], STRUCTURE, saved, now=NOW)
        self.assertIsNone(plan["purchase"])
        self.assertEqual(plan["fields"], {})
        self.assertEqual(plan["action"], "")
        self.assertFalse(plan_needs_apply(plan, saved))
        review = make_review([plan], STRUCTURE, now=NOW, state=state)
        weni, farm = Mock(), Mock()
        with (
            patch("recompra.atomic_json") as writer,
            patch("recompra.resolve_latest") as resolver,
        ):
            self.assertEqual(self.apply(review, weni, farm), {"sem_mudanca": 1})
        self.assertEqual(self.state_path.read_bytes(), before)
        self.assertEqual(
            load_state(self.state_path)["contacts"]["u1"]["purchase"], saved_purchase
        )
        self.assertEqual(weni.mock_calls, [])
        self.assertEqual(farm.mock_calls, [])
        resolver.assert_not_called()
        writer.assert_not_called()

    def test_group_admission_needs_local_persistence_even_without_field_changes(self):
        for record in (None, {"admitted": False}, {"admitted": True, "pending": True}):
            with self.subTest(record=record):
                contact, order, items, saved, state, plan = self.existing_plan(
                    grouped=True, record=record
                )
                self.assertEqual(plan["action"], "")
                self.assertTrue(plan_needs_apply(plan, saved))
                review = make_review([plan], STRUCTURE, now=NOW, state=state)
                weni, farm = Mock(), Mock()
                weni.get_contact.return_value = contact
                with patch(
                    "recompra.resolve_latest", return_value=(order, items)
                ) as resolver:
                    self.assertEqual(self.apply(review, weni, farm), {"aplicados": 1})
                persisted = load_state(self.state_path)["contacts"]["u1"]["orders"][
                    "100"
                ]
                self.assertTrue(persisted["admitted"])
                self.assertFalse(persisted["pending"])
                weni.get_contact.assert_called_once_with(uuid="u1")
                weni.update_contact_fields.assert_not_called()
                weni.contact_group_action.assert_not_called()
                resolver.assert_called_once()

    def test_contact_guard_blocks_even_when_only_local_admission_is_pending(self):
        contact, _, _, _, state, plan = self.existing_plan(grouped=True)
        before = self.state_path.read_bytes()
        review = make_review([plan], STRUCTURE, now=NOW, state=state)
        changed = deepcopy(contact)
        changed["fields"]["marketing_opt_in"] = "recusado"
        weni, farm = Mock(), Mock()
        weni.get_contact.return_value = changed
        with patch("recompra.resolve_latest") as resolver:
            self.assertEqual(self.apply(review, weni, farm), {"conflitos": 1})
        resolver.assert_not_called()
        self.assertEqual(self.state_path.read_bytes(), before)
        weni.update_contact_fields.assert_not_called()
        weni.contact_group_action.assert_not_called()

    def test_order_guard_blocks_changed_order_before_local_admission_is_saved(self):
        contact, _, items, _, state, plan = self.existing_plan(grouped=True)
        _, changed_order, _ = fixture(23, order_id="200")
        before = self.state_path.read_bytes()
        review = make_review([plan], STRUCTURE, now=NOW, state=state)
        weni, farm = Mock(), Mock()
        weni.get_contact.return_value = contact
        with patch("recompra.resolve_latest", return_value=(changed_order, items)):
            self.assertEqual(self.apply(review, weni, farm), {"conflitos": 1})
        self.assertEqual(self.state_path.read_bytes(), before)
        weni.update_contact_fields.assert_not_called()
        weni.contact_group_action.assert_not_called()

    def test_purchase_only_change_writes_state_once_and_repeat_is_noop(self):
        contact, order, items = fixture(26)
        initial = propose(contact, order, items, STRUCTURE, now=NOW)
        contact["fields"].update(initial["fields"])
        state = {"schema": 1, "contacts": {"u1": {"orders": {}}}}
        atomic_json(self.state_path, state)
        plan = propose(
            contact, order, items, STRUCTURE, state["contacts"]["u1"], now=NOW
        )
        self.assertEqual(plan["fields"], {})
        self.assertEqual(plan["action"], "")
        weni, farm = Mock(), Mock()
        weni.get_contact.return_value = contact
        review = make_review([plan], STRUCTURE, now=NOW, state=state)
        with (
            patch("recompra.resolve_latest", return_value=(order, items)),
            patch("recompra.atomic_json", wraps=atomic_json) as writer,
        ):
            self.assertEqual(self.apply(review, weni, farm), {"aplicados": 1})
            writer.assert_called_once()
        persisted = load_state(self.state_path)
        self.assertEqual(persisted["contacts"]["u1"]["purchase"], plan["purchase"])
        repeated = make_review([plan], STRUCTURE, now=NOW, state=persisted)
        with patch("recompra.atomic_json", wraps=atomic_json) as writer:
            self.assertEqual(self.apply(repeated, weni, farm), {"sem_mudanca": 1})
            writer.assert_not_called()
        weni.get_contact.assert_called_once_with(uuid="u1")
        weni.update_contact_fields.assert_not_called()
        weni.contact_group_action.assert_not_called()

    def test_pending_is_on_disk_before_uncertain_admission_and_prevents_retry(self):
        contact, order, items = fixture(24)
        state = load_state(self.state_path)
        plan = propose(contact, order, items, STRUCTURE, now=NOW)
        review = make_review([plan], STRUCTURE, now=NOW, state=state)
        live = deepcopy(contact)
        weni, farm = Mock(), Mock()
        weni.get_contact.side_effect = lambda **kwargs: deepcopy(live)
        weni.update_contact_fields.side_effect = lambda uid, fields: live[
            "fields"
        ].update(fields)

        def uncertain(uid, *, action, group):
            self.assertEqual(action, "add")
            self.assertTrue(
                load_state(self.state_path)["contacts"][uid]["orders"]["100"]["pending"]
            )
            raise RuntimeError("Resposta sintética perdida")

        weni.contact_group_action.side_effect = uncertain
        with patch("recompra.resolve_latest", return_value=(order, items)):
            with self.assertRaisesRegex(RuntimeError, "Estado preservado"):
                self.apply(review, weni, farm)
        saved = load_state(self.state_path)["contacts"]["u1"]
        self.assertTrue(saved["orders"]["100"]["pending"])
        retry = propose(live, order, items, STRUCTURE, saved, now=NOW)
        self.assertEqual(retry["action"], "")
        self.assertEqual(retry["reason"], "PEDIDO_JA_ADMITIDO_OU_INDETERMINADO")
        weni.contact_group_action.assert_called_once()


class RecompraRunnerOptimizationTests(unittest.TestCase):
    def test_worker_parser_defaults_and_rejects_values_outside_global_limit(self):
        parser = build_parser()
        self.assertEqual(
            parser.parse_args(["--dry-run"]).workers, FARMALIDER_MAX_WORKERS
        )
        self.assertEqual(parser.parse_args(["--dry-run", "--workers", "1"]).workers, 1)
        for workers in ("0", str(FARMALIDER_MAX_WORKERS + 1)):
            with (
                self.subTest(workers=workers),
                patch("sys.stderr"),
                self.assertRaises(SystemExit) as raised,
            ):
                parser.parse_args(["--dry-run", "--workers", workers])
            self.assertEqual(raised.exception.code, 2)

    def test_dry_run_forwards_workers_and_streams_contacts_then_closes_clients(self):
        for extra_args, workers in (
            ([], FARMALIDER_MAX_WORKERS),
            (["--workers", "1"], 1),
        ):
            with self.subTest(workers=workers), ExitStack() as stack:
                contact, _, _ = fixture()
                contacts = iter([contact])
                weni, farm = Mock(), Mock()
                weni.iter_contacts.return_value = contacts
                state = {"schema": 1, "contacts": {}}
                plan = {"synthetic": "plan"}
                review = {"synthetic": "review", "day": NOW.date().isoformat()}
                stack.enter_context(patch("proxy_config.configure_proxy"))
                stack.enter_context(patch("logging_config.configure_logging"))
                stack.enter_context(
                    patch(
                        "secrets_config.WENI_API_TOKEN", "synthetic-token", create=True
                    )
                )
                stack.enter_context(patch("weni_client.WeniClient", return_value=weni))
                stack.enter_context(
                    patch("weni_update._open_farmalider", return_value=farm)
                )
                stack.enter_context(
                    patch("recompra.inspect_structure", return_value=STRUCTURE)
                )
                stack.enter_context(patch("recompra.now_local", return_value=NOW))
                stack.enter_context(patch("recompra.load_state", return_value=state))
                planner = stack.enter_context(
                    patch("recompra.build_plans", return_value=[plan])
                )
                reviewer = stack.enter_context(
                    patch("recompra.make_review", return_value=review)
                )
                writer = stack.enter_context(patch("recompra.write_review"))
                finalizer = stack.enter_context(patch("recompra.finalize_review"))
                stack.enter_context(patch("builtins.print"))
                review_out = Path("synthetic/review.json")
                self.assertEqual(
                    run_recompra(
                        ["--dry-run", "--review-out", str(review_out), *extra_args]
                    ),
                    0,
                )
                planner.assert_called_once_with(
                    contacts,
                    farm,
                    STRUCTURE,
                    state,
                    now=NOW,
                    workers=workers,
                    pagination_stats={},
                )
                reviewer.assert_called_once_with(
                    [plan], STRUCTURE, now=NOW, state=state
                )
                writer.assert_called_once_with(review, review_out)
                finalizer.assert_called_once_with(review, NOW)
                weni.iter_contacts.assert_called_once_with()
                weni.list_contacts.assert_not_called()
                weni.close.assert_called_once_with()
                farm.close.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
