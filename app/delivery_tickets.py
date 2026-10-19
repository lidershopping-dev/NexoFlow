"""Reconciliação dos atendimentos marcados como pedido, sem presumir atribuição."""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import re
import tempfile
from collections import Counter
from copy import deepcopy
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from uuid import NAMESPACE_URL, UUID, uuid5

from analytics import (
    _sales_order_is_valid,
    cpf_query_priority,
    get_fields,
    normalize_contact,
)
from farmalider_client import (
    FarmaliderAuthError,
    FarmaliderError,
    FarmaliderUnavailableError,
)
from farmalider_integration import (
    apply_pedido_api_confirmation,
    cpf_cadastro_status,
    resolve_contact_order,
    weni_identity_guard,
)
from pedido_utils import business_order_datetime, effective_order_status_code
from product_scope import PHARMACY_SCOPE_VERSION
from urn_utils import native_contact_urns
from weni_client import WeniError
from weni_update import (
    ContactUpdatePlan,
    GroupAction,
    PlanAuditError,
    UpdateStats,
    _apply_purchase_candidate,
    _available_weni_fields,
    _ensure_order_fields,
    _execute_contact_plan,
    _prepare_frozen_plan_for_apply,
    _recompra_products_need_unscheduling,
    _same_guard_value,
    _same_value,
    audit_plans,
    load_review_snapshot,
    write_review_snapshot,
)

TICKET_COLUMNS = [
    "ticket_uuid",
    "protocolo",
    "contact_uuid",
    "tags",
    "aberto_em",
    "encerrado_em",
    "resultado_validacao",
    "pedido_chave",
    "pedido_id",
    "pedido_data",
    "pedido_status",
    "pedido_valor",
    "vinculo_atendimento",
    "atualizado_em",
    "chats_room_uuid",
    "chat_contact_uuid",
    "origem_registro",
]
DELIVERY_ORDER_COLUMNS = [
    "pedido_chave",
    "pedido_id",
    "cliente_id",
    "pedido_data",
    "data",
    "pedido_status",
    "pedido_status_nome",
    "pedido_valor",
    "venda_realizada",
    "vinculo_atendimento",
    "quantidade_atendimentos",
    "atualizado_em",
]
TAGS = {"pedido", "pedido finalizado", "pedido is deleted"}


def is_order_tag(value):
    """Accept exact order labels and Weni's timestamped deleted Pedido label."""
    if not isinstance(value, str):
        return False
    raw = value.strip()
    if " ".join(raw.casefold().split()) in TAGS:
        return True
    prefix = "pedido_is_deleted_"
    if not raw.casefold().startswith(prefix):
        return False
    timestamp = raw[len(prefix) :]
    if not re.fullmatch(
        r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})",
        timestamp,
    ):
        return False
    return _datetime(timestamp) is not None


def _datetime(value):
    try:
        result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return result if result.tzinfo is not None else None
    except (TypeError, ValueError):
        return None


def delivery_ticket_key(chat_contact_uuid, opened, ended, *, fallback=""):
    """Stable local key shared by XLSX and API, independent of mutable tags."""
    try:
        contact_id = str(UUID(str(chat_contact_uuid)))
    except ValueError:
        return fallback
    dates = [_datetime(opened), _datetime(ended)]
    if any(value is None for value in dates):
        return fallback
    natural = [
        contact_id,
        *(value.astimezone(UTC).isoformat() for value in dates if value is not None),
    ]
    return str(uuid5(NAMESPACE_URL, "nexoflow:delivery:" + json.dumps(natural)))


