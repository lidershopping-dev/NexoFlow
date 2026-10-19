from __future__ import annotations

import logging
import math
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from analytics import (
    build_bi_ads_rows,
    build_bi_channel_records,
    build_bi_conversion_records,
    build_bi_order_rows,
    build_bi_sales_rows,
    build_bi_summary_record,
    build_daily_metrics,
    build_sales_metrics,
    cpf_query_priority,
    get_fields,
    is_relevant_contact,
    is_valid_cpf,
    mark_full_load_quarter_complete,
    normalize_contact,
    normalize_cpf,
    prepare_full_load_quarter,
    validate_full_load_quarter,
)
from config import (
    FARMALIDER_BATCH_ERROR_ABORT_MIN_ERRORS,
    FARMALIDER_BATCH_ERROR_ABORT_RATE,
    FARMALIDER_BATCH_SIZE,
    FARMALIDER_CACHE_CONFIRMED_HOURS,
    FARMALIDER_CACHE_NOT_FOUND_HOURS,
    FARMALIDER_MAX_WORKERS,
    FARMALIDER_PARALLEL_MIN_CPFS,
    FARMALIDER_REQUEST_DELAY_SECONDS,
    SYNC_OVERLAP_MINUTES,
)
from farmalider_client import (
    FarmaliderAuthError,
    FarmaliderBadRequestError,
    FarmaliderClient,
    FarmaliderError,
    FarmaliderUnavailableError,
)
from farmalider_integration import (
    cliente_link_status,
    cpf_cadastro_status,
    resolve_contact_order,
    weni_customer_identity,
)
from google_sheets import GoogleSheetsStore
from logging_config import configure_logging
from looker_bi import build_looker_tables, reporting_period
from observed_orders import (
    observed_orders_by_contact,
    observed_orders_by_identity,
    preserve_old_latest_order,
    revalidate_observed_orders,
    verified_order_cancellation,
)
from pedido_utils import PENDING_ITEMS_STATUS
from product_scope import PHARMACY_SCOPE_VERSION
from project_paths import load_private_settings
from task_progress import TaskProgress, request_progress
from weni_client import WeniClient

secrets = load_private_settings()

Contact = dict[str, Any]
Row = dict[str, str]
ClienteResult = tuple[str, str, str]

TOKEN_PLACEHOLDER = "COLE_SEU_TOKEN_WENI_AQUI"
FARMALIDER_TOKEN_PLACEHOLDER = "COLE_SEU_TOKEN_FARMALIDER_AQUI"
SHEET_PLACEHOLDER = "COLE_O_ID_DA_PLANILHA_GOOGLE_AQUI"
APP_VERSION = "1.0"
PARTIAL_SYNC_EXIT_CODE = 3
ORDER_API_FAILURES = frozenset(
    {
        "ERRO_API",
        "ERRO_RESPOSTA_CLIENTE_API",
        "CLIENTE_ID_INVALIDO_API",
        "CLIENTE_ID_DIVERGENTE_API",
        "PEDIDO_NAO_ENCONTRADO_API",
        "ERRO_RESPOSTA_PEDIDO_API",
        "PEDIDO_ID_INVALIDO_API",
        "PEDIDO_ID_DIVERGENTE_API",
        "PEDIDO_CLIENTE_NAO_VERIFICADO_API",
        "PEDIDO_CLIENTE_DIVERGENTE_API",
        "PEDIDO_DATA_INVALIDA_API",
        "ERRO_ITENS_PEDIDO_API",
        "ITEM_PEDIDO_DIVERGENTE_API",
    }
)


@dataclass(frozen=True)
class FarmaliderSettings:
    enabled: bool
    token: str
    use_hml: bool


@dataclass
class ClienteValidationStats:
    unique_queries: int = 0
    cache_reused: int = 0
    confirmed: int = 0
    not_found: int = 0
    invalid: int = 0
    invalid_registration: int = 0
    api_errors: int = 0
    prior_details_retained: int = 0
    http_requests: int = 0
    retries: int = 0
    rate_limits: int = 0
    server_errors: int = 0
    network_errors: int = 0
    slow_requests: int = 0
    average_response_ms: float = 0.0
    p95_response_ms: float = 0.0
    max_response_ms: float = 0.0
    effective_delay_ms: float = 0.0
    responses_200: int = 0
    responses_404: int = 0
    concurrency_limit: int = 0
    recovery_activations: int = 0
    server_error_unique_queries: int = 0
    server_error_serial_queries: int = 0
    elapsed_seconds: float = 0.0


@dataclass
class OrderStats:
    parse_errors: int = 0
    confirmed_api: int = 0
    api_errors: int = 0
    cancellations_without_items: int = 0
    pending_without_items: int = 0
    failure_reasons: dict[str, int] = field(default_factory=dict)


def _apply_request_metrics(
    stats: ClienteValidationStats, metrics: dict[str, Any]
) -> None:
    for attribute, key in {
        "http_requests": "total_requests",
        "retries": "retries",
        "rate_limits": "rate_limits",
        "server_errors": "server_errors",
        "network_errors": "network_errors",
        "slow_requests": "slow_requests",
        "responses_200": "responses_200",
        "responses_404": "responses_404",
        "concurrency_limit": "concurrency_limit",
        "recovery_activations": "recovery_activations",
        "server_error_unique_queries": "server_error_unique_queries",
        "server_error_serial_queries": "server_error_serial_queries",
    }.items():
        setattr(stats, attribute, int(metrics.get(key, 0)))
    for key in (
        "average_response_ms",
        "p95_response_ms",
        "max_response_ms",
        "effective_delay_ms",
    ):
        setattr(stats, key, float(metrics.get(key, 0)))


def validate_configuration() -> None:
    weni_token = str(getattr(secrets, "WENI_API_TOKEN", "")).strip()
    spreadsheet_id = str(getattr(secrets, "GOOGLE_SPREADSHEET_ID", "")).strip()

    if not weni_token or weni_token == TOKEN_PLACEHOLDER:
        raise RuntimeError(
            "Cole o token da Weni em secrets_config.py antes de executar."
        )
    if not spreadsheet_id or spreadsheet_id == SHEET_PLACEHOLDER:
        raise RuntimeError(
            "Cole o ID da planilha Google em secrets_config.py antes de executar."
        )


def load_farmalider_settings() -> FarmaliderSettings:
    token = str(getattr(secrets, "FARMALIDER_API_TOKEN", "")).strip()
    if token == FARMALIDER_TOKEN_PLACEHOLDER:
        token = ""

    return FarmaliderSettings(
        enabled=bool(getattr(secrets, "FARMALIDER_API_ENABLED", True)),
        token=token,
        use_hml=bool(getattr(secrets, "FARMALIDER_USE_HML", False)),
    )


def parse_control_datetime(value: str) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def utc_iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat(timespec="seconds")


def _completion_controls(
    sync_started: datetime,
    *,
    api_errors: int,
) -> tuple[str, dict[str, str]]:
    """Preserve the cursor while customer or observed-order lookups need retry."""
    if api_errors:
        return "parcial", {"ultima_execucao_status": "parcial"}
    return "sucesso", {
        "ultima_execucao_status": "sucesso",
        "ultima_sincronizacao_sucesso_utc": utc_iso(sync_started),
    }


