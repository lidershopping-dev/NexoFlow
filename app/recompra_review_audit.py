"""Resumo agregado da revisão; não decide elegibilidade nem expõe dados pessoais."""

from __future__ import annotations

import unicodedata
from collections import Counter
from collections.abc import Iterable, Mapping
from typing import Any

_KNOWN_REASONS = frozenset(
    {
        "CONTATO_DUPLICADO_DIVERGENTE",
        "CPF_CONFIRMADO",
        "CPF_DIVERGENTE_API",
        "CPF_IDENTIDADE_AMBIGUA",
        "CPF_IDENTIDADE_DIVERGENTE",
        "CPF_IDENTIDADE_INSUFICIENTE",
        "CPF_INVALIDO_CADASTRO",
        "CPF_INVALIDO_ENCONTRADO",
        "CPF_NAO_ENCONTRADO",
        "CPF_NAO_INFORMADO",
        "CPF_NAO_INFORMADO_API",
        "CPF_REJEITADO_API",
        "ERRO_API",
        "ERRO_ITENS_PEDIDO_API",
        "ERRO_RESPOSTA_CLIENTE_API",
        "ERRO_RESPOSTA_PEDIDO_API",
        "HORARIO_D25_ENCERRADO",
        "JANELA_0_A_25",
        "MAIS_DE_25_DIAS",
        "PAGAMENTO_0_CANCELADO",
        "PAGAMENTO_0_CANCELADO_WENI",
        "PEDIDO_ANTIGO_BLOQUEADO",
        "PEDIDO_CANCELADO",
        "PEDIDO_CLIENTE_DIVERGENTE_API",
        "PEDIDO_CLIENTE_NAO_VERIFICADO_API",
        "PEDIDO_DATA_INVALIDA_API",
        "PEDIDO_ID_DIVERGENTE_API",
        "PEDIDO_ID_INVALIDO_API",
        "PEDIDO_INCOMPLETO",
        "PEDIDO_JA_ADMITIDO_OU_INDETERMINADO",
        "PEDIDO_NAO_ENCONTRADO_API",
        "PEDIDO_POSTERIOR_PENDENTE_API",
        "PEDIDO_SOMENTE_PRODUTOS_ANIMAIS",
        "PEDIDO_STATUS_3_CANCELADO",
        "PEDIDO_STATUS_3_CANCELADO_WENI",
        "RECUSA_MARKETING",
        "SEM_PEDIDO_CONFIRMADO",
    }
)
_PENDING_REASONS = frozenset(
    reason
    for reason in _KNOWN_REASONS
    if reason.startswith(("ERRO_", "CPF_"))
    and reason not in {"CPF_CONFIRMADO", "CPF_INVALIDO_ENCONTRADO"}
) | {
    "CONTATO_DUPLICADO_DIVERGENTE",
    "PEDIDO_ANTIGO_BLOQUEADO",
    "PEDIDO_CLIENTE_DIVERGENTE_API",
    "PEDIDO_CLIENTE_NAO_VERIFICADO_API",
    "PEDIDO_DATA_INVALIDA_API",
    "PEDIDO_ID_DIVERGENTE_API",
    "PEDIDO_ID_INVALIDO_API",
    "PEDIDO_INCOMPLETO",
    "PEDIDO_NAO_ENCONTRADO_API",
    "PEDIDO_POSTERIOR_PENDENTE_API",
}
_AFFIRMATIVE_VALUES = frozenset(
    {"ativo", "true", "sim", "1", "autorizado", "aceito", "opt-in", "opt in"}
)
_REFUSED_VALUES = frozenset(
    {
        "recusado",
        "nao",
        "negado",
        "inativo",
        "recusa",
        "opt-out",
        "opt out",
        "false",
        "0",
        "cancelado",
        "nao autorizado",
        "nao quero receber",
    }
)


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _normalized(value: Any) -> str:
    return " ".join(
        "".join(
            char
            for char in unicodedata.normalize("NFKD", _text(value).casefold())
            if not unicodedata.combining(char)
        ).split()
    )


def _marketing_counts() -> dict[str, int]:
    return dict.fromkeys(
        (
            "empty",
            "affirmative_value",
            "refused_value",
            "other_value",
            "opt_out_group",
            "opt_out_with_affirmative_value",
        ),
        0,
    )


