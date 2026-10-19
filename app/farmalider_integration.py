from __future__ import annotations

import hashlib
import json
from decimal import Decimal, InvalidOperation
from typing import Any, cast

from pedido_utils import (
    business_order_datetime,
    effective_order_status_code,
    farmalider_order_status_name,
    farmalider_payment_method_display,
    is_order_cancelled,
    is_order_sale,
    serialize_pedido_products,
)


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def cpf_cadastro_status(value: Any) -> str:
    """Separate absent/placeholding data from an official API rejection.

    A populated, nonzero CPF with invalid check digits can still identify an
    official customer when the existing name/phone guard confirms that person.
    Only values that cannot be used as a CPF locator are rejected locally.
    """
    from analytics import normalize_cpf, normalize_value

    while isinstance(value, dict) and "value" in value:
        value = value["value"]
    raw = normalize_value(value).strip()
    if not raw:
        return "CPF_NAO_INFORMADO"
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, str))
        or not raw.isascii()
        or any(char not in "0123456789.- \t\r\n" for char in raw)
    ):
        return "CPF_INVALIDO_CADASTRO"
    cpf = normalize_cpf(value)
    if not cpf or len(cpf) > 11 or not cpf.strip("0"):
        return "CPF_INVALIDO_CADASTRO"
    return ""


def _positive_id(value: Any) -> str:
    """API identities are positive integers, not booleans or decimal numbers."""
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        return ""
    text = str(value).strip()
    if not text.isascii() or not text.isdigit() or int(text) <= 0:
        return ""
    return str(int(text))


def _cliente_object(payload: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(payload, dict):
        return None
    if "cliente" in payload:
        cliente = payload["cliente"]
        return cliente if isinstance(cliente, dict) else None
    return payload


def _identity_text(value: Any) -> str:
    from analytics import normalize_value

    return normalize_value(value).strip()


def _identity_name(value: Any) -> str:
    import unicodedata

    text = unicodedata.normalize("NFKD", _identity_text(value)).casefold()
    text = "".join(char for char in text if not unicodedata.combining(char))
    return " ".join(text.split())


def _identity_email(value: Any) -> str:
    import re

    email = _identity_text(value).casefold()
    return email if re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email) else ""


def _identity_phone(value: Any) -> str:
    import re

    text = _identity_text(value)
    for prefix in ("tel:", "whatsapp:"):
        if text.startswith(prefix):
            text = text[len(prefix) :]
            break
    if not re.fullmatch(r"[+0-9()\s.-]+", text):
        return ""
    digits = re.sub(r"[^0-9]", "", text)
    if len(digits) in {12, 13} and digits.startswith("55"):
        digits = digits[2:]
    # Preserve full digits for guards; identity matching compares the local number.
    return digits if 8 <= len(digits) <= 15 and len(set(digits)) > 1 else ""


def _identity_local_phone(phone: str) -> str:
    local = (
        phone[2:] if len(phone) in {10, 11} else phone if len(phone) in {8, 9} else ""
    )
    return local if len(set(local)) > 1 else ""


def _identity_values(identity: dict[str, Any]) -> tuple[str, set[str], set[str]]:
    name = _identity_name(identity.get("nome")) or _identity_name(
        identity.get("weni_name")
    )
    email = _identity_email(identity.get("email"))
    phones = {
        _identity_phone(identity.get(key))
        for key in (
            "telefone",
            "celular",
            "contact_urn_whatsapp",
        )
    }
    phones.discard("")
    return name, {email} if email else set(), phones


def weni_customer_identity(contact: dict[str, Any]) -> dict[str, Any]:
    from analytics import get_fields, get_phone_and_urn
    from urn_utils import native_contact_urns, resolve_contact_urns

    fields = get_fields(contact)
    phone, _ = get_phone_and_urn(contact, fields)
    resolved = resolve_contact_urns(contact, fields)
    native = native_contact_urns(contact)
    native_tel = next((urn for urn in native if urn.startswith("tel:")), "")
    native_whatsapp = next((urn for urn in native if urn.startswith("whatsapp:")), "")
    native_phone = bool(native_tel or native_whatsapp)
    if native_tel:
        phone = native_tel[4:]
    elif native_whatsapp:
        phone = native_whatsapp.split(":", 1)[1]
    return {
        "nome": fields.get("nome"),
        "weni_name": contact.get("name"),
        "email": fields.get("email"),
        "telefone": phone,
        "celular": "" if native_phone else fields.get("celular"),
        "contact_urn_whatsapp": native_whatsapp
        if native_phone
        else resolved.channel_urns.get("whatsapp", ""),
    }


def weni_identity_guard(contact: dict[str, Any]) -> str:
    identity = weni_customer_identity(contact)
    name, emails, phones = _identity_values(identity)
    values = [name, sorted(emails), sorted(phones)]
    display_name = _identity_name(identity.get("weni_name"))
    if display_name and display_name != name:
        values.append(display_name)
    value = json.dumps(values, ensure_ascii=False)
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _identity_first_names(identity: dict[str, Any]) -> set[str]:
    import re

    names = set()
    for key in ("nome", "weni_name"):
        for token in re.split(r"[\s-]+", _identity_name(identity.get(key))):
            first_name = "".join(char for char in token if char.isalpha())
            if first_name:
                names.add(first_name)
                break
    return names