def contact_has_cpf(contact: Contact) -> bool:
    fields = contact.get("fields")
    if not isinstance(fields, dict):
        return False
    return bool(normalize_cpf(fields.get("cpf")))


def split_contacts(
    contacts: list[Contact],
) -> tuple[list[Contact], list[Contact], list[Contact]]:
    """Separa contatos relevantes/com CPF e mantém toda a base para o Analytics/BI."""
    relevant: list[Contact] = []
    with_cpf: list[Contact] = []

    for contact in contacts:
        if is_relevant_contact(contact):
            relevant.append(contact)
        if contact_has_cpf(contact):
            with_cpf.append(contact)

    # Desde o NexoFlow 1.0 o Analytics preserva todos os contatos retornados pela Weni.
    # Consulta CPFs válidos primeiro e depois os demais informados. Pedidos podem ser
    # recuperados para estes contatos mesmo quando ultimo_pedido está vazio.
    return relevant, with_cpf, list(contacts)


def _format_duration(seconds: float) -> str:
    seconds = max(0, math.ceil(seconds))
    minutes, sec = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)

    if hours:
        return f"{hours}h{minutes:02d}m"
    if minutes:
        return f"{minutes}m{sec:02d}s"
    return f"{sec}s"


def _format_ms_as_seconds(milliseconds: float) -> str:
    """Formata latência interna em ms para exibição legível em segundos."""
    return f"{max(0.0, milliseconds) / 1000:.2f}s"


def _query_cliente(
    cpf: str,
    farm_client: FarmaliderClient,
    response_cache: dict[str, dict[str, Any] | list[dict[str, Any]] | None]
    | None = None,
) -> ClienteResult:
    try:
        api_cliente = farm_client.get_cliente_by_cpf(cpf)
        if response_cache is not None:
            response_cache[cpf] = api_cliente
        return cliente_link_status(cpf, api_cliente)
    except (FarmaliderAuthError, FarmaliderUnavailableError):
        raise
    except FarmaliderBadRequestError:
        return "CPF_REJEITADO_API", "", ""
    except FarmaliderError:
        # O erro individual vira status. Se a taxa de falha do lote subir
        # demais, validate_contacts interrompe a carga antes de gravar.
        return "ERRO_API", "", ""


def _apply_cliente_result(
    row: Row,
    result: ClienteResult,
) -> None:
    status, cliente_id, cliente_nome = result
    row["farmalider_cliente_status"] = status
    row["farmalider_cliente_id"] = cliente_id
    row["farmalider_cliente_nome"] = cliente_nome


def _preserve_cliente_on_api_error(
    row: Row,
    existing_by_uuid: dict[str, Row] | None,
) -> bool:
    """Keep prior verified details, while recording the current lookup as failed."""
    if not existing_by_uuid:
        return False
    existing = existing_by_uuid.get(str(row.get("contact_uuid") or "").strip())
    if not existing:
        return False
    cpf = normalize_cpf(row.get("cpf_normalizado") or row.get("cpf"))
    existing_cpf = normalize_cpf(existing.get("cpf_normalizado") or existing.get("cpf"))
    status = str(existing.get("farmalider_cliente_status") or "").strip()
    if (
        not is_valid_cpf(cpf)
        or cpf != existing_cpf
        or status
        not in {
            "CPF_CONFIRMADO",
            "CPF_INVALIDO_ENCONTRADO",
            "ERRO_API",
        }
    ):
        return False
    if not str(existing.get("farmalider_cliente_id") or "").strip():
        return False
    row["farmalider_cliente_id"] = str(existing.get("farmalider_cliente_id") or "")
    row["farmalider_cliente_nome"] = str(existing.get("farmalider_cliente_nome") or "")
    # The old timestamp describes the last successful validation, not this error.
    row["farmalider_cliente_consultado_em"] = str(
        existing.get("farmalider_cliente_consultado_em") or ""
    )
    return True


def _parse_iso_datetime(value: str) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC)
    except ValueError:
        return None


def _reusable_farmalider_result(
    row: Row,
    existing: Row | None,
    *,
    now: datetime,
) -> ClienteResult | None:
    if not existing:
        return None
    cpf = normalize_cpf(row.get("cpf", ""))
    existing_cpf = normalize_cpf(
        existing.get("cpf_normalizado") or existing.get("cpf", "")
    )
    if not is_valid_cpf(cpf) or cpf != existing_cpf:
        return None

    status = str(existing.get("farmalider_cliente_status") or "").strip()
    if status not in {
        "CPF_CONFIRMADO",
        "CPF_INVALIDO_ENCONTRADO",
        "CPF_DIVERGENTE_API",
        "CPF_NAO_ENCONTRADO",
    }:
        return None
    consulted = _parse_iso_datetime(
        str(existing.get("farmalider_cliente_consultado_em") or "")
    )
    if consulted is None:
        return None
    max_hours = (
        FARMALIDER_CACHE_NOT_FOUND_HOURS
        if status == "CPF_NAO_ENCONTRADO"
        else FARMALIDER_CACHE_CONFIRMED_HOURS
    )
    if (now - consulted).total_seconds() > max(0, float(max_hours)) * 3600:
        return None
    return (
        status,
        str(existing.get("farmalider_cliente_id") or ""),
        str(existing.get("farmalider_cliente_nome") or ""),
    )


