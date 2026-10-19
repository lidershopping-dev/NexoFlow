"""Consulta da base preserva identificadores inválidos e barreiras de prioridade."""

import json
import tempfile
import threading
import unittest
from contextlib import nullcontext
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import Mock, patch

import weni_update
from analytics import is_valid_cpf, normalize_cpf
from farmalider_client import (
    FarmaliderBadRequestError,
    FarmaliderClient,
    FarmaliderError,
    RequestPermit,
)
from farmalider_integration import cliente_link_status, resolve_contact_order
from main import FarmaliderSettings, validate_contacts
from nexo_cli import _normalize_cpf
from private_diagnostics import append_private_failure

VALID = "52998224725"
OTHER_VALID = "16899535009"


class PriorityClient:
    def __init__(self):
        self.calls = []
        self.finished = set()
        self.lock = threading.Lock()
        self.both_valid_started = threading.Barrier(2)
        self.invalid_started_early = False

    def get_cliente_by_cpf(self, cpf):
        with self.lock:
            self.calls.append(cpf)
            if not is_valid_cpf(cpf) and not {VALID, OTHER_VALID} <= self.finished:
                self.invalid_started_early = True
        if is_valid_cpf(cpf):
            self.both_valid_started.wait(timeout=5)
        with self.lock:
            self.finished.add(cpf)
        return {
            "cliente": {
                "id": 99,
                "cpf": int(cpf),
                "nome": "Cliente sintético",
                "email": "cliente@example.invalid",
            }
        }

    def metrics_snapshot(self):
        return dict(
            total_requests=len(self.calls),
            retries=0,
            rate_limits=0,
            server_errors=0,
            network_errors=0,
            max_response_ms=1,
            effective_delay_ms=0,
        )