def _add_marketing(
    counts: dict[str, int], plan: Mapping[str, Any], opt_out_uuid: str
) -> None:
    guard = plan.get("guard") or {}
    value = _normalized((guard.get("fields") or {}).get("marketing_opt_in"))
    field_class = (
        "empty"
        if not value
        else "affirmative_value"
        if value in _AFFIRMATIVE_VALUES
        else "refused_value"
        if value in _REFUSED_VALUES
        else "other_value"
    )
    counts[field_class] += 1
    opt_out = any(
        (
            group.get("name") == "Opt-Out Marketing"
            or bool(opt_out_uuid and group.get("uuid") == opt_out_uuid)
        )
        if isinstance(group, Mapping)
        else group == "Opt-Out Marketing"
        or bool(opt_out_uuid and group == opt_out_uuid)
        for group in guard.get("groups", []) or []
    )
    if opt_out:
        counts["opt_out_group"] += 1
        if field_class == "affirmative_value":
            counts["opt_out_with_affirmative_value"] += 1


def _warning_classes(plan: Mapping[str, Any]) -> set[str]:
    classes = set()
    for warning in plan.get("warnings", []) or []:
        warning = _text(warning)
        if warning.startswith("STATUS_WENI_DIVERGENTE_API:"):
            classes.add("STATUS_WENI_DIVERGENTE_API")
        elif warning.startswith("Pedido misto:"):
            classes.add("PEDIDO_MISTO")
        elif warning.startswith("Compra posterior identificada;"):
            classes.add("COMPRA_POSTERIOR_PENDENTE")
        elif warning.startswith("O mesmo UUID veio com cadastros divergentes"):
            classes.add("CONTATO_DUPLICADO_DIVERGENTE")
        else:
            classes.add("OUTRO_AVISO")
    return classes


def build_review_audit(
    plans: Iterable[Mapping[str, Any]], *, structure: Mapping[str, Any] | None = None
) -> dict[str, Any]:
    """Conta cada UUID uma vez, sem modificar planos ou validar consentimento.

    Duplicidades divergentes são sinalizadas, não conciliadas: os totais usam
    a primeira ocorrência, e este resumo nunca autoriza sua aplicação.
    Códigos e avisos desconhecidos são agregados sem copiar conteúdo livre.
    """
    first_by_uuid: dict[str, Mapping[str, Any]] = {}
    unique: list[Mapping[str, Any]] = []
    conflicting_uuids: set[str] = set()
    reviewed_rows = duplicates = missing_uuid = 0
    for plan in plans:
        reviewed_rows += 1
        uid = _text(plan.get("contact_uuid"))
        if not uid:
            missing_uuid += 1
            unique.append(plan)
        elif uid not in first_by_uuid:
            first_by_uuid[uid] = plan
            unique.append(plan)
        else:
            duplicates += 1
            if plan != first_by_uuid[uid]:
                conflicting_uuids.add(uid)

    reasons: Counter[str] = Counter()
    pending: Counter[str] = Counter()
    warning_classes: Counter[str] = Counter()
    actions = dict.fromkeys(("add", "remove", "replace", "none", "unknown"), 0)
    marketing = _marketing_counts()
    window_marketing = _marketing_counts()
    product_counts = {"rows": 0, "category_empty": 0}
    window_products = {"rows": 0, "category_empty": 0}
    opt_out_uuid = _text((structure or {}).get("opt_out_uuid"))
    fields_updates = planned_changes = within_window = 0
    admissions_without_future_event = 0
    for plan in unique:
        raw_reason = _text(plan.get("reason"))
        reason = raw_reason if raw_reason in _KNOWN_REASONS else "OUTRO_MOTIVO"
        reasons[reason] += 1
        if reason in _PENDING_REASONS:
            pending[reason] += 1
        action = _text(plan.get("action")) or "none"
        if action not in actions:
            action = "unknown"
        actions[action] += 1
        fields_updates += bool(plan.get("fields"))
        planned_changes += bool(plan.get("fields") or plan.get("action"))
        is_window = bool(plan.get("within_window"))
        within_window += is_window
        admissions_without_future_event += action in {"add", "replace"} and not bool(
            plan.get("scheduled_future")
        )
        _add_marketing(marketing, plan, opt_out_uuid)
        if is_window:
            _add_marketing(window_marketing, plan, opt_out_uuid)
        for item in plan.get("campaign_products", []) or []:
            if not isinstance(item, Mapping):
                continue
            product_counts["rows"] += 1
            product_counts["category_empty"] += not bool(_text(item.get("categoria")))
            if is_window:
                window_products["rows"] += 1
                window_products["category_empty"] += not bool(
                    _text(item.get("categoria"))
                )
        warning_classes.update(_warning_classes(plan))
    return {
        "reviewed_rows": reviewed_rows,
        "reviewed_unique": len(unique),
        "duplicate_rows": duplicates,
        "conflicting_duplicate_uuids": len(conflicting_uuids),
        "missing_uuid": missing_uuid,
        "actions": actions,
        "fields_updates": fields_updates,
        "planned_changes": planned_changes,
        "within_window": within_window,
        "admissions_without_future_event": admissions_without_future_event,
        "reasons": dict(sorted(reasons.items())),
        "pending_reasons": dict(sorted(pending.items())),
        "cpf_cadastro_pending": reasons["CPF_INVALIDO_CADASTRO"],
        "marketing": marketing,
        "marketing_within_window": window_marketing,
        "products": product_counts,
        "products_within_window": window_products,
        "warning_classes": dict(sorted(warning_classes.items())),
    }


