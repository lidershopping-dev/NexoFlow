from __future__ import annotations

import hashlib
import json
import logging
import os
import tempfile
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from ad_attribution import (
    GOOGLE_ADS_ORIGIN,
    GOOGLE_ADS_WRITE_FIELDS,
    extract_google_ads_attribution,
)
from analytics import (
    cpf_query_priority,
    get_fields,
    is_valid_cpf,
    mark_full_load_quarter_complete,
    normalize_cpf,
    normalize_value,
    prepare_full_load_quarter,
)
from config import (
    FARMALIDER_BATCH_SIZE,
    WENI_UPDATE_AUDIT_FILE,
    WENI_UPDATE_CONTEXT_LOOKBACK_HOURS,
    WENI_UPDATE_INITIAL_LOOKBACK_DAYS,
    WENI_UPDATE_OPT_IN_GROUP,
    WENI_UPDATE_OPT_OUT_GROUP,
    WENI_UPDATE_OVERLAP_MINUTES,
    WENI_UPDATE_STATE_FILE,
)
from contact_enrichment import (
    FieldEvidence,
    infer_contact_fields,
    message_created_on,
    message_direction,
    message_id,
)
from farmalider_client import (
    FarmaliderAuthError,
    FarmaliderClient,
    FarmaliderError,
    FarmaliderUnavailableError,
)
from farmalider_integration import resolve_contact_order, weni_identity_guard
from geography import GEOGRAPHY_FIELDS, geography_changes
from pedido_utils import (
    business_order_datetime,
    effective_order_status_code,
    farmalider_order_status_name,
    farmalider_payment_method_display,
    farmalider_payment_method_name,
    is_order_cancelled,
    is_order_sale,
    normalize_farmalider_payment_code,
    parse_weni_order_list,
    serialize_weni_order_list,
    weni_order_datetime_text,
)
from product_scope import is_animal_product
from project_paths import load_private_settings
from purchase_checks import (
    PENDING_PURCHASE_FIELD,
    contact_pending_purchase_check,
    infer_pending_purchase_check,
    load_pending_purchase_checks,
    merge_purchase_checks,
    normalize_purchase_checks,
)
from task_progress import TaskProgress
from urn_utils import (
    CHANNEL_FIELD_MAP,
    native_contact_urns,
    resolve_contact_urns,
    urn_channel,
)
from weni_client import WeniClient, WeniError

secrets = load_private_settings()

ALLOWED_WRITE_FIELDS = {
    *GEOGRAPHY_FIELDS,
    PENDING_PURCHASE_FIELD,
    "contact_urn",
    "contact_urn_whatsapp",
    "contact_urn_instagram",
    "contact_urn_facebook",
    "contact_canal_principal",
    "cpf",
    "idade",
    "maior_de_18_anos",
    "opt_in_lgpd",
    "pedido_id",
    "pedido_data",
    "pedido_status",
    "pedido_produtos",
    "pedido_quantidade_itens",
    "pedido_valor_produtos",
    "pedido_valor",
    "pedido_forma_pagamento",
    "ultima_compra",
    *GOOGLE_ADS_WRITE_FIELDS,
}

REQUIRED_MULTICHANNEL_FIELDS = {
    "contact_urn_whatsapp",
    "contact_urn_instagram",
    "contact_urn_facebook",
    "contact_canal_principal",
}


PURCHASE_FIELDS = {
    "pedido_id",
    "pedido_data",
    "pedido_status",
    "pedido_produtos",
    "pedido_quantidade_itens",
    "pedido_valor_produtos",
    "pedido_valor",
    "pedido_forma_pagamento",
    "ultima_compra",
}

REQUIRED_ORDER_FIELDS = {
    "pedido_id",
    "pedido_data",
    "pedido_status",
    "pedido_produtos",
    "pedido_quantidade_itens",
    "pedido_valor_produtos",
    "pedido_valor",
    "pedido_forma_pagamento",
}

# Alias mantido para compatibilidade com testes/documentação anteriores.
RECOMMENDED_ORDER_FIELDS = REQUIRED_ORDER_FIELDS

OPEN_ORDER_STATUSES = {
    "Em aberto",
    "Pendente de pagamento",
    "Em processamento",
    "Pronto para rota",
    "Em trânsito",
}
TERMINAL_ORDER_STATUSES = {"Concluído", "Cancelado"}


def _track_open_order(
    open_order_uuids: set[str],
    contact_uuid: str,
    status: Any,
) -> None:
    label = farmalider_order_status_name(status) or normalize_value(status).strip()
    if label in OPEN_ORDER_STATUSES:
        open_order_uuids.add(contact_uuid)
    elif label in TERMINAL_ORDER_STATUSES:
        open_order_uuids.discard(contact_uuid)


@dataclass(frozen=True)
class GroupAction:
    action: str
    group: str
    reason: str
    group_name: str = ""


@dataclass
class ContactUpdatePlan:
    contact_uuid: str
    contact_name: str = ""
    contact_urn: str = ""
    contact_channel: str = ""
    fields: dict[str, Any] = field(default_factory=dict)
    previous: dict[str, Any] = field(default_factory=dict)
    guards: dict[str, Any] = field(default_factory=dict)
    evidence: dict[str, dict[str, str]] = field(default_factory=dict)
    group_actions: list[GroupAction] = field(default_factory=list)
    order_validated: bool = False
    # Diagnostic metadata from this GET; never written to Weni or plan digest.
    order_payment_code: str = ""
    order_resolution_status: str = ""
    order_found: bool = False
    purchase_confirmed: bool = False
    purchase_confirmation: str = ""
    official_order_date: str = ""
    # Internal evidence survives the review; no new Weni field is created.
    order_has_animal_products: bool = False

    @property
    def has_changes(self) -> bool:
        return bool(self.fields or self.group_actions)


@dataclass
class UpdateStats:
    messages_incoming: int = 0
    contacts_modified: int = 0
    contacts_considered: int = 0
    plans_with_changes: int = 0
    field_changes: int = 0
    group_actions: int = 0
    contacts_updated: int = 0
    contacts_failed: int = 0
    contacts_conflicted: int = 0
    contacts_already_satisfied: int = 0
    purchase_confirmed: int = 0
    purchase_skipped: int = 0
    orders_validated: int = 0
    orders_api_failed: int = 0
    payment_unmapped_codes: dict[str, int] = field(default_factory=dict)
    orders_cancelled_payment_zero: int = 0
    orders_pending_payment: int = 0
    invalid_cpf_checked: int = 0
    invalid_cpf_orders_found: int = 0
    invalid_cpf_orders_validated: int = 0
    invalid_cpf_outcomes: dict[str, int] = field(default_factory=dict)
    pending_retry_uuids: list[str] = field(default_factory=list)


def _record_invalid_cpf_result(stats: UpdateStats, plan: ContactUpdatePlan) -> None:
    """Count completed contact checks, including unchanged and blocked plans."""
    stats.invalid_cpf_checked += 1
    stats.invalid_cpf_orders_found += int(plan.order_found or plan.order_validated)
    stats.invalid_cpf_orders_validated += int(plan.order_validated)
    outcome = (
        "PEDIDO_VALIDADO"
        if plan.order_validated
        else "PEDIDO_CONFIRMADO_NAO_PROPOSTO"
        if plan.order_found
        else plan.order_resolution_status or "SEM_PEDIDO_VALIDADO"
    )
    stats.invalid_cpf_outcomes[outcome] = stats.invalid_cpf_outcomes.get(outcome, 0) + 1


def _invalid_cpf_summary(stats: UpdateStats) -> str:
    counts = stats.invalid_cpf_outcomes
    divergent = {
        "CPF_IDENTIDADE_DIVERGENTE",
        "CPF_DIVERGENTE_API",
        "CLIENTE_ID_DIVERGENTE_API",
        "PEDIDO_CLIENTE_DIVERGENTE_API",
    }
    known = divergent | {
        "PEDIDO_VALIDADO",
        "PEDIDO_CONFIRMADO_NAO_PROPOSTO",
        "CPF_IDENTIDADE_AMBIGUA",
        "CPF_IDENTIDADE_INSUFICIENTE",
        "CPF_NAO_ENCONTRADO",
        "SEM_PEDIDO_VALIDADO",
        "ERRO_API",
    }
    return (
        f"contatos avaliados={stats.invalid_cpf_checked}; "
        f"pedidos encontrados={stats.invalid_cpf_orders_found}; "
        f"pedidos validados={stats.invalid_cpf_orders_validated}; "
        f"divergência={sum(counts.get(key, 0) for key in divergent)}; "
        f"identidade ambígua={counts.get('CPF_IDENTIDADE_AMBIGUA', 0)}; "
        f"dados insuficientes={counts.get('CPF_IDENTIDADE_INSUFICIENTE', 0)}; "
        f"cliente não encontrado={counts.get('CPF_NAO_ENCONTRADO', 0)}; "
        f"sem pedido validado={counts.get('SEM_PEDIDO_VALIDADO', 0)}; "
        f"falha API={counts.get('ERRO_API', 0)}; "
        f"outros impedimentos={sum(v for k, v in counts.items() if k not in known)}"
    )


@dataclass
class _InvalidCpfBatch:
    size: int = FARMALIDER_BATCH_SIZE
    number: int = 1
    stats: UpdateStats = field(default_factory=UpdateStats)

    def record(self, plan: ContactUpdatePlan) -> None:
        _record_invalid_cpf_result(self.stats, plan)
        if self.stats.invalid_cpf_checked >= self.size:
            self.flush()

    def flush(self, *, interrupted: bool = False) -> None:
        if not self.stats.invalid_cpf_checked:
            return
        logging.info(
            "Weni CPFs inválidos | lote %s%s | %s. "
            "Contagens por contato, incluindo pedidos cancelados; "
            "nenhuma gravação foi iniciada.",
            self.number,
            " interrompido (contatos concluídos)" if interrupted else "",
            _invalid_cpf_summary(self.stats),
        )
        self.number += 1
        self.stats = UpdateStats()


def _record_validated_order(stats: UpdateStats, plan: ContactUpdatePlan) -> None:
    """Count validated orders and incomplete queries, including unchanged contacts."""
    if (
        plan.order_resolution_status.startswith("ERRO_")
        or plan.order_resolution_status == "API_DESABILITADA"
    ):
        stats.orders_api_failed += 1
    if not plan.order_validated:
        return
    stats.orders_validated += 1
    status = normalize_value(
        plan.fields.get(
            "pedido_status",
            plan.guards.get("pedido_status"),
        )
    ).strip()
    if status == "-1":
        stats.orders_pending_payment += 1
    code = normalize_farmalider_payment_code(plan.order_payment_code)
    if code == "0":
        stats.orders_cancelled_payment_zero += 1
        if stats.orders_cancelled_payment_zero == 1:
            logging.info(
                "FarmaLíder: regra de pagamento 0 aplicada. "
                "Pedido tratado como Cancelado; forma de pagamento Cancelado "
                "e pedido_status=3 no plano Weni. Fora das vendas e de ultima_compra. "
                "Nenhuma gravação foi iniciada; o plano será auditado.",
            )
    if not code or farmalider_payment_method_name(code):
        return
    count = stats.payment_unmapped_codes.get(code, 0) + 1
    stats.payment_unmapped_codes[code] = count
    if count == 1:
        logging.info(
            "FarmaLíder: pedido validado com formaPagamento=%s. "
            "Descrição no Analytics: %s. Código preservado; "
            "preparação dos campos de pedido continua normalmente. "
            "Este aviso informativo aparece uma vez por código nesta execução.",
            code,
            farmalider_payment_method_display(code),
        )


@dataclass(frozen=True)
class UpdateWindow:
    started_at: datetime
    after: datetime | None
    full: bool


class PlanAuditError(RuntimeError):
    pass


def utc_now() -> datetime:
    return datetime.now(UTC)


def iso_utc(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _parse_datetime(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None

    candidates = [text]
    if text.endswith("Z"):
        candidates.append(text[:-1] + "+00:00")

    for candidate in candidates:
        try:
            parsed = datetime.fromisoformat(candidate)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=UTC)
            return parsed.astimezone(UTC)
        except ValueError:
            pass

    for fmt in (
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%dT%H:%M:%S",
        "%d/%m/%Y %H:%M:%S",
        "%d/%m/%Y %H:%M",
    ):
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=UTC)
        except ValueError:
            continue
    return None


def _weni_datetime(value: Any) -> str:
    dt = _parse_datetime(value)
    if dt is None:
        return ""
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def _safe_number(value: Any) -> int | float | None:
    text = str(value if value is not None else "").strip().replace(",", ".")
    if not text:
        return None
    try:
        number = Decimal(text)
    except (InvalidOperation, ValueError):
        return None
    if not number.is_finite():
        return None
    if number == number.to_integral_value():
        return int(number)
    return float(number)


def _semantic_text(value: Any) -> str:
    return str(value or "").strip()


def _same_value(field_name: str, current: Any, proposed: Any) -> bool:
    if field_name in GEOGRAPHY_FIELDS:
        return str(current or "") == str(proposed or "")
    if field_name == "cpf":
        current_cpf = normalize_cpf(current)
        proposed_cpf = normalize_cpf(proposed)
        if current_cpf and proposed_cpf:
            return current_cpf == proposed_cpf
    if field_name in {"idade", "pedido_valor"}:
        current_num = _safe_number(current)
        proposed_num = _safe_number(proposed)
        if current_num is not None and proposed_num is not None:
            return Decimal(str(current_num)) == Decimal(str(proposed_num))
    if field_name in {"pedido_data", "ultima_compra"}:
        current_dt = business_order_datetime(normalize_value(current))
        proposed_dt = business_order_datetime(normalize_value(proposed))
        if current_dt is not None and proposed_dt is not None:
            return current_dt == proposed_dt
    if field_name == "pedido_produtos":
        try:
            return json.loads(normalize_value(current)) == json.loads(
                normalize_value(proposed)
            )
        except (ValueError, TypeError):
            pass
    return _semantic_text(current).casefold() == _semantic_text(proposed).casefold()


def _extract_contact_uuid(message: dict[str, Any]) -> str:
    contact = message.get("contact")
    if isinstance(contact, dict):
        return str(contact.get("uuid") or "").strip()
    if isinstance(contact, str):
        return contact.strip()
    return str(message.get("contact_uuid") or "").strip()


def _extract_message_urn(message: dict[str, Any]) -> str:
    for key in ("urn", "contact_urn"):
        value = str(message.get(key) or "").strip()
        if value:
            return value
    contact = message.get("contact")
    if isinstance(contact, dict):
        urn = str(contact.get("urn") or "").strip()
        if urn:
            return urn
    return ""


