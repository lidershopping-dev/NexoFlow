"""Pure checks for destructive contact deduplication; no network or file writes."""

import re
import unicodedata
from collections import Counter
from uuid import UUID

from analytics import get_fields, is_valid_cpf, normalize_cpf, normalize_value
from farmalider_integration import (
    _identity_phone,
    _positive_id,
    _ultimo_pedido_locator_id,
)
from urn_utils import native_contact_urns


def _normalized(value):
    text = unicodedata.normalize("NFKD", normalize_value(value)).casefold()
    return " ".join("".join(c for c in text if not unicodedata.combining(c)).split())


def contact_uuid(contact):
    """Require a non-nil UUID with canonical hyphens, never an omitted selector."""
    if not isinstance(contact, dict):
        return ""
    value = contact.get("uuid")
    if not isinstance(value, str) or not re.fullmatch(
        r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}", value
    ):
        return ""
    parsed = UUID(value)
    return str(parsed) if parsed.int else ""


def contact_refusals(contact, *, opt_out_lgpd_uuid="", opt_out_marketing_uuid=""):
    """Capture only refusals and blocking; never infer or transfer an Opt-In.

    Optional resolved group UUIDs recognize Opt-Out memberships whose API objects
    omit names. Returned evidence does not change the input contact.
    """
    contact = contact if isinstance(contact, dict) else {}
    fields = get_fields(contact)
    negative_values = {
        "recusado",
        "nao",
        "negado",
        "inativo",
        "recusa",
        "opt-out",
        "opt out",
        "opt_out",
        "optout",
        "false",
        "0",
        "cancelado",
        "nao autorizado",
        "nao quero receber",
        "bloqueado",
    }
    lgpd_sources = [
        "campo:" + key
        for key in ("opt_in_lgpd", "opt_in_lgpd_status")
        if _normalized(fields.get(key)) in negative_values
    ]
    marketing_sources = (
        ["campo:marketing_opt_in"]
        if _normalized(fields.get("marketing_opt_in")) in negative_values
        else []
    )
    for group in contact.get("groups") or []:
        if isinstance(group, dict):
            name, identifier = group.get("name"), group.get("uuid")
        elif isinstance(group, str):
            name, identifier = group, group
        else:
            continue
        key = re.sub(r"[^a-z0-9]", "", _normalized(name))
        if key == "optoutlgpd" or (
            opt_out_lgpd_uuid and identifier == opt_out_lgpd_uuid
        ):
            lgpd_sources.append("grupo:Opt-Out LGPD")
        if key == "optoutmarketing" or (
            opt_out_marketing_uuid and identifier == opt_out_marketing_uuid
        ):
            marketing_sources.append("grupo:Opt-Out Marketing")
    true_values = {"true", "1", "sim"}
    blocked = _normalized(contact.get("blocked")) in true_values
    stopped = _normalized(contact.get("stopped")) in true_values
    reasons = []
    if lgpd_sources:
        reasons.append("RECUSA_LGPD")
    if marketing_sources:
        reasons.append("RECUSA_MARKETING")
    if blocked:
        reasons.append("BLOQUEADO")
    if stopped:
        reasons.append("PARADO")
    return {
        "lgpd_refused": bool(lgpd_sources),
        "marketing_refused": bool(marketing_sources),
        "blocked": blocked,
        "stopped": stopped,
        "lgpd_sources": sorted(set(lgpd_sources)),
        "marketing_sources": sorted(set(marketing_sources)),
        "reasons": reasons,
    }


def native_phones(contact):
    return {
        _identity_phone(u)
        for u in native_contact_urns(contact)
        if u.startswith(("tel:", "whatsapp:")) and len(_identity_phone(u)) in (10, 11)
    } - {""}


def customer_phones(customer):
    return {
        _identity_phone(customer.get(k))
        for k in ("telefone", "celular", "telefone1", "telefone2", "fone")
        if len(_identity_phone(customer.get(k))) in (10, 11)
    } - {""}