def _identity_matches(identity: dict[str, Any], cliente: dict[str, Any]) -> bool:
    _, _, phones = _identity_values(identity)
    _, _, api_phones = _identity_values(cliente)
    local_phones = {_identity_local_phone(phone) for phone in phones} - {""}
    api_local_phones = {_identity_local_phone(phone) for phone in api_phones} - {""}
    return bool(
        _identity_first_names(identity) & _identity_first_names(cliente)
    ) or bool(local_phones & api_local_phones)


def _cliente_candidates(payload: Any) -> list[dict[str, Any]] | None:
    """Keep each customer's order snapshot isolated from other candidates."""
    if isinstance(payload, list):
        entries = payload
    elif isinstance(payload, dict) and "clientes" in payload:
        entries = payload["clientes"]
    elif isinstance(payload, dict) and isinstance(payload.get("cliente"), list):
        entries = payload["cliente"]
    elif isinstance(payload, dict):
        entries = [payload]
    else:
        return None
    if not isinstance(entries, list) or any(
        not isinstance(entry, dict) for entry in entries
    ):
        return None
    candidates = []
    for entry in entries:
        cliente = _cliente_object(entry)
        if cliente is None:
            return None
        if "cliente" in entry:
            candidates.append(dict(entry))
        else:
            candidates.append(
                {
                    "cliente": cliente,
                    **{
                        key: entry[key]
                        for key in ("ultimoPedido", "itens")
                        if key in entry
                    },
                }
            )
    return candidates


def _cpf_customer_status(cpf: str, cliente: dict[str, Any]) -> tuple[str, str, str]:
    from analytics import is_valid_cpf, normalize_cpf

    raw_cpf = cliente.get("cpf")
    if isinstance(raw_cpf, bool) or not isinstance(raw_cpf, (int, str)):
        return "CPF_NAO_INFORMADO_API", "", ""
    api_cpf = normalize_cpf(raw_cpf, pad_to_11=is_valid_cpf(cpf))
    if not api_cpf:
        return "CPF_NAO_INFORMADO_API", "", ""
    if not (
        api_cpf == cpf
        or (isinstance(raw_cpf, int) and api_cpf.lstrip("0") == cpf.lstrip("0"))
    ):
        return "CPF_DIVERGENTE_API", "", ""
    cliente_id = _positive_id(cliente.get("id"))
    if not cliente_id:
        return "CLIENTE_ID_INVALIDO_API", "", ""
    status = "CPF_CONFIRMADO" if is_valid_cpf(cpf) else "CPF_INVALIDO_ENCONTRADO"
    return status, cliente_id, _text(cliente.get("nome"))


def select_cliente_response(
    weni_cpf: str,
    api_cliente: Any,
    *,
    weni_identity: dict[str, Any] | None = None,
) -> tuple[tuple[str, str, str], dict[str, Any] | None]:
    """Select one official customer; invalid CPFs require a matching first name or local phone."""
    from analytics import is_valid_cpf, normalize_cpf

    cpf = normalize_cpf(weni_cpf)
    if not cpf:
        return ("CPF_NAO_INFORMADO", "", ""), None
    if api_cliente is None:
        return ("CPF_NAO_ENCONTRADO", "", ""), None
    candidates = _cliente_candidates(api_cliente)
    if candidates is None:
        return ("ERRO_RESPOSTA_CLIENTE_API", "", ""), None
    if not candidates:
        return ("CPF_NAO_ENCONTRADO", "", ""), None
    checked = [
        (_cpf_customer_status(cpf, candidate["cliente"]), candidate)
        for candidate in candidates
    ]
    eligible = [
        (result, candidate)
        for result, candidate in checked
        if result[0] in {"CPF_CONFIRMADO", "CPF_INVALIDO_ENCONTRADO"}
    ]
    if not eligible:
        return checked[0][0], None
    if not is_valid_cpf(cpf):
        _, _, phones = _identity_values(weni_identity or {})
        if not _identity_first_names(weni_identity or {}) and not any(
            _identity_local_phone(phone) for phone in phones
        ):
            return ("CPF_IDENTIDADE_INSUFICIENTE", "", ""), None
        # A matching person with a broken API ID cannot be silently discarded.
        if any(
            result[0] == "CLIENTE_ID_INVALIDO_API"
            and _identity_matches(weni_identity or {}, candidate["cliente"])
            for result, candidate in checked
        ):
            return ("CLIENTE_ID_INVALIDO_API", "", ""), None
        eligible = [
            (result, candidate)
            for result, candidate in eligible
            if _identity_matches(weni_identity or {}, candidate["cliente"])
        ]
        if not eligible:
            return ("CPF_IDENTIDADE_DIVERGENTE", "", ""), None
    if len(eligible) != 1:
        return ("CPF_IDENTIDADE_AMBIGUA", "", ""), None
    return eligible[0]