def _preferred_contact_urn(contact: dict[str, Any]) -> str:
    return resolve_contact_urns(contact, get_fields(contact)).principal_urn


def _sync_contact_channel_fields(
    plan: ContactUpdatePlan,
    contact: dict[str, Any],
    current_fields: dict[str, Any],
) -> None:
    """Sincroniza somente identidades de canal confirmadas pela Weni."""
    resolved = resolve_contact_urns(contact, current_fields)
    plan.contact_urn = resolved.principal_urn
    plan.contact_channel = resolved.principal_channel

    if resolved.principal_urn:
        _set_field_if_changed(
            plan,
            current_fields,
            "contact_urn",
            resolved.principal_urn,
            reason="URN principal determinada pelas URNs oficiais retornadas pela Weni",
        )

    for channel, field_name in CHANNEL_FIELD_MAP.items():
        urn = resolved.channel_urns.get(channel, "")
        if not urn:
            continue
        _set_field_if_changed(
            plan,
            current_fields,
            field_name,
            urn,
            reason=f"URN oficial de {channel} retornada pela Weni",
        )

    if resolved.principal_channel:
        _set_field_if_changed(
            plan,
            current_fields,
            "contact_canal_principal",
            resolved.principal_channel,
            reason="canal principal classificado a partir da URN oficial da Weni",
        )

    channel_fields = {
        "contact_urn",
        "contact_urn_whatsapp",
        "contact_urn_instagram",
        "contact_urn_facebook",
        "contact_canal_principal",
    }
    if channel_fields & set(plan.fields):
        # Guarda a fonte nativa usada para montar o plano. No apply congelado,
        # se a identidade de canal mudar enquanto o atendimento continua 24h,
        # apenas este contato entra em conflito e é reprocessado depois.
        plan.guards["__native_urns"] = list(resolved.native_urns)


def _ad_event_values(prefix: str, attribution: Any) -> dict[str, str]:
    """Mapeia uma evidência Google Ads para os campos genéricos de atribuição."""
    occurred = _weni_datetime(attribution.occurred_at)
    ad_id = attribution.ad_id or attribution.click_id
    title = attribution.title or attribution.campaign_type or GOOGLE_ADS_ORIGIN
    values = {
        f"{prefix}_anuncio_origem": GOOGLE_ADS_ORIGIN,
        f"{prefix}_anuncio_id": ad_id,
        f"{prefix}_anuncio_titulo": title,
        f"{prefix}_anuncio_url": attribution.url,
        f"{prefix}_anuncio_clique_id": attribution.click_id,
        f"{prefix}_anuncio_data": occurred,
        f"{prefix}_anuncio_tipo_campanha": attribution.campaign_type,
        f"{prefix}_anuncio_canal_destino": attribution.destination_channel,
    }
    return {key: value for key, value in values.items() if str(value or "").strip()}


def _set_ad_field(
    plan: ContactUpdatePlan,
    current_fields: dict[str, Any],
    field_name: str,
    value: str,
    *,
    attribution: Any,
    available_weni_fields: set[str] | None,
    only_if_blank: bool = False,
) -> None:
    if available_weni_fields is not None and field_name not in available_weni_fields:
        return
    if only_if_blank and str(current_fields.get(field_name) or "").strip():
        return
    evidence = FieldEvidence(
        field=field_name,
        value=value,
        message_id=attribution.message_id,
        created_on=attribution.occurred_at,
        reason=attribution.reason,
    )
    _set_field_if_changed(plan, current_fields, field_name, value, evidence=evidence)


def _same_ad_event(
    current_fields: dict[str, Any], prefix: str, attribution: Any
) -> bool:
    origin = (
        str(current_fields.get(f"{prefix}_anuncio_origem") or "").strip().casefold()
    )
    if origin not in {"google ads", "googleads", "google adwords", "adwords"}:
        return False
    current_click = str(current_fields.get(f"{prefix}_anuncio_clique_id") or "").strip()
    current_id = str(current_fields.get(f"{prefix}_anuncio_id") or "").strip()
    if current_click and attribution.click_id:
        return current_click == attribution.click_id
    candidate_id = attribution.ad_id or attribution.click_id
    if current_id and candidate_id:
        return current_id == candidate_id
    current_date = _parse_datetime(current_fields.get(f"{prefix}_anuncio_data"))
    candidate_date = _parse_datetime(attribution.occurred_at)
    return (
        current_date is not None
        and candidate_date is not None
        and current_date == candidate_date
    )


def _sync_google_ads_attribution(
    plan: ContactUpdatePlan,
    contact: dict[str, Any],
    current_fields: dict[str, Any],
    context_messages: list[dict[str, Any]],
    *,
    available_weni_fields: set[str] | None,
) -> None:
    """
    Sincroniza atribuição Google Ads somente quando a evidência chega à Weni.

    O canal de destino vem da URN da própria mensagem. Origem de anúncio nunca
    fabrica nem altera URNs de contato.
    """
    detected: list[tuple[datetime, Any]] = []
    resolved_contact = resolve_contact_urns(contact, current_fields)
    # Se a mensagem não trouxer URN, só usamos fallback quando o contato possui
    # exatamente um canal conhecido. Em contato multicanal, deixamos em branco
    # para não inventar o canal por onde o anúncio abriu a conversa.
    contact_channels = [
        channel for channel, urn in resolved_contact.channel_urns.items() if urn
    ]
    single_contact_channel = contact_channels[0] if len(contact_channels) == 1 else ""

    for message in context_messages:
        attribution = extract_google_ads_attribution(message)
        if attribution is None:
            continue
        if not attribution.destination_channel and single_contact_channel:
            attribution = replace(
                attribution, destination_channel=single_contact_channel
            )
        occurred = _parse_datetime(attribution.occurred_at)
        if occurred is None:
            # Sem data confiável não há como decidir primeiro/último anúncio.
            continue
        detected.append((occurred, attribution))

    if not detected:
        return
    detected.sort(key=lambda item: (item[0], item[1].message_id))
    first = detected[0][1]
    last = detected[-1][1]

    # Primeiro anúncio é imutável depois que existe evidência válida.
    first_markers = (
        "primeiro_anuncio_origem",
        "primeiro_anuncio_id",
        "primeiro_anuncio_clique_id",
        "primeiro_anuncio_data",
    )
    has_first = any(str(current_fields.get(key) or "").strip() for key in first_markers)
    if not has_first:
        for field_name, value in _ad_event_values("primeiro", first).items():
            _set_ad_field(
                plan,
                current_fields,
                field_name,
                value,
                attribution=first,
                available_weni_fields=available_weni_fields,
            )
    elif _same_ad_event(current_fields, "primeiro", first):
        # Mesmo anúncio: apenas completa metadados ausentes, sem sobrescrever o primeiro registro.
        for field_name, value in _ad_event_values("primeiro", first).items():
            _set_ad_field(
                plan,
                current_fields,
                field_name,
                value,
                attribution=first,
                available_weni_fields=available_weni_fields,
                only_if_blank=True,
            )

    # Último anúncio só muda quando a evidência Google Ads é realmente mais nova.
    last_markers = (
        "ultimo_anuncio_origem",
        "ultimo_anuncio_id",
        "ultimo_anuncio_clique_id",
        "ultimo_anuncio_data",
    )
    has_last = any(str(current_fields.get(key) or "").strip() for key in last_markers)
    current_last_date = _parse_datetime(current_fields.get("ultimo_anuncio_data"))
    candidate_last_date = _parse_datetime(last.occurred_at)

    replace_last = not has_last
    if has_last and current_last_date is not None and candidate_last_date is not None:
        replace_last = candidate_last_date > current_last_date

    if replace_last:
        for field_name, value in _ad_event_values("ultimo", last).items():
            _set_ad_field(
                plan,
                current_fields,
                field_name,
                value,
                attribution=last,
                available_weni_fields=available_weni_fields,
            )
    elif _same_ad_event(current_fields, "ultimo", last):
        for field_name, value in _ad_event_values("ultimo", last).items():
            _set_ad_field(
                plan,
                current_fields,
                field_name,
                value,
                attribution=last,
                available_weni_fields=available_weni_fields,
                only_if_blank=True,
            )


def _contact_in_group(
    contact: dict[str, Any], *, group_name: str, group_uuid: str
) -> bool:
    groups = contact.get("groups")
    if not isinstance(groups, list):
        return False
    expected_name = group_name.strip().casefold()
    expected_uuid = group_uuid.strip().casefold()
    for item in groups:
        if isinstance(item, dict):
            name = str(item.get("name") or "").strip().casefold()
            uuid = str(item.get("uuid") or "").strip().casefold()
            if (expected_name and name == expected_name) or (
                expected_uuid and uuid == expected_uuid
            ):
                return True
        else:
            text = str(item or "").strip().casefold()
            if text and text in {expected_name, expected_uuid}:
                return True
    return False


def _message_sort_key(message: dict[str, Any]) -> tuple[datetime, str]:
    return (
        _parse_datetime(message_created_on(message))
        or datetime.min.replace(tzinfo=UTC),
        message_id(message),
    )


def _load_state(path: Path = WENI_UPDATE_STATE_FILE) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else {}
    except (OSError, json.JSONDecodeError):
        logging.warning("Estado do NexoFlow Weni inválido. Será ignorado.")
        return {}


def _purchase_check_state_payload(
    plans: list[ContactUpdatePlan], conflicted_uuids: list[str] | None = None
) -> dict[str, Any]:
    pending = {}
    resolved = {}
    conflicted = set(conflicted_uuids or [])
    for plan in plans:
        if check := plan.guards.get("__pending_purchase_check"):
            pending[plan.contact_uuid] = check
        if (
            check := plan.guards.get("__pending_purchase_check_resolved")
        ) and plan.contact_uuid not in conflicted:
            resolved[plan.contact_uuid] = check
    return {
        "pending_purchase_checks": normalize_purchase_checks(pending),
        "_resolved_purchase_checks": normalize_purchase_checks(resolved),
    }


def _save_state(payload: dict[str, Any], path: Path = WENI_UPDATE_STATE_FILE) -> None:
    payload = dict(payload)
    pending = merge_purchase_checks(
        load_pending_purchase_checks(path), payload.get("pending_purchase_checks")
    )
    for uid, check in normalize_purchase_checks(
        payload.pop("_resolved_purchase_checks", None)
    ).items():
        if pending.get(uid) == check:
            pending.pop(uid)
    if pending or "pending_purchase_checks" in payload:
        payload["pending_purchase_checks"] = pending
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(
        prefix="weni-update-", suffix=".json", dir=path.parent
    )
    os.close(fd)
    temp_path = Path(temp_name)
    try:
        temp_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temp_path.replace(path)
    finally:
        if temp_path.exists():
            temp_path.unlink(missing_ok=True)


_AUDIT_HANDLES: dict[str, Any] = {}
_AUDIT_BYTES_WRITTEN = 0


def _append_audit(event: dict[str, Any], path: Path = WENI_UPDATE_AUDIT_FILE) -> None:
    """Anexa JSONL reutilizando o handle durante a execução.

    Em cargas completas isso evita dezenas de milhares de open/close sem
    alterar a ordem nem a durabilidade por linha (buffering=1).
    """
    global _AUDIT_BYTES_WRITTEN
    path.parent.mkdir(parents=True, exist_ok=True)
    key = str(path.resolve())
    handle = _AUDIT_HANDLES.get(key)
    if handle is None or getattr(handle, "closed", True):
        handle = path.open("a", encoding="utf-8", buffering=1)
        _AUDIT_HANDLES[key] = handle
    line = json.dumps(event, ensure_ascii=False, default=str) + "\n"
    handle.write(line)
    # TextIO converte LF para CRLF no Windows; contar bytes realmente escritos.
    _AUDIT_BYTES_WRITTEN += len(line.encode("utf-8")) + (
        1 if os.linesep == "\r\n" else 0
    )


def _close_audit_handles() -> None:
    for handle in list(_AUDIT_HANDLES.values()):
        try:
            handle.flush()
            handle.close()
        except OSError:
            pass
    _AUDIT_HANDLES.clear()


def resolve_window(*, full: bool, started_at: datetime | None = None) -> UpdateWindow:
    started = started_at or utc_now()
    if full:
        return UpdateWindow(started_at=started, after=None, full=True)

    state = _load_state()
    checkpoint = _parse_datetime(state.get("last_success_utc"))
    if checkpoint is None:
        checkpoint = started - timedelta(days=WENI_UPDATE_INITIAL_LOOKBACK_DAYS)
        logging.info(
            "Primeira execução incremental: usando janela inicial de %s dias.",
            WENI_UPDATE_INITIAL_LOOKBACK_DAYS,
        )
    after = checkpoint - timedelta(minutes=WENI_UPDATE_OVERLAP_MINUTES)
    return UpdateWindow(started_at=started, after=after, full=False)