class CpfQueryPriorityTests(unittest.TestCase):
    def test_parallel_valid_phase_completes_before_any_invalid_request(self):
        rows = [
            {
                "contact_uuid": str(i),
                "cpf": cpf,
                "nome": "Cliente sintético",
                "email": "cliente@example.invalid",
            }
            for i, cpf in enumerate(
                ["00000000000", VALID, "123", OTHER_VALID, "0", VALID, "123", None]
            )
        ]
        farm = PriorityClient()
        with (
            patch("main.FARMALIDER_PARALLEL_MIN_CPFS", 1),
            patch("main.FARMALIDER_MAX_WORKERS", 4),
            patch("main.FARMALIDER_BATCH_SIZE", 500),
        ):
            stats = validate_contacts(
                rows,
                farm_client=farm,
                farm_settings=FarmaliderSettings(True, "synthetic", False),
                sync_time="2026-10-08T10:00:00-03:00",
            )
        self.assertFalse(farm.invalid_started_early)
        self.assertEqual(set(farm.calls[:2]), {VALID, OTHER_VALID})
        self.assertCountEqual(farm.calls[2:], ["123"])
        self.assertEqual(stats.unique_queries, 3)
        self.assertEqual(stats.invalid, 4)
        self.assertEqual(stats.invalid_registration, 2)
        self.assertEqual(stats.confirmed, 5)
        self.assertEqual(rows[0]["cpf"], "00000000000")
        self.assertEqual(rows[0]["farmalider_cliente_status"], "CPF_INVALIDO_CADASTRO")
        self.assertEqual(rows[-1]["farmalider_cliente_status"], "CPF_NAO_INFORMADO")

    def test_invalid_lookup_status_is_distinct_from_mathematical_validity(self):
        for cpf in ["0", "00000000000", "123", "11111111111", "52998224724"]:
            with self.subTest(cpf=cpf):
                status, identifier, _ = cliente_link_status(
                    cpf,
                    {
                        "cliente": {
                            "id": 99,
                            "cpf": int(cpf),
                            "nome": "Sintético",
                            "email": "teste@example.invalid",
                        }
                    },
                    weni_identity={
                        "nome": "Sintético",
                        "email": "teste@example.invalid",
                    },
                )
                self.assertEqual(status, "CPF_INVALIDO_ENCONTRADO")
                self.assertEqual(identifier, "99")
                self.assertFalse(is_valid_cpf(cpf))
                self.assertEqual(
                    cliente_link_status(cpf, None)[0], "CPF_NAO_ENCONTRADO"
                )

    def test_invalid_customer_mismatch_is_blocked(self):
        self.assertEqual(
            cliente_link_status("123", {"id": 99, "cpf": "124"})[0],
            "CPF_DIVERGENTE_API",
        )
        self.assertEqual(
            cliente_link_status("000", {"id": 99, "cpf": "0"})[0], "CPF_DIVERGENTE_API"
        )  # only numeric JSON loses leading zeros

    def test_400_404_500_are_separate_results(self):
        farm = Mock()
        farm.get_cliente_by_cpf.side_effect = [
            FarmaliderBadRequestError("400"),
            None,
            FarmaliderError("500"),
        ]
        farm.metrics_snapshot.return_value = dict(
            total_requests=3,
            retries=0,
            rate_limits=0,
            server_errors=1,
            network_errors=0,
            max_response_ms=1,
            effective_delay_ms=0,
        )
        rows = [{"cpf": cpf} for cpf in ["123", "124", "125"]]
        with patch("main.FARMALIDER_MAX_WORKERS", 1):
            stats = validate_contacts(
                rows,
                farm_client=farm,
                farm_settings=FarmaliderSettings(True, "synthetic", False),
                sync_time="2026-10-08T10:00:00-03:00",
            )
        self.assertEqual(
            [r["farmalider_cliente_status"] for r in rows],
            ["CPF_REJEITADO_API", "CPF_NAO_ENCONTRADO", "ERRO_API"],
        )
        self.assertEqual(stats.api_errors, 1)

    def test_placeholder_cpf_does_not_query_customer_or_reuse_order_locators(self):
        payload = {
            "cliente": {
                "id": 99,
                "cpf": 0,
                "nome": "Cliente sintético",
                "email": "teste@example.invalid",
            },
            "ultimoPedido": {
                "id": 123,
                "codCliente": 99,
                "emissao": "2026-10-08T10:00:00",
                "status": 2,
                "formaPagamento": 1,
                "frete": 0,
            },
            "itens": [
                {
                    "codPedido": 123,
                    "descricao": "Produto sintético",
                    "quantidade": 2,
                    "valor": 10,
                    "total": 20,
                }
            ],
        }
        farm = Mock()
        farm.get_cliente_by_cpf.return_value = payload
        farm.get_pedido.return_value = {
            "capa": payload["ultimoPedido"],
            "itens": payload["itens"],
        }
        contact = {
            "uuid": "synthetic",
            "fields": {
                "cpf": "00000000000",
                "nome": "Cliente sintético",
                "email": "teste@example.invalid",
            },
        }
        order, items = resolve_contact_order(
            contact, farm_client=farm, captured_at="2026-10-08T10:00:00-03:00"
        )
        self.assertEqual(order["status_confirmacao"], "CPF_INVALIDO_CADASTRO")
        self.assertEqual(order["pedido_id"], "")
        self.assertEqual(items, [])
        farm.get_cliente_by_cpf.assert_not_called()
        farm.get_pedido.assert_not_called()
        self.assertEqual(contact["fields"]["cpf"], "00000000000")
        plan = weni_update._build_contact_plan(
            contact,
            context_messages=[],
            candidate_message_ids=set(),
            farm_client=farm,
            captured_at="2026-10-08T10:00:00-03:00",
            opt_out_group_name="Opt-Out LGPD",
            opt_out_group_uuid="g1",
        )
        self.assertFalse(plan.order_validated)
        self.assertNotIn("pedido_id", plan.fields)
        self.assertNotIn("cpf", plan.fields)
        weni_update.audit_plans([plan], opt_out_group_available=True)

    def test_http_400_does_not_retry(self):
        client = FarmaliderClient("synthetic-token")
        response = Mock(status_code=400, headers={}, content=b"invalid")
        try:
            with (
                patch.object(client.session, "get", return_value=response) as get,
                patch.object(
                    client,
                    "_request_slot",
                    side_effect=lambda _: nullcontext(RequestPermit(0, 4)),
                ),
            ):
                with self.assertRaises(FarmaliderBadRequestError):
                    client.get_cliente_by_cpf("0")
            self.assertEqual(get.call_count, 1)
        finally:
            client.close()

    def test_private_500_diagnostic_preserves_short_identifiers(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            patch(
                "private_diagnostics.private_diagnostic_directory",
                return_value=Path(directory),
            ),
        ):
            for cpf in ["0", "00000000000", "123"]:
                path = append_private_failure(
                    execution_id="synthetic",
                    reference="ref",
                    cpf=cpf,
                    status=500,
                    attempts=4,
                    reason="test",
                    environment="test",
                    raw=b"{}",
                    token="synthetic",
                    summary="safe",
                )
                self.assertIsNotNone(path)
            records = [json.loads(line) for line in path.read_text().splitlines()]
        self.assertEqual([row["cpf"] for row in records], ["0", "00000000000", "123"])

    def test_cli_accepts_zero_and_short_identifiers_without_padding(self):
        for raw in ["0", "00000000000", "123", "529.982.247-25"]:
            self.assertEqual(_normalize_cpf(raw), normalize_cpf(raw))
        with self.assertRaises(RuntimeError):
            _normalize_cpf("")

    def test_weni_dry_run_queries_valid_contacts_before_invalid(self):
        contacts = [
            {"uuid": "a-invalid", "fields": {"cpf": "0"}},
            {"uuid": "z-valid", "fields": {"cpf": VALID}},
            {"uuid": "b-short", "fields": {"cpf": "123"}},
        ]
        calls = []
        farm = Mock()
        farm.get_cliente_by_cpf.side_effect = lambda cpf: calls.append(cpf)
        client = Mock()
        client.list_fields.return_value = [
            {"key": key}
            for key in weni_update.REQUIRED_MULTICHANNEL_FIELDS
            | weni_update.REQUIRED_ORDER_FIELDS
        ]
        client.list_groups.return_value = [
            {"uuid": "g1", "name": "Opt-Out LGPD"},
            {"uuid": "g2", "name": "Opt-In LGPD"},
        ]
        client.list_contacts.return_value = contacts
        client.list_messages.return_value = []
        window = weni_update.UpdateWindow(datetime.now(UTC), None, True)
        with (
            patch("weni_update.WeniClient", return_value=client),
            patch("weni_update._open_farmalider", return_value=farm),
            patch("weni_update._load_state", return_value={}),
            patch("weni_update.resolve_window", return_value=window),
            patch("weni_update._append_audit"),
            patch("weni_update._save_state"),
            tempfile.TemporaryDirectory() as tmp,
        ):
            review = Path(tmp) / "review.json"
            result = weni_update.run_update(apply=False, full=True, review_out=review)
            self.assertEqual(result.invalid_cpf_checked, 2)
            self.assertEqual(result.invalid_cpf_orders_found, 0)
            self.assertEqual(
                result.invalid_cpf_outcomes,
                {"CPF_INVALIDO_CADASTRO": 1, "CPF_NAO_ENCONTRADO": 1},
            )
            payload = json.loads(review.read_text(encoding="utf-8"))
            self.assertEqual(payload["summary"]["invalid_cpf_checked"], 2)
            resumed = weni_update.run_update(
                apply=True, full=True, require_review=review
            )
            self.assertEqual(resumed.invalid_cpf_checked, 2)
            self.assertEqual(resumed.invalid_cpf_outcomes, result.invalid_cpf_outcomes)
        self.assertEqual(calls, [VALID, "123"])
        client.update_contact_fields.assert_not_called()
        client.contact_group_action.assert_not_called()