def validate_contacts(
    rows: list[Row],
    *,
    farm_client: FarmaliderClient | None,
    farm_settings: FarmaliderSettings,
    sync_time: str,
    existing_by_uuid: dict[str, Row] | None = None,
    reuse_existing: bool = False,
    cliente_responses_by_cpf: dict[str, dict[str, Any] | list[dict[str, Any]] | None]
    | None = None,
    cliente_lookup_status_by_cpf: dict[str, str] | None = None,
    identity_by_uuid: dict[str, dict[str, Any]] | None = None,
) -> ClienteValidationStats:
    """
    Consulta CPFs válidos primeiro, depois inválidos, com cache por CPF único.

    A API recebe um CPF por requisição. O projeto agrupa até
    FARMALIDER_BATCH_SIZE CPFs por lote e executa no máximo
    FARMALIDER_MAX_WORKERS chamadas simultâneas. O FarmaliderClient ainda
    aplica um limitador GLOBAL entre inícios de requisição e cooldown de 429.
    """
    stats = ClienteValidationStats()
    started = time.monotonic()
    rows_by_cpf: dict[str, list[Row]] = {}

    for row in rows:
        cpf = normalize_cpf(row.get("cpf", ""))
        row["cpf_normalizado"] = cpf
        row["farmalider_cliente_consultado_em"] = ""
        row["farmalider_cliente_id"] = ""
        row["farmalider_cliente_nome"] = ""

        cadastro_status = cpf_cadastro_status(row.get("cpf", ""))
        if cadastro_status:
            row["farmalider_cliente_status"] = cadastro_status
            if cadastro_status == "CPF_INVALIDO_CADASTRO":
                stats.invalid_registration += 1
            if cpf and cliente_lookup_status_by_cpf is not None:
                cliente_lookup_status_by_cpf[cpf] = cadastro_status
            continue

        if farm_client is None:
            row["farmalider_cliente_status"] = (
                "TOKEN_NAO_CONFIGURADO"
                if farm_settings.enabled and not farm_settings.token
                else "API_DESABILITADA"
            )
            continue

        if reuse_existing and existing_by_uuid is not None:
            existing = existing_by_uuid.get(str(row.get("contact_uuid") or "").strip())
            reused = _reusable_farmalider_result(
                row,
                existing,
                now=datetime.now(UTC),
            )
            if reused is not None:
                _apply_cliente_result(row, reused)
                row["farmalider_cliente_consultado_em"] = str(
                    (existing or {}).get("farmalider_cliente_consultado_em") or ""
                )
                if (
                    reused[0] == "CPF_NAO_ENCONTRADO"
                    and cliente_lookup_status_by_cpf is not None
                ):
                    cliente_lookup_status_by_cpf[cpf] = reused[0]
                stats.cache_reused += 1
                continue

        row["farmalider_cliente_consultado_em"] = sync_time
        rows_by_cpf.setdefault(cpf, []).append(row)

    unique_cpfs = list(rows_by_cpf)
    cache: dict[str, ClienteResult] = {}
    response_cache = (
        cliente_responses_by_cpf if cliente_responses_by_cpf is not None else {}
    )

    if unique_cpfs and farm_client is not None:
        parallel_min = max(1, int(FARMALIDER_PARALLEL_MIN_CPFS))
        worker_count = (
            1
            if len(unique_cpfs) < parallel_min
            else min(
                max(1, int(FARMALIDER_MAX_WORKERS)),
                len(unique_cpfs),
            )
        )
        batch_size = max(1, int(FARMALIDER_BATCH_SIZE))
        delay_ms = int(round(FARMALIDER_REQUEST_DELAY_SECONDS * 1000))
        rate_cap = (
            1 / FARMALIDER_REQUEST_DELAY_SECONDS
            if FARMALIDER_REQUEST_DELAY_SECONDS > 0
            else 0
        )
        valid_cpfs = [cpf for cpf in unique_cpfs if is_valid_cpf(cpf)]
        invalid_cpfs = [cpf for cpf in unique_cpfs if not is_valid_cpf(cpf)]
        # Separate batches are a completion barrier, including when a phase is
        # smaller than the batch/chunk size. Never mix phases in the executor.
        batches = [
            (label, phase[start : start + batch_size])
            for label, phase in (("válidos", valid_cpfs), ("inválidos", invalid_cpfs))
            for start in range(0, len(phase), batch_size)
        ]
        total_batches = len(batches)

        logging.info(
            "FarmaLíder: %s CPFs únicos: %s válidos primeiro, %s inválidos depois. "
            "Concorrência controlada com %s workers, lotes de até %s CPFs, "
            "paralelismo a partir de %s CPFs, intervalo global mínimo de %s ms%s.",
            len(unique_cpfs),
            len(valid_cpfs),
            len(invalid_cpfs),
            worker_count,
            batch_size,
            parallel_min,
            delay_ms,
            (f" (~{rate_cap:.0f} inícios/s no máximo)" if rate_cap > 0 else ""),
        )

        executor = ThreadPoolExecutor(
            max_workers=worker_count,
            thread_name_prefix="farmalider",
        )
        completed = 0

        with TaskProgress(
            "FarmaLíder: consultas de CPF", len(unique_cpfs), unit="CPFs"
        ) as progress:
            try:
                for batch_number, (phase, batch) in enumerate(batches, start=1):
                    logging.info(
                        "FarmaLíder: etapa de CPFs %s | lote %s/%s | %s consultas.",
                        phase,
                        batch_number,
                        total_batches,
                        len(batch),
                    )
                    batch_errors = 0
                    batch_completed = 0
                    # Evaluate complete, bounded sub-batches. The first responses
                    # to finish may be fast failures while successful calls are slow.
                    for chunk_start in range(0, len(batch), 100):
                        chunk = batch[chunk_start : chunk_start + 100]
                        futures = {
                            executor.submit(
                                _query_cliente,
                                cpf,
                                farm_client,
                                response_cache,
                            ): cpf
                            for cpf in chunk
                        }
                        for future in as_completed(futures):
                            cpf = futures[future]
                            try:
                                result = future.result()
                            except (FarmaliderAuthError, FarmaliderUnavailableError):
                                if isinstance(farm_client, FarmaliderClient):
                                    farm_client.cancel_pending_requests()
                                for pending in futures:
                                    pending.cancel()
                                raise

                            cache[cpf] = result
                            if cliente_lookup_status_by_cpf is not None:
                                cliente_lookup_status_by_cpf[cpf] = result[0]
                            completed += 1
                            progress.advance()
                            batch_completed += 1
                            if result[0] == "ERRO_API":
                                batch_errors += 1
                                contact_refs = (
                                    sorted(
                                        {
                                            farm_client.weni_contact_diagnostic_ref(
                                                str(row["contact_uuid"])
                                            )
                                            for row in rows_by_cpf[cpf]
                                            if row.get("contact_uuid")
                                        }
                                    )
                                    if isinstance(farm_client, FarmaliderClient)
                                    else []
                                )
                                lookup_ref = (
                                    farm_client.cpf_diagnostic_ref(cpf)
                                    if isinstance(farm_client, FarmaliderClient)
                                    else "-"
                                )
                                logging.warning(
                                    "FarmaLíder cliente/cpf [consulta %s]: falha para %s "
                                    "contato(s) Weni; contato_ref=%s%s.",
                                    lookup_ref,
                                    len(rows_by_cpf[cpf]),
                                    ",".join(contact_refs[:3]) or "indisponivel",
                                    ",..." if len(contact_refs) > 3 else "",
                                )

                        if (
                            batch_completed >= 100
                            and batch_errors >= FARMALIDER_BATCH_ERROR_ABORT_MIN_ERRORS
                            and batch_errors / batch_completed
                            >= FARMALIDER_BATCH_ERROR_ABORT_RATE
                        ):
                            raise FarmaliderError(
                                "FarmaLíder apresentou taxa elevada de erros no lote "
                                f"{batch_number}/{total_batches}: "
                                f"{batch_errors}/{batch_completed} consultas concluídas "
                                f"({batch_errors / batch_completed:.1%}). "
                                "Carga interrompida antes de atualizar contatos "
                                "e pedidos no Google Sheets."
                            )

                    error_rate = batch_errors / len(batch) if batch else 0.0
                    if (
                        batch_errors >= FARMALIDER_BATCH_ERROR_ABORT_MIN_ERRORS
                        and error_rate >= FARMALIDER_BATCH_ERROR_ABORT_RATE
                    ):
                        raise FarmaliderError(
                            "FarmaLíder apresentou taxa elevada de erros no lote "
                            f"{batch_number}/{total_batches}: "
                            f"{batch_errors}/{len(batch)} "
                            f"({error_rate:.1%}). Carga interrompida antes da "
                            "atualização de contatos e pedidos no Google Sheets."
                        )

                    elapsed = max(time.monotonic() - started, 0.001)
                    rate = completed / elapsed
                    remaining = len(unique_cpfs) - completed
                    eta = remaining / rate if rate > 0 else 0.0
                    metrics = farm_client.metrics_snapshot()

                    logging.info(
                        "FarmaLíder: %s/%s CPFs concluídos | lote %s/%s | "
                        "%.1f CPF/s | ETA %s | HTTP=%s retries=%s 429=%s 5xx=%s "
                        "lentas=%s p95=%s max=%s delay=%.0fms limite=%s 200=%s 404=%s.",
                        completed,
                        len(unique_cpfs),
                        batch_number,
                        total_batches,
                        rate,
                        _format_duration(eta),
                        metrics["total_requests"],
                        metrics["retries"],
                        metrics["rate_limits"],
                        metrics["server_errors"],
                        metrics.get("slow_requests", 0),
                        _format_ms_as_seconds(metrics.get("p95_response_ms", 0.0)),
                        _format_ms_as_seconds(metrics["max_response_ms"]),
                        metrics["effective_delay_ms"],
                        metrics.get("concurrency_limit", worker_count),
                        metrics.get("responses_200", 0),
                        metrics.get("responses_404", 0),
                    )
            finally:
                executor.shutdown(wait=True, cancel_futures=True)

        for cpf, cpf_rows in rows_by_cpf.items():
            query_result = cache.get(cpf, ("ERRO_API", "", ""))
            for row in cpf_rows:
                result = query_result
                if not is_valid_cpf(cpf) and cpf in response_cache:
                    identity = (identity_by_uuid or {}).get(
                        str(row.get("contact_uuid") or ""), row
                    )
                    result = cliente_link_status(
                        cpf, response_cache[cpf], weni_identity=identity
                    )
                _apply_cliente_result(row, result)
                if result[0] == "ERRO_API" and _preserve_cliente_on_api_error(
                    row,
                    existing_by_uuid,
                ):
                    stats.prior_details_retained += 1

        metrics = farm_client.metrics_snapshot()
        stats.unique_queries = len(cache)
        _apply_request_metrics(stats, metrics)
        stats.api_errors = sum(
            len(rows_by_cpf[cpf])
            for cpf, result in cache.items()
            if result[0] == "ERRO_API"
        )

    for row in rows:
        status = row.get("farmalider_cliente_status", "")
        if status in {"CPF_CONFIRMADO", "CPF_INVALIDO_ENCONTRADO"}:
            stats.confirmed += 1
        elif status == "CPF_NAO_ENCONTRADO":
            stats.not_found += 1
        if normalize_cpf(row.get("cpf")) and not is_valid_cpf(row.get("cpf")):
            stats.invalid += 1

    identity_counts = {
        status: sum(row.get("farmalider_cliente_status") == status for row in rows)
        for status in (
            "CPF_INVALIDO_ENCONTRADO",
            "CPF_IDENTIDADE_INSUFICIENTE",
            "CPF_IDENTIDADE_DIVERGENTE",
            "CPF_IDENTIDADE_AMBIGUA",
        )
    }
    if stats.invalid:
        logging.info(
            "FarmaLíder: identidade de contatos com CPF inválido: %s.", identity_counts
        )
    if stats.invalid_registration:
        logging.info(
            "FarmaLíder: %s contato(s) com CPF inválido no cadastro; "
            "nenhuma consulta enviada para esses valores. Originais preservados.",
            stats.invalid_registration,
        )
    stats.elapsed_seconds = time.monotonic() - started
    return stats