def cliente_link_status(
    weni_cpf: str,
    api_cliente: Any,
    *,
    weni_identity: dict[str, Any] | None = None,
) -> tuple[str, str, str]:
    result, _ = select_cliente_response(
        weni_cpf, api_cliente, weni_identity=weni_identity
    )
    return result


def _decimal(value: Any, label: str, *, positive: bool = False) -> Decimal:
    if isinstance(value, bool) or value is None or value == "":
        raise ValueError(f"{label} ausente ou inválido.")
    try:
        number = Decimal(str(value).replace(",", "."))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValueError(f"{label} inválido.") from exc
    if not number.is_finite() or number < 0 or (positive and number <= 0):
        raise ValueError(f"{label} fora do domínio permitido.")
    return number


def _quantity_text(value: Decimal) -> str:
    return format(value.normalize(), "f")


def _api_product_identity(api_item: dict[str, Any]) -> tuple[str, str]:
    """Use descricao as the product name; codProduto remains only an optional code."""
    raw_code = api_item.get("codProduto")
    if raw_code is not None and (
        isinstance(raw_code, bool) or not isinstance(raw_code, (int, str))
    ):
        raise ValueError("codProduto da API deve ser inteiro ou texto.")

    description = api_item.get("descricao")
    if not isinstance(description, str) or not description.strip():
        raise ValueError("descricao do produto ausente ou inválida.")

    return _text(raw_code), description.strip()


def _is_freight_item(api_item: dict[str, Any]) -> bool:
    """O lançamento de frete usa código 0 e descrição ausente ou vazia."""
    code = api_item.get("codProduto")
    description = api_item.get("descricao")
    return (
        not isinstance(code, bool)
        and isinstance(code, (int, str))
        and str(code).strip() == "0"
        and (
            description is None
            or isinstance(description, str)
            and not description.strip()
        )
    )