def _group_messages_by_contact(
    messages: list[dict[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for message in messages:
        uuid = _extract_contact_uuid(message)
        if not uuid:
            continue
        grouped.setdefault(uuid, []).append(message)
    for values in grouped.values():
        values.sort(key=_message_sort_key)
    return grouped


def _build_evidence_payload(evidence: FieldEvidence) -> dict[str, str]:
    return {
        "message_id": evidence.message_id,
        "created_on": evidence.created_on,
        "reason": evidence.reason,
    }


def _set_field_if_changed(
    plan: ContactUpdatePlan,
    current_fields: dict[str, Any],
    field_name: str,
    value: Any,
    *,
    evidence: FieldEvidence | None = None,
    reason: str = "",
) -> None:
    if value is None or str(value).strip() == "":
        return
    current = current_fields.get(field_name)
    if _same_value(field_name, current, value):
        return
    plan.previous[field_name] = current
    plan.fields[field_name] = value
    if evidence is not None:
        plan.evidence[field_name] = _build_evidence_payload(evidence)
    elif reason:
        plan.evidence[field_name] = {"reason": reason}


def _sync_contact_geography_fields(
    plan: ContactUpdatePlan,
    fields: dict[str, Any],
    available_fields: set[str] | None,
) -> None:
    changes = geography_changes(fields)
    changes = {
        key: value
        for key, value in changes.items()
        if available_fields is None or key in available_fields
    }
    if not changes:
        return
    for key in GEOGRAPHY_FIELDS | {"cep"}:
        plan.guards[key] = fields.get(key)
    for key, value in changes.items():
        _set_field_if_changed(
            plan,
            fields,
            key,
            value,
            reason="grafia padronizada por referência de município, UF e bairro",
        )


def _purchase_is_cancelled(order: dict[str, str]) -> bool:
    return is_order_cancelled(order)


def _weni_number_text(value: Any) -> str:
    number = _safe_number(value)
    if number is None:
        return ""
    return format(Decimal(str(number)).normalize(), "f")


def _apply_purchase_candidate(
    plan: ContactUpdatePlan,
    contact: dict[str, Any],
    *,
    farm_client: FarmaliderClient | None,
    captured_at: str,
    available_weni_fields: set[str] | None = None,
    resolved_order: tuple[dict[str, str] | None, list[dict[str, str]]] | None = None,
) -> bool:
    """Resolve the latest official FarmaLíder order and flatten it into Weni."""
    current_fields = get_fields(contact)
    candidate = dict(contact)
    candidate["fields"] = dict(current_fields)
    if "cpf" in plan.fields:
        candidate["fields"]["cpf"] = plan.fields["cpf"]

    try:
        order, items = (
            resolved_order
            if resolved_order is not None
            else resolve_contact_order(
                candidate,
                farm_client=farm_client,
                captured_at=captured_at,
            )
        )
    except (FarmaliderAuthError, FarmaliderUnavailableError):
        raise
    except FarmaliderError:
        plan.order_resolution_status = "ERRO_API"
        logging.warning("Falha ao consultar ou confirmar pedido na FarmaLíder API.")
        return False

    plan.order_resolution_status = (
        str(order.get("status_confirmacao") or "SEM_PEDIDO_VALIDADO")
        if order is not None
        else "SEM_PEDIDO_VALIDADO"
        if farm_client is not None
        else "API_DESABILITADA"
    )
    if (
        order is not None
        and order.get("_farmalider_cabecalho_verificado") == "1"
        and _purchase_is_cancelled(order)
    ):
        # A verified negative header is enough to withdraw scheduling. Missing
        # items still prevent every partial pedido_* update and sale admission.
        plan.purchase_confirmation = "CANCELADO_API"
    if order is not None and str(order.get("status_confirmacao") or "").startswith(
        "CPF_IDENTIDADE_"
    ):
        identity_level = (
            logging.DEBUG
            if cpf_query_priority(candidate["fields"].get("cpf")) == 1
            else logging.WARNING
        )
        if logging.getLogger().isEnabledFor(identity_level):
            reference = (
                farm_client.weni_contact_diagnostic_ref(str(contact.get("uuid") or ""))
                if isinstance(farm_client, FarmaliderClient)
                else "-"
            )
            logging.log(
                identity_level,
                "Weni [contato_ref=%s]: pedido bloqueado: %s; primeiro nome ou telefone local não identificaram um cliente único.",
                reference,
                order["status_confirmacao"],
            )
    if order is None or not items:
        return False

    confirmation = order.get("status_confirmacao", "")
    if confirmation not in {"CONFIRMADO_API", "CANCELADO_API"}:
        return False
    if order.get("status_validacao") != "OK":
        return False

    pedido_id = str(order.get("pedido_id") or "").strip()
    proposed_dt = business_order_datetime(order.get("data_hora"))
    order_value = _safe_number(order.get("valor_total_compra"))
    if not pedido_id or proposed_dt is None or order_value is None or order_value < 0:
        return False

    # A confirmed order may remain unchanged or be older than Weni's snapshot.
    # Diagnostic counts must not confuse those cases with a missing order.
    plan.order_found = True
    plan.official_order_date = proposed_dt.isoformat()

    names = [(item.get("produto_nome") or "").strip() for item in items]
    quantities = [_weni_number_text(item.get("quantidade")) for item in items]
    unit_values = [_weni_number_text(item.get("valor_unitario")) for item in items]
    if (
        not names
        or any(not value for value in names)
        or any(not value for value in quantities)
        or any(not value for value in unit_values)
        or not (len(names) == len(quantities) == len(unit_values))
    ):
        return False

    current_order_date = business_order_datetime(
        normalize_value(current_fields.get("pedido_data"))
    )
    current_order_id = normalize_value(current_fields.get("pedido_id")).strip()
    same_order = (
        current_order_id.isascii()
        and current_order_id.isdigit()
        and str(int(current_order_id)) == pedido_id
    )
    # A different, newer order must remain intact. For the same verified ID,
    # the official emission also corrects a mistaken Weni date; otherwise that
    # date would prevent current status/payment, including cancellation, updates.
    if (
        not same_order
        and current_order_date is not None
        and current_order_date > proposed_dt
    ):
        return False

    proposed_date = weni_order_datetime_text(proposed_dt)
    status_code = effective_order_status_code(order)
    payment_code = str(order.get("farmalider_forma_pagamento") or "").strip()
    normalized_payment = normalize_farmalider_payment_code(payment_code)
    if normalized_payment:
        payment_code = "Cancelado" if normalized_payment == "0" else normalized_payment
    if not farmalider_order_status_name(status_code):
        return False

    plan.order_validated = True
    plan.order_payment_code = normalized_payment
    plan.purchase_confirmation = confirmation
    plan.purchase_confirmed = confirmation == "CONFIRMADO_API" and is_order_sale(order)
    plan.order_has_animal_products = any(is_animal_product(item) for item in items)
    reason = "pedido oficial FarmaLíder atualizado por GET e normalizado para Weni"

    order_fields_to_write: dict[str, Any] = {
        "pedido_id": pedido_id,
        "pedido_data": proposed_date,
        "pedido_status": status_code,
        "pedido_produtos": serialize_weni_order_list(names),
        "pedido_quantidade_itens": serialize_weni_order_list(quantities),
        "pedido_valor_produtos": serialize_weni_order_list(unit_values),
        "pedido_valor": order_value,
    }
    if payment_code:
        order_fields_to_write["pedido_forma_pagamento"] = payment_code

    for field_name, value in order_fields_to_write.items():
        if (
            available_weni_fields is not None
            and field_name not in available_weni_fields
        ):
            continue
        _set_field_if_changed(plan, current_fields, field_name, value, reason=reason)

    if plan.purchase_confirmed and (
        available_weni_fields is None or "ultima_compra" in available_weni_fields
    ):
        current_last_purchase = business_order_datetime(
            normalize_value(current_fields.get("ultima_compra"))
        )
        if current_last_purchase is None or current_last_purchase <= proposed_dt:
            _set_field_if_changed(
                plan,
                current_fields,
                "ultima_compra",
                proposed_dt.isoformat(),
                reason="pedido não cancelado confirmado pela API FarmaLíder",
            )
    return True


def _recompra_products_need_unscheduling(plan: ContactUpdatePlan) -> bool:
    """A carga completa não pode restaurar itens pet em campanha agendada."""
    if "pedido_produtos" not in plan.fields:
        return False
    if plan.order_has_animal_products:
        return True
    names = parse_weni_order_list(normalize_value(plan.fields["pedido_produtos"]))
    return any(is_animal_product({"produto_nome": name}) for name in names)


def _build_contact_plan(
    contact: dict[str, Any],
    *,
    context_messages: list[dict[str, Any]],
    candidate_message_ids: set[str],
    farm_client: FarmaliderClient | None,
    opt_out_group_name: str,
    opt_out_group_uuid: str,
    captured_at: str,
    opt_in_group_name: str = WENI_UPDATE_OPT_IN_GROUP,
    opt_in_group_uuid: str = "",
    available_weni_fields: set[str] | None = None,
    pending_purchase_check: dict[str, str] | None = None,
) -> ContactUpdatePlan:
    contact_uuid = str(contact.get("uuid") or "").strip()
    fields = get_fields(contact)
    contact_name = str(contact.get("name") or fields.get("nome") or "").strip()
    plan = ContactUpdatePlan(
        contact_uuid=contact_uuid,
        contact_name=contact_name,
    )

    _sync_contact_channel_fields(plan, contact, fields)
    _sync_contact_geography_fields(plan, fields, available_weni_fields)

    evidences = infer_contact_fields(
        context_messages,
        candidate_message_ids=candidate_message_ids,
        current_lgpd=fields.get("opt_in_lgpd"),
    )
    for field_name, evidence in evidences.items():
        _set_field_if_changed(
            plan,
            fields,
            field_name,
            evidence.value,
            evidence=evidence,
        )

    _sync_google_ads_attribution(
        plan,
        contact,
        fields,
        context_messages,
        available_weni_fields=available_weni_fields,
    )

    # O valor efetivo de opt_in_lgpd é a fonte de verdade para os grupos.
    # A rotina corrige os dois lados, inclusive quando o contato já chega com
    # associação inconsistente: ativo <-> Opt-In, recusado <-> Opt-Out.
    desired_lgpd = (
        str(plan.fields.get("opt_in_lgpd", fields.get("opt_in_lgpd") or ""))
        .strip()
        .casefold()
    )
    is_opt_in = _contact_in_group(
        contact,
        group_name=opt_in_group_name,
        group_uuid=opt_in_group_uuid,
    )
    is_opt_out = _contact_in_group(
        contact,
        group_name=opt_out_group_name,
        group_uuid=opt_out_group_uuid,
    )
    opt_in_target = opt_in_group_uuid or opt_in_group_name
    opt_out_target = opt_out_group_uuid or opt_out_group_name

    if desired_lgpd == "ativo":
        if not is_opt_in:
            plan.group_actions.append(
                GroupAction(
                    action="add",
                    group=opt_in_target,
                    reason="opt_in_lgpd ativo",
                    group_name=opt_in_group_name,
                )
            )
        if is_opt_out:
            plan.group_actions.append(
                GroupAction(
                    action="remove",
                    group=opt_out_target,
                    reason="opt_in_lgpd ativo",
                    group_name=opt_out_group_name,
                )
            )
    elif desired_lgpd == "recusado":
        # Primeiro garante o grupo correto e só depois remove o incompatível.
        # Assim, uma falha intermediária não deixa o contato sem o grupo que
        # corresponde ao valor atual de opt_in_lgpd.
        if not is_opt_out:
            plan.group_actions.append(
                GroupAction(
                    action="add",
                    group=opt_out_target,
                    reason="opt_in_lgpd recusado",
                    group_name=opt_out_group_name,
                )
            )
        if is_opt_in:
            plan.group_actions.append(
                GroupAction(
                    action="remove",
                    group=opt_in_target,
                    reason="opt_in_lgpd recusado",
                    group_name=opt_in_group_name,
                )
            )

    if plan.group_actions:
        # As ações de grupo dependem do valor efetivo de opt_in_lgpd. Guardar
        # a origem impede aplicar uma decisão antiga caso outro flow altere o
        # consentimento durante a revisão humana.
        plan.guards["opt_in_lgpd"] = fields.get("opt_in_lgpd")

    _apply_purchase_candidate(
        plan,
        contact,
        farm_client=farm_client,
        captured_at=captured_at,
        available_weni_fields=available_weni_fields,
    )
    observed_check = infer_pending_purchase_check(
        context_messages, captured_at=captured_at
    )
    if observed_check:
        observed_date = business_order_datetime(observed_check["observed_at"])
        known_dates = [
            business_order_datetime(normalize_value(fields.get("pedido_data"))),
            business_order_datetime(plan.official_order_date),
        ]
        if observed_date and any(
            known and known.date() >= observed_date.date() for known in known_dates
        ):
            # Automatic evidence only introduces a restriction for a newer
            # day. Existing reviewed/portable restrictions still require API
            # confirmation; Weni's date alone never clears those restrictions.
            observed_check = None
    checks = merge_purchase_checks(
        {contact_uuid: current_check}
        if (
            current_check := contact_pending_purchase_check(
                contact, pending_purchase_check
            )
        )
        else {},
        {contact_uuid: observed_check} if observed_check else {},
    )
    check = checks.get(contact_uuid)
    resolved = False
    if check:
        observed = business_order_datetime(check["observed_at"])
        official = business_order_datetime(plan.official_order_date)
        resolved = bool(official and observed and official.date() >= observed.date())
        key = (
            "__pending_purchase_check_resolved"
            if resolved
            else "__pending_purchase_check"
        )
        plan.guards[key] = check
        plan.guards[PENDING_PURCHASE_FIELD] = fields.get(PENDING_PURCHASE_FIELD)
        if (
            available_weni_fields is None
            or PENDING_PURCHASE_FIELD in available_weni_fields
        ):
            _set_field_if_changed(
                plan,
                fields,
                PENDING_PURCHASE_FIELD,
                "resolvido" if resolved else check["observed_at"],
                reason="revisão de compra posterior; emissão oficial preservada",
            )
    if plan.order_validated or plan.purchase_confirmation == "CANCELADO_API":
        effective_cpf = normalize_cpf(plan.fields.get("cpf", fields.get("cpf")))
        if effective_cpf and not is_valid_cpf(effective_cpf):
            plan.guards["__invalid_cpf_identity"] = weni_identity_guard(contact)
            plan.guards["__native_urns"] = sorted(native_contact_urns(contact))
        # A revisão depende da identidade e das duas possíveis fontes.
        for key in (
            "cpf",
            "ultimo_pedido",
            "pedido_id",
            "pedido_data",
            "pedido_status",
            "pedido_produtos",
            "pedido_quantidade_itens",
            "pedido_valor_produtos",
            "pedido_valor",
            "pedido_forma_pagamento",
        ):
            plan.guards[key] = fields.get(key)
    from recompra import GROUP, OPT_OUT, TZ, member, refused

    campaign_groups = [
        g
        for g in contact.get("groups", []) or []
        if isinstance(g, dict) and g.get("name") == GROUP and g.get("uuid")
    ]
    if campaign_groups:
        reference = business_order_datetime(
            plan.fields.get("pedido_data", fields.get("pedido_data"))
        )
        local_now = business_order_datetime(captured_at)
        expired = reference is None or (
            local_now and (local_now.astimezone(TZ).date() - reference.date()).days > 25
        )
        if (
            "pedido_data" in plan.fields
            or _recompra_products_need_unscheduling(plan)
            or (plan.order_validated and not plan.purchase_confirmed)
            or plan.purchase_confirmation == "CANCELADO_API"
            or expired
            or refused(contact)
            or member(contact, "", OPT_OUT)
            or (check and not resolved)
        ):
            plan.group_actions.append(
                GroupAction(
                    "remove",
                    campaign_groups[0]["uuid"],
                    "compra posterior pendente de confirmação pela API"
                    if check and not resolved
                    else "referência de recompra mudou, expirou ou cliente inelegível; reavaliar na rotina de recompra",
                    GROUP,
                )
            )
            plan.guards["marketing_opt_in"] = fields.get("marketing_opt_in")

    return plan


def _execute_contact_plan(weni, plan):
    # Uma referência nova não pode deixar o evento antigo ativo enquanto os
    # campos são atualizados. A remoção já faz parte do plano revisado.
    removals = [
        a
        for a in plan.group_actions
        if a.action == "remove" and a.group_name == "Recompra FarmaLíder"
    ]
    for action in removals:
        weni.contact_group_action(
            plan.contact_uuid, action="remove", group=action.group
        )
    if plan.fields:
        response = weni.update_contact_fields(plan.contact_uuid, plan.fields)
        if isinstance(response, dict) and isinstance(response.get("fields"), dict):
            persisted = get_fields(response)
            mismatched = sorted(
                name
                for name, proposed in plan.fields.items()
                if not _same_value(name, persisted.get(name), proposed)
            )
            if mismatched:
                raise WeniError(
                    "A Weni não confirmou a persistência dos campos atualizados: "
                    + ", ".join(mismatched)
                    + ". Confira o estado atual antes de retomar o plano revisado."
                )
    for action in plan.group_actions:
        if action not in removals:
            weni.contact_group_action(
                plan.contact_uuid, action=action.action, group=action.group
            )


def audit_plans(
    plans: list[ContactUpdatePlan],
    *,
    opt_out_group_available: bool,
    opt_in_group_available: bool = True,
) -> None:
    errors: list[str] = []
    seen: set[str] = set()

    for plan in plans:
        if not plan.contact_uuid:
            errors.append("plano sem contact_uuid")
            continue
        if plan.contact_uuid in seen:
            errors.append(f"contact_uuid duplicado no plano: {plan.contact_uuid}")
        seen.add(plan.contact_uuid)

        unknown = sorted(set(plan.fields) - ALLOWED_WRITE_FIELDS)
        if unknown:
            errors.append(
                f"{plan.contact_uuid}: campos não autorizados: {', '.join(unknown)}"
            )

        if GEOGRAPHY_FIELDS & set(plan.fields):
            required = GEOGRAPHY_FIELDS | {"cep"}
            expected = geography_changes(plan.guards)
            if not required.issubset(plan.guards) or any(
                expected.get(key) != value
                for key, value in plan.fields.items()
                if key in GEOGRAPHY_FIELDS
            ):
                errors.append(
                    f"{plan.contact_uuid}: padronização de endereço sem fonte válida"
                )

        for field_name, value in plan.fields.items():
            if value is None or str(value).strip() == "":
                errors.append(
                    f"{plan.contact_uuid}: tentativa de gravar vazio em {field_name}"
                )

        if "cpf" in plan.fields:
            cpf = normalize_cpf(plan.fields["cpf"])
            if not is_valid_cpf(cpf):
                errors.append(f"{plan.contact_uuid}: CPF inválido")
            elif str(plan.fields["cpf"]).strip() != cpf:
                errors.append(
                    f"{plan.contact_uuid}: CPF deve ser gravado com 11 dígitos sem pontuação"
                )

        if "idade" in plan.fields:
            age = _safe_number(plan.fields["idade"])
            if not isinstance(age, int) or not 1 <= age <= 120:
                errors.append(f"{plan.contact_uuid}: idade fora da faixa segura")
            expected = "sim" if isinstance(age, int) and age >= 18 else "não"
            majority = (
                str(plan.fields.get("maior_de_18_anos", expected)).strip().casefold()
            )
            if age is not None and majority != expected:
                errors.append(f"{plan.contact_uuid}: idade e maioridade inconsistentes")

        if "maior_de_18_anos" in plan.fields:
            if str(plan.fields["maior_de_18_anos"]).strip().casefold() not in {
                "sim",
                "não",
            }:
                errors.append(
                    f"{plan.contact_uuid}: valor inválido em maior_de_18_anos"
                )

        if "opt_in_lgpd" in plan.fields:
            if str(plan.fields["opt_in_lgpd"]).strip().casefold() not in {
                "ativo",
                "recusado",
            }:
                errors.append(f"{plan.contact_uuid}: valor inválido em opt_in_lgpd")

        if PENDING_PURCHASE_FIELD in plan.fields:
            marker = normalize_value(plan.fields[PENDING_PURCHASE_FIELD]).strip()
            key = (
                "__pending_purchase_check_resolved"
                if marker == "resolvido"
                else "__pending_purchase_check"
            )
            if not plan.guards.get(key) or (
                marker != "resolvido" and business_order_datetime(marker) is None
            ):
                errors.append(
                    f"{plan.contact_uuid}: revisão de pedido sem evidência válida"
                )

        if "pedido_status" in plan.fields:
            if not farmalider_order_status_name(plan.fields["pedido_status"]):
                errors.append(f"{plan.contact_uuid}: código inválido em pedido_status")

        if "pedido_forma_pagamento" in plan.fields:
            payment = str(plan.fields["pedido_forma_pagamento"]).strip()
            normalized_payment = normalize_farmalider_payment_code(payment)
            if payment.casefold() != "cancelado" and (
                not normalized_payment
                or normalized_payment != payment
                or normalized_payment == "0"
            ):
                errors.append(
                    f"{plan.contact_uuid}: código inválido em pedido_forma_pagamento"
                )

        if plan.order_validated and set(plan.fields) & PURCHASE_FIELDS:
            cpf = normalize_cpf(plan.fields.get("cpf", plan.guards.get("cpf")))
            if (
                cpf
                and not is_valid_cpf(cpf)
                and not plan.guards.get("__invalid_cpf_identity")
            ):
                errors.append(
                    f"{plan.contact_uuid}: CPF inválido exige nova validação por primeiro nome ou telefone local; gere outro dry-run"
                )
            payment = normalize_value(
                plan.fields.get(
                    "pedido_forma_pagamento",
                    plan.guards.get(
                        "pedido_forma_pagamento",
                        plan.previous.get("pedido_forma_pagamento"),
                    ),
                )
            ).strip()
            status = normalize_value(
                plan.fields.get(
                    "pedido_status",
                    plan.guards.get(
                        "pedido_status", plan.previous.get("pedido_status")
                    ),
                )
            ).strip()
            if normalize_farmalider_payment_code(payment) == "0":
                errors.append(
                    f"{plan.contact_uuid}: pagamento 0 exige novo plano com Cancelado e pedido_status=3"
                )
            if payment.casefold() == "cancelado" and status != "3":
                errors.append(
                    f"{plan.contact_uuid}: pagamento Cancelado exige pedido_status=3"
                )
            if "ultima_compra" in plan.fields and (
                status not in {"-2", "-1", "0", "4", "1", "2"}
                or payment.casefold() == "cancelado"
            ):
                errors.append(
                    f"{plan.contact_uuid}: ultima_compra exige status documentado não cancelado"
                )

        if "contact_canal_principal" in plan.fields:
            channel = str(plan.fields["contact_canal_principal"]).strip().casefold()
            if channel not in {"whatsapp", "instagram", "facebook", "outro"}:
                errors.append(f"{plan.contact_uuid}: canal principal inválido")

        expected_channel_fields = {
            "contact_urn_whatsapp": "whatsapp",
            "contact_urn_instagram": "instagram",
            "contact_urn_facebook": "facebook",
        }
        for field_name, expected_channel in expected_channel_fields.items():
            if field_name not in plan.fields:
                continue
            detected = urn_channel(plan.fields[field_name])
            if detected != expected_channel:
                errors.append(
                    f"{plan.contact_uuid}: {field_name} não contém URN de {expected_channel}"
                )

        for field_name in (
            "primeiro_anuncio_canal_destino",
            "ultimo_anuncio_canal_destino",
        ):
            if field_name in plan.fields:
                channel = str(plan.fields[field_name]).strip().casefold()
                if channel not in {"whatsapp", "instagram", "facebook", "outro"}:
                    errors.append(
                        f"{plan.contact_uuid}: canal de destino de anúncio inválido em {field_name}"
                    )

        for field_name in ("primeiro_anuncio_origem", "ultimo_anuncio_origem"):
            if (
                field_name in plan.fields
                and str(plan.fields[field_name]).strip() != GOOGLE_ADS_ORIGIN
            ):
                errors.append(
                    f"{plan.contact_uuid}: origem de anúncio não autorizada pelo detector Google Ads"
                )

        if set(plan.fields) & (PURCHASE_FIELDS - {"ultima_compra"}) and not (
            plan.order_validated or plan.purchase_confirmed
        ):
            errors.append(f"{plan.contact_uuid}: campos de pedido sem validação")
        if "ultima_compra" in plan.fields and not plan.purchase_confirmed:
            errors.append(
                f"{plan.contact_uuid}: ultima_compra sem compra não cancelada confirmada"
            )
        for date_field in ("pedido_data", "ultima_compra"):
            if (
                date_field in plan.fields
                and business_order_datetime(plan.fields[date_field]) is None
            ):
                errors.append(f"{plan.contact_uuid}: data inválida em {date_field}")
        if "pedido_valor" in plan.fields:
            number = _safe_number(plan.fields["pedido_valor"])
            if number is None or number < 0:
                errors.append(f"{plan.contact_uuid}: número inválido em pedido_valor")

        aligned_fields = (
            "pedido_produtos",
            "pedido_quantidade_itens",
            "pedido_valor_produtos",
        )
        effective_fields = {**plan.guards, **plan.fields}
        parsed_lists: dict[str, list[str]] = {}
        if set(plan.fields) & set(aligned_fields):
            for list_field in aligned_fields:
                if (
                    list_field not in effective_fields
                    or effective_fields[list_field] is None
                ):
                    continue
                try:
                    parsed_lists[list_field] = parse_weni_order_list(
                        normalize_value(effective_fields[list_field])
                    )
                except ValueError:
                    errors.append(f"{plan.contact_uuid}: CSV inválido em {list_field}")

        if "pedido_produtos" in plan.fields:
            products = parsed_lists.get("pedido_produtos", [])
            if not products or not all(products):
                errors.append(f"{plan.contact_uuid}: lista vazia em pedido_produtos")

        for list_field in ("pedido_quantidade_itens", "pedido_valor_produtos"):
            if list_field not in plan.fields:
                continue
            values = parsed_lists.get(list_field, [])
            if not values:
                errors.append(f"{plan.contact_uuid}: lista vazia em {list_field}")
                continue
            for value in values:
                number = _safe_number(value)
                if (
                    number is None
                    or number < 0
                    or (list_field == "pedido_quantidade_itens" and number <= 0)
                ):
                    errors.append(
                        f"{plan.contact_uuid}: valor inválido em {list_field}"
                    )
                    break

        if all(field in parsed_lists for field in aligned_fields):
            product_count = len(parsed_lists["pedido_produtos"])
            quantity_count = len(parsed_lists["pedido_quantidade_itens"])
            value_count = len(parsed_lists["pedido_valor_produtos"])
            if len({product_count, quantity_count, value_count}) != 1:
                errors.append(
                    f"{plan.contact_uuid}: listas de produto/quantidade/valor desalinhadas "
                    f"(produtos={product_count}; quantidades={quantity_count}; valores={value_count})"
                )

        for action in plan.group_actions:
            if action.action not in {"add", "remove"}:
                errors.append(f"{plan.contact_uuid}: ação de grupo inválida")
            if not action.group:
                errors.append(f"{plan.contact_uuid}: grupo vazio")
                continue

            group_name = (action.group_name or action.group).strip()
            if (
                group_name.casefold() == WENI_UPDATE_OPT_IN_GROUP.casefold()
                and not opt_in_group_available
            ):
                errors.append(
                    f"{plan.contact_uuid}: grupo '{WENI_UPDATE_OPT_IN_GROUP}' não foi localizado"
                )
            if (
                group_name.casefold() == WENI_UPDATE_OPT_OUT_GROUP.casefold()
                and not opt_out_group_available
            ):
                errors.append(
                    f"{plan.contact_uuid}: grupo '{WENI_UPDATE_OPT_OUT_GROUP}' não foi localizado"
                )

    if errors:
        preview = "\n".join(f"- {item}" for item in errors[:30])
        if len(errors) > 30:
            preview += f"\n- ... e mais {len(errors) - 30} erro(s)"
        raise PlanAuditError(
            "Auditoria obrigatória do plano falhou. Nenhuma gravação foi iniciada:\n"
            + preview
        )


def _audit_plan_event(
    plan: ContactUpdatePlan,
    *,
    mode: str,
    status: str,
    error: str = "",
) -> dict[str, Any]:
    return {
        "timestamp_utc": iso_utc(utc_now()),
        "mode": mode,
        "status": status,
        "contact_uuid": plan.contact_uuid,
        "previous": plan.previous,
        "guards": plan.guards,
        "changes": plan.fields,
        "evidence": plan.evidence,
        "group_actions": [asdict(item) for item in plan.group_actions],
        "order_validated": plan.order_validated,
        "purchase_confirmed": plan.purchase_confirmed,
        "purchase_confirmation": plan.purchase_confirmation,
        "error": error,
    }


REVIEW_SCHEMA_VERSION = 6


def _canonical_plan(plan: ContactUpdatePlan) -> dict[str, Any]:
    """Parte semanticamente relevante do plano usada para travar o apply ao dry-run revisado."""
    return {
        "contact_uuid": plan.contact_uuid,
        "previous": plan.previous,
        "guards": plan.guards,
        "changes": plan.fields,
        "evidence": plan.evidence,
        "group_actions": [asdict(item) for item in plan.group_actions],
        "order_validated": plan.order_validated,
        "purchase_confirmed": plan.purchase_confirmed,
        "purchase_confirmation": plan.purchase_confirmation,
        **(
            {"order_has_animal_products": True}
            if plan.order_has_animal_products
            else {}
        ),
    }


def _plan_digest(plans: list[ContactUpdatePlan]) -> str:
    canonical = [
        _canonical_plan(plan)
        for plan in sorted(plans, key=lambda item: item.contact_uuid)
    ]
    payload = json.dumps(
        canonical,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _display_value(value: Any) -> str:
    if value is None or str(value).strip() == "":
        return "<vazio>"
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    return str(value)


def _review_summary(
    plans: list[ContactUpdatePlan], stats: UpdateStats
) -> dict[str, Any]:
    field_counts: dict[str, int] = {}
    opt_in_add = 0
    opt_in_remove = 0
    opt_out_add = 0
    opt_out_remove = 0
    lgpd_contacts = 0
    cpf_contacts = 0
    age_contacts = 0
    purchase_contacts = 0
    channel_contacts = 0
    google_ads_contacts = 0
    principal_channels: dict[str, int] = {}

    for plan in plans:
        for field_name in plan.fields:
            field_counts[field_name] = field_counts.get(field_name, 0) + 1
        if "opt_in_lgpd" in plan.fields:
            lgpd_contacts += 1
        if "cpf" in plan.fields:
            cpf_contacts += 1
        if {"idade", "maior_de_18_anos"} & set(plan.fields):
            age_contacts += 1
        if PURCHASE_FIELDS & set(plan.fields):
            purchase_contacts += 1
        if {
            "contact_urn",
            "contact_urn_whatsapp",
            "contact_urn_instagram",
            "contact_urn_facebook",
            "contact_canal_principal",
        } & set(plan.fields):
            channel_contacts += 1
        if any(
            str(plan.fields.get(field_name) or "").strip().casefold() == "google ads"
            for field_name in ("primeiro_anuncio_origem", "ultimo_anuncio_origem")
        ):
            google_ads_contacts += 1
        if plan.contact_channel:
            principal_channels[plan.contact_channel] = (
                principal_channels.get(plan.contact_channel, 0) + 1
            )
        for action in plan.group_actions:
            group_name = (action.group_name or action.group).strip().casefold()
            if group_name == WENI_UPDATE_OPT_IN_GROUP.casefold():
                if action.action == "add":
                    opt_in_add += 1
                elif action.action == "remove":
                    opt_in_remove += 1
            elif group_name == WENI_UPDATE_OPT_OUT_GROUP.casefold():
                if action.action == "add":
                    opt_out_add += 1
                elif action.action == "remove":
                    opt_out_remove += 1

    return {
        "messages_incoming": stats.messages_incoming,
        "contacts_considered": stats.contacts_considered,
        "contacts_with_changes": len(plans),
        "field_changes": sum(len(plan.fields) for plan in plans),
        "group_actions": sum(len(plan.group_actions) for plan in plans),
        "purchase_confirmed": stats.purchase_confirmed,
        "orders_validated": stats.orders_validated,
        "payment_unmapped_codes": dict(sorted(stats.payment_unmapped_codes.items())),
        "orders_cancelled_payment_zero": stats.orders_cancelled_payment_zero,
        "orders_pending_payment": stats.orders_pending_payment,
        "invalid_cpf_checked": stats.invalid_cpf_checked,
        "invalid_cpf_orders_found": stats.invalid_cpf_orders_found,
        "invalid_cpf_orders_validated": stats.invalid_cpf_orders_validated,
        "invalid_cpf_outcomes": dict(sorted(stats.invalid_cpf_outcomes.items())),
        "field_counts": dict(sorted(field_counts.items())),
        "lgpd_contacts": lgpd_contacts,
        "cpf_contacts": cpf_contacts,
        "age_or_majority_contacts": age_contacts,
        "purchase_contacts": purchase_contacts,
        "channel_contacts": channel_contacts,
        "google_ads_contacts": google_ads_contacts,
        "principal_channels": dict(sorted(principal_channels.items())),
        "opt_in_additions": opt_in_add,
        "opt_in_removals": opt_in_remove,
        "opt_out_additions": opt_out_add,
        "opt_out_removals": opt_out_remove,
    }


def write_review_snapshot(
    plans: list[ContactUpdatePlan],
    stats: UpdateStats,
    *,
    full: bool,
    json_path: Path,
    captured_at_utc: str = "",
    quarter: int | None = None,
    quarter_campaign_id: str = "",
    quarter_total_contacts: int = 0,
    quarter_selected_contacts: int = 0,
) -> tuple[Path, Path, str]:
    """Grava snapshot verificável e relatório TXT para revisão humana antes do apply."""
    json_path = Path(json_path)
    json_path.parent.mkdir(parents=True, exist_ok=True)
    txt_path = json_path.with_suffix(".txt")
    digest = _plan_digest(plans)
    summary = _review_summary(plans, stats)

    created_at = iso_utc(utc_now())
    snapshot_cutoff = str(captured_at_utc or created_at).strip()
    payload = {
        "schema_version": REVIEW_SCHEMA_VERSION,
        "created_at_utc": created_at,
        "captured_at_utc": snapshot_cutoff,
        "mode": "full" if full else "incremental",
        "plan_sha256": digest,
        "summary": summary,
        "pending_retry_uuids": sorted(set(stats.pending_retry_uuids)),
        "plans": [
            {
                "display": {
                    "contact_name": plan.contact_name,
                    "contact_urn": plan.contact_urn,
                    "contact_channel": plan.contact_channel,
                },
                **_canonical_plan(plan),
            }
            for plan in sorted(plans, key=lambda item: item.contact_uuid)
        ],
    }
    if quarter is not None:
        payload["partition"] = {
            "kind": "full_25",
            "part": quarter,
            "total_parts": 4,
            "campaign_id": quarter_campaign_id,
            "total_contacts": quarter_total_contacts,
            "selected_contacts": quarter_selected_contacts,
        }
    json_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )

    lines = [
        "==========================================================",
        " NexoFlow - Revisão Humana do NexoFlow Weni",
        "==========================================================",
        "",
        f"Gerado em UTC: {payload['created_at_utc']}",
        f"Corte do snapshot em UTC: {payload['captured_at_utc']}",
        f"Modo: {'CARGA COMPLETA' if full else 'INCREMENTAL'}",
        *(
            [
                f"Fragmento: PARTE {quarter}/4 (25%)",
                f"Campanha 25%: {quarter_campaign_id}",
                f"Base congelada: {quarter_total_contacts} contatos",
                f"Contatos desta parte: {quarter_selected_contacts}",
            ]
            if quarter is not None
            else []
        ),
        f"Plano SHA-256: {digest}",
        "",
        "IMPORTANTE:",
        "Este relatório é somente leitura. Nenhuma alteração foi aplicada.",
        "Na carga completa, este plano fica CONGELADO após o dry-run.",
        "Novas mensagens e novos contatos não invalidam toda a carga.",
        "No apply, cada contato é relido imediatamente antes da gravação.",
        "Se os dados relevantes daquele contato mudaram, somente ele é",
        "ignorado como conflito e será reprocessado pela próxima incremental.",
        "",
        "RESUMO",
        "----------------------------------------------------------",
        f"Mensagens recebidas analisadas: {summary['messages_incoming']}",
        f"Contatos considerados: {summary['contacts_considered']}",
        f"Contatos com alterações: {summary['contacts_with_changes']}",
        f"Alterações de campos: {summary['field_changes']}",
        f"Ações de grupo: {summary['group_actions']}",
        f"Contatos com alteração de LGPD: {summary['lgpd_contacts']}",
        f"Contatos com CPF detectado/alterado: {summary['cpf_contacts']}",
        f"Contatos com identificação de canal alterada: {summary['channel_contacts']}",
        f"Contatos com atribuição Google Ads detectada/atualizada: {summary['google_ads_contacts']}",
        f"Canais principais no plano: {summary['principal_channels']}",
        f"Contatos com idade/maioridade: {summary['age_or_majority_contacts']}",
        f"Inclusões em Opt-In LGPD: {summary['opt_in_additions']}",
        f"Remoções de Opt-In LGPD: {summary['opt_in_removals']}",
        f"Inclusões em Opt-Out LGPD: {summary['opt_out_additions']}",
        f"Remoções de Opt-Out LGPD: {summary['opt_out_removals']}",
        f"Contatos com campos de pedido: {summary['purchase_contacts']}",
        f"Pedidos confirmados pela FarmaLíder: {summary['purchase_confirmed']}",
        f"Contatos com pedido validado (inclusive sem alteração): {summary['orders_validated']}",
        f"Pagamento não identificado por código: {summary['payment_unmapped_codes']}",
        f"Contatos com pedido cancelado por pagamento 0: {summary['orders_cancelled_payment_zero']}",
        f"Contatos com pedido pendente de pagamento: {summary['orders_pending_payment']}",
        "CPFs inválidos (contagens por contato; não são pedidos únicos nem gravações):",
        _invalid_cpf_summary(stats),
        "",
        "ALTERAÇÕES POR CAMPO",
        "----------------------------------------------------------",
    ]
    if summary["field_counts"]:
        for field_name, count in summary["field_counts"].items():
            lines.append(f"{field_name}: {count}")
    else:
        lines.append("Nenhuma alteração de campo proposta.")

    lines.extend(
        [
            "",
            "DETALHES POR CONTATO",
            "==========================================================",
        ]
    )
    if not plans:
        lines.append("Nenhuma alteração proposta.")

    for index, plan in enumerate(
        sorted(plans, key=lambda item: item.contact_uuid), start=1
    ):
        label = plan.contact_name or "Contato sem nome"
        lines.extend(
            [
                "",
                f"[{index}] {label}",
                f"UUID: {plan.contact_uuid}",
                f"URN principal: {plan.contact_urn or '<não disponível>'}",
                f"Canal principal: {plan.contact_channel or '<não identificado>'}",
            ]
        )
        if plan.fields:
            lines.append("Campos:")
            for field_name in sorted(plan.fields):
                previous = _display_value(plan.previous.get(field_name))
                proposed = _display_value(plan.fields.get(field_name))
                lines.append(f"  - {field_name}: {previous}  ->  {proposed}")
                evidence = plan.evidence.get(field_name) or {}
                evidence_parts = []
                if evidence.get("message_id"):
                    evidence_parts.append(f"mensagem={evidence['message_id']}")
                if evidence.get("created_on"):
                    evidence_parts.append(f"data={evidence['created_on']}")
                if evidence.get("reason"):
                    evidence_parts.append(f"motivo={evidence['reason']}")
                if evidence_parts:
                    lines.append("    Evidência: " + " | ".join(evidence_parts))
        if plan.group_actions:
            lines.append("Grupos:")
            for action in plan.group_actions:
                verb = "ADICIONAR" if action.action == "add" else "REMOVER"
                group_label = action.group_name or action.group
                lines.append(
                    f"  - {verb} em {group_label} "
                    f"(alvo={action.group}) | motivo={action.reason}"
                )
        if plan.purchase_confirmed:
            lines.append(
                "Pedido FarmaLíder: CONFIRMADO"
                + (
                    f" | {plan.purchase_confirmation}"
                    if plan.purchase_confirmation
                    else ""
                )
            )

    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8-sig")
    return json_path, txt_path, digest


def verify_review_snapshot(
    plans: list[ContactUpdatePlan],
    *,
    full: bool,
    json_path: Path,
    quarter: int | None = None,
) -> str:
    """Impede aplicar um plano diferente daquele que foi exibido ao operador."""
    path = Path(json_path)
    if not path.exists():
        raise PlanAuditError(
            f"Snapshot de revisão não encontrado: {path}. Execute o dry-run e revise o relatório novamente."
        )
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PlanAuditError("Snapshot de revisão inválido ou corrompido.") from exc

    if payload.get("schema_version") != REVIEW_SCHEMA_VERSION:
        raise PlanAuditError("Versão do snapshot de revisão incompatível.")
    expected_mode = "full" if full else "incremental"
    if payload.get("mode") != expected_mode:
        raise PlanAuditError("O snapshot revisado pertence a outro modo de execução.")
    partition = payload.get("partition")
    if quarter is None:
        if isinstance(partition, dict) and partition.get("kind") == "full_25":
            raise PlanAuditError(
                "O snapshot revisado pertence a uma carga completa 25%."
            )
    else:
        if not isinstance(partition, dict) or partition.get("kind") != "full_25":
            raise PlanAuditError(
                "O snapshot revisado não pertence à carga completa 25%."
            )
        if int(partition.get("part") or 0) != quarter:
            raise PlanAuditError(
                f"O snapshot revisado pertence à Parte {partition.get('part')}, não à Parte {quarter}."
            )

    reviewed_digest = str(payload.get("plan_sha256") or "").strip().lower()
    current_digest = _plan_digest(plans)
    if not reviewed_digest or reviewed_digest != current_digest:
        raise PlanAuditError(
            "O plano mudou depois da revisão humana. Nenhuma gravação foi iniciada. "
            "Execute novamente o dry-run, revise o novo relatório e confirme outra vez."
        )
    logging.info("Plano atual confere com o snapshot revisado: %s", current_digest)
    return current_digest


def load_review_snapshot(
    *,
    full: bool,
    json_path: Path,
    quarter: int | None = None,
) -> tuple[list[ContactUpdatePlan], dict[str, Any], str]:
    """Carrega exatamente o plano revisado e valida a integridade do snapshot."""
    path = Path(json_path)
    if not path.exists():
        raise PlanAuditError(
            f"Snapshot de revisão não encontrado: {path}. Execute o dry-run e revise o relatório novamente."
        )
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PlanAuditError("Snapshot de revisão inválido ou corrompido.") from exc

    if payload.get("schema_version") != REVIEW_SCHEMA_VERSION:
        raise PlanAuditError(
            "Versão do snapshot de revisão incompatível. Execute novamente a carga completa para gerar um snapshot novo."
        )
    expected_mode = "full" if full else "incremental"
    if payload.get("mode") != expected_mode:
        raise PlanAuditError("O snapshot revisado pertence a outro modo de execução.")
    partition = payload.get("partition")
    if quarter is None:
        if isinstance(partition, dict) and partition.get("kind") == "full_25":
            raise PlanAuditError(
                "O snapshot revisado pertence a uma carga completa 25%."
            )
    else:
        if not isinstance(partition, dict) or partition.get("kind") != "full_25":
            raise PlanAuditError(
                "O snapshot revisado não pertence à carga completa 25%."
            )
        if int(partition.get("part") or 0) != quarter:
            raise PlanAuditError(
                f"O snapshot revisado pertence à Parte {partition.get('part')}, não à Parte {quarter}."
            )

    raw_plans = payload.get("plans")
    if not isinstance(raw_plans, list):
        raise PlanAuditError("Snapshot de revisão sem lista de planos válida.")

    plans: list[ContactUpdatePlan] = []
    for raw in raw_plans:
        if not isinstance(raw, dict):
            raise PlanAuditError("Snapshot de revisão contém plano inválido.")
        display = raw.get("display")
        if not isinstance(display, dict):
            display = {}
        raw_actions = raw.get("group_actions") or []
        if not isinstance(raw_actions, list):
            raise PlanAuditError("Snapshot de revisão contém ações de grupo inválidas.")
        actions: list[GroupAction] = []
        for item in raw_actions:
            if not isinstance(item, dict):
                raise PlanAuditError(
                    "Snapshot de revisão contém ação de grupo inválida."
                )
            actions.append(
                GroupAction(
                    action=str(item.get("action") or ""),
                    group=str(item.get("group") or ""),
                    reason=str(item.get("reason") or ""),
                    group_name=str(item.get("group_name") or ""),
                )
            )
        plan = ContactUpdatePlan(
            contact_uuid=str(raw.get("contact_uuid") or "").strip(),
            contact_name=str(display.get("contact_name") or "").strip(),
            contact_urn=str(display.get("contact_urn") or "").strip(),
            contact_channel=str(display.get("contact_channel") or "").strip(),
            fields=dict(raw.get("changes") or {}),
            previous=dict(raw.get("previous") or {}),
            guards=dict(raw.get("guards") or {}),
            evidence=dict(raw.get("evidence") or {}),
            group_actions=actions,
            order_validated=bool(raw.get("order_validated")),
            purchase_confirmed=bool(raw.get("purchase_confirmed")),
            purchase_confirmation=str(raw.get("purchase_confirmation") or ""),
            order_has_animal_products=bool(raw.get("order_has_animal_products")),
        )
        plans.append(plan)

    reviewed_digest = str(payload.get("plan_sha256") or "").strip().lower()
    current_digest = _plan_digest(plans)
    if not reviewed_digest or reviewed_digest != current_digest:
        raise PlanAuditError(
            "O snapshot de revisão foi alterado ou está corrompido. Nenhuma gravação foi iniciada."
        )

    captured_at = str(payload.get("captured_at_utc") or "").strip()
    if not captured_at or _parse_datetime(captured_at) is None:
        raise PlanAuditError("Snapshot de revisão sem corte temporal válido.")

    logging.info(
        "Snapshot revisado carregado sem recalcular o histórico: %s contato(s) | sha256=%s",
        len(plans),
        current_digest,
    )
    return plans, payload, current_digest


def _same_guard_value(current: Any, expected: Any) -> bool:
    if isinstance(current, (dict, list)) or isinstance(expected, (dict, list)):
        try:
            left = json.dumps(
                current,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                default=str,
            )
            right = json.dumps(
                expected,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                default=str,
            )
            return left == right
        except TypeError:
            pass
    return _semantic_text(current).casefold() == _semantic_text(expected).casefold()


def _prepare_frozen_plan_for_apply(
    plan: ContactUpdatePlan,
    current_contact: dict[str, Any],
    *,
    opt_in_group_name: str,
    opt_in_group_uuid: str,
    opt_out_group_name: str,
    opt_out_group_uuid: str,
) -> tuple[ContactUpdatePlan | None, str]:
    """
    Faz controle otimista por contato sem recalcular mensagens/histórico.

    Retorna um plano somente com operações ainda pendentes. Se algum valor
    relevante mudou para algo diferente do anterior e do revisado, o contato
    inteiro é marcado como conflito para não sobrescrever informação nova.
    """
    current_fields = get_fields(current_contact)
    pending_fields: dict[str, Any] = {}
    conflicts: list[str] = []

    if _recompra_products_need_unscheduling(plan):
        campaign_name = "Recompra FarmaLíder"
        reviewed_removals = {
            action.group
            for action in plan.group_actions
            if action.action == "remove" and action.group_name == campaign_name
        }
        if _contact_in_group(
            current_contact, group_name=campaign_name, group_uuid=""
        ) and (
            not reviewed_removals
            or any(
                isinstance(group, dict)
                and str(group.get("name") or "").strip().casefold()
                == campaign_name.casefold()
                and group.get("uuid") not in reviewed_removals
                for group in current_contact.get("groups", []) or []
            )
        ):
            return None, (
                "grupo Recompra mudou após a revisão; revise sua remoção antes "
                "de restaurar produtos pet"
            )

    for field_name, proposed in plan.fields.items():
        current = current_fields.get(field_name)
        previous = plan.previous.get(field_name)
        if _same_value(field_name, current, proposed):
            # Outra automação já chegou ao mesmo resultado. Não regrava.
            continue
        if _same_value(field_name, current, previous):
            pending_fields[field_name] = proposed
            continue
        conflicts.append(
            f"{field_name} mudou de {_display_value(previous)} para {_display_value(current)}"
        )

    for guard_name, expected in plan.guards.items():
        if guard_name in {
            "__pending_purchase_check",
            "__pending_purchase_check_resolved",
        }:
            normalize_purchase_checks({plan.contact_uuid: expected})
            continue
        if guard_name == "__invalid_cpf_identity":
            if weni_identity_guard(current_contact) != expected:
                conflicts.append(
                    "nome, e-mail ou telefone usados para identificar o cliente mudaram após a revisão"
                )
            continue
        if guard_name == "__native_urns":
            current_native = sorted(native_contact_urns(current_contact))
            expected_native = sorted(
                str(item) for item in (expected or []) if str(item).strip()
            )
            if current_native != expected_native:
                conflicts.append("URNs nativas da Weni mudaram após a revisão")
            continue

        # Quando o próprio plano altera o campo guardado, o teste de previous
        # acima já faz a validação correta (anterior ou proposto).
        if guard_name in plan.fields:
            continue
        current = current_fields.get(guard_name)
        if not _same_guard_value(current, expected):
            conflicts.append(
                f"fonte {guard_name} mudou de {_display_value(expected)} para {_display_value(current)}"
            )

    if conflicts:
        return None, "; ".join(conflicts[:8])

    pending_actions: list[GroupAction] = []
    for action in plan.group_actions:
        group_name = action.group_name or action.group
        group_uuid = action.group
        if group_name.strip().casefold() == opt_in_group_name.strip().casefold():
            group_uuid = opt_in_group_uuid or action.group
        elif group_name.strip().casefold() == opt_out_group_name.strip().casefold():
            group_uuid = opt_out_group_uuid or action.group
        is_member = _contact_in_group(
            current_contact,
            group_name=group_name,
            group_uuid=group_uuid,
        )
        if action.action == "add" and is_member:
            continue
        if action.action == "remove" and not is_member:
            continue
        pending_actions.append(
            GroupAction(
                action=action.action,
                group=group_uuid,
                reason=action.reason,
                group_name=group_name,
            )
        )

    # A field already set to the approved value leaves pending_fields, but its
    # current value still participates in the audit of remaining writes. Keep
    # the frozen snapshot untouched and use a new set of effective guards.
    effective_guards = {
        **plan.guards,
        **{
            name: current_fields.get(name)
            for name in plan.fields
            if name not in pending_fields
        },
    }
    prepared = ContactUpdatePlan(
        contact_uuid=plan.contact_uuid,
        contact_name=plan.contact_name,
        contact_urn=plan.contact_urn,
        contact_channel=plan.contact_channel,
        fields=pending_fields,
        previous=plan.previous,
        guards=effective_guards,
        evidence=plan.evidence,
        group_actions=pending_actions,
        order_validated=plan.order_validated,
        order_payment_code=plan.order_payment_code,
        purchase_confirmed=plan.purchase_confirmed,
        purchase_confirmation=plan.purchase_confirmation,
        order_has_animal_products=plan.order_has_animal_products,
    )
    return prepared, ""


def _build_context_for_contact(
    client: WeniClient,
    *,
    contact_uuid: str,
    candidate_messages: list[dict[str, Any]],
    started_at: datetime,
    full: bool,
) -> list[dict[str, Any]]:
    if not candidate_messages:
        return []
    if full:
        return client.list_messages(
            contact=contact_uuid,
            before=started_at,
        )

    timestamps = [
        dt
        for dt in (
            _parse_datetime(message_created_on(item)) for item in candidate_messages
        )
        if dt is not None
    ]
    earliest = min(timestamps) if timestamps else started_at
    context_start = earliest - timedelta(hours=WENI_UPDATE_CONTEXT_LOOKBACK_HOURS)
    return client.list_messages(
        after=context_start,
        before=started_at,
        contact=contact_uuid,
    )


def _resolve_group(client: WeniClient, group_name: str) -> tuple[bool, str]:
    groups = client.list_groups(group_name)
    for group in groups:
        name = str(group.get("name") or "").strip()
        if name.casefold() == group_name.casefold():
            return True, str(group.get("uuid") or "").strip()
    return False, ""


def _resolve_required_groups(
    client: WeniClient,
    group_names: tuple[str, ...],
) -> dict[str, tuple[bool, str]]:
    """Resolve vários grupos com uma única listagem da API."""
    groups = client.list_groups()
    by_name = {
        str(group.get("name") or "").strip().casefold(): str(
            group.get("uuid") or ""
        ).strip()
        for group in groups
        if isinstance(group, dict) and str(group.get("name") or "").strip()
    }
    return {
        name: (name.casefold() in by_name, by_name.get(name.casefold(), ""))
        for name in group_names
    }


def _available_weni_fields(client: WeniClient) -> set[str]:
    fields = client.list_fields()
    wrong_types = []
    for item in fields:
        if not isinstance(item, dict):
            continue
        key = str(item.get("key") or "").strip()
        if key not in {"pedido_quantidade_itens", "pedido_forma_pagamento"}:
            continue
        # Older API responses/test doubles may omit type metadata. When
        # declared, number/date cannot preserve CSV or the Cancelado sentinel.
        value_type = item.get("value_type") or item.get("type")
        if value_type and str(value_type).strip().casefold() != "text":
            wrong_types.append(key)
    if wrong_types:
        raise PlanAuditError(
            "Campos de pedido precisam ser Texto na Weni: "
            + ", ".join(sorted(set(wrong_types)))
            + ". Quantidades usam listas e pagamento pode conter Cancelado. "
            "Corrija os tipos antes de executar; nenhuma atualização foi iniciada."
        )
    return {
        str(item.get("key") or "").strip()
        for item in fields
        if isinstance(item, dict) and str(item.get("key") or "").strip()
    }


def _missing_multichannel_fields(
    client: WeniClient, available: set[str] | None = None
) -> list[str]:
    available_fields = (
        available if available is not None else _available_weni_fields(client)
    )
    return sorted(REQUIRED_MULTICHANNEL_FIELDS - available_fields)


def _missing_google_ads_fields(available: set[str]) -> list[str]:
    return sorted(GOOGLE_ADS_WRITE_FIELDS - available)


def _missing_order_fields(available: set[str]) -> list[str]:
    return sorted(REQUIRED_ORDER_FIELDS - available)


def _ensure_order_fields(available: set[str]) -> None:
    missing = _missing_order_fields(available)
    if not missing:
        return
    raise PlanAuditError(
        "Campos de pedido obrigatórios não existem na Weni: "
        + ", ".join(missing)
        + ". Crie todos esses campos antes de executar o NexoFlow Weni. "
        "Nenhum snapshot parcial de pedido será gravado."
    )


def _ensure_multichannel_fields(
    client: WeniClient, available: set[str] | None = None
) -> None:
    missing = _missing_multichannel_fields(client, available)
    if not missing:
        return
    joined = ", ".join(missing)
    raise PlanAuditError(
        "Campos multicanal obrigatórios não existem na Weni: "
        f"{joined}. Crie esses campos como Texto na Weni antes de continuar. "
        "Nenhuma alteração foi gravada."
    )


def _resolve_opt_out_group(client: WeniClient) -> tuple[bool, str]:
    return _resolve_group(client, WENI_UPDATE_OPT_OUT_GROUP)


def _resolve_opt_in_group(client: WeniClient) -> tuple[bool, str]:
    return _resolve_group(client, WENI_UPDATE_OPT_IN_GROUP)


def _open_farmalider() -> FarmaliderClient | None:
    enabled = bool(getattr(secrets, "FARMALIDER_API_ENABLED", True))
    token = str(getattr(secrets, "FARMALIDER_API_TOKEN", "")).strip()
    placeholder = "COLE_SEU_TOKEN_FARMALIDER_AQUI"
    if not enabled:
        logging.warning(
            "FarmaLíder API desabilitada: campos pedido_* não serão atualizados; pedido_id/ultimo_pedido só servem como localizadores quando a API estiver disponível."
        )
        return None
    if not token or token == placeholder:
        logging.warning(
            "Token FarmaLíder ausente: campos pedido_* não serão atualizados; pedido_id/ultimo_pedido não substituem os dados oficiais da API."
        )
        return None
    use_hml = bool(getattr(secrets, "FARMALIDER_USE_HML", False))
    return FarmaliderClient(token, use_hml=use_hml)


def check_update_connections() -> dict[str, Any]:
    token = str(getattr(secrets, "WENI_API_TOKEN", "")).strip()
    if not token or token == "COLE_SEU_TOKEN_WENI_AQUI":
        raise RuntimeError("Token da Weni não configurado em secrets_config.py.")

    result: dict[str, Any] = {}
    weni = WeniClient(token)
    farm: FarmaliderClient | None = None
    try:
        result["weni"] = weni.test_update_access(WENI_UPDATE_OPT_OUT_GROUP)
        opt_in_available, _ = _resolve_opt_in_group(weni)
        available_fields = _available_weni_fields(weni)
        result["weni"]["opt_in_group_found"] = opt_in_available
        result["weni"]["missing_multichannel_fields"] = _missing_multichannel_fields(
            weni, available_fields
        )
        result["weni"]["pedido_status_field_found"] = (
            "pedido_status" in available_fields
        )
        result["weni"]["missing_order_fields"] = _missing_order_fields(available_fields)
        result["weni"]["missing_google_ads_fields"] = _missing_google_ads_fields(
            available_fields
        )
        farm = _open_farmalider()
        if farm is not None:
            result["farmalider"] = farm.test_connection()
        else:
            result["farmalider"] = {"ok": False, "optional": True}
        return result
    finally:
        _close_audit_handles()
        weni.close()
        if farm is not None:
            farm.close()


def _read_retry_contact(client, uuid: str, stats: UpdateStats):
    try:
        return client.get_contact(uuid=uuid)
    except Exception as exc:
        stats.contacts_failed += 1
        stats.pending_retry_uuids.append(uuid)
        logging.warning(
            "Weni: reconsulta de contato pendente (%s); demais contatos continuarão.",
            type(exc).__name__,
        )
        return None


def run_update(
    *,
    apply: bool,
    full: bool = False,
    review_out: Path | None = None,
    require_review: Path | None = None,
    quarter: int | None = None,
    deduplicated_uuids: set[str] | None = None,
    dedup_contacts: list[dict[str, Any]] | None = None,
) -> UpdateStats:
    token = str(getattr(secrets, "WENI_API_TOKEN", "")).strip()
    if not token or token == "COLE_SEU_TOKEN_WENI_AQUI":
        raise RuntimeError("Token da Weni não configurado em secrets_config.py.")
    if quarter is not None:
        if not full:
            raise RuntimeError("A carga 25% Weni exige --full.")
        if quarter not in {1, 2, 3, 4}:
            raise RuntimeError("A parte da carga 25% Weni deve ser 1, 2, 3 ou 4.")

    frozen_full_apply = bool(apply and full and require_review is not None)
    window = None if frozen_full_apply else resolve_window(full=full)
    stats = UpdateStats()
    weni = WeniClient(token)
    farm = None if frozen_full_apply else _open_farmalider()
    captured_at = "" if window is None else iso_utc(window.started_at)

    try:
        available_weni_fields = _available_weni_fields(weni)
        _ensure_multichannel_fields(weni, available_weni_fields)
        _ensure_order_fields(available_weni_fields)
        missing_google_ads = _missing_google_ads_fields(available_weni_fields)
        if missing_google_ads:
            logging.warning(
                "Campos de atribuição Google Ads ausentes na Weni: %s. "
                "A detecção continuará, mas esses metadados não serão gravados até os campos existirem.",
                ", ".join(missing_google_ads),
            )
        resolved_groups = _resolve_required_groups(
            weni,
            (WENI_UPDATE_OPT_OUT_GROUP, WENI_UPDATE_OPT_IN_GROUP),
        )
        opt_out_available, opt_out_uuid = resolved_groups[WENI_UPDATE_OPT_OUT_GROUP]
        opt_in_available, opt_in_uuid = resolved_groups[WENI_UPDATE_OPT_IN_GROUP]
        logging.info(
            "Weni: grupos operacionais resolvidos em uma única listagem da API."
        )
        if not opt_in_available:
            logging.warning(
                "Grupo '%s' não foi localizado. Planos de Opt-In serão bloqueados pela auditoria.",
                WENI_UPDATE_OPT_IN_GROUP,
            )
        if not opt_out_available:
            logging.warning(
                "Grupo '%s' não foi localizado. Planos de Opt-Out serão bloqueados pela auditoria.",
                WENI_UPDATE_OPT_OUT_GROUP,
            )

        state = _load_state()
        pending_purchase_checks = normalize_purchase_checks(
            state.get("pending_purchase_checks")
        )
        open_order_uuids = {
            str(item).strip()
            for item in (state.get("open_order_uuids") or [])
            if str(item).strip()
        }

        if frozen_full_apply:
            if require_review is None:
                raise PlanAuditError("A aplicação exige um snapshot revisado.")
            plans, review_payload, digest = load_review_snapshot(
                full=True,
                json_path=Path(require_review),
                quarter=quarter,
            )
            audit_plans(
                plans,
                opt_out_group_available=opt_out_available,
                opt_in_group_available=opt_in_available,
            )

            # A rotina de deduplicação já confirmou a exclusão e a limpeza
            # destes UUIDs. Não classificá-los como conflito nem recriá-los.
            plans = [
                p for p in plans if p.contact_uuid not in (deduplicated_uuids or set())
            ]
            summary = review_payload.get("summary")
            if not isinstance(summary, dict):
                summary = {}
            stats.messages_incoming = int(summary.get("messages_incoming") or 0)
            stats.contacts_considered = int(
                summary.get("contacts_considered") or len(plans)
            )
            stats.plans_with_changes = len(plans)
            stats.field_changes = sum(len(plan.fields) for plan in plans)
            stats.group_actions = sum(len(plan.group_actions) for plan in plans)
            stats.purchase_confirmed = sum(
                1 for plan in plans if plan.purchase_confirmed
            )
            stats.orders_validated = int(
                summary.get("orders_validated")
                or sum(1 for plan in plans if plan.order_validated)
            )
            stats.orders_cancelled_payment_zero = int(
                summary.get("orders_cancelled_payment_zero") or 0
            )
            stats.orders_api_failed = int(summary.get("orders_api_failed") or 0)
            stats.orders_pending_payment = int(
                summary.get("orders_pending_payment") or 0
            )
            stats.invalid_cpf_checked = int(summary.get("invalid_cpf_checked") or 0)
            stats.invalid_cpf_orders_found = int(
                summary.get("invalid_cpf_orders_found") or 0
            )
            stats.invalid_cpf_orders_validated = int(
                summary.get("invalid_cpf_orders_validated") or 0
            )
            stats.invalid_cpf_outcomes = dict(summary.get("invalid_cpf_outcomes") or {})
            unmapped = summary.get("payment_unmapped_codes")
            if isinstance(unmapped, dict):
                stats.payment_unmapped_codes = {
                    code: count
                    for code, count in unmapped.items()
                    if normalize_farmalider_payment_code(code) == code
                    and not farmalider_payment_method_name(code)
                    and isinstance(count, int)
                    and not isinstance(count, bool)
                    and count > 0
                }
            captured_at = str(review_payload.get("captured_at_utc") or "").strip()
            reviewed_partition = review_payload.get("partition")
            if not isinstance(reviewed_partition, dict):
                reviewed_partition = {}
            quarter_campaign_id = str(reviewed_partition.get("campaign_id") or "")
            if quarter is not None:
                logging.info(
                    "APPLY Weni 25%% Parte %s/4 | campanha=%s | contatos revisados=%s.",
                    quarter,
                    quarter_campaign_id,
                    len(plans),
                )

            logging.info(
                "APPLY de carga completa usará o snapshot congelado; o histórico não será recalculado. "
                "Novas mensagens/contatos ficam para a próxima incremental."
            )

            conflicted_uuids: list[str] = list(
                review_payload.get("pending_retry_uuids") or []
            )
            stats.contacts_failed = len(conflicted_uuids)
            with TaskProgress(
                "Aplicação do snapshot",
                len(plans),
                detail=lambda: (
                    f"aplicados={stats.contacts_updated}; conflitos={stats.contacts_conflicted}; já corretos={stats.contacts_already_satisfied}; falhas={stats.contacts_failed}"
                ),
            ) as progress:
                for index, plan in progress.track(enumerate(plans, start=1)):
                    # Em atendimento 24h, relê somente este contato imediatamente
                    # antes da escrita. Mudanças em outros contatos não cancelam a carga.
                    try:
                        current_contact = weni.get_contact(uuid=plan.contact_uuid)
                    except Exception as exc:
                        stats.contacts_failed += 1
                        conflicted_uuids.append(plan.contact_uuid)
                        _append_audit(
                            _audit_plan_event(
                                plan,
                                mode="apply-frozen",
                                status="failed",
                                error=type(exc).__name__,
                            )
                        )
                        logging.warning(
                            "Weni: leitura de um contato pendente (%s); carga continuará.",
                            type(exc).__name__,
                        )
                        continue
                    stats.contacts_modified += 1
                    if current_contact is None:
                        stats.contacts_conflicted += 1
                        conflicted_uuids.append(plan.contact_uuid)
                        reason = "contato não localizado no momento do apply"
                        _append_audit(
                            _audit_plan_event(
                                plan,
                                mode="apply-frozen",
                                status="conflict_skipped",
                                error=reason,
                            )
                        )
                        logging.warning(
                            "Conflito %s/%s em %s: %s",
                            index,
                            len(plans),
                            plan.contact_uuid,
                            reason,
                        )
                        continue

                    prepared, conflict = _prepare_frozen_plan_for_apply(
                        plan,
                        current_contact,
                        opt_in_group_name=WENI_UPDATE_OPT_IN_GROUP,
                        opt_in_group_uuid=opt_in_uuid,
                        opt_out_group_name=WENI_UPDATE_OPT_OUT_GROUP,
                        opt_out_group_uuid=opt_out_uuid,
                    )
                    if prepared is None:
                        stats.contacts_conflicted += 1
                        conflicted_uuids.append(plan.contact_uuid)
                        _append_audit(
                            _audit_plan_event(
                                plan,
                                mode="apply-frozen",
                                status="conflict_skipped",
                                error=conflict,
                            )
                        )
                        logging.warning(
                            "Conflito %s/%s em %s: %s",
                            index,
                            len(plans),
                            plan.contact_uuid,
                            conflict,
                        )
                        continue

                    if not prepared.has_changes:
                        _track_open_order(
                            open_order_uuids,
                            plan.contact_uuid,
                            plan.fields.get(
                                "pedido_status",
                                get_fields(current_contact).get("pedido_status"),
                            ),
                        )
                        stats.contacts_already_satisfied += 1
                        _append_audit(
                            _audit_plan_event(
                                plan,
                                mode="apply-frozen",
                                status="already_satisfied",
                            )
                        )
                        continue

                    try:
                        _execute_contact_plan(weni, prepared)
                        _track_open_order(
                            open_order_uuids,
                            prepared.contact_uuid,
                            prepared.fields.get(
                                "pedido_status",
                                get_fields(current_contact).get("pedido_status"),
                            ),
                        )
                        stats.contacts_updated += 1
                        _append_audit(
                            _audit_plan_event(
                                prepared,
                                mode="apply-frozen",
                                status="applied",
                            )
                        )
                    except Exception as exc:
                        stats.contacts_failed += 1
                        _append_audit(
                            _audit_plan_event(
                                prepared,
                                mode="apply-frozen",
                                status="failed",
                                error=exc.__class__.__name__,
                            )
                        )
                        conflicted_uuids.append(prepared.contact_uuid)
                        logging.warning(
                            "Weni: contato pendente após falha de gravação (%s); próxima incremental fará nova conferência.",
                            type(exc).__name__,
                        )
                        continue

            if quarter is None:
                state_payload = {
                    "last_success_utc": captured_at,
                    "last_mode": "full-frozen-snapshot",
                    "review_sha256": digest,
                    "contacts_updated": stats.contacts_updated,
                    "contacts_conflicted": stats.contacts_conflicted,
                    "pending_conflict_uuids": sorted(set(conflicted_uuids)),
                    "open_order_uuids": sorted(open_order_uuids),
                    "contacts_already_satisfied": stats.contacts_already_satisfied,
                    "field_changes": stats.field_changes,
                    "group_actions": stats.group_actions,
                }
            else:
                reviewed_uuids = {plan.contact_uuid for plan in plans}
                previous_pending = {
                    str(item).strip()
                    for item in (state.get("pending_conflict_uuids") or [])
                    if str(item).strip()
                }
                pending_after = (previous_pending - reviewed_uuids) | set(
                    conflicted_uuids
                )
                component_complete, campaign_complete = mark_full_load_quarter_complete(
                    component="weni",
                    part=quarter,
                    campaign_id=quarter_campaign_id,
                )
                state_payload = {
                    **state,
                    "last_mode": f"full-25-part-{quarter}",
                    "review_sha256": digest,
                    "contacts_updated": stats.contacts_updated,
                    "contacts_conflicted": stats.contacts_conflicted,
                    "pending_conflict_uuids": sorted(pending_after),
                    "open_order_uuids": sorted(open_order_uuids),
                    "contacts_already_satisfied": stats.contacts_already_satisfied,
                    "field_changes": stats.field_changes,
                    "group_actions": stats.group_actions,
                    "full_25_campaign_id": quarter_campaign_id,
                    "full_25_weni_part": quarter,
                }
                if component_complete:
                    state_payload["last_success_utc"] = captured_at
                logging.info(
                    "Carga 25%% Weni Parte %s/4 concluída. "
                    "Weni completo=%s | campanha ativa concluída=%s.",
                    quarter,
                    "sim" if component_complete else "não",
                    "sim" if campaign_complete else "não",
                )
            state_payload.update(_purchase_check_state_payload(plans, conflicted_uuids))
            _save_state(state_payload)
            logging.info(
                "Snapshot congelado concluído: aplicados=%s | conflitos ignorados=%s | já satisfeitos=%s. "
                "O checkpoint foi mantido no corte do dry-run para que mudanças posteriores sejam reprocessadas.",
                stats.contacts_updated,
                stats.contacts_conflicted,
                stats.contacts_already_satisfied,
            )
            return stats

        if window is None:
            raise PlanAuditError(
                "Janela de atualização ausente; aplicação interrompida."
            )

        quarter_context: dict[str, Any] | None = None
        selected_uuids: set[str] | None = None
        if quarter is not None:
            all_contacts = weni.list_contacts(after=None)
            quarter_context = prepare_full_load_quarter(
                all_contacts,
                part=quarter,
                component="weni",
            )
            cutoff = _parse_datetime(quarter_context["captured_at_utc"])
            if cutoff is None:
                raise RuntimeError(
                    "Manifesto da carga 25% não possui corte temporal válido."
                )
            window = UpdateWindow(started_at=cutoff, after=None, full=True)
            captured_at = iso_utc(cutoff)
            selected_uuids = set(quarter_context["selected_uuids"])
            modified_contacts = list(quarter_context["selected_contacts"])
            logging.info(
                "Carga 25%% Weni Parte %s/4: campanha=%s | "
                "base congelada=%s | contatos desta parte=%s | "
                "novos fora do corte=%s | ausentes=%s.",
                quarter,
                quarter_context["campaign_id"],
                quarter_context["total_contacts"],
                quarter_context["selected_count"],
                quarter_context["new_contacts_ignored"],
                quarter_context["missing_contacts"],
            )
        else:
            # API status can change without modifying the Weni contact.
            # Re-read contacts in every load so completed/cancelled orders are
            # also queried again; message discovery still uses its checkpoint.
            modified_contacts = weni.list_contacts(after=None)

        if dedup_contacts is not None:
            dedup_contacts.extend(
                all_contacts if quarter is not None else modified_contacts
            )

        incoming_all = weni.list_messages(
            after=window.after,
            before=window.started_at,
            folder="incoming",
        )
        if selected_uuids is None:
            incoming = incoming_all
        else:
            # Mensagens sem UUID permanecem temporariamente para resolução por
            # URN; após resolver, somente contatos desta parte são aceitos.
            incoming = [
                message
                for message in incoming_all
                if not _extract_contact_uuid(message)
                or _extract_contact_uuid(message) in selected_uuids
            ]
        stats.messages_incoming = len(incoming)
        incoming_by_contact = _group_messages_by_contact(incoming)
        full_context_by_contact: dict[str, list[dict[str, Any]]] = {}
        if window.full:
            # A listagem de mensagens continua global (mais eficiente que uma
            # requisição por contato), mas somente o quarto selecionado entra
            # no processamento pesado/FarmaLíder.
            all_context_messages = weni.list_messages(before=window.started_at)
            grouped_context = _group_messages_by_contact(all_context_messages)
            full_context_by_contact = (
                grouped_context
                if selected_uuids is None
                else {
                    uuid: messages
                    for uuid, messages in grouped_context.items()
                    if uuid in selected_uuids
                }
            )

        stats.contacts_modified = len(modified_contacts)
        contacts_by_uuid = {
            str(contact.get("uuid") or "").strip(): contact
            for contact in modified_contacts
            if str(contact.get("uuid") or "").strip()
        }

        # Conflitos isolados de uma carga completa congelada entram
        # explicitamente na próxima incremental, mesmo se a Weni não refletir
        # a mudança no modified_on do contato (por exemplo, só associação a grupo).
        pending_conflicts = state.get("pending_conflict_uuids") or []
        pending_conflict_set = (
            {str(uid).strip() for uid in pending_conflicts if str(uid).strip()}
            if isinstance(pending_conflicts, list)
            else set()
        )
        if isinstance(pending_conflicts, list) and pending_conflicts:
            unique_pending = sorted(
                {str(item).strip() for item in pending_conflicts if str(item).strip()}
            )
            logging.info(
                "Reprocessando %s conflito(s) pendente(s) da carga completa.",
                len(unique_pending),
            )
            for pending_uuid in unique_pending:
                if selected_uuids is not None and pending_uuid not in selected_uuids:
                    continue
                if pending_uuid in contacts_by_uuid:
                    continue
                pending_contact = _read_retry_contact(weni, pending_uuid, stats)
                if pending_contact is not None:
                    contacts_by_uuid[pending_uuid] = pending_contact

        # Mensagens eventualmente podem vir sem UUID no payload. Resolve cada
        # URN apenas uma vez; mensagens repetidas do mesmo canal reutilizam cache.
        urn_contact_cache: dict[str, dict[str, Any] | None] = {}
        for message in incoming:
            uuid = _extract_contact_uuid(message)
            if uuid:
                continue
            urn = _extract_message_urn(message)
            if not urn:
                continue
            if urn not in urn_contact_cache:
                urn_contact_cache[urn] = weni.get_contact(urn=urn)
            contact = urn_contact_cache[urn]
            if contact is None:
                continue
            resolved_uuid = str(contact.get("uuid") or "").strip()
            if not resolved_uuid:
                continue
            if selected_uuids is not None and resolved_uuid not in selected_uuids:
                continue
            contacts_by_uuid[resolved_uuid] = contact
            incoming_by_contact.setdefault(resolved_uuid, []).append(message)
        if urn_contact_cache:
            logging.info(
                "Weni: %s URN(s) sem UUID resolvida(s) com cache local.",
                len(urn_contact_cache),
            )

        # Pedidos ainda operacionais precisam ser rechecados mesmo quando o
        # contato não teve nova mensagem/modificação na Weni.
        if open_order_uuids:
            logging.info(
                "Revalidando status de %s pedido(s) aberto(s).",
                len(open_order_uuids),
            )
            for open_uuid in sorted(open_order_uuids):
                if selected_uuids is not None and open_uuid not in selected_uuids:
                    continue
                if open_uuid in contacts_by_uuid:
                    continue
                open_contact = _read_retry_contact(weni, open_uuid, stats)
                if open_contact is None:
                    open_order_uuids.discard(open_uuid)
                    continue
                contacts_by_uuid[open_uuid] = open_contact

        all_contact_uuids = set(contacts_by_uuid) | set(incoming_by_contact)
        if selected_uuids is not None:
            all_contact_uuids &= selected_uuids
        plans: list[ContactUpdatePlan] = []
        logging.info(
            "Preparando atualizações de %s contato(s): consulta e validação dos pedidos "
            "antes da auditoria. Nenhuma gravação foi iniciada.",
            len(all_contact_uuids),
        )

        prepared_contacts = []
        with TaskProgress(
            "Preparação da fila Weni", len(all_contact_uuids)
        ) as progress:
            for _preparation_index, contact_uuid in progress.track(
                enumerate(sorted(all_contact_uuids), start=1)
            ):
                if contact_uuid in stats.pending_retry_uuids:
                    continue
                try:
                    contact = contacts_by_uuid.get(contact_uuid)
                    if contact is None:
                        contact = weni.get_contact(uuid=contact_uuid)
                    if contact is None:
                        logging.warning(
                            "Contato %s não foi localizado; ignorando.", contact_uuid
                        )
                        continue

                    candidate_messages = incoming_by_contact.get(contact_uuid, [])
                    candidate_ids = {
                        message_id(item)
                        for item in candidate_messages
                        if message_id(item)
                    }
                    if not window.full and contact_uuid in pending_conflict_set:
                        context = weni.list_messages(
                            contact=contact_uuid, before=window.started_at
                        )
                        candidate_ids = {
                            message_id(item)
                            for item in context
                            if message_id(item) and message_direction(item) == "in"
                        }
                    elif window.full:
                        context = full_context_by_contact.get(
                            contact_uuid, candidate_messages
                        )
                    else:
                        context = _build_context_for_contact(
                            weni,
                            contact_uuid=contact_uuid,
                            candidate_messages=candidate_messages,
                            started_at=window.started_at,
                            full=False,
                        )
                        if not context:
                            context = candidate_messages

                    # Determine priority from the effective CPF, including a valid CPF
                    # inferred from messages. Preparation performs no FarmaLíder GET.
                    evidences = infer_contact_fields(
                        context,
                        candidate_message_ids=candidate_ids,
                        current_lgpd=get_fields(contact).get("opt_in_lgpd"),
                    )
                    effective_cpf = (
                        evidences["cpf"].value
                        if "cpf" in evidences
                        else get_fields(contact).get("cpf")
                    )
                    prepared_contacts.append(
                        (
                            cpf_query_priority(effective_cpf),
                            contact_uuid,
                            contact,
                            context,
                            candidate_ids,
                        )
                    )
                except Exception as exc:
                    stats.contacts_failed += 1
                    stats.pending_retry_uuids.append(contact_uuid)
                    logging.warning(
                        "Weni: preparação de um contato pendente (%s); demais contatos serão processados.",
                        type(exc).__name__,
                    )

        previous_phase = None
        invalid_batch = _InvalidCpfBatch()
        with TaskProgress(
            "Preparação Weni",
            len(prepared_contacts),
            detail=lambda: (
                f"pedidos validados={stats.orders_validated}; alterações propostas={stats.plans_with_changes}; nenhuma gravação na Weni"
            ),
        ) as progress:
            for _index, (
                priority,
                contact_uuid,
                contact,
                context,
                candidate_ids,
            ) in progress.track(
                enumerate(
                    sorted(prepared_contacts, key=lambda entry: (entry[0], entry[1])),
                    start=1,
                )
            ):
                if priority != previous_phase:
                    invalid_batch.flush()
                    logging.info(
                        "Weni: etapa de consulta FarmaLíder: %s.",
                        ("CPFs válidos", "CPFs inválidos", "contatos sem CPF")[
                            priority
                        ],
                    )
                    previous_phase = priority
                try:
                    plan = _build_contact_plan(
                        contact,
                        context_messages=context,
                        candidate_message_ids=candidate_ids,
                        farm_client=farm,
                        opt_out_group_name=WENI_UPDATE_OPT_OUT_GROUP,
                        opt_out_group_uuid=opt_out_uuid,
                        captured_at=captured_at,
                        opt_in_group_name=WENI_UPDATE_OPT_IN_GROUP,
                        opt_in_group_uuid=opt_in_uuid,
                        available_weni_fields=available_weni_fields,
                        pending_purchase_check=pending_purchase_checks.get(
                            contact_uuid
                        ),
                    )
                except Exception as exc:
                    stats.contacts_failed += 1
                    stats.pending_retry_uuids.append(contact_uuid)
                    logging.warning(
                        "Weni: preparação de um contato pendente (%s); nova tentativa na próxima incremental.",
                        type(exc).__name__,
                    )
                    continue
                except BaseException:
                    invalid_batch.flush(interrupted=True)
                    raise
                if priority == 1:
                    _record_invalid_cpf_result(stats, plan)
                    invalid_batch.record(plan)
                stats.contacts_considered += 1
                _record_validated_order(stats, plan)
                _track_open_order(
                    open_order_uuids,
                    contact_uuid,
                    plan.fields.get(
                        "pedido_status", get_fields(contact).get("pedido_status")
                    ),
                )
                if plan.purchase_confirmed:
                    stats.purchase_confirmed += 1
                elif str(get_fields(contact).get("ultimo_pedido") or "").strip():
                    stats.purchase_skipped += 1

                if plan.has_changes or any(
                    key.startswith("__pending_purchase_check") for key in plan.guards
                ):
                    plans.append(plan)
                stats.plans_with_changes = len(plans)

        invalid_batch.flush()
        if stats.invalid_cpf_checked:
            logging.info(
                "Weni CPFs inválidos | total da preparação | %s. "
                "Contagens por contato; nenhuma gravação foi iniciada.",
                _invalid_cpf_summary(stats),
            )
        stats.plans_with_changes = len(plans)
        stats.field_changes = sum(len(plan.fields) for plan in plans)
        stats.group_actions = sum(len(plan.group_actions) for plan in plans)
        logging.info(
            "Preparação concluída: %s contato(s) com pedido validado "
            "(inclusive sem alteração); pagamento não identificado por código: %s. "
            "Nenhuma gravação foi iniciada; iniciando auditoria do plano.",
            stats.orders_validated,
            dict(sorted(stats.payment_unmapped_codes.items())),
        )
        logging.info(
            "Pedidos no plano por contato: cancelados por pagamento 0=%s; "
            "pendentes de pagamento=%s; concluídos confirmados=%s. "
            "Os valores não representam pedidos únicos nem gravações realizadas.",
            stats.orders_cancelled_payment_zero,
            stats.orders_pending_payment,
            stats.purchase_confirmed,
        )

        # ETAPA OBRIGATÓRIA: nenhum POST é feito antes desta auditoria completa.
        audit_plans(
            plans,
            opt_out_group_available=opt_out_available,
            opt_in_group_available=opt_in_available,
        )
        logging.info(
            "Auditoria do plano aprovada: %s contato(s), %s campo(s), %s ação(ões) de grupo.",
            stats.plans_with_changes,
            stats.field_changes,
            stats.group_actions,
        )

        if review_out is not None:
            review_json, review_txt, digest = write_review_snapshot(
                plans,
                stats,
                full=full,
                json_path=Path(review_out),
                captured_at_utc=captured_at,
                quarter=quarter,
                quarter_campaign_id=(
                    str(quarter_context["campaign_id"])
                    if quarter_context is not None
                    else ""
                ),
                quarter_total_contacts=(
                    int(quarter_context["total_contacts"])
                    if quarter_context is not None
                    else 0
                ),
                quarter_selected_contacts=(
                    int(quarter_context["selected_count"])
                    if quarter_context is not None
                    else 0
                ),
            )
            logging.info(
                "Relatório de revisão humana gerado: %s | snapshot=%s | sha256=%s",
                review_txt,
                review_json,
                digest,
            )

        if apply and full and require_review is None:
            raise PlanAuditError(
                "Carga completa em modo APPLY exige um snapshot de revisão humana. "
                "Execute primeiro o dry-run completo e revise o relatório."
            )
        if require_review is not None:
            verify_review_snapshot(
                plans,
                full=full,
                json_path=Path(require_review),
                quarter=quarter,
            )

        mode = "apply" if apply else "dry-run"
        if not apply:
            with TaskProgress(
                "Gravação da auditoria JSONL (dry-run)",
                len(plans),
                unit="registros",
                byte_count=lambda: _AUDIT_BYTES_WRITTEN,
            ) as progress:
                for plan in progress.track(plans):
                    _append_audit(_audit_plan_event(plan, mode=mode, status="planned"))
            logging.info("DRY-RUN concluído. Nenhum contato foi alterado na Weni.")
            return stats

        conflicted_uuids: list[str] = list(stats.pending_retry_uuids)
        with TaskProgress(
            "Aplicação Weni",
            len(plans),
            detail=lambda: (
                f"aplicados={stats.contacts_updated}; conflitos={stats.contacts_conflicted}; já corretos={stats.contacts_already_satisfied}; falhas={stats.contacts_failed}"
            ),
        ) as progress:
            for plan in progress.track(plans):
                try:
                    if plan.order_validated or any(
                        action.group_name == "Recompra FarmaLíder"
                        for action in plan.group_actions
                    ):
                        current_contact = weni.get_contact(uuid=plan.contact_uuid)
                        prepared = None
                        conflict = "contato não localizado no momento do apply"
                        if current_contact is not None:
                            prepared, conflict = _prepare_frozen_plan_for_apply(
                                plan,
                                current_contact,
                                opt_in_group_name=WENI_UPDATE_OPT_IN_GROUP,
                                opt_in_group_uuid=opt_in_uuid,
                                opt_out_group_name=WENI_UPDATE_OPT_OUT_GROUP,
                                opt_out_group_uuid=opt_out_uuid,
                            )
                        if prepared is None:
                            stats.contacts_conflicted += 1
                            conflicted_uuids.append(plan.contact_uuid)
                            _append_audit(
                                _audit_plan_event(
                                    plan,
                                    mode=mode,
                                    status="conflict_skipped",
                                    error=conflict,
                                )
                            )
                            continue
                        if not prepared.has_changes:
                            stats.contacts_already_satisfied += 1
                            _append_audit(
                                _audit_plan_event(
                                    plan, mode=mode, status="already_satisfied"
                                )
                            )
                            continue
                        audit_plans(
                            [prepared],
                            opt_out_group_available=opt_out_available,
                            opt_in_group_available=opt_in_available,
                        )
                        plan = prepared
                    _execute_contact_plan(weni, plan)
                    stats.contacts_updated += 1
                    _append_audit(_audit_plan_event(plan, mode=mode, status="applied"))
                except Exception as exc:
                    stats.contacts_failed += 1
                    _append_audit(
                        _audit_plan_event(
                            plan,
                            mode=mode,
                            status="failed",
                            error=exc.__class__.__name__,
                        )
                    )
                    conflicted_uuids.append(plan.contact_uuid)
                    logging.warning(
                        "Weni: contato pendente após falha de gravação (%s); carga continuará e a próxima incremental fará nova conferência.",
                        type(exc).__name__,
                    )
                    continue

        _save_state(
            {
                "last_success_utc": captured_at,
                "last_mode": "full" if full else "incremental",
                "contacts_updated": stats.contacts_updated,
                "contacts_conflicted": stats.contacts_conflicted,
                "pending_conflict_uuids": sorted(set(conflicted_uuids)),
                "open_order_uuids": sorted(open_order_uuids),
                "field_changes": stats.field_changes,
                "group_actions": stats.group_actions,
                **_purchase_check_state_payload(plans, conflicted_uuids),
            }
        )
        return stats
    finally:
        _close_audit_handles()
        weni.close()
        if farm is not None:
            farm.close()


# NexoFlow 1.0
