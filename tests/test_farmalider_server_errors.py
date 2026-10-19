"""HTTP 500 diagnostics and protection of previous customer validation."""

from __future__ import annotations

import json
import time
import traceback
import unittest
from contextlib import nullcontext
from datetime import UTC, datetime
from unittest.mock import Mock, patch

from requests.exceptions import ConnectionError, ReadTimeout

from farmalider_client import FarmaliderClient, FarmaliderError, RequestPermit
from main import (
    PARTIAL_SYNC_EXIT_CODE,
    FarmaliderSettings,
    _completion_controls,
    build_orders,
    validate_contacts,
)

CPF = "52998224725"  # Synthetic, checksum-valid test fixture.
OLD_TIME = "2026-09-01T12:00:00+00:00"
NEW_TIME = "2026-10-07T12:00:00+00:00"


def _response(status: int, body: bytes = b"", content_type: str = "") -> Mock:
    response = Mock()
    response.status_code = status
    response.content = body
    response.headers = {"Content-Type": content_type}
    return response


def _logged_messages(warnings: Mock) -> str:
    return "\n".join(call.args[0] % call.args[1:] for call in warnings.call_args_list)


class FarmaliderServerErrorTests(unittest.TestCase):
    def test_500_logs_only_safe_body_classification_and_opaque_lookup_ref(self) -> None:
        token = "test-token-should-never-be-logged"
        email = "person@example.invalid"
        body = json.dumps(
            {
                "message": f"Bad customer {CPF}, {email}, token={token}",
            }
        ).encode()
        response = _response(500, body, "application/json; charset=utf-8")
        client = FarmaliderClient(token)
        expected_ref = client.cpf_diagnostic_ref(CPF)
        self.assertEqual(expected_ref, client.cpf_diagnostic_ref(CPF))
        self.assertNotEqual(expected_ref, client.cpf_diagnostic_ref("16899535009"))
        self.assertEqual(
            client.weni_contact_diagnostic_ref("contact-one"),
            client.weni_contact_diagnostic_ref("contact-one"),
        )
        self.assertNotEqual(
            expected_ref,
            client.weni_contact_diagnostic_ref("contact-one"),
        )
        try:
            with (
                patch.object(client.session, "get", return_value=response) as get,
                patch.object(
                    client,
                    "_request_slot",
                    side_effect=lambda _: nullcontext(RequestPermit(0, 4)),
                ),
                patch("farmalider_client.time.sleep"),
                patch("farmalider_client.logging.warning") as warnings,
            ):
                with self.assertRaises(FarmaliderError):
                    client.get_cliente_by_cpf(CPF)
            text = _logged_messages(warnings)
            self.assertEqual(get.call_count, 4)
            self.assertGreaterEqual(text.count(expected_ref), 4)
            self.assertIn("HTTP 500", text)
            self.assertIn("tipo=json", text)
            self.assertIn("categoria=nao_classificado", text)
            for secret in (CPF, email, token, "Bad customer"):
                self.assertNotIn(secret, text)
        finally:
            client.close()

    def test_generic_backend_message_is_classified_without_echoing_body(self) -> None:
        client = FarmaliderClient("synthetic")
        try:
            response = _response(
                500,
                b"Object reference not set to an instance of an object.",
                "text/plain",
            )
            summary = client._safe_server_error_summary(response)
            self.assertIn("categoria=null_reference", summary)
            self.assertNotIn("Object reference", summary)
            nested = _response(
                500,
                b'{"title":"Server error","detail":"Sequence contains no elements"}',
                "text/json",
            )
            self.assertIn(
                "categoria=sequencia_vazia",
                client._safe_server_error_summary(nested),
            )
        finally:
            client.close()

    def test_preflight_retries_timeout_and_recovers(self) -> None:
        client = FarmaliderClient("synthetic")
        try:
            recovered = _response(404)
            with (
                patch.object(
                    client.session,
                    "get",
                    side_effect=[ReadTimeout("slow"), recovered],
                ) as get,
                patch.object(
                    client,
                    "_request_slot",
                    side_effect=lambda _: nullcontext(RequestPermit(0, 4)),
                ),
                patch.object(client, "_protect_after_failure"),
                patch.object(client, "_refresh_network_route", return_value=False),
                patch.object(client, "_sleep_before_retry") as retry,
            ):
                result = client.test_connection()
            self.assertTrue(result["ok"])
            self.assertEqual(result["status_code"], 404)
            self.assertEqual(result["attempts"], 2)
            self.assertEqual(get.call_count, 2)
            retry.assert_called_once()
        finally:
            client.close()

    def test_preflight_retries_5xx_and_recovers(self) -> None:
        client = FarmaliderClient("synthetic")
        try:
            failure = _response(500)
            recovered = _response(404)
            with (
                patch.object(
                    client.session,
                    "get",
                    side_effect=[failure, recovered],
                ) as get,
                patch.object(
                    client,
                    "_request_slot",
                    side_effect=lambda _: nullcontext(RequestPermit(0, 4)),
                ),
                patch.object(client, "_protect_after_failure"),
                patch.object(client, "_sleep_before_retry") as retry,
            ):
                result = client.test_connection()
            self.assertTrue(result["ok"])
            self.assertEqual(result["attempts"], 2)
            self.assertEqual(get.call_count, 2)
            retry.assert_called_once()
        finally:
            client.close()

    def test_preflight_auth_failure_does_not_retry(self) -> None:
        client = FarmaliderClient("synthetic")
        try:
            with (
                patch.object(client.session, "get", return_value=_response(401)) as get,
                patch.object(
                    client,
                    "_request_slot",
                    side_effect=lambda _: nullcontext(RequestPermit(0, 4)),
                ),
            ):
                with self.assertRaisesRegex(FarmaliderError, "401"):
                    client.test_connection()
            self.assertEqual(get.call_count, 1)
        finally:
            client.close()

    def test_preflight_exhausts_network_retries_before_failing(self) -> None:
        client = FarmaliderClient("synthetic")
        try:
            with (
                patch.object(
                    client.session, "get", side_effect=ReadTimeout("slow")
                ) as get,
                patch.object(
                    client,
                    "_request_slot",
                    side_effect=lambda _: nullcontext(RequestPermit(0, 4)),
                ),
                patch.object(client, "_protect_after_failure"),
                patch.object(client, "_refresh_network_route", return_value=False),
                patch.object(client, "_sleep_before_retry"),
                patch("farmalider_client.FARMALIDER_MAX_RETRIES", 3),
            ):
                with self.assertRaisesRegex(FarmaliderError, r"após 3 tentativa\(s\)"):
                    client.test_connection()
            self.assertEqual(get.call_count, 3)
        finally:
            client.close()

    def test_500_retry_after_cannot_remove_exponential_backoff(self) -> None:
        client = FarmaliderClient("synthetic")
        response = _response(500)
        response.headers["Retry-After"] = "0"
        try:
            self.assertEqual(client._retry_delay(2, response), 10.0)
        finally:
            client.close()

    def test_404_is_not_retried_and_network_exception_hides_url_and_token(self) -> None:
        client = FarmaliderClient("top-secret-token")
        try:
            with patch.object(
                client.session, "get", return_value=_response(404)
            ) as get:
                self.assertIsNone(client.get_cliente_by_cpf(CPF))
            self.assertEqual(get.call_count, 1)
            unsafe_url = (
                f"https://api.example.invalid/cliente/cpf/{CPF}?token=top-secret-token"
            )
            with (
                patch.object(
                    client.session, "get", side_effect=ConnectionError(unsafe_url)
                ),
                patch.object(
                    client,
                    "_request_slot",
                    side_effect=lambda _: nullcontext(RequestPermit(0, 4)),
                ),
                patch("farmalider_client.FARMALIDER_MAX_RETRIES", 1),
            ):
                try:
                    client.get_cliente_by_cpf(CPF)
                except FarmaliderError:
                    rendered = traceback.format_exc()
                else:
                    self.fail("Expected a network error")
            self.assertNotIn(CPF, rendered)
            self.assertNotIn("top-secret-token", rendered)
        finally:
            client.close()

    def test_500_keeps_verified_customer_when_cpf_is_unchanged(self) -> None:
        farm = Mock()
        farm.get_cliente_by_cpf.side_effect = FarmaliderError("HTTP 500")
        farm.metrics_snapshot.return_value = {
            "total_requests": 4,
            "retries": 3,
            "rate_limits": 0,
            "server_errors": 4,
            "network_errors": 0,
            "slow_requests": 0,
            "average_response_ms": 1.0,
            "p95_response_ms": 1.0,
            "max_response_ms": 1.0,
            "effective_delay_ms": 10.0,
        }
        previous = {
            "contact_uuid": "contact-one",
            "cpf_normalizado": CPF,
            "farmalider_cliente_status": "CPF_CONFIRMADO",
            "farmalider_cliente_id": "321",
            "farmalider_cliente_nome": "Cliente anterior",
            "farmalider_cliente_consultado_em": OLD_TIME,
        }
        rows = [
            {"contact_uuid": "contact-one", "cpf": CPF},
            {"contact_uuid": "contact-two", "cpf": "16899535009"},
        ]
        lookup_status: dict[str, str] = {}
        with patch("main.logging.warning") as warnings:
            stats = validate_contacts(
                rows,
                farm_client=farm,
                farm_settings=FarmaliderSettings(True, "synthetic", False),
                sync_time=NEW_TIME,
                existing_by_uuid={"contact-one": previous, "contact-two": previous},
                reuse_existing=False,
                cliente_lookup_status_by_cpf=lookup_status,
            )
        self.assertEqual(rows[0]["farmalider_cliente_status"], "ERRO_API")
        self.assertEqual(rows[0]["farmalider_cliente_id"], "321")
        self.assertEqual(rows[0]["farmalider_cliente_consultado_em"], OLD_TIME)
        self.assertEqual(rows[1]["farmalider_cliente_status"], "ERRO_API")
        self.assertEqual(rows[1]["farmalider_cliente_id"], "")
        self.assertEqual(stats.api_errors, 2)
        self.assertEqual(stats.prior_details_retained, 1)
        self.assertEqual(stats.confirmed, 0)
        self.assertEqual(set(lookup_status.values()), {"ERRO_API"})
        text = _logged_messages(warnings)
        for private in (CPF, "16899535009", "contact-one", "contact-two"):
            self.assertNotIn(private, text)
        self.assertIn("contato_ref=", text)

        # A second partial run must not erase the previously confirmed ID.
        repeated = {"contact_uuid": "contact-one", "cpf": CPF}
        validate_contacts(
            [repeated],
            farm_client=farm,
            farm_settings=FarmaliderSettings(True, "synthetic", False),
            sync_time=NEW_TIME,
            existing_by_uuid={"contact-one": rows[0]},
        )
        self.assertEqual(repeated["farmalider_cliente_status"], "ERRO_API")
        self.assertEqual(repeated["farmalider_cliente_id"], "321")

    def test_previous_404_is_not_kept_as_current_result_after_500(self) -> None:
        farm = Mock()
        farm.get_cliente_by_cpf.side_effect = FarmaliderError("HTTP 500")
        farm.metrics_snapshot.return_value = {
            "total_requests": 4,
            "retries": 3,
            "rate_limits": 0,
            "server_errors": 4,
            "network_errors": 0,
            "slow_requests": 0,
            "average_response_ms": 1.0,
            "p95_response_ms": 1.0,
            "max_response_ms": 1.0,
            "effective_delay_ms": 10.0,
        }
        row = {"contact_uuid": "contact-one", "cpf": CPF}
        previous = {
            "contact_uuid": "contact-one",
            "cpf_normalizado": CPF,
            "farmalider_cliente_status": "CPF_NAO_ENCONTRADO",
            "farmalider_cliente_consultado_em": OLD_TIME,
        }
        validate_contacts(
            [row],
            farm_client=farm,
            farm_settings=FarmaliderSettings(True, "synthetic", False),
            sync_time=NEW_TIME,
            existing_by_uuid={"contact-one": previous},
        )
        self.assertEqual(row["farmalider_cliente_status"], "ERRO_API")
        self.assertEqual(row["farmalider_cliente_id"], "")

    def test_partial_run_keeps_incremental_checkpoint_for_retry(self) -> None:
        started = datetime(2026, 10, 7, tzinfo=UTC)
        status, controls = _completion_controls(started, api_errors=1)
        self.assertEqual(status, "parcial")
        self.assertEqual(PARTIAL_SYNC_EXIT_CODE, 3)
        self.assertNotIn("ultima_sincronizacao_sucesso_utc", controls)
        status, controls = _completion_controls(started, api_errors=0)
        self.assertEqual(status, "sucesso")
        self.assertEqual(
            controls["ultima_sincronizacao_sucesso_utc"],
            "2026-10-07T00:00:00+00:00",
        )

    def test_order_lookup_error_after_cached_customer_is_counted(self) -> None:
        contact = {"uuid": "synthetic-contact", "fields": {"cpf": CPF}}
        api = Mock()
        api.get_cliente_by_cpf.side_effect = FarmaliderError("HTTP 500")
        api.get_pedido.side_effect = AssertionError("No order locator")
        orders, items, stats = build_orders(
            [contact],
            rows_by_uuid={
                "synthetic-contact": {"farmalider_cliente_id": "321"},
            },
            farm_client=api,
            sync_time=NEW_TIME,
        )
        self.assertEqual(len(orders), 1)
        self.assertEqual(orders[0]["status_confirmacao"], "ERRO_API")
        self.assertEqual(items, [])
        self.assertEqual(stats.api_errors, 1)

    def test_known_404_and_500_are_not_queried_again_for_orders(self) -> None:
        contact = {"uuid": "synthetic-contact", "fields": {"cpf": CPF}}
        api = Mock()
        api.get_cliente_by_cpf.side_effect = AssertionError("duplicate CPF lookup")
        api.get_pedido.side_effect = AssertionError("no order locator exists")
        for status in ("CPF_NAO_ENCONTRADO", "ERRO_API"):
            with self.subTest(status=status):
                orders, items, _ = build_orders(
                    [contact],
                    rows_by_uuid={"synthetic-contact": {}},
                    farm_client=api,
                    sync_time=NEW_TIME,
                    cliente_lookup_status_by_cpf={CPF: status},
                )
                self.assertEqual(len(orders), 1)
                self.assertEqual(orders[0]["status_confirmacao"], status)
                self.assertEqual(items, [])
        api.get_cliente_by_cpf.assert_not_called()
        api.get_pedido.assert_not_called()

    def test_batch_aborts_before_finishing_500_failing_contacts(self) -> None:
        farm = Mock()
        calls = 0

        def fail(_cpf: str):
            nonlocal calls
            calls += 1
            time.sleep(0.001)
            raise FarmaliderError("HTTP 500")

        farm.get_cliente_by_cpf.side_effect = fail
        rows = [
            {"contact_uuid": f"contact-{index}", "cpf": f"{index + 1000:011d}"}
            for index in range(200)
        ]
        with (
            patch("main.is_valid_cpf", return_value=True),
            patch("main.FARMALIDER_BATCH_SIZE", 200),
            patch("main.FARMALIDER_MAX_WORKERS", 1),
            patch("main.FARMALIDER_BATCH_ERROR_ABORT_MIN_ERRORS", 2),
            patch("main.FARMALIDER_BATCH_ERROR_ABORT_RATE", 0.5),
            patch("main.logging.warning"),
        ):
            with self.assertRaises(FarmaliderError):
                validate_contacts(
                    rows,
                    farm_client=farm,
                    farm_settings=FarmaliderSettings(True, "synthetic", False),
                    sync_time=NEW_TIME,
                )
        self.assertEqual(calls, 100)

    def test_fast_500s_do_not_bias_the_completed_chunk_error_rate(self) -> None:
        farm = Mock()

        def query(cpf: str):
            if int(cpf) < 1030:
                raise FarmaliderError("HTTP 500")
            time.sleep(0.002)
            return None  # A 404 is a successful HTTP consultation.

        farm.get_cliente_by_cpf.side_effect = query
        farm.metrics_snapshot.return_value = {
            "total_requests": 100,
            "retries": 0,
            "rate_limits": 0,
            "server_errors": 30,
            "network_errors": 0,
            "slow_requests": 0,
            "average_response_ms": 1.0,
            "p95_response_ms": 1.0,
            "max_response_ms": 1.0,
            "effective_delay_ms": 10.0,
        }
        rows = [
            {"contact_uuid": f"contact-{index}", "cpf": f"{index + 1000:011d}"}
            for index in range(100)
        ]
        with (
            patch("main.is_valid_cpf", return_value=True),
            patch("main.FARMALIDER_MAX_WORKERS", 4),
            patch("main.FARMALIDER_BATCH_ERROR_ABORT_MIN_ERRORS", 20),
            patch("main.FARMALIDER_BATCH_ERROR_ABORT_RATE", 0.5),
            patch("main.logging.warning"),
        ):
            stats = validate_contacts(
                rows,
                farm_client=farm,
                farm_settings=FarmaliderSettings(True, "synthetic", False),
                sync_time=NEW_TIME,
            )
        self.assertEqual(farm.get_cliente_by_cpf.call_count, 100)
        self.assertEqual(stats.api_errors, 30)


if __name__ == "__main__":
    unittest.main()