def select_tickets(rooms, *, cutoff):
    selected, rejected, seen = [], Counter(), set()
    for room in rooms:
        uid = str(room.get("uuid") or "")
        if uid in seen:
            continue
        seen.add(uid)
        tags = room.get("tags") or []
        if not isinstance(tags, list):
            raise ValueError("Atendimento com estrutura de tags inválida.")
        names = [
            str(tag.get("name") or "").strip() for tag in tags if isinstance(tag, dict)
        ]
        if not any(is_order_tag(name) for name in names):
            continue
        ended = _datetime(room.get("ended_at"))
        if ended is None or ended > cutoff or room.get("is_active") is True:
            rejected["ENCERRAMENTO_AUSENTE_OU_FORA_DO_CORTE"] += 1
            continue
        if not (
            room.get("user")
            or room.get("first_user_message_sent_at")
            or room.get("first_user_assigned_at")
        ):
            rejected["ATENDENTE_NAO_IDENTIFICADO"] += 1
            continue
        contact = room.get("contact")
        external_id = (
            str(contact.get("external_id") or "") if isinstance(contact, dict) else ""
        )
        try:
            UUID(uid)
            UUID(external_id)
        except ValueError:
            rejected["VINCULO_WENI_AUSENTE_OU_INVALIDO"] += 1
            continue
        chat_uid = str(contact.get("uuid") or "")
        selected.append(
            {
                "ticket_uuid": delivery_ticket_key(
                    chat_uid, room.get("created_on"), room.get("ended_at"), fallback=uid
                ),
                "chats_room_uuid": uid,
                "chat_contact_uuid": chat_uid,
                "origem_registro": "API_CHATS",
                "protocolo": str(room.get("protocol") or ""),
                "contact_uuid": external_id,
                "tags": ", ".join(names),
                "aberto_em": str(room.get("created_on") or ""),
                "encerrado_em": str(room.get("ended_at") or ""),
                "custom_fields": room.get("custom_fields") or {},
            }
        )
    return selected, dict(rejected)


def ticket_order_link(ticket, order, *, order_id_field=""):
    """A tag e a proximidade de datas não comprovam qual pedido gerou o ticket."""
    fields = ticket.get("custom_fields")
    raw_id = fields.get(order_id_field) if isinstance(fields, dict) else None
    if isinstance(raw_id, (str, int)) and str(raw_id).strip():
        return (
            "CONFIRMADO_POR_ID"
            if str(raw_id).strip() == order.get("pedido_id")
            else "OUTRO_PEDIDO_NO_TICKET"
        )
    opened, ended = (
        _datetime(ticket.get("aberto_em")),
        _datetime(ticket.get("encerrado_em")),
    )
    emitted = business_order_datetime(order.get("data_hora"))
    if opened and ended and emitted:
        if opened <= emitted <= ended:
            return "COMPATIVEL_COM_PERIODO_SEM_ID"
        return "FORA_DO_PERIODO_DO_ATENDIMENTO"
    return "SEM_VINCULO_COMPROVADO"


def _recompra_removals(plan, contact, captured_at):
    """Invalidate the old D+25 reference without admitting a new campaign entry."""
    from recompra import GROUP, OPT_OUT, TZ, member, refused

    fields = get_fields(contact)
    reference = business_order_datetime(
        plan.fields.get("pedido_data", fields.get("pedido_data"))
    )
    local_now = business_order_datetime(captured_at)
    expired = reference is None or bool(
        local_now and (local_now.astimezone(TZ).date() - reference.date()).days > 25
    )
    if not (
        "pedido_data" in plan.fields
        or _recompra_products_need_unscheduling(plan)
        or (plan.order_validated and not plan.purchase_confirmed)
        or expired
        or refused(contact)
        or member(contact, "", OPT_OUT)
    ):
        return []
    return [
        GroupAction(
            "remove",
            group_uuid,
            "referência de recompra mudou, expirou ou cliente inelegível; reavaliar na rotina de recompra",
            GROUP,
        )
        for group_uuid in sorted(
            {
                str(group["uuid"])
                for group in contact.get("groups", []) or []
                if isinstance(group, dict)
                and group.get("name") == GROUP
                and group.get("uuid")
            }
        )
    ]