def _pedido_from_cliente_payload(
    payload: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """Adapt GET cliente/cpf to the same capa+itens shape used by GET pedido."""
    if not isinstance(payload, dict):
        return None
    latest = payload.get("ultimoPedido")
    if not isinstance(latest, dict):
        return None
    # Pass missing/malformed items through canonical validation as well. A
    # verified header can still establish cancellation, but never a full sale.
    return {"capa": dict(latest), "itens": payload.get("itens")}


def _failure(
    order: dict[str, str],
    items: list[dict[str, str]],
    status: str,
    message: str,
) -> tuple[dict[str, str], list[dict[str, str]]]:
    order["status_confirmacao"] = status
    order["status_validacao"] = "ERRO"
    order["farmalider_erro"] = message
    return order, items


def _matching_name(
    code: str,
    old_items: list[dict[str, str]],
    quantity: Decimal,
    unit: Decimal,
    total: Decimal,
    api_items: list[dict[str, Any]],
) -> str:
    if code:
        matches = [
            item for item in old_items if _text(item.get("produto_codigo")) == code
        ]
        if len(matches) == 1:
            return _text(matches[0].get("produto_nome"))
        if matches:
            return ""
    # No positional correspondence: only one exact financial match is usable.
    signature_matches = 0
    for candidate in api_items:
        if not isinstance(candidate, dict):
            continue
        try:
            if (
                _decimal(candidate.get("quantidade"), "quantidade", positive=True)
                == quantity
                and _decimal(candidate.get("valor"), "valor unitário") == unit
                and _decimal(candidate.get("total"), "total") == total
            ):
                signature_matches += 1
        except ValueError:
            continue
    if signature_matches != 1:
        return ""
    matches = []
    for item in old_items:
        if code and _text(item.get("produto_codigo")):
            continue
        try:
            same = (
                _decimal(item.get("quantidade"), "quantidade", positive=True)
                == quantity
                and _decimal(item.get("valor_unitario"), "valor_unitario") == unit
                and _decimal(item.get("valor_total"), "valor_total") == total
            )
        except ValueError:
            continue
        if same:
            matches.append(item)
    return _text(matches[0].get("produto_nome")) if len(matches) == 1 else ""


def apply_pedido_api_confirmation(
    order: dict[str, str],
    items: list[dict[str, str]],
    pedido_api: dict[str, Any] | None,
    *,
    linked_cliente_id: str,
    confirmed_at: str,
    allow_locator_without_customer: bool = False,
) -> tuple[dict[str, str], list[dict[str, str]]]:
    """Build the canonical order from the current FarmaLíder API response."""
    from analytics import _item_key

    order = dict(order)
    order.pop("_farmalider_cabecalho_verificado", None)
    order.pop("_farmalider_itens_pendentes", None)
    order.pop("_farmalider_erro_consulta_pedido", None)
    if pedido_api is None:
        return _failure(
            order, [], "PEDIDO_NAO_ENCONTRADO_API", "Pedido não encontrado na API."
        )
    if not isinstance(pedido_api, dict) or not isinstance(pedido_api.get("capa"), dict):
        return _failure(
            order, [], "ERRO_RESPOSTA_PEDIDO_API", "Resposta sem objeto capa."
        )

    capa = pedido_api["capa"]
    api_id = _positive_id(capa.get("id"))
    requested_id = _positive_id(order.get("pedido_id"))
    if not api_id or not requested_id:
        return _failure(
            order,
            [],
            "PEDIDO_ID_INVALIDO_API",
            "Identidade do pedido ausente ou inválida.",
        )
    if api_id != requested_id:
        return _failure(
            order, [], "PEDIDO_ID_DIVERGENTE_API", "Identidade do pedido divergente."
        )

    api_cliente_id = _positive_id(capa.get("codCliente"))
    linked_id = _positive_id(linked_cliente_id)
    ownership_verified = bool(linked_id)
    if not api_cliente_id:
        return _failure(
            order,
            [],
            "PEDIDO_CLIENTE_NAO_VERIFICADO_API",
            "Proprietário do pedido não verificado.",
        )
    if not linked_id:
        if not allow_locator_without_customer:
            return _failure(
                order,
                [],
                "PEDIDO_CLIENTE_NAO_VERIFICADO_API",
                "Proprietário do pedido não verificado.",
            )
        # Fallback controlado: pedido_id/ultimo_pedido pertencem ao próprio
        # contato Weni e servem apenas como localizadores. A API continua sendo
        # a fonte dos dados do pedido; o codCliente retornado fica registrado.
        linked_id = api_cliente_id
    elif api_cliente_id != linked_id:
        return _failure(
            order,
            [],
            "PEDIDO_CLIENTE_DIVERGENTE_API",
            "Proprietário do pedido divergente.",
        )

    # Negative official signals remain actionable when item/date validation
    # fails later. Copy them only after verifying both order ID and ownership;
    # status_validacao stays ERRO unless the complete order succeeds below.
    for target, source in (
        ("farmalider_status_pedido", "status"),
        ("farmalider_forma_pagamento", "formaPagamento"),
        ("farmalider_cancelamento", "cancelamento"),
    ):
        order[target] = _text(capa.get(source))
    if ownership_verified:
        order["_farmalider_cabecalho_verificado"] = "1"

    emitted = business_order_datetime(capa.get("emissao"))
    if emitted is None:
        return _failure(
            order,
            [],
            "PEDIDO_DATA_INVALIDA_API",
            "Emissão do pedido ausente ou inválida.",
        )

    api_items = pedido_api.get("itens")
    if not isinstance(api_items, list) or not api_items:
        if ownership_verified and (api_items is None or api_items == []):
            # Preserve the verified identity/date; never infer products or amounts.
            order.update(
                pedido_id=api_id,
                pedido_chave=f"FARMALIDER:{api_id}",
                farmalider_cliente_id=linked_id,
                farmalider_cod_cliente=api_cliente_id,
                data_hora=emitted.isoformat(timespec="seconds"),
                data=emitted.date().isoformat(),
                hora=emitted.strftime("%H:%M:%S"),
                farmalider_emissao=_text(capa.get("emissao")),
                farmalider_confirmado_em=confirmed_at,
            )
        current_header = dict(order, status_confirmacao="")
        if (
            ownership_verified
            and (api_items is None or api_items == [])
            and (is_order_sale(current_header) or is_order_cancelled(current_header))
        ):
            order.update(
                status_confirmacao="CANCELADO_API",
                status_validacao="CANCELADO_SEM_ITENS",
                farmalider_status_pedido_nome="Cancelado",
                farmalider_erro="Pedido sem itens: tratado como cancelado pela regra operacional.",
                ultimo_pedido_raw="",
            )
            return order, []
        return _failure(
            order, [], "ERRO_ITENS_PEDIDO_API", "Lista de itens ausente ou vazia."
        )

    api_total = Decimal("0")
    api_qty = Decimal("0")
    freight_items_total = Decimal("0")
    freight_item_count = 0
    canonical_items: list[dict[str, str]] = []
    key = f"FARMALIDER:{api_id}"
    date_time = emitted.isoformat(timespec="seconds")
    date = emitted.date().isoformat()
    hour = emitted.strftime("%H:%M:%S")

    try:
        for position, api_item in enumerate(api_items, 1):
            if not isinstance(api_item, dict):
                raise ValueError("Item da API não é um objeto.")
            if (
                "codPedido" in api_item
                and _positive_id(api_item["codPedido"]) != api_id
            ):
                return _failure(
                    order,
                    [],
                    "ITEM_PEDIDO_DIVERGENTE_API",
                    "Item pertence a outro pedido ou não possui identidade válida.",
                )

            quantity = _decimal(api_item.get("quantidade"), "quantidade", positive=True)
            unit = _decimal(api_item.get("valor"), "valor unitário")
            total = (
                _decimal(api_item.get("total"), "total do item")
                if api_item.get("total") not in (None, "")
                else quantity * unit
            )
            if _is_freight_item(api_item):
                freight_items_total += total
                freight_item_count += 1
                continue
            code, name = _api_product_identity(api_item)

            canonical_items.append(
                {
                    "item_chave": _item_key(key, position),
                    "pedido_chave": key,
                    "pedido_id": api_id,
                    "data_hora": date_time,
                    "data": date,
                    "hora": hour,
                    "contact_uuid": order.get("contact_uuid", ""),
                    "telefone": order.get("telefone", ""),
                    "cliente": order.get("cliente", ""),
                    "produto_ordem": str(position),
                    "produto_nome": name,
                    "produto_codigo": code,
                    "categoria": (api_item.get("categoria") or "").strip()
                    if isinstance(api_item.get("categoria"), str)
                    else "",
                    "quantidade": _quantity_text(quantity),
                    "valor_unitario": f"{unit:.2f}",
                    "valor_total": f"{total:.2f}",
                    "capturado_em": confirmed_at,
                }
            )
            api_total += total
            api_qty += quantity

        freight = (
            _decimal(capa["frete"], "frete")
            if capa.get("frete") not in (None, "")
            else None
        )
        if freight_item_count and (freight is None or freight_items_total != freight):
            raise ValueError(
                "Lançamento de frete sem correspondência com o frete do cabeçalho."
            )
        official_total = None
        for field in (
            "valorTotal",
            "valorTotalPedido",
            "valorTotalCompra",
            "totalPedido",
            "totalGeral",
            "total",
        ):
            if capa.get(field) not in (None, ""):
                official_total = _decimal(capa[field], "total geral")
                break
    except ValueError as exc:
        return _failure(order, [], "ERRO_ITENS_PEDIDO_API", str(exc))

    if not canonical_items:
        # Frete não é produto. Sem produtos, aplicar a mesma regra de pedido
        # vazio, após validar o vínculo, os valores e o frete do cabeçalho.
        return apply_pedido_api_confirmation(
            order,
            [],
            dict(pedido_api, itens=[]),
            linked_cliente_id=linked_cliente_id,
            confirmed_at=confirmed_at,
            allow_locator_without_customer=allow_locator_without_customer,
        )

    # Contrato Weni: pedido_valor é somente produtos. Para BI/faturamento,
    # o frete fica separado e também compõe o total cobrado do pedido.
    purchase_total = api_total
    freight_value = freight if freight is not None else Decimal("0")
    billing_total = purchase_total + freight_value
    provenance = "SOMA_ITENS_API"

    order.update(
        {
            "pedido_id": api_id,
            "pedido_chave": key,
            "data_hora": date_time,
            "data": date,
            "hora": hour,
            "farmalider_cliente_id": linked_id,
            "farmalider_cod_cliente": api_cliente_id,
            "status_validacao": "OK",
            "erro_parse": "",
            "farmalider_erro": "",
            "farmalider_confirmado_em": confirmed_at,
            "origem_pedido": order.get("origem_pedido") or "FARMALIDER_API",
            "valor_total_compra": f"{purchase_total:.2f}",
            "valor_total_faturamento": f"{billing_total:.2f}",
            "valor_total_itens": f"{api_total:.2f}",
            "valor_total_api_itens": f"{api_total:.2f}",
            "quantidade_itens_api": _quantity_text(api_qty),
            "total_api": f"{official_total:.2f}" if official_total is not None else "",
            "quantidade_itens": _quantity_text(api_qty),
            "quantidade_produtos": str(len(canonical_items)),
            "diferenca_total": "0.00",
            "valor_origem": provenance,
            "valor_total_weni": "",
            "diferenca_total_weni_api": "",
            "farmalider_frete": f"{freight:.2f}" if freight is not None else "",
        }
    )

    for target, source in (
        ("farmalider_public_id", "publicId"),
        ("farmalider_emissao", "emissao"),
        ("farmalider_status_pedido", "status"),
        ("farmalider_cod_filial", "codFilial"),
        ("farmalider_forma_pagamento", "formaPagamento"),
        ("farmalider_cupom", "cupom"),
        ("farmalider_pedido_web", "pedidoWeb"),
        ("farmalider_cancelamento", "cancelamento"),
        ("farmalider_entrega", "entrega"),
    ):
        order[target] = _text(capa.get(source))

    # The current GET replaces an older confirmation (including payment-0
    # cancellation); an order may subsequently become paid/completed.
    order["status_confirmacao"] = ""
    order["farmalider_status_pedido_nome"] = farmalider_order_status_name(
        effective_order_status_code(order)
    )
    order["farmalider_forma_pagamento_nome"] = farmalider_payment_method_display(
        capa.get("formaPagamento")
    )
    order["status_confirmacao"] = (
        "CANCELADO_API" if is_order_cancelled(order) else "CONFIRMADO_API"
    )
    # O GET substitui o snapshot legado do contato, inclusive categorias e
    # itens; metadados antigos não podem classificar a compra oficial atual.
    order["ultimo_pedido_raw"] = serialize_pedido_products(order, canonical_items)
    return order, canonical_items


def _empty_order(contact: dict[str, Any], captured_at: str) -> dict[str, str]:
    from analytics import (
        ORDER_COLUMNS,
        get_fields,
        get_phone_and_urn,
        normalize_cpf,
        normalize_order_source,
        normalize_value,
    )

    fields = get_fields(contact)
    order = {field: "" for field in ORDER_COLUMNS}
    uuid = normalize_value(contact.get("uuid")).strip()
    phone, _ = get_phone_and_urn(contact, fields)
    raw = normalize_order_source(fields.get("ultimo_pedido"))
    order.update(
        {
            "contact_uuid": uuid,
            "telefone": phone,
            "cliente": normalize_value(
                fields.get("nome") or contact.get("name")
            ).strip(),
            "cpf": normalize_value(fields.get("cpf")).strip(),
            "cpf_normalizado": normalize_cpf(fields.get("cpf")),
            "pedido_chave": hashlib.sha256(f"{uuid}|{raw}".encode()).hexdigest()[:32],
            "ultimo_pedido_raw": raw,
            "capturado_em": captured_at,
            "weni_modified_on": normalize_value(contact.get("modified_on")).strip(),
            "origem_pedido": "FARMALIDER_CLIENTE_ULTIMO_PEDIDO",
        }
    )
    return order


def _ultimo_pedido_locator_id(fields: dict[str, Any]) -> str:
    """Extract only an order ID from ultimo_pedido; never trust it as order data."""
    raw: Any = fields.get("ultimo_pedido")
    if isinstance(raw, dict) and "value" in raw:
        raw = raw.get("value")

    payload: Any = raw
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return ""
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            return ""

    if not isinstance(payload, dict):
        return ""
    return _positive_id(
        payload.get("pedido_id")
        or payload.get("pedidoId")
        or payload.get("codigo")
        or payload.get("codPedido")
        or payload.get("id")
    )


def resolve_contact_order(
    contact: dict[str, Any],
    *,
    farm_client: Any,
    captured_at: str,
    linked_cliente_id: str = "",
    cliente_response: dict[str, Any] | list[dict[str, Any]] | None = None,
    cliente_lookup_status: str = "",
    pedido_responses_by_id: dict[str, dict[str, Any] | list[dict[str, Any]] | None]
    | None = None,
) -> tuple[dict[str, str] | None, list[dict[str, str]]]:
    """
    Resolve o pedido usando a FarmaLíder como fonte de verdade.

    Ordem de descoberta:
      1. CPF -> GET cliente/cpf -> ultimoPedido + itens;
      2. campo Weni pedido_id, quando o GET por CPF não puder ser usado;
      3. ultimo_pedido, somente como último localizador de pedido.

    pedido_id e ultimo_pedido nunca substituem os dados oficiais retornados por
    GET pedido/{id}. Se a API não estiver disponível, os campos pedido_* não
    são reconstruídos a partir do snapshot legado.
    """
    from analytics import get_fields, is_valid_cpf, normalize_cpf
    from farmalider_client import (
        FarmaliderAuthError,
        FarmaliderBadRequestError,
        FarmaliderError,
        FarmaliderUnavailableError,
    )

    fields = get_fields(contact)
    cpf = normalize_cpf(fields.get("cpf"))
    invalid_cpf = bool(cpf) and not is_valid_cpf(cpf)
    identity_verified = False
    linked_id = _positive_id(linked_cliente_id)
    current_pedido_id = _positive_id(fields.get("pedido_id"))
    ultimo_pedido_id = _ultimo_pedido_locator_id(fields)

    if farm_client is None:
        return None, []

    if cpf_cadastro_status(fields.get("cpf")) == "CPF_INVALIDO_CADASTRO":
        return _failure(
            _empty_order(contact, captured_at),
            [],
            "CPF_INVALIDO_CADASTRO",
            "CPF de cadastro inválido (zero ou formato não utilizável); nenhuma consulta enviada à API.",
        )

    def fetch_pedido(order_id: str) -> dict[str, Any] | list[dict[str, Any]] | None:
        if not order_id:
            return None
        if pedido_responses_by_id is not None and order_id in pedido_responses_by_id:
            return pedido_responses_by_id[order_id]
        response = farm_client.get_pedido(order_id)
        if pedido_responses_by_id is not None:
            pedido_responses_by_id[order_id] = response
        return response

    def resolve_locator(
        order_id: str,
        source: str,
        *,
        expected_cliente_id: str = "",
    ) -> tuple[dict[str, str] | None, list[dict[str, str]]]:
        if not order_id:
            return None, []
        try:
            response = fetch_pedido(order_id)
        except (FarmaliderAuthError, FarmaliderUnavailableError):
            raise
        except FarmaliderError:
            return None, []
        if not isinstance(response, dict):
            return None, []

        order = _empty_order(contact, captured_at)
        order["pedido_id"] = order_id
        order["farmalider_cliente_id"] = expected_cliente_id
        order["origem_pedido"] = source + (
            "_IDENTIDADE_VALIDADA" if invalid_cpf and identity_verified else ""
        )
        confirmed_order, confirmed_items = apply_pedido_api_confirmation(
            order,
            [],
            response,
            linked_cliente_id=expected_cliente_id,
            confirmed_at=captured_at,
            allow_locator_without_customer=not bool(expected_cliente_id),
        )
        if confirmed_order.get("status_validacao") == "OK" and confirmed_items:
            return confirmed_order, confirmed_items
        return confirmed_order, confirmed_items

    def resolve_weni_locators(
        *,
        expected_cliente_id: str = "",
    ) -> tuple[dict[str, str] | None, list[dict[str, str]]]:
        # pedido_id é um campo do próprio NexoFlow e tem precedência.
        # ultimo_pedido vem de outra integração e é deliberadamente o último
        # recurso para descobrir um ID que depois será validado via GET pedido.
        tried: set[str] = set()
        for order_id, source in (
            (current_pedido_id, "FARMALIDER_PEDIDO_ID_WENI"),
            (ultimo_pedido_id, "FARMALIDER_ULTIMO_PEDIDO_LOCATOR"),
        ):
            if not order_id or order_id in tried:
                continue
            tried.add(order_id)
            resolved_order, resolved_items = resolve_locator(
                order_id,
                source,
                expected_cliente_id=expected_cliente_id,
            )
            if (
                resolved_order is not None
                and resolved_order.get("status_validacao") == "OK"
                and resolved_items
            ):
                return resolved_order, resolved_items
        return None, []

    # Sem CPF, ainda podemos revalidar um pedido associado ao contato por
    # pedido_id e, por último, por ultimo_pedido.
    if not cpf:
        return resolve_weni_locators(expected_cliente_id=linked_id)

    identity_failure_statuses = {
        "CPF_IDENTIDADE_INSUFICIENTE",
        "CPF_IDENTIDADE_DIVERGENTE",
        "CPF_IDENTIDADE_AMBIGUA",
        "CPF_DIVERGENTE_API",
        "CLIENTE_ID_INVALIDO_API",
        "ERRO_RESPOSTA_CLIENTE_API",
        "CPF_NAO_INFORMADO_API",
    }
    if invalid_cpf and cliente_lookup_status in identity_failure_statuses | {
        "ERRO_API",
        "CPF_NAO_ENCONTRADO",
        "CPF_REJEITADO_API",
    }:
        order = _empty_order(contact, captured_at)
        return _failure(
            order,
            [],
            cliente_lookup_status,
            "CPF inválido: identidade do cliente não confirmada; pedido bloqueado.",
        )

    # A validação desta execução já consultou o CPF. Repetir 404 ou 500 na
    # etapa de pedidos aumentaria a carga da API sem acrescentar informação.
    if cliente_lookup_status in {"ERRO_API", "CPF_NAO_ENCONTRADO", "CPF_REJEITADO_API"}:
        recovered_order, recovered_items = resolve_weni_locators(
            expected_cliente_id=linked_id
        )
        if recovered_order is not None:
            return recovered_order, recovered_items
        order = _empty_order(contact, captured_at)
        if cliente_lookup_status == "CPF_REJEITADO_API":
            return _failure(
                order,
                [],
                "CPF_REJEITADO_API",
                "Consulta de CPF rejeitada pela API (HTTP 400).",
            )
        if cliente_lookup_status == "ERRO_API":
            return _failure(
                order,
                [],
                "ERRO_API",
                "Falha na consulta oficial do cliente e nenhum localizador Weni pôde ser validado.",
            )
        return _failure(
            order,
            [],
            "CPF_NAO_ENCONTRADO",
            "Cliente não localizado e nenhum pedido Weni pôde ser validado.",
        )

    try:
        payload = (
            cliente_response
            if cliente_response is not None
            else farm_client.get_cliente_by_cpf(cpf)
        )
    except (FarmaliderAuthError, FarmaliderUnavailableError):
        raise
    except FarmaliderBadRequestError:
        order = _empty_order(contact, captured_at)
        return _failure(
            order,
            [],
            "CPF_REJEITADO_API",
            "Consulta de CPF rejeitada pela API (HTTP 400).",
        )
    except FarmaliderError:
        if invalid_cpf:
            order = _empty_order(contact, captured_at)
            return _failure(
                order,
                [],
                "ERRO_API",
                "CPF inválido: falha na confirmação de identidade; pedido bloqueado.",
            )
        recovered_order, recovered_items = resolve_weni_locators(
            expected_cliente_id=linked_id
        )
        if recovered_order is not None:
            return recovered_order, recovered_items
        order = _empty_order(contact, captured_at)
        return _failure(
            order,
            [],
            "ERRO_API",
            "Falha na consulta oficial do cliente e nenhum localizador Weni pôde ser validado.",
        )

    if payload is None:
        if invalid_cpf:
            order = _empty_order(contact, captured_at)
            return _failure(
                order,
                [],
                "CPF_NAO_ENCONTRADO",
                "CPF inválido: cliente não encontrado; pedido bloqueado.",
            )
        recovered_order, recovered_items = resolve_weni_locators(
            expected_cliente_id=linked_id
        )
        if recovered_order is not None:
            return recovered_order, recovered_items
        order = _empty_order(contact, captured_at)
        return _failure(
            order,
            [],
            "CPF_NAO_ENCONTRADO",
            "Cliente não localizado e nenhum pedido Weni pôde ser validado.",
        )

    result, selected_payload = select_cliente_response(
        cpf,
        payload,
        weni_identity=weni_customer_identity(contact),
    )
    status, confirmed_cliente_id, _ = result
    if status not in {"CPF_CONFIRMADO", "CPF_INVALIDO_ENCONTRADO"}:
        # Resposta concreta com CPF divergente/cliente inválido é falha dura:
        # não tentamos contornar uma inconsistência de identidade.
        order = _empty_order(contact, captured_at)
        return _failure(
            order,
            [],
            status,
            "Cliente não confirmado pela consulta oficial de CPF.",
        )

    payload = selected_payload

    if linked_id and linked_id != confirmed_cliente_id:
        order = _empty_order(contact, captured_at)
        return _failure(
            order,
            [],
            "CLIENTE_ID_DIVERGENTE_API",
            "Vínculo do cliente divergente da consulta oficial.",
        )
    linked_id = confirmed_cliente_id
    identity_verified = True

    latest = payload.get("ultimoPedido") if isinstance(payload, dict) else None
    pedido_id = _positive_id(latest.get("id")) if isinstance(latest, dict) else ""
    if not pedido_id:
        if isinstance(latest, dict) and latest:
            return _failure(
                _empty_order(contact, captured_at),
                [],
                "PEDIDO_ID_INVALIDO_API",
                "Último pedido oficial possui identidade ausente ou inválida.",
            )
        return resolve_weni_locators(expected_cliente_id=linked_id)

    # pedido_id é produzido acima somente quando latest é um objeto JSON.
    latest = cast(dict[str, Any], latest)
    latest_owner = (
        _positive_id(latest.get("codCliente")) if "codCliente" in latest else linked_id
    )
    if latest_owner != linked_id:
        order = _empty_order(contact, captured_at)
        return _failure(
            order,
            [],
            "PEDIDO_CLIENTE_DIVERGENTE_API",
            "Último pedido pertence a outro cliente.",
        )

    # O GET por CPF já é suficiente para montar o snapshot completo.
    fallback_order = _empty_order(contact, captured_at)
    fallback_order["pedido_id"] = pedido_id
    fallback_order["farmalider_cliente_id"] = linked_id
    fallback_order["origem_pedido"] = "FARMALIDER_CLIENTE_CPF" + (
        "_IDENTIDADE_VALIDADA" if invalid_cpf else ""
    )
    fallback_response = _pedido_from_cliente_payload(payload)
    if fallback_response is not None:
        fallback_order, fallback_items = apply_pedido_api_confirmation(
            fallback_order,
            [],
            fallback_response,
            linked_cliente_id=linked_id,
            confirmed_at=captured_at,
        )
    else:
        fallback_items = []

    # GET pedido/{codigo} é o snapshot final preferencial do mesmo pedido.
    try:
        response = fetch_pedido(pedido_id)
    except (FarmaliderAuthError, FarmaliderUnavailableError):
        raise
    except FarmaliderError:
        if not fallback_items:
            fallback_order["_farmalider_erro_consulta_pedido"] = "1"
            if fallback_order.get("status_validacao") == "CANCELADO_SEM_ITENS":
                return _failure(
                    fallback_order,
                    [],
                    "ERRO_ITENS_PEDIDO_API",
                    "Falha ao consultar pedido; ausência de itens não confirmada.",
                )
        return fallback_order, fallback_items

    if isinstance(response, dict):
        order = _empty_order(contact, captured_at)
        order["pedido_id"] = pedido_id
        order["farmalider_cliente_id"] = linked_id
        order["origem_pedido"] = "FARMALIDER_PEDIDO_API" + (
            "_IDENTIDADE_VALIDADA" if invalid_cpf else ""
        )
        confirmed_order, confirmed_items = apply_pedido_api_confirmation(
            order,
            [],
            response,
            linked_cliente_id=linked_id,
            confirmed_at=captured_at,
        )
        if confirmed_order.get("status_validacao") == "OK" and confirmed_items:
            return confirmed_order, confirmed_items
        # Resposta concreta inconsistente do endpoint de pedido é falha dura.
        return confirmed_order, confirmed_items

    if not fallback_items:
        fallback_order["_farmalider_erro_consulta_pedido"] = "1"
        if fallback_order.get("status_validacao") == "CANCELADO_SEM_ITENS":
            return _failure(
                fallback_order,
                [],
                "ERRO_ITENS_PEDIDO_API",
                "Pedido não retornado; ausência de itens não confirmada.",
            )
    return fallback_order, fallback_items


# NexoFlow 1.0