def build_orders(
    relevant_contacts: list[Contact],
    *,
    rows_by_uuid: dict[str, Row],
    farm_client: FarmaliderClient | None,
    sync_time: str,
    cliente_responses_by_cpf: dict[str, dict[str, Any] | list[dict[str, Any]] | None]
    | None = None,
    cliente_lookup_status_by_cpf: dict[str, str] | None = None,
    pedido_responses_by_id: dict[str, dict[str, Any] | list[dict[str, Any]] | None]
    | None = None,
    existing_orders: list[Row] | None = None,
) -> tuple[list[Row], list[Row], OrderStats]:
    snapshots: dict[str, tuple[Row, list[Row]]] = {}
    contacts_by_order: dict[str, set[str]] = {}
    if pedido_responses_by_id is None:
        pedido_responses_by_id = {}
    stats = OrderStats()
    prior_by_contact = observed_orders_by_contact(existing_orders or [])
    prior_by_identity = observed_orders_by_identity(existing_orders or [])

    # CPF duplicates can point to the same official order. Select a complete
    # order/items snapshot consistently, regardless of Weni pagination order.
    ordered_contacts = sorted(
        relevant_contacts,
        key=lambda row: (
            cpf_query_priority(get_fields(row).get("cpf")),
            str(row.get("uuid") or ""),
        ),
    )
    with TaskProgress(
        "FarmaLíder: validação de pedidos", len(ordered_contacts), unit="contatos"
    ) as progress:
        for contact in progress.track(ordered_contacts):
            fields = get_fields(contact)
            contact_uuid = str(contact.get("uuid") or "")
            contact_row = rows_by_uuid.get(contact_uuid, {})
            cpf = normalize_cpf(fields.get("cpf"))
            if preserve_old_latest_order(
                contact,
                existing_by_contact=prior_by_contact,
                existing_by_identity=prior_by_identity,
                cliente_response=(cliente_responses_by_cpf or {}).get(cpf),
                captured_at=sync_time,
            ):
                # Keep the paired observed snapshot through the ordinary
                # upsert; the fresh CPF lookup already checked for a new ID.
                continue
            try:
                with request_progress("FarmaLíder: consulta de pedido e itens"):
                    order, items = resolve_contact_order(
                        contact,
                        farm_client=farm_client,
                        captured_at=sync_time,
                        linked_cliente_id=contact_row.get("farmalider_cliente_id", ""),
                        cliente_response=(cliente_responses_by_cpf or {}).get(cpf),
                        cliente_lookup_status=(
                            contact_row.get("farmalider_cliente_status", "")
                            if cpf and not is_valid_cpf(cpf)
                            else (cliente_lookup_status_by_cpf or {}).get(cpf, "")
                        ),
                        pedido_responses_by_id=pedido_responses_by_id,
                    )
            except (FarmaliderAuthError, FarmaliderUnavailableError):
                raise
            except FarmaliderError:
                stats.api_errors += 1
                stats.failure_reasons["FALHA_CONSULTA_PEDIDO"] = (
                    stats.failure_reasons.get("FALHA_CONSULTA_PEDIDO", 0) + 1
                )
                continue
            if order is None:
                continue

            diagnostic = order.get("status_confirmacao", "")
            if verified_order_cancellation(order) and not items:
                # The official negative header is sufficient to exclude this
                # order. Do not wait for products that will not count as sales.
                stats.cancellations_without_items += 1
            elif diagnostic in ORDER_API_FAILURES:
                stats.api_errors += 1
                stats.failure_reasons[diagnostic] = (
                    stats.failure_reasons.get(diagnostic, 0) + 1
                )

            contact_uuid = order.get("contact_uuid", "")
            contact_row = rows_by_uuid.get(contact_uuid, {})

            order["cpf_normalizado"] = contact_row.get(
                "cpf_normalizado",
                order.get("cpf_normalizado", ""),
            )
            order["farmalider_cliente_id"] = contact_row.get(
                "farmalider_cliente_id",
                "",
            ) or order.get("farmalider_cliente_id", "")

            for key in (
                "primeiro_anuncio_origem",
                "primeiro_anuncio_id",
                "primeiro_anuncio_titulo",
                "primeiro_anuncio_data",
                "primeiro_anuncio_tipo_campanha",
                "primeiro_anuncio_canal_destino",
                "ultimo_anuncio_origem",
                "ultimo_anuncio_id",
                "ultimo_anuncio_titulo",
                "ultimo_anuncio_data",
                "ultimo_anuncio_tipo_campanha",
                "ultimo_anuncio_canal_destino",
            ):
                order[key] = contact_row.get(key, "")

            pedido_id = order.get("pedido_id", "").strip()
            for item in items:
                item["pedido_id"] = pedido_id

            order_key = order.get("pedido_chave", "").strip()
            if not order_key:
                continue
            contacts_by_order.setdefault(order_key, set()).add(contact_uuid)
            snapshots.setdefault(order_key, (order, items))

            if order.get("status_validacao") == "ERRO":
                stats.parse_errors += 1

    order_rows: list[Row] = []
    items_by_key: dict[str, Row] = {}
    for order_key in sorted(snapshots):
        order, items = snapshots[order_key]
        if len(contacts_by_order[order_key]) > 1:
            # A single order shared by multiple Weni contacts does not identify
            # which contact's ad snapshot should receive that order attribution.
            for key in order:
                if key.startswith(("primeiro_anuncio_", "ultimo_anuncio_")):
                    order[key] = ""
        order_rows.append(order)
        for item in items:
            key = item.get("item_chave", "").strip()
            if key:
                items_by_key.setdefault(key, item)
    stats.confirmed_api = sum(
        row.get("status_confirmacao", "").startswith("CONFIRMADO_API")
        for row in order_rows
    )
    return order_rows, [items_by_key[key] for key in sorted(items_by_key)], stats