def refs(contact):
    f = get_fields(contact)
    return {_positive_id(f.get("pedido_id")), _ultimo_pedido_locator_id(f)} - {""}


def validate_pair(
    canonical,
    duplicate,
    *,
    customer,
    related_contacts,
    orders,
    history_owner_ids=(),
    recorded_order_ids=(),
    opt_out_lgpd_uuid="",
    opt_out_marketing_uuid="",
):
    reasons = []
    if not isinstance(canonical, dict) or not canonical:
        return ["CONTATO_NAO_ATIVO"]
    if not isinstance(duplicate, dict) or not duplicate:
        return ["CONTATO_NAO_ATIVO"]
    canonical_uuid, duplicate_uuid = contact_uuid(canonical), contact_uuid(duplicate)
    if not canonical_uuid:
        reasons.append("UUID_CANONICO_INVALIDO")
    if not duplicate_uuid:
        reasons.append("UUID_DUPLICADO_INVALIDO")
    if canonical_uuid and canonical_uuid == duplicate_uuid:
        reasons.append("MESMO_UUID")
    related_contacts = list(related_contacts)
    related_uuids = [contact_uuid(c) for c in related_contacts]
    if "" in related_uuids:
        reasons.append("UUID_RELACIONADO_INVALIDO")
    counts = Counter(related_uuids)
    if canonical_uuid and counts[canonical_uuid] != 1:
        reasons.append("CANONICO_AUSENTE_OU_REPETIDO_NOS_RELACIONADOS")
    if duplicate_uuid and counts[duplicate_uuid] != 1:
        reasons.append("DUPLICADO_AUSENTE_OU_REPETIDO_NOS_RELACIONADOS")
    cf = normalize_cpf(get_fields(canonical).get("cpf"))
    df = normalize_cpf(get_fields(duplicate).get("cpf"))
    af = normalize_cpf(customer.get("cpf"))
    if not (is_valid_cpf(cf) and cf == df == af):
        reasons.append("CPF_SEM_VINCULO_VALIDO_UNICO")
    cid = _positive_id(customer.get("id"))
    if not cid:
        reasons.append("CLIENTE_API_INVALIDO")
    ps = customer_phones(customer)
    matches = {
        contact_uuid(c)
        for c in related_contacts
        if isinstance(c, dict) and ps & native_phones(c)
    }
    if not canonical_uuid or matches != {canonical_uuid}:
        reasons.append("TELEFONE_SEM_CONTATO_CANONICO_UNICO")
    if ps & native_phones(duplicate):
        reasons.append("DUPLICADO_TAMBEM_POSSUI_TELEFONE_API")
    if set(history_owner_ids) - {cid}:
        reasons.append("HISTORICO_DE_OUTRO_CLIENTE")
    for oid in refs(duplicate) | set(recorded_order_ids):
        payload = orders.get(oid)
        capa = (payload or {}).get("capa") or {}
        if (
            _positive_id(capa.get("id")) != oid
            or _positive_id(capa.get("codCliente")) != cid
        ):
            reasons.append("PEDIDO_NAO_COMPROVADO_DO_MESMO_CLIENTE")
    consent_args = {
        "opt_out_lgpd_uuid": opt_out_lgpd_uuid,
        "opt_out_marketing_uuid": opt_out_marketing_uuid,
    }
    canonical_refusals = contact_refusals(canonical, **consent_args)
    duplicate_refusals = contact_refusals(duplicate, **consent_args)
    for key, reason in (
        ("lgpd_refused", "RECUSA_LGPD_NAO_PRESERVADA"),
        ("marketing_refused", "RECUSA_MARKETING_NAO_PRESERVADA"),
        ("blocked", "BLOQUEIO_NAO_PRESERVADO"),
        ("stopped", "PARADA_NAO_PRESERVADA"),
    ):
        if duplicate_refusals[key] and not canonical_refusals[key]:
            reasons.append(reason)
    return sorted(set(reasons))