def _order_plan(contact, resolved, farm, captured_at, available_fields):
    fields = get_fields(contact)
    plan = ContactUpdatePlan(
        contact_uuid=contact["uuid"], contact_name=str(contact.get("name") or "")
    )
    _apply_purchase_candidate(
        plan,
        contact,
        farm_client=farm,
        captured_at=captured_at,
        available_weni_fields=available_fields,
        resolved_order=resolved,
    )
    plan.fields.pop("ultima_compra", None)
    plan.previous.pop("ultima_compra", None)
    plan.evidence.pop("ultima_compra", None)
    plan.group_actions.extend(_recompra_removals(plan, contact, captured_at))
    if plan.group_actions:
        plan.guards["marketing_opt_in"] = fields.get("marketing_opt_in")
    # Always guard the identity, including orders kept only in Sheets because Weni has a newer snapshot.
    plan.guards["__invalid_cpf_identity"] = weni_identity_guard(contact)
    for name in (
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
        plan.guards[name] = fields.get(name)
    return plan


def reconcile_tickets(
    tickets, contacts, farm, *, captured_at, available_fields, order_id_field=""
):
    _ensure_order_fields(available_fields)
    by_uuid = {str(contact.get("uuid") or ""): contact for contact in contacts}
    selected_ids = {ticket["contact_uuid"] for ticket in tickets}
    selected_contacts = [by_uuid[uid] for uid in selected_ids if uid in by_uuid]
    selected_contacts.sort(
        key=lambda contact: (
            cpf_query_priority(get_fields(contact).get("cpf")),
            contact["uuid"],
        )
    )
    resolved_by_uuid, plans, summary = {}, [], Counter()
    groups = {}
    for index, contact in enumerate(selected_contacts, 1):
        try:
            resolved = resolve_contact_order(
                contact, farm_client=farm, captured_at=captured_at
            )
        except (FarmaliderAuthError, FarmaliderUnavailableError):
            raise
        except FarmaliderError:
            resolved = ({"status_confirmacao": "ERRO_API"}, [])
        if (
            resolved[0] is None
            and cpf_cadastro_status(get_fields(contact).get("cpf"))
            == "CPF_NAO_INFORMADO"
        ):
            # Sem localizador cadastral não significa que o cliente nunca comprou.
            # Preserve a consulta legada por ID quando ela já confirmou um pedido.
            resolved = ({"status_confirmacao": "CPF_NAO_INFORMADO"}, [])
        resolved_by_uuid[contact["uuid"]] = resolved
        plan = _order_plan(contact, resolved, farm, captured_at, available_fields)
        # Include unchanged/blocked contacts so their identity is checked again before publishing evidence.
        plans.append(plan)
        outcome = plan.order_resolution_status or "SEM_PEDIDO_VALIDADO"
        summary[outcome] += 1
        priority = cpf_query_priority(get_fields(contact).get("cpf"))
        groups.setdefault(str(priority), Counter())[outcome] += 1
        if index % 100 == 0 or index == len(selected_contacts):
            logging.info(
                "Delivery: clientes verificados=%s/%s; resultados=%s; grupos CPF=%s.",
                index,
                len(selected_contacts),
                dict(summary),
                {key: dict(value) for key, value in groups.items()},
            )
    audit_plans(plans, opt_out_group_available=True, opt_in_group_available=True)
    orders, items, rows, ticket_orders = {}, {}, [], {}
    for ticket in tickets:
        order, order_items = resolved_by_uuid.get(ticket["contact_uuid"], (None, []))
        custom = ticket.get("custom_fields")
        explicit_id = custom.get(order_id_field) if isinstance(custom, dict) else None
        explicit_id = (
            str(explicit_id).strip()
            if isinstance(explicit_id, (int, str)) and not isinstance(explicit_id, bool)
            else ""
        )
        # An explicit historical ID can be verified only against the customer identified in this execution.
        if (
            explicit_id.isdigit()
            and int(explicit_id) > 0
            and order
            and order.get("status_confirmacao") in {"CONFIRMADO_API", "CANCELADO_API"}
            and order.get("farmalider_cliente_id")
            and explicit_id != order.get("pedido_id")
        ):
            cache_key = (ticket["contact_uuid"], explicit_id)
            if cache_key not in ticket_orders:
                template = dict(order, pedido_id=explicit_id)
                try:
                    response = farm.get_pedido(explicit_id)
                    ticket_orders[cache_key] = apply_pedido_api_confirmation(
                        template,
                        [],
                        response,
                        linked_cliente_id=order["farmalider_cliente_id"],
                        confirmed_at=captured_at,
                    )
                except (FarmaliderAuthError, FarmaliderUnavailableError):
                    raise
                except FarmaliderError:
                    ticket_orders[cache_key] = ({"status_confirmacao": "ERRO_API"}, [])
            order, order_items = ticket_orders[cache_key]
        outcome = str(
            (order or {}).get("status_confirmacao")
            or (
                "CONTATO_WENI_NAO_LOCALIZADO"
                if ticket["contact_uuid"] not in by_uuid
                else "SEM_PEDIDO_VALIDADO"
            )
        )
        usable = bool(
            order
            and order_items
            and order.get("status_validacao") == "OK"
            and outcome in {"CONFIRMADO_API", "CANCELADO_API"}
        )
        row = {key: ticket.get(key, "") for key in TICKET_COLUMNS}
        row.update(
            resultado_validacao=outcome,
            atualizado_em=captured_at,
            vinculo_atendimento="SEM_PEDIDO_VALIDADO",
        )
        if usable:
            row.update(
                pedido_chave=order["pedido_chave"],
                pedido_id=order["pedido_id"],
                pedido_data=order["data_hora"],
                pedido_status=effective_order_status_code(order),
                pedido_valor=order["valor_total_compra"],
                vinculo_atendimento=ticket_order_link(
                    ticket, order, order_id_field=order_id_field
                ),
            )
            orders[order["pedido_chave"]] = order
            for item in order_items:
                items[item["item_chave"]] = item
        rows.append(row)
    return plans, {
        "tickets": rows,
        "orders": list(orders.values()),
        "items": list(items.values()),
        "summary": dict(summary),
        "cpf_groups": {key: dict(value) for key, value in groups.items()},
        "cutoff": captured_at,
    }


def build_delivery_order_records(tickets, orders, *, updated_at):
    by_key = {
        order["pedido_chave"]: order for order in orders if order.get("pedido_chave")
    }
    grouped = {}
    for ticket in tickets:
        key = ticket.get("pedido_chave")
        if key and ticket.get("resultado_validacao") in {
            "CONFIRMADO_API",
            "CANCELADO_API",
        }:
            grouped.setdefault(key, {})[ticket["ticket_uuid"]] = ticket
    records = []
    for key, unique in sorted(grouped.items()):
        order = by_key.get(key)
        if not order or order.get("status_validacao") != "OK":
            continue
        links = {ticket.get("vinculo_atendimento") for ticket in unique.values()}
        records.append(
            dict(
                zip(
                    DELIVERY_ORDER_COLUMNS,
                    [
                        key,
                        order["pedido_id"],
                        order.get("farmalider_cliente_id", ""),
                        order["data_hora"],
                        order["data"],
                        effective_order_status_code(order),
                        order.get("farmalider_status_pedido_nome", ""),
                        float(Decimal(order["valor_total_compra"])),
                        int(_sales_order_is_valid(order)),
                        "CONFIRMADO_POR_ID"
                        if "CONFIRMADO_POR_ID" in links
                        else "SEM_VINCULO_COMPROVADO",
                        len(unique),
                        updated_at,
                    ],
                    strict=False,
                )
            )
        )
    return records


def write_delivery_review(plans, delivery, path):
    stats = UpdateStats(
        contacts_considered=len(plans),
        orders_validated=sum(p.order_validated for p in plans),
    )
    write_review_snapshot(
        plans, stats, full=True, json_path=path, captured_at_utc=delivery["cutoff"]
    )
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["delivery"] = delivery
    payload["delivery_sha256"] = _digest(delivery)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    if os.name != "nt":
        path.chmod(0o600)
        path.with_suffix(".txt").chmod(0o600)
    with path.with_suffix(".txt").open("a", encoding="utf-8-sig") as report:
        report.write("\nATENDIMENTOS DELIVERY\n")
        report.write(
            f"Tickets selecionados: {len(delivery['tickets'])}; pedidos únicos confirmados: {len(delivery['orders'])}.\n"
        )
        report.write(
            "A tag seleciona clientes. Apenas CONFIRMADO_POR_ID comprova o vínculo de um pedido com o ticket.\n"
        )
        report.write(
            "O endpoint por CPF informa somente o último pedido; o histórico de pedidos de cada ticket pode ficar pendente.\n"
        )
        report.write(
            "Resultados por cliente: "
            + json.dumps(delivery["summary"], ensure_ascii=False)
            + "\n"
        )
        report.write("Destino revisado: " + delivery.get("target", "both") + "\n")
        report.write(
            "Grupos CPF (0 válido, 1 inválido preenchido, 2 ausente): "
            + json.dumps(delivery.get("cpf_groups", {}), ensure_ascii=False)
            + "\n"
        )
        if delivery.get("spreadsheet_import"):
            report.write(
                "Importação de planilhas: "
                + json.dumps(delivery["spreadsheet_import"], ensure_ascii=False)
                + "\n"
            )
        for ticket in delivery["tickets"]:
            report.write(
                f"Ticket {ticket['ticket_uuid']} | contato {ticket['contact_uuid']} | tags {ticket['tags']} | "
                f"checagem {ticket['resultado_validacao']} | pedido {ticket['pedido_id'] or '<não validado>'} | "
                f"emissão {ticket['pedido_data'] or '<ausente>'} | vínculo {ticket['vinculo_atendimento']}\n"
            )
    return payload


def _digest(value):
    return hashlib.sha256(
        json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode()
    ).hexdigest()


class _DeliveryConflictReport:
    """Diagnóstico privado, sem copiar dados pessoais ou textos de produtos."""

    def __init__(self, review_path, payload):
        review_path = Path(review_path)
        self.path = (
            review_path.parent / "privado" / f"{review_path.stem}_conflitos.json"
        )
        if self.path.exists():
            # Uma retomada não deve apagar a evidência da tentativa anterior.
            history = self.path.parent / "historico_conflitos"
            history.mkdir(parents=True, exist_ok=True, mode=0o700)
            descriptor, archived = tempfile.mkstemp(
                dir=history, prefix=f"{review_path.stem}_", suffix=".json"
            )
            try:
                with os.fdopen(descriptor, "wb") as stream:
                    stream.write(self.path.read_bytes())
                    stream.flush()
                    os.fsync(stream.fileno())
            except BaseException:
                Path(archived).unlink(missing_ok=True)
                raise
        self.salt = os.urandom(32)
        self.records = []
        self.payload = {
            "snapshot_sha256": payload.get("plan_sha256", ""),
            "delivery_sha256": payload.get("delivery_sha256", ""),
            "gerado_em": datetime.now(UTC).isoformat(),
            "valores_ocultos": True,
            "orientacao": (
                "Compare as assinaturas dos valores esperados e encontrados. "
                "Confira o UUID na Weni antes de retomar o mesmo snapshot; "
                "não sobrescreva alterações posteriores."
            ),
            "conflitos": self.records,
            "avaliacao_contatos_concluida": False,
            "aplicacao_concluida": False,
            "contatos_aceitos": 0,
        }
        self.save()

    def _value(self, value):
        serialized = json.dumps(
            value, ensure_ascii=False, sort_keys=True, default=str
        ).encode("utf-8")
        return {
            "presente": value is not None,
            "tipo": type(value).__name__,
            "assinatura": hmac.new(self.salt, serialized, hashlib.sha256).hexdigest()[
                :20
            ],
        }

    def add(self, plan, reason, differences):
        fields = sorted({difference[0] for difference in differences})
        reference = hashlib.sha256(plan.contact_uuid.encode()).hexdigest()[:12]
        self.records.append(
            {
                "contact_uuid": plan.contact_uuid,
                "contato_ref": reference,
                "motivo": reason,
                "campos": fields,
                "comparacoes": [
                    {
                        "campo": field,
                        "esperado": self._value(expected),
                        "encontrado": self._value(current),
                    }
                    for field, expected, current in differences
                ],
            }
        )
        self.save()
        logging.warning(
            "Delivery conflito: contato_ref=%s; motivo=%s; campos=%s. "
            "Confira o relatório privado de conflitos.",
            reference,
            reason,
            ", ".join(fields),
        )

    def save(self, *, completed=False, accepted=None, application_completed=False):
        self.payload["avaliacao_contatos_concluida"] = completed
        self.payload["aplicacao_concluida"] = application_completed
        if accepted is not None:
            self.payload["contatos_aceitos"] = accepted
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor, temporary = tempfile.mkstemp(
            dir=self.path.parent, prefix=".delivery-", suffix=".tmp"
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                json.dump(self.payload, stream, ensure_ascii=False, indent=2)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
        finally:
            Path(temporary).unlink(missing_ok=True)

    def summary(self):
        return {
            "conflict_report": str(self.path),
            "conflict_reasons": dict(Counter(row["motivo"] for row in self.records)),
        }


def _delivery_conflict_differences(plan, current):
    """Reproduce apenas comparações, sem divulgar o texto bruto do guard."""
    fields = get_fields(current)
    differences = []
    reasons = set()
    for name, proposed in plan.fields.items():
        found, expected = fields.get(name), plan.previous.get(name)
        if not _same_value(name, found, proposed) and not _same_value(
            name, found, expected
        ):
            differences.append((name, expected, found))
            reasons.add("CAMPO_ALTERADO")
    for name, expected in plan.guards.items():
        if name in {"__pending_purchase_check", "__pending_purchase_check_resolved"}:
            continue
        if name == "__invalid_cpf_identity":
            found = weni_identity_guard(current)
            if found != expected:
                differences.append((name, expected, found))
                reasons.add("IDENTIDADE_ALTERADA")
        elif name == "__native_urns":
            found = sorted(native_contact_urns(current))
            expected = sorted(
                str(value) for value in (expected or []) if str(value).strip()
            )
            if found != expected:
                differences.append((name, expected, found))
                reasons.add("URN_NATIVA_ALTERADA")
        elif name not in plan.fields:
            found = fields.get(name)
            if not _same_guard_value(found, expected):
                differences.append((name, expected, found))
                reasons.add("FONTE_ALTERADA")
    if not differences:
        # A proteção do grupo pode bloquear antes das comparações dos campos.
        differences.append(
            ("__grupos_recompra", plan.group_actions, current.get("groups"))
        )
        reasons.add("GRUPO_RECOMPRA_ALTERADO")
    return "+".join(sorted(reasons)), differences


def apply_delivery_review(path, weni, *, store_factory=None, target=None):
    plans, payload, _ = load_review_snapshot(full=True, json_path=path)
    delivery = payload.get("delivery")
    if not isinstance(delivery, dict) or _digest(delivery) != payload.get(
        "delivery_sha256"
    ):
        raise ValueError(
            "Snapshot Delivery ausente ou alterado; execute a reconciliação novamente."
        )
    cutoff_dt = business_order_datetime(delivery.get("cutoff"))
    if cutoff_dt is None:
        raise ValueError("Data de corte Delivery inválida; execute um novo dry-run.")
    frozen_target = delivery.get("target", "both")
    target = frozen_target if target is None else target
    if target not in {"weni", "analytics", "both"} or target != frozen_target:
        raise ValueError(
            "Destino diferente do snapshot revisado; execute um novo dry-run."
        )
    if target in {"analytics", "both"} and store_factory is None:
        raise ValueError(
            "Google Sheets deve estar configurado para o destino revisado."
        )
    audit_plans(plans, opt_out_group_available=True, opt_in_group_available=True)
    available = _available_weni_fields(weni)
    _ensure_order_fields(available)
    # A orientação vigente usa pedido_data; não editar o arquivo revisado.
    # A projeção abaixo somente elimina a escrita legada de ultima_compra.
    legacy = sum("ultima_compra" in p.fields for p in plans)
    for plan in plans:
        for data in (plan.fields, plan.previous, plan.guards, plan.evidence):
            data.pop("ultima_compra", None)
    if legacy:
        logging.warning(
            "Delivery: escrita legada de ultima_compra desconsiderada em %s plano(s); pedido_data permanece como referência. Snapshot original preservado.",
            legacy,
        )
    unknown = sorted(
        {key for plan in plans for key in plan.fields if key not in available}
    )
    if unknown:
        raise PlanAuditError(
            "Campos do snapshot ausentes na Weni: "
            + ", ".join(unknown)
            + ". Aplicação bloqueada antes de gravar; confira os campos e a revisão."
        )
    conflict_report = _DeliveryConflictReport(path, payload)
    accepted, conflicts, current_contacts = set(), set(), []
    reviewed_order_contacts = {
        order.get("contact_uuid") for order in delivery["orders"]
    }
    for position, plan in enumerate(plans, 1):
        reference = hashlib.sha256(plan.contact_uuid.encode()).hexdigest()[:12]
        if position == 1 or position % 100 == 0:
            logging.info(
                "Delivery aplicação: contato=%s/%s; aceitos=%s; conflitos=%s.",
                position,
                len(plans),
                len(accepted),
                len(conflicts),
            )
        try:
            current = weni.get_contact(uuid=plan.contact_uuid)
        except WeniError as exc:
            conflict_report.save(accepted=len(accepted))
            raise WeniError(
                f"Delivery etapa=RELER_CONTATO contato_ref={reference}: {exc}"
            ) from None
        if current is None:
            conflicts.add(plan.contact_uuid)
            conflict_report.add(
                plan, "CONTATO_NAO_ENCONTRADO", [("__contato", True, None)]
            )
            continue
        if cpf_cadastro_status(
            get_fields(current).get("cpf")
        ) == "CPF_INVALIDO_CADASTRO" and (
            plan.order_validated or plan.contact_uuid in reviewed_order_contacts
        ):
            # Snapshots antigos podem conter pedidos consultados por CPF zero.
            # Preserve sua retomada para os demais contatos sem reaplicar esse vínculo.
            conflicts.add(plan.contact_uuid)
            conflict_report.add(
                plan,
                "CPF_INVALIDO_CADASTRO",
                [("cpf", "CPF preenchido consultável", get_fields(current).get("cpf"))],
            )
            continue
        prepared, _ = _prepare_frozen_plan_for_apply(
            plan,
            current,
            opt_in_group_name="",
            opt_in_group_uuid="",
            opt_out_group_name="",
            opt_out_group_uuid="",
        )
        if prepared is None:
            conflicts.add(plan.contact_uuid)
            reason, differences = _delivery_conflict_differences(plan, current)
            conflict_report.add(plan, reason, differences)
            continue
        if target in {"weni", "both"}:
            reviewed_groups = {
                action.group
                for action in plan.group_actions
                if action.action == "remove"
                and action.group_name == "Recompra FarmaLíder"
            }
            if any(
                action.group not in reviewed_groups
                for action in _recompra_removals(plan, current, delivery["cutoff"])
            ):
                # An admission after the review may have scheduled an old event.
                # Do not change its reference without reviewing its removal.
                conflicts.add(plan.contact_uuid)
                conflict_report.add(
                    plan,
                    "GRUPO_RECOMPRA_ALTERADO",
                    [
                        (
                            "__grupos_recompra",
                            sorted(reviewed_groups),
                            current.get("groups"),
                        )
                    ],
                )
                continue
        audit_plans(
            [prepared], opt_out_group_available=True, opt_in_group_available=True
        )
        if prepared.has_changes and target in {"weni", "both"}:
            try:
                _execute_contact_plan(weni, prepared)
            except WeniError as exc:
                conflict_report.save(accepted=len(accepted))
                raise WeniError(
                    f"Delivery etapa=GRAVAR_CAMPOS_OU_REMOVER_RECOMPRA contato_ref={reference}: {exc}"
                ) from None
        accepted.add(plan.contact_uuid)
        conflict_report.payload["contatos_aceitos"] = len(accepted)
        merged = deepcopy(current)
        merged["fields"] = {**get_fields(current), **prepared.fields}
        base_record = normalize_contact(merged, delivery["cutoff"])
        confirmed = next(
            (
                order
                for order in delivery["orders"]
                if order["contact_uuid"] == plan.contact_uuid
            ),
            None,
        )
        if confirmed:
            priority = cpf_query_priority(get_fields(current).get("cpf"))
            base_record.update(
                farmalider_cliente_id=confirmed.get("farmalider_cliente_id", ""),
                farmalider_cliente_status={
                    0: "CPF_CONFIRMADO",
                    1: "CPF_INVALIDO_ENCONTRADO",
                    2: "CPF_NAO_INFORMADO",
                }[priority],
                farmalider_cliente_consultado_em=delivery["cutoff"],
            )
            # The order confirms the customer ID, but does not supply the official customer name.
            base_record.pop("farmalider_cliente_nome", None)
        else:
            # Preserve the separate customer-validation metadata when only the ticket check failed.
            for name in (
                "farmalider_cliente_id",
                "farmalider_cliente_nome",
                "farmalider_cliente_status",
                "farmalider_cliente_consultado_em",
            ):
                base_record.pop(name, None)
        current_contacts.append(base_record)
    # A partial/conflicting client never exports a successful reconciliation to Sheets.
    tickets = [row for row in delivery["tickets"] if row["contact_uuid"] in accepted]
    orders = [
        order for order in delivery["orders"] if order["contact_uuid"] in accepted
    ]
    keys = {order["pedido_chave"] for order in orders}
    items = [item for item in delivery["items"] if item["pedido_chave"] in keys]
    conflict_report.save(
        completed=True,
        accepted=len(accepted),
        application_completed=target == "weni" and not conflicts,
    )
    if conflicts:
        logging.warning(
            "Delivery: %s contato(s) preservado(s) por conflito; detalhes privados em %s. "
            "Retome o mesmo snapshot após conferir o estado atual na Weni.",
            len(conflicts),
            conflict_report.path,
        )
    if target == "weni":
        return {
            "contacts_accepted": len(accepted),
            "conflicts": len(conflicts),
            "tickets": len(tickets),
            "orders": len(keys),
            "target": target,
            **conflict_report.summary(),
        }
    if store_factory is None:
        raise ValueError(
            "Google Sheets deve estar configurado para o destino revisado."
        )
    store = store_factory()
    try:
        base = store.upsert_contacts_detailed(current_contacts)
        orders_result, items_result = store.upsert_pharmacy_orders(orders, items)
        store.upsert_delivery_tickets(tickets)
        scope = store.reconcile_pharmacy_scope(
            base_records=base.final_records,
            order_records=orders_result.final_records,
            item_records=items_result.final_records,
            include_history=(
                store.get_control("escopo_farmacia_versao") != PHARMACY_SCOPE_VERSION
            ),
        )
        base.final_records = scope.contacts
        all_orders, all_items = scope.orders, scope.items
        if store.get_control("escopo_farmacia_versao") != PHARMACY_SCOPE_VERSION:
            store.resanitize_pharmacy_bi_snapshots(
                base.final_records, all_orders, all_items
            )
        store.refresh_delivery_bi(all_orders)
        from analytics import (
            build_bi_conversion_records,
            build_bi_order_rows,
            build_bi_sales_rows,
            build_bi_summary_record,
            build_sales_metrics,
        )

        snapshot_date = cutoff_dt.date().isoformat()
        store.replace_sales_metrics(build_sales_metrics(all_orders, all_items))
        store.replace_bi_orders(
            build_bi_order_rows(
                base.final_records, all_orders, all_items, updated_at=delivery["cutoff"]
            )
        )
        store.replace_bi_sales(
            build_bi_sales_rows(
                all_orders,
                all_items,
                snapshot_date=snapshot_date,
                updated_at=delivery["cutoff"],
            )
        )
        store.upsert_bi_summary(
            build_bi_summary_record(
                base.final_records,
                all_orders,
                all_items,
                snapshot_date=snapshot_date,
                updated_at=delivery["cutoff"],
            )
        )
        store.upsert_bi_conversion(
            build_bi_conversion_records(
                base.final_records,
                all_orders,
                snapshot_date=snapshot_date,
                updated_at=delivery["cutoff"],
            )
        )
        from looker_bi import build_looker_tables

        history_started = store.get_control("historico_observado_desde")
        store.replace_looker_tables(
            build_looker_tables(
                base.final_records,
                all_orders,
                all_items,
                snapshot_date=snapshot_date,
                updated_at=delivery["cutoff"],
                history_observed_since=history_started or snapshot_date,
            )
        )
        if not history_started:
            store.set_controls({"historico_observado_desde": snapshot_date})
    finally:
        store.close()

    conflict_report.save(
        completed=True, accepted=len(accepted), application_completed=not conflicts
    )
    logging.info(
        "Delivery: contatos aceitos=%s; conflitos=%s; tickets enviados=%s; pedidos únicos enviados=%s.",
        len(accepted),
        len(conflicts),
        len(tickets),
        len(keys),
    )
    return {
        "contacts_accepted": len(accepted),
        "conflicts": len(conflicts),
        "tickets": len(tickets),
        "orders": len(keys),
        **conflict_report.summary(),
    }