def public_review_lines(review: Mapping[str, Any]) -> list[str]:
    """Linhas agregadas; nenhum campo vazio é apresentado como autorização."""
    audit = build_review_audit(
        review.get("plans", []), structure=review.get("structure")
    )
    actions = audit["actions"]
    marketing = audit["marketing_within_window"]
    products = audit["products_within_window"]
    lines = [
        "Resumo por contato único; não representa mensagens enviadas.",
        f"Contatos únicos revisados: {audit['reviewed_unique']}; "
        f"linhas repetidas: {audit['duplicate_rows']}; "
        f"duplicidades divergentes: {audit['conflicting_duplicate_uuids']}.",
        f"Na janela de 0 a 25 dias: {audit['within_window']}.",
        f"Ações de grupo: inclusões={actions['add']}; remoções={actions['remove']}; "
        f"trocas de referência={actions['replace']}; sem ação={actions['none']}.",
        f"Contatos com alterações planejadas: {audit['planned_changes']}; "
        f"com atualização de campos: {audit['fields_updates']} "
        "(pode coincidir com ação de grupo).",
        "Marketing na janela: "
        f"campo vazio={marketing['empty']}; "
        f"valor afirmativo={marketing['affirmative_value']}; "
        f"recusa no campo={marketing['refused_value']}; "
        f"outro valor={marketing['other_value']}.",
        "Campo vazio não comprova consentimento. "
        "A revisão não altera a política vigente nem registra autorização.",
        f"Opt-Out com valor afirmativo no campo: "
        f"{audit['marketing']['opt_out_with_affirmative_value']}; "
        "o bloqueio por recusa permanece prioritário.",
        f"CPF inválido no cadastro: {audit['cpf_cadastro_pending']}; "
        "não equivale a rejeição HTTP da API.",
        f"Produtos da campanha na janela: {products['rows']}; "
        f"sem categoria na origem: {products['category_empty']}. "
        "Categoria ausente não comprova uso humano ou animal.",
    ]
    if audit["admissions_without_future_event"]:
        lines.append(
            "ATENÇÃO: propostas de inclusão/troca sem evento futuro: "
            f"{audit['admissions_without_future_event']}; "
            "este resumo não autoriza sua aplicação."
        )
    if audit["pending_reasons"]:
        lines.append(
            "Pendências por motivo: "
            + "; ".join(
                f"{reason}={count}"
                for reason, count in audit["pending_reasons"].items()
            )
            + "."
        )
    if audit["warning_classes"]:
        lines.append(
            "Avisos por classe: "
            + "; ".join(
                f"{kind}={count}" for kind, count in audit["warning_classes"].items()
            )
            + "."
        )
    return lines