def open_farmalider_client(
    settings: FarmaliderSettings,
) -> FarmaliderClient | None:
    if settings.enabled and settings.token:
        client = FarmaliderClient(settings.token, use_hml=settings.use_hml)
        logging.info(
            "Integração FarmaLíder habilitada (%s).",
            "HML" if settings.use_hml else "produção",
        )
        return client

    if settings.enabled:
        logging.warning(
            "Integração FarmaLíder habilitada, mas token não configurado. "
            "A sincronização continuará sem validação FarmaLíder."
        )
    else:
        logging.info("Integração FarmaLíder desabilitada.")
    return None


def main() -> int:
    configure_logging()
    farm_client: FarmaliderClient | None = None
    sheets: GoogleSheetsStore | None = None

    try:
        validate_configuration()
        force_full = os.getenv("FORCE_FULL_SYNC", "").strip() == "1"
        full_quarter: int | None = None
        raw_quarter = os.getenv("FULL_SYNC_QUARTER", "").strip()
        if raw_quarter:
            if not force_full:
                raise RuntimeError(
                    "FULL_SYNC_QUARTER só pode ser usado junto com FORCE_FULL_SYNC=1."
                )
            try:
                full_quarter = int(raw_quarter)
            except ValueError as exc:
                raise RuntimeError("FULL_SYNC_QUARTER deve ser 1, 2, 3 ou 4.") from exc
            if full_quarter not in {1, 2, 3, 4}:
                raise RuntimeError("FULL_SYNC_QUARTER deve ser 1, 2, 3 ou 4.")

        if full_quarter is not None:
            validate_full_load_quarter(part=full_quarter, component="analytics")

        total_started_monotonic = time.monotonic()
        sync_started = datetime.now(UTC)
        sync_time = utc_iso(sync_started)
        # FarmaLíder/Grupo Líder opera em Belém (UTC-3, sem horário de verão).
        bi_snapshot_date = (sync_started - timedelta(hours=3)).date().isoformat()
        looker_start, looker_end = reporting_period(
            bi_snapshot_date,
            os.getenv("NEXOFLOW_BI_INICIO") or None,
            os.getenv("NEXOFLOW_BI_FIM") or None,
        )
        logging.info("Iniciando sincronização Weni → FarmaLíder → Google Sheets.")

        spreadsheet_id = str(getattr(secrets, "GOOGLE_SPREADSHEET_ID", "")).strip()
        weni_token = str(getattr(secrets, "WENI_API_TOKEN", "")).strip()
        farm_settings = load_farmalider_settings()
        farm_client = open_farmalider_client(farm_settings)
        sheets_started = time.monotonic()
        sheets = GoogleSheetsStore(spreadsheet_id)
        logging.info(
            "Google Sheets inicializado em %.2fs.",
            time.monotonic() - sheets_started,
        )

        last_success = None
        if not force_full:
            last_success = parse_control_datetime(
                sheets.get_control("ultima_sincronizacao_sucesso_utc")
            )

        if force_full:
            after = None
            if full_quarter is None:
                logging.info("Carga completa forçada (100%%).")
            else:
                logging.info(
                    "Carga completa fracionada: Parte %s/4 (25%% da base congelada).",
                    full_quarter,
                )
        elif last_success is not None:
            after = last_success - timedelta(minutes=SYNC_OVERLAP_MINUTES)
            logging.info(
                "Sincronização incremental desde %s UTC | horário local=%s "
                "(%s min de sobreposição).",
                utc_iso(after),
                after.astimezone(ZoneInfo("America/Belem")).isoformat(
                    timespec="seconds"
                ),
                SYNC_OVERLAP_MINUTES,
            )
        else:
            after = None
            logging.info("Primeira execução: carga completa de contatos.")

        # A Base_Contatos é pré-carregada enquanto a Weni pagina contatos.
        # São APIs independentes, então a latência de uma fica escondida pela outra.
        with ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="sheets-preload",
        ) as preload_executor:
            preload_base = force_full or after is None
            base_snapshot_future = (
                preload_executor.submit(sheets.load_base_snapshot)
                if preload_base
                else None
            )

            weni_started = time.monotonic()
            weni_client = WeniClient(weni_token)
            try:
                # FarmaLíder status changes do not necessarily modify the Weni contact.
                # Every load revalidates current orders for the selected scope.
                contacts = weni_client.list_contacts(after=None)
            finally:
                weni_client.close()
            contacts_api_total = len(contacts)
            quarter_context: dict[str, Any] | None = None
            if full_quarter is not None:
                quarter_context = prepare_full_load_quarter(
                    contacts,
                    part=full_quarter,
                    component="analytics",
                )
                contacts = list(quarter_context["selected_contacts"])
                logging.info(
                    "Carga 25%% Analytics Parte %s/4: campanha=%s | "
                    "base congelada=%s | contatos desta parte=%s | "
                    "novos fora do corte=%s | ausentes=%s.",
                    full_quarter,
                    quarter_context["campaign_id"],
                    quarter_context["total_contacts"],
                    quarter_context["selected_count"],
                    quarter_context["new_contacts_ignored"],
                    quarter_context["missing_contacts"],
                )
            logging.info(
                "Weni concluída em %.2fs.",
                time.monotonic() - weni_started,
            )

            relevant_contacts, cpf_contacts, contacts_to_process = split_contacts(
                contacts
            )
            normalized = [
                normalize_contact(contact, sync_time) for contact in contacts_to_process
            ]
            valid_unique_cpfs = {
                normalize_cpf(row.get("cpf", ""))
                for row in normalized
                if is_valid_cpf(normalize_cpf(row.get("cpf", "")))
            }

            logging.info(
                "Weni: recebidos=%s | relevantes=%s | com CPF=%s | processados=%s.",
                len(contacts),
                len(relevant_contacts),
                len(cpf_contacts),
                len(contacts_to_process),
            )
            logging.info(
                "FarmaLíder: CPFs válidos únicos para consulta=%s.",
                len(valid_unique_cpfs),
            )

            logging.info(
                "FarmaLíder: CPFs inválidos únicos para consulta posterior=%s.",
                len(
                    {
                        normalize_cpf(row.get("cpf"))
                        for row in normalized
                        if normalize_cpf(row.get("cpf"))
                        and not is_valid_cpf(row.get("cpf"))
                    }
                ),
            )

            if not force_full and after is not None and not contacts_to_process:
                sheets.set_controls(
                    {
                        "ultima_sincronizacao_sucesso_utc": utc_iso(sync_started),
                        "ultima_execucao_status": "sucesso",
                        "ultima_execucao_contatos_api": str(contacts_api_total),
                        "ultima_execucao_contatos_processados": "0",
                        "ultima_execucao_contatos_inseridos": "0",
                        "ultima_execucao_contatos_atualizados": "0",
                    }
                )
                request_stats = sheets.request_stats()
                logging.info(
                    "Incremental sem contatos processáveis: carga encerrada sem ler Base_Contatos, "
                    "consultar FarmaLíder ou recalcular métricas."
                )
                logging.info(
                    "Google Sheets: requisições HTTP da execução: leituras=%s | escritas=%s | total=%s.",
                    request_stats["reads"],
                    request_stats["writes"],
                    request_stats["total"],
                )
                return 0

            # Uma única leitura integral alimenta cache FarmaLíder incremental,
            # detecção de mudanças, histórico e métricas. Em carga completa/primeira
            # execução ela já ocorre em paralelo à paginação da Weni.
            if base_snapshot_future is not None:
                base_values, base_records_before, existing_uuids = (
                    base_snapshot_future.result()
                )
            else:
                base_values, base_records_before, existing_uuids = (
                    sheets.load_base_snapshot()
                )
        existing_by_uuid = {
            row.get("contact_uuid", ""): row
            for row in base_records_before
            if row.get("contact_uuid")
        }
        cliente_responses_by_cpf: dict[
            str, dict[str, Any] | list[dict[str, Any]] | None
        ] = {}
        cliente_lookup_status_by_cpf: dict[str, str] = {}
        cliente_stats = validate_contacts(
            normalized,
            farm_client=farm_client,
            farm_settings=farm_settings,
            sync_time=sync_time,
            existing_by_uuid=existing_by_uuid,
            reuse_existing=False,
            identity_by_uuid={
                str(contact.get("uuid") or ""): weni_customer_identity(contact)
                for contact in contacts_to_process
            },
            cliente_responses_by_cpf=cliente_responses_by_cpf,
            cliente_lookup_status_by_cpf=cliente_lookup_status_by_cpf,
        )
        if cliente_stats.cache_reused:
            logging.info(
                "FarmaLíder: %s validação(ões) reutilizada(s) do cache da Base_Contatos; "
                "consultas HTTP desnecessárias foram evitadas.",
                cliente_stats.cache_reused,
            )

        contacts_sheets_started = time.monotonic()
        logging.info(
            "Google Sheets: etapa 1/4 - Base_Contatos + Historico_Alteracoes (%s contatos).",
            len(normalized),
        )
        contact_result = sheets.upsert_contacts_detailed(
            normalized,
            existing_values=base_values,
        )
        inserted, updated = contact_result.inserted, contact_result.updated
        logging.info(
            "Google Sheets: etapa 1/4 - Base_Contatos finalizada; histórico receberá somente %s contato(s) com mudança real.",
            len(contact_result.changed_records),
        )
        sheets.append_history(contact_result.changed_records, existing_uuids)
        logging.info(
            "Google Sheets: contatos + histórico em %.2fs.",
            time.monotonic() - contacts_sheets_started,
        )

        rows_by_uuid = {
            row["contact_uuid"]: row for row in normalized if row.get("contact_uuid")
        }
        existing_orders = sheets.load_order_records()
        existing_order_items = sheets.load_order_item_records() if farm_client else []
        pedido_responses_by_id: dict[
            str, dict[str, Any] | list[dict[str, Any]] | None
        ] = {}
        order_rows, item_rows, order_stats = build_orders(
            [
                contact
                for contact in contacts_to_process
                if is_relevant_contact(contact)
                or bool(normalize_cpf(get_fields(contact).get("cpf")))
            ],
            rows_by_uuid=rows_by_uuid,
            farm_client=farm_client,
            sync_time=sync_time,
            cliente_responses_by_cpf=cliente_responses_by_cpf,
            cliente_lookup_status_by_cpf=cliente_lookup_status_by_cpf,
            pedido_responses_by_id=pedido_responses_by_id,
            existing_orders=existing_orders,
        )
        observed = revalidate_observed_orders(
            existing_orders,
            scoped_contact_uuids=set(rows_by_uuid),
            current_orders=order_rows,
            current_items=item_rows,
            existing_items=existing_order_items,
            farm_client=farm_client,
            captured_at=sync_time,
            pedido_responses_by_id=pedido_responses_by_id,
        )
        order_rows, item_rows = observed.orders, observed.items
        order_stats.api_errors += observed.api_errors
        order_stats.pending_without_items = len(
            {
                (row.get("pedido_id"), row.get("farmalider_cliente_id"))
                for row in order_rows
                if row.get("status_confirmacao") == PENDING_ITEMS_STATUS
            }
        )
        if observed.api_errors:
            order_stats.failure_reasons["REVALIDACAO_HISTORICO"] = observed.api_errors
        logging.info(
            "Pedidos: cancelados confirmados sem itens=%s; falhas por motivo=%s.",
            order_stats.cancellations_without_items + observed.negative_headers,
            dict(sorted(order_stats.failure_reasons.items())),
        )
        if farm_client is not None:
            # Include order lookups in the final HTTP diagnostics, not only CPF validation.
            _apply_request_metrics(cliente_stats, farm_client.metrics_snapshot())

        postprocess_started = time.monotonic()
        logging.info("Google Sheets: etapa 2/4 - Pedidos e itens.")
        order_result, item_result = sheets.upsert_pharmacy_orders(order_rows, item_rows)
        scope_pending = (
            sheets.get_control("escopo_farmacia_versao") != PHARMACY_SCOPE_VERSION
        )
        pharmacy_scope = sheets.reconcile_pharmacy_scope(
            base_records=contact_result.final_records,
            order_records=order_result.final_records,
            item_records=item_result.final_records,
            include_history=scope_pending,
        )
        contact_result.final_records = pharmacy_scope.contacts
        orders_inserted = order_result.inserted
        orders_updated = order_result.updated
        items_inserted = item_result.inserted
        items_updated = item_result.updated

        logging.info("Google Sheets: etapa 3/4 - Métricas de contatos e anúncios.")
        ad_metrics = build_daily_metrics(contact_result.final_records)
        sheets.replace_metrics(ad_metrics)

        logging.info("Google Sheets: etapa 4/4 - Métricas de vendas e fechamento.")
        all_order_records = pharmacy_scope.orders
        all_item_records = pharmacy_scope.items
        if scope_pending:
            sheets.resanitize_pharmacy_bi_snapshots(
                contact_result.final_records, all_order_records, all_item_records
            )
        sales_metrics = build_sales_metrics(all_order_records, all_item_records)
        sheets.replace_sales_metrics(sales_metrics)
        sales_metrics_count = len(sales_metrics)
        logging.info(
            "Google Sheets: pedidos + métricas em %.2fs.",
            time.monotonic() - postprocess_started,
        )

        # Camada BI pronta para o Looker Studio. As abas são mantidas pelo próprio
        # Analytics para que o relatório não precise recalcular regras de negócio.
        bi_started = time.monotonic()
        logging.info("Google Sheets: camada BI/Looker Studio - iniciando 7 tabelas.")
        bi_summary = build_bi_summary_record(
            contact_result.final_records,
            all_order_records,
            all_item_records,
            snapshot_date=bi_snapshot_date,
            updated_at=sync_time,
        )
        sheets.upsert_bi_summary(bi_summary)
        logging.info("Google Sheets: BI 1/7 - BI_Resumo_Diario atualizado.")

        bi_channels = build_bi_channel_records(
            contact_result.final_records,
            snapshot_date=bi_snapshot_date,
            updated_at=sync_time,
        )
        sheets.upsert_bi_channels(bi_channels)
        logging.info("Google Sheets: BI 2/7 - BI_Canais atualizado.")

        bi_conversion = build_bi_conversion_records(
            contact_result.final_records,
            all_order_records,
            snapshot_date=bi_snapshot_date,
            updated_at=sync_time,
        )
        sheets.upsert_bi_conversion(bi_conversion)
        logging.info("Google Sheets: BI 3/7 - BI_Conversao atualizado.")

        bi_sales = build_bi_sales_rows(
            all_order_records,
            all_item_records,
            snapshot_date=bi_snapshot_date,
            updated_at=sync_time,
        )
        sheets.replace_bi_sales(bi_sales)
        logging.info("Google Sheets: BI 4/7 - BI_Vendas atualizado.")

        bi_ads = build_bi_ads_rows(
            contact_result.final_records,
            all_order_records,
            snapshot_date=bi_snapshot_date,
            updated_at=sync_time,
        )
        sheets.replace_bi_ads(bi_ads)
        logging.info("Google Sheets: BI 5/7 - BI_Ads atualizado.")

        bi_orders = build_bi_order_rows(
            contact_result.final_records,
            all_order_records,
            all_item_records,
            updated_at=sync_time,
        )
        sheets.replace_bi_orders(bi_orders)
        logging.info("Google Sheets: BI 6/7 - BI_Pedidos atualizado.")
        history_started = (
            sheets.get_control("historico_observado_desde") or bi_snapshot_date
        )
        looker_tables = build_looker_tables(
            contact_result.final_records,
            all_order_records,
            all_item_records,
            snapshot_date=bi_snapshot_date,
            updated_at=sync_time,
            period_start=looker_start,
            period_end=looker_end,
            history_observed_since=history_started,
        )
        sheets.replace_looker_tables(looker_tables)
        sheets.set_controls(
            {
                "historico_observado_desde": history_started,
                "historico_ultima_coleta": sync_time,
            }
        )
        # Keep optional Delivery metrics current when existing orders change status/value.
        # The ordinary Analytics load does not create these tabs or infer new ticket tags.
        sheets.refresh_delivery_bi(all_order_records, create=False)

        # O status é gravado por último para refletir a própria execução do BI.
        completion_status, completion_controls = _completion_controls(
            sync_started,
            api_errors=cliente_stats.api_errors + order_stats.api_errors,
        )
        if full_quarter is not None:
            # Partes 1-3 não podem avançar o cursor incremental. Na Parte 4,
            # o cursor volta ao corte da campanha para que mudanças ocorridas
            # durante as quatro cargas sejam capturadas pela próxima incremental.
            completion_controls.pop("ultima_sincronizacao_sucesso_utc", None)
            if completion_status == "sucesso" and full_quarter == 4:
                if quarter_context is None:
                    raise RuntimeError(
                        "Contexto da carga fracionada ausente; checkpoint preservado."
                    )
                completion_controls["ultima_sincronizacao_sucesso_utc"] = str(
                    quarter_context["captured_at_utc"]
                )
        status_stats = sheets.request_stats()
        status_elapsed = time.monotonic() - total_started_monotonic
        sheets.replace_bi_status(
            [
                sync_time,
                APP_VERSION,
                completion_status,
                (
                    f"carga_completa_25_parte_{full_quarter}"
                    if full_quarter is not None
                    else (
                        "carga_completa"
                        if force_full or after is None
                        else "incremental"
                    )
                ),
                round(status_elapsed, 2),
                contacts_api_total,
                len(contacts_to_process),
                cliente_stats.unique_queries,
                cliente_stats.http_requests,
                cliente_stats.retries,
                cliente_stats.rate_limits,
                cliente_stats.server_errors,
                status_stats["reads"],
                status_stats["writes"],
                inserted,
                updated,
                contact_result.unchanged,
                order_stats.confirmed_api,
            ]
        )
        logging.info(
            "Google Sheets: BI 7/7 - BI_Status atualizado; camada BI concluída em %.2fs.",
            time.monotonic() - bi_started,
        )

        sheets.set_controls(
            {
                **completion_controls,
                "escopo_farmacia_versao": PHARMACY_SCOPE_VERSION,
                "ultima_execucao_contatos_api": str(len(contacts)),
                "ultima_execucao_contatos_relevantes": str(len(relevant_contacts)),
                "ultima_execucao_contatos_com_cpf": str(len(cpf_contacts)),
                "ultima_execucao_contatos_processados": str(len(contacts_to_process)),
                "ultima_execucao_cpfs_unicos_consultados": str(
                    cliente_stats.unique_queries
                ),
                "ultima_execucao_contatos_inseridos": str(inserted),
                "ultima_execucao_contatos_atualizados": str(updated),
                "ultima_execucao_contatos_sem_mudanca": str(contact_result.unchanged),
                "ultima_execucao_farmalider_cache_reutilizado": str(
                    cliente_stats.cache_reused
                ),
                "ultima_execucao_cpfs_confirmados_farmalider": str(
                    cliente_stats.confirmed
                ),
                "ultima_execucao_cpfs_nao_encontrados": str(cliente_stats.not_found),
                "ultima_execucao_cpfs_invalidos": str(cliente_stats.invalid),
                "ultima_execucao_erros_cliente_farmalider": str(
                    cliente_stats.api_errors
                ),
                "ultima_execucao_farmalider_detalhes_anteriores_mantidos": str(
                    cliente_stats.prior_details_retained
                ),
                "ultima_execucao_farmalider_recuperacoes_serial": str(
                    cliente_stats.recovery_activations
                ),
                "ultima_execucao_farmalider_limite_final": str(
                    cliente_stats.concurrency_limit
                ),
                "ultima_execucao_farmalider_5xx_consultas_distintas": str(
                    cliente_stats.server_error_unique_queries
                ),
                "ultima_execucao_farmalider_5xx_consultas_em_serial": str(
                    cliente_stats.server_error_serial_queries
                ),
                "ultima_execucao_farmalider_respostas_200": str(
                    cliente_stats.responses_200
                ),
                "ultima_execucao_farmalider_respostas_404": str(
                    cliente_stats.responses_404
                ),
                "ultima_execucao_farmalider_http_requests": str(
                    cliente_stats.http_requests
                ),
                "ultima_execucao_farmalider_retries": str(cliente_stats.retries),
                "ultima_execucao_farmalider_429": str(cliente_stats.rate_limits),
                "ultima_execucao_farmalider_5xx": str(cliente_stats.server_errors),
                "ultima_execucao_farmalider_erros_rede": str(
                    cliente_stats.network_errors
                ),
                "ultima_execucao_farmalider_tempo_segundos": (
                    f"{cliente_stats.elapsed_seconds:.2f}"
                ),
                "ultima_execucao_farmalider_requisicoes_lentas": str(
                    cliente_stats.slow_requests
                ),
                "ultima_execucao_farmalider_latencia_media_ms": (
                    f"{cliente_stats.average_response_ms:.2f}"
                ),
                "ultima_execucao_farmalider_latencia_p95_ms": (
                    f"{cliente_stats.p95_response_ms:.2f}"
                ),
                "ultima_execucao_farmalider_latencia_max_ms": (
                    f"{cliente_stats.max_response_ms:.2f}"
                ),
                "ultima_execucao_farmalider_delay_final_ms": (
                    f"{cliente_stats.effective_delay_ms:.2f}"
                ),
                "ultima_execucao_pedidos_lidos": str(len(order_rows)),
                "ultima_execucao_pedidos_confirmados_api": str(
                    order_stats.confirmed_api
                ),
                "ultima_execucao_pedidos_inseridos": str(orders_inserted),
                "ultima_execucao_pedidos_atualizados": str(orders_updated),
                "ultima_execucao_itens_inseridos": str(items_inserted),
                "ultima_execucao_itens_atualizados": str(items_updated),
                "ultima_execucao_erros_ultimo_pedido": str(order_stats.parse_errors),
                "ultima_execucao_erros_pedido_farmalider": str(order_stats.api_errors),
                "ultima_execucao_pedidos_pendentes_sem_itens": str(
                    order_stats.pending_without_items
                ),
                "ultima_execucao_cancelados_sem_itens": str(
                    order_stats.cancellations_without_items + observed.negative_headers
                ),
                "ultima_execucao_metricas_ads": str(len(ad_metrics)),
                "ultima_execucao_metricas_vendas": str(sales_metrics_count),
                **(
                    {
                        "ultima_execucao_carga_25_parte": f"{full_quarter}/4",
                        "ultima_execucao_carga_25_campanha": str(
                            quarter_context["campaign_id"]
                        ),
                        "ultima_execucao_carga_25_total_contatos": str(
                            quarter_context["total_contacts"]
                        ),
                        "ultima_execucao_carga_25_contatos_parte": str(
                            quarter_context["selected_count"]
                        ),
                    }
                    if full_quarter is not None and quarter_context is not None
                    else {}
                ),
            }
        )

        if (
            full_quarter is not None
            and quarter_context is not None
            and completion_status == "sucesso"
        ):
            component_complete, campaign_complete = mark_full_load_quarter_complete(
                component="analytics",
                part=full_quarter,
                campaign_id=str(quarter_context["campaign_id"]),
            )
            logging.info(
                "Carga 25%% Analytics Parte %s/4 concluída. "
                "Analytics completo=%s | campanha ativa concluída=%s.",
                full_quarter,
                "sim" if component_complete else "não",
                "sim" if campaign_complete else "não",
            )

        total_elapsed = time.monotonic() - total_started_monotonic
        logging.info("Resumo da execução:")
        logging.info(
            "  Weni | recebidos=%s | processados=%s | com CPF=%s.",
            len(contacts),
            len(contacts_to_process),
            len(cpf_contacts),
        )
        logging.info(
            "  FarmaLíder | CPFs válidos únicos=%s | contatos com CPF inválido=%s | "
            "consultas=%s | cache=%s | clientes encontrados=%s | "
            "HTTP=%s | retries=%s | 429=%s | 5xx=%s | lentas=%s | "
            "latência média/p95/máx=%s/%s/%s | tempo=%.2fs.",
            len(valid_unique_cpfs),
            cliente_stats.invalid,
            cliente_stats.unique_queries,
            cliente_stats.cache_reused,
            cliente_stats.confirmed,
            cliente_stats.http_requests,
            cliente_stats.retries,
            cliente_stats.rate_limits,
            cliente_stats.server_errors,
            cliente_stats.slow_requests,
            _format_ms_as_seconds(cliente_stats.average_response_ms),
            _format_ms_as_seconds(cliente_stats.p95_response_ms),
            _format_ms_as_seconds(cliente_stats.max_response_ms),
            cliente_stats.elapsed_seconds,
        )
        request_stats = sheets.request_stats()
        logging.info(
            "  Google Sheets | contatos inseridos=%s | atualizados=%s | sem mudança=%s | "
            "pedidos inseridos=%s | pedidos atualizados=%s | HTTP leituras/escritas=%s/%s.",
            inserted,
            updated,
            contact_result.unchanged,
            orders_inserted,
            orders_updated,
            request_stats["reads"],
            request_stats["writes"],
        )
        logging.info(
            "  Pedidos | confirmados pela API=%s | erros ultimo_pedido=%s.",
            order_stats.confirmed_api,
            order_stats.parse_errors,
        )
        if order_stats.pending_without_items:
            logging.warning(
                "Pedidos pendentes sem itens=%s: excluídos das vendas e dos "
                "produtos do dashboard. Reconsulta nas próximas cargas até "
                "20 dias da data do pedido. A pendência não bloqueia esta parte.",
                order_stats.pending_without_items,
            )
        if completion_status == "parcial":
            logging.warning(
                "Sincronização parcial em %.2fs: %s erro(s) em consulta de "
                "clientes e %s em pedido(s). O marco incremental anterior foi mantido "
                "para nova tentativa na próxima execução.",
                total_elapsed,
                cliente_stats.api_errors,
                order_stats.api_errors,
            )
        else:
            logging.info("Sincronização concluída com sucesso em %.2fs.", total_elapsed)
        return PARTIAL_SYNC_EXIT_CODE if completion_status == "parcial" else 0

    except Exception as exc:
        logging.exception("Falha na sincronização: %s", exc)

        if sheets is not None:
            try:
                sheets.set_controls(
                    {
                        "ultima_execucao_status": "erro",
                        "ultima_execucao_erro_tipo": exc.__class__.__name__,
                        "ultima_execucao_erro_em_utc": utc_iso(datetime.now(UTC)),
                    }
                )
            except Exception:
                logging.warning(
                    "Não foi possível registrar o status de erro na aba Controle.",
                    exc_info=True,
                )
        return 1
    finally:
        if farm_client is not None:
            farm_client.close()
        if sheets is not None:
            sheets.close()


if __name__ == "__main__":
    sys.exit(main())
# NexoFlow 1.0
