"""Deduplicação revisada da rotina Weni, com comprovantes locais e retomada."""

from __future__ import annotations

import json
import logging
from collections import Counter, defaultdict
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path

from analytics import _full_load_25_load, get_fields, is_valid_cpf, normalize_cpf
from config import LOG_DIR
from contact_dedup_guard import (
    contact_refusals,
    contact_uuid,
    customer_phones,
    native_phones,
    refs,
    validate_pair,
)
from dedup_io import digest, execution_lock, save_private
from dedup_sheets import DedupSheets, record, requests_for
from farmalider_client import FarmaliderError
from farmalider_integration import (
    _cliente_candidates,
    _cpf_customer_status,
    _positive_id,
)
from project_paths import load_private_settings
from task_progress import TaskProgress
from weni_client import WeniClient, WeniError

SCHEMA = 1
DIRECTORY = LOG_DIR / "duplicados"
PRESERVABLE = {"RECUSA_LGPD_NAO_PRESERVADA", "RECUSA_MARKETING_NAO_PRESERVADA"}


def now():
    return datetime.now(UTC).isoformat(timespec="seconds")


def companion(path):
    path = Path(path)
    return path.with_name(path.stem + "_duplicados.json")


def exact_contact(weni, uid):
    contact = weni.get_contact(uuid=uid)
    if contact is not None and contact_uuid(contact) != uid:
        raise ValueError("A Weni retornou outro UUID; exclusão bloqueada.")
    return contact


def groups_index(contact, names):
    result = {}
    for group in contact.get("groups") or []:
        uid = group.get("uuid") if isinstance(group, dict) else group
        if uid in names:
            result[names[uid]] = uid
    return result


def populate_group_names(contacts, names):
    for contact in contacts:
        contact["groups"] = [
            dict(g, name=names.get(g.get("uuid"), g.get("name", "")))
            if isinstance(g, dict)
            else {"uuid": g, "name": names.get(g, "")}
            for g in contact.get("groups") or []
        ]
    return contacts


def group_names(weni):
    return {g["uuid"]: g.get("name", "") for g in weni.list_groups() if g.get("uuid")}


def selected_scope(quarter):
    if quarter is None:
        return None, ""
    manifest = _full_load_25_load()
    ids = manifest["contact_uuids"]
    return set(
        ids[((quarter - 1) * len(ids)) // 4 : (quarter * len(ids)) // 4]
    ), manifest["campaign_id"]


def candidate_groups(contacts, scope=None):
    groups = defaultdict(list)
    for contact in contacts:
        if not contact_uuid(contact):
            raise ValueError("A listagem da Weni contém UUID inválido.")
        cpf = normalize_cpf(get_fields(contact).get("cpf"))
        if cpf:
            groups[cpf].append(contact)
    return {
        cpf: cs
        for cpf, cs in groups.items()
        if len(cs) > 1 and (scope is None or any(c["uuid"] in scope for c in cs))
    }


def unique_customer(farm, cpf):
    candidates = _cliente_candidates(farm.get_cliente_by_cpf(cpf)) or []
    valid = [
        c["cliente"]
        for c in candidates
        if _cpf_customer_status(cpf, c["cliente"])[0] == "CPF_CONFIRMADO"
        and _positive_id(c["cliente"].get("id"))
    ]
    return valid[0] if len(valid) == 1 else None


def preview_refusals(canonical, pair):
    """Somente simulação de recusa para auditar as células antes da exclusão."""
    preview = deepcopy(canonical)
    preview["fields"] = dict(get_fields(preview))
    if pair["refusals"]["lgpd_refused"]:
        preview["fields"]["opt_in_lgpd"] = "recusado"
    return preview


def prepare(contacts, farm, store_factory, *, scope=None):
    """Apenas GET e cálculo; nenhum construtor com migração de planilha."""
    groups = candidate_groups(contacts, scope)
    pending, pairs = [], []
    valid = {cpf: cs for cpf, cs in groups.items() if is_valid_cpf(cpf)}
    for cpf, cs in groups.items():
        if cpf not in valid:
            pending.append(
                {
                    "reason": "CPF_INVALIDO_NAO_COMPROVA_DUPLICIDADE",
                    "contacts": [c["uuid"] for c in cs],
                }
            )
    # O mesmo pedido em CPFs diferentes nunca autoriza apagar outra pessoa.
    by_order = defaultdict(list)
    for c in contacts:
        for oid in refs(c):
            by_order[oid].append(c)
    for cs in by_order.values():
        if (
            len(cs) > 1
            and len({normalize_cpf(get_fields(c).get("cpf")) for c in cs}) > 1
        ):
            pending.append(
                {
                    "reason": "PEDIDO_COM_CPFS_DIFERENTES",
                    "contacts": [c["uuid"] for c in cs],
                }
            )
    if not valid:
        return {"pairs": pairs, "pending": pending}
    if farm is None:
        pending.extend(
            {
                "reason": "API_FARMALIDER_INDISPONIVEL",
                "contacts": [c["uuid"] for c in cs],
            }
            for cs in valid.values()
        )
        return {"pairs": pairs, "pending": pending}
    store = store_factory()
    try:
        targets = {c["uuid"] for cs in valid.values() for c in cs}
        tables, ledger = store.capture(targets)
        api_orders = {}
        with TaskProgress(
            "Weni: revisão de duplicados", len(valid), unit="grupos"
        ) as progress:
            for cpf, cs in progress.track(valid.items()):
                try:
                    customer = unique_customer(farm, cpf)
                except FarmaliderError:
                    pending.append(
                        {"reason": "FALHA_API_CPF", "contacts": [c["uuid"] for c in cs]}
                    )
                    continue
                if customer is None:
                    pending.append(
                        {
                            "reason": "CLIENTE_API_AUSENTE_OU_AMBIGUO",
                            "contacts": [c["uuid"] for c in cs],
                        }
                    )
                    continue
                related = [
                    c
                    for c in contacts
                    if normalize_cpf(get_fields(c).get("cpf")) == cpf
                    or native_phones(c) & customer_phones(customer)
                ]
                matched = [
                    c for c in related if native_phones(c) & customer_phones(customer)
                ]
                if len(matched) != 1 or matched[0]["uuid"] not in {
                    c["uuid"] for c in cs
                }:
                    pending.append(
                        {
                            "reason": "TELEFONE_SEM_CONTATO_CORRETO_UNICO",
                            "contacts": [c["uuid"] for c in cs],
                        }
                    )
                    continue
                canonical = matched[0]
                for duplicate in cs:
                    uid = duplicate["uuid"]
                    if uid == canonical["uuid"] or (
                        scope is not None and uid not in scope
                    ):
                        continue
                    recorded, owners, reasons = store.context_for(
                        tables, uid, cpf, ledger
                    )
                    if (
                        sum(
                            record(tables["Base_Contatos"], r)["contact_uuid"]
                            == canonical["uuid"]
                            for r in tables["Base_Contatos"]["cells"].values()
                        )
                        != 1
                    ):
                        reasons.append("CONTATO_CORRETO_SEM_BASE_ANALYTICS_UNICA")
                    order_ids = refs(duplicate) | recorded
                    orders = {}
                    for oid in sorted(order_ids, key=int):
                        if oid not in api_orders:
                            try:
                                api_orders[oid] = farm.get_pedido(oid)
                            except FarmaliderError:
                                api_orders[oid] = None
                        orders[oid] = api_orders[oid]
                    reasons += validate_pair(
                        canonical,
                        duplicate,
                        customer=customer,
                        related_contacts=related,
                        orders=orders,
                        recorded_order_ids=recorded,
                        history_owner_ids=owners,
                    )
                    if set(reasons) - PRESERVABLE:
                        pending.append(
                            {
                                "reason": ";".join(sorted(set(reasons))),
                                "contacts": [uid],
                            }
                        )
                        continue
                    pair = {
                        "duplicate_uuid": uid,
                        "canonical_uuid": canonical["uuid"],
                        "duplicate_name": duplicate.get("name", ""),
                        "canonical_name": canonical.get("name", ""),
                        "cpf": cpf,
                        "customer_id": _positive_id(customer["id"]),
                        "customer": customer,
                        "orders": orders,
                        "recorded_order_ids": sorted(recorded, key=int),
                        "refusals": contact_refusals(duplicate),
                        "before_duplicate": duplicate,
                    }
                    # Validar também fórmulas, donos e diagnósticos antes de propor exclusão.
                    try:
                        requests_for(
                            tables,
                            [pair],
                            {canonical["uuid"]: preview_refusals(canonical, pair)},
                        )
                    except ValueError as exc:
                        pending.append(
                            {
                                "reason": "FONTE_INCONSISTENTE: " + str(exc),
                                "contacts": [uid],
                            }
                        )
                        continue
                    pairs.append(pair)
    finally:
        store.close()
    return {"pairs": pairs, "pending": pending}


def write_review(path, result, *, full, quarter, campaign_id):
    payload = {
        "schema": SCHEMA,
        "full": full,
        "quarter": quarter,
        "campaign_id": campaign_id,
        "created_at": now(),
        **result,
    }
    payload["sha256"] = digest(payload)
    save_private(companion(path), payload)
    if Path(path).exists():
        main = json.loads(Path(path).read_text(encoding="utf-8"))
        main["dedup_sha256"] = payload["sha256"]
        save_private(path, main)
    lines = [
        "",
        "DEDUPLICAÇÃO DE CONTATOS",
        f"Exclusões propostas: {len(result['pairs'])}",
        f"Pendências preservadas: {len(result['pending'])}",
        "Os pedidos reais serão vinculados ao contato correto; os valores serão preservados.",
        "A aplicação interromperá fluxos e excluirá da Weni os contatos listados abaixo.",
        f"Plano de duplicados SHA-256: {payload['sha256']}",
    ]
    for pair in result["pairs"]:
        lines += [
            f"Excluir: {pair['duplicate_name']} | UUID {pair['duplicate_uuid']}",
            f"Manter: {pair['canonical_name']} | UUID {pair['canonical_uuid']}",
            f"Cliente oficial {pair['customer_id']}; pedidos comprovados: {', '.join(sorted(pair['orders'], key=int)) or 'sem pedido'}.",
            f"Proteções a preservar: {', '.join(pair['refusals']['reasons']) or 'nenhuma recusa identificada'}.",
        ]
    for reason, count in sorted(
        Counter(p["reason"] for p in result["pending"]).items()
    ):
        lines.append(f"Pendente: {reason} — {count} caso(s).")
    report = Path(path).with_suffix(".txt")
    with report.open("a", encoding="utf-8") as stream:
        stream.write("\n".join(lines) + "\n")
    return payload


def load_review(path, *, full, quarter):
    payload = json.loads(companion(path).read_text(encoding="utf-8"))
    original_hash = payload.pop("sha256", "")
    if (
        not original_hash
        or digest(payload) != original_hash
        or payload.get("schema") != SCHEMA
    ):
        raise ValueError(
            "Plano de duplicados alterado ou incompatível; execute novo dry-run."
        )
    if payload.get("full") != full or payload.get("quarter") != quarter:
        raise ValueError("Revisão de duplicados pertence a outro modo ou parte.")
    if (
        not Path(path).exists()
        or json.loads(Path(path).read_text(encoding="utf-8")).get("dedup_sha256")
        != original_hash
    ):
        raise ValueError(
            "Revisão Weni não corresponde à revisão de duplicados; execute novo dry-run."
        )
    if quarter is not None and _full_load_25_load()["campaign_id"] != payload.get(
        "campaign_id"
    ):
        raise ValueError("Revisão de duplicados pertence a outra campanha 25%.")
    payload["sha256"] = original_hash
    losers = [p["duplicate_uuid"] for p in payload["pairs"]]
    if len(losers) != len(set(losers)) or set(losers) & {
        p["canonical_uuid"] for p in payload["pairs"]
    }:
        raise ValueError("Plano de duplicados contém vínculos conflitantes.")
    return payload


def preserve_refusals(weni, canonical, duplicate, names):
    old, current = contact_refusals(duplicate), contact_refusals(canonical)
    lgpd = old["lgpd_refused"] and not current["lgpd_refused"]
    marketing = (old["marketing_refused"] or old["lgpd_refused"]) and not current[
        "marketing_refused"
    ]
    if not any(old[k] or current[k] for k in ("lgpd_refused", "marketing_refused")):
        return canonical
    fields = {}
    if lgpd:
        fields["opt_in_lgpd"] = "recusado"
    if marketing:
        fields["marketing_opt_in"] = "recusado"
    uid = canonical["uuid"]
    if fields:
        weni.update_contact_fields(uid, fields)
    cg, dg = groups_index(canonical, names), groups_index(duplicate, names)
    for name in ("Opt-Out LGPD", "Opt-Out Marketing"):
        if name in dg and name not in cg:
            weni.contact_group_action(uid, action="add", group=dg[name])
    for name in (
        "Recompra FarmaLíder",
        "Opt-In Marketing",
        *(["Opt-In LGPD"] if old["lgpd_refused"] or current["lgpd_refused"] else []),
    ):
        if name in cg:
            weni.contact_group_action(uid, action="remove", group=cg[name])
    canonical = exact_contact(weni, uid)
    if canonical is None:
        raise ValueError("Contato correto não localizado após preservar recusa.")
    populate_group_names([canonical], names)
    flags = contact_refusals(canonical)
    if (
        (lgpd and not flags["lgpd_refused"])
        or (marketing and not flags["marketing_refused"])
        or "Recompra FarmaLíder" in groups_index(canonical, names)
    ):
        raise ValueError(
            "Recusa não confirmada no contato correto; exclusão bloqueada."
        )
    return canonical


def verify_retired_canonical(pair, canonical, contacts, deleted, names):
    """Uma retomada não pode trocar o dono nem perder recusas já preservadas."""
    if normalize_cpf(get_fields(canonical).get("cpf")) != pair["cpf"]:
        raise ValueError("CPF do contato correto mudou durante a retomada.")
    matching = {
        c["uuid"]
        for c in contacts
        if c["uuid"] not in deleted
        and customer_phones(pair["customer"]) & native_phones(c)
    }
    matching.discard(pair["canonical_uuid"])
    if matching or not customer_phones(pair["customer"]) & native_phones(canonical):
        raise ValueError("Telefone correto deixou de ser único durante a retomada.")
    current = contact_refusals(canonical)
    if any(
        pair["refusals"][k] and not current[k]
        for k in ("lgpd_refused", "marketing_refused", "blocked", "stopped")
    ):
        raise ValueError("Proteção do contato mudou durante a retomada; exige revisão.")
    if (
        current["lgpd_refused"] or current["marketing_refused"]
    ) and "Recompra FarmaLíder" in groups_index(canonical, names):
        raise ValueError("Contato com recusa voltou à Recompra; exige revisão.")


def apply_batch(payload, weni, farm, store, contacts, names, directory):
    """Um lote durável; a primeira prova não é sobrescrita em retomadas."""
    directory = Path(directory)
    state_path = directory / "state.json"
    state = (
        json.loads(state_path.read_text(encoding="utf-8"))
        if state_path.exists()
        else {
            "pairs": payload,
            "deleted": [],
            "skipped": [],
            "delivery_keys": [],
            "done": False,
            "deletions": {},
        }
    )
    if state["pairs"] != payload:
        raise ValueError("Lote diverge do comprovante; preserve a revisão anterior.")
    if state["done"]:
        if set(state["deleted"]) & {c["uuid"] for c in contacts}:
            raise ValueError("Contato excluído reapareceu; execute nova revisão.")
        return set(state["deleted"]), len(state["skipped"])
    layout = store.layout.values()
    if "storage" in state and state["storage"] != layout:
        raise ValueError(
            "Os arquivos Google mudaram desde a exclusão; preserve os comprovantes para revisão."
        )
    state["storage"] = layout
    save_private(state_path, state)
    targets = {p[k] for p in payload for k in ("duplicate_uuid", "canonical_uuid")}
    tables, ledger = store.capture(targets)
    if not (directory / "preflight.json").exists():
        save_private(
            directory / "preflight.json",
            {"tables": tables, "ledger_order_ids": sorted(ledger, key=int)},
        )
    ready, canonicals = [], {}
    for pair in payload:
        uid, cid = pair["duplicate_uuid"], pair["canonical_uuid"]
        canonical, duplicate = exact_contact(weni, cid), exact_contact(weni, uid)
        if canonical is None:
            raise ValueError("Contato correto ausente; limpeza interrompida.")
        populate_group_names([canonical] + ([duplicate] if duplicate else []), names)
        canonicals[cid] = canonical
        if uid in state["deleted"]:
            verify_retired_canonical(
                pair, canonical, contacts, set(state["deleted"]), names
            )
            if duplicate is not None:
                raise ValueError("Contato excluído reapareceu; exige revisão.")
            ready.append(pair)
            continue
        if duplicate is None:
            if state["deletions"].get(uid) != "sending":
                state["skipped"].append(uid)
                continue
            state["deleted"].append(uid)
            verify_retired_canonical(
                pair, canonical, contacts, set(state["deleted"]), names
            )
            save_private(state_path, state)
            ready.append(pair)
            continue
        if state["deletions"].get(uid) == "sending":
            raise RuntimeError(
                "Exclusão anterior sem confirmação; não será repetida automaticamente."
            )
        recorded, owners, reasons = store.context_for(tables, uid, pair["cpf"], ledger)
        fresh_refusals = contact_refusals(duplicate)
        if any(
            fresh_refusals[k] != pair["refusals"][k]
            for k in ("lgpd_refused", "marketing_refused", "blocked", "stopped")
        ):
            reasons.append("RECUSA_OU_BLOQUEIO_MUDOU_APOS_REVISAO")
        try:
            customer = unique_customer(farm, pair["cpf"])
            if customer != pair["customer"]:
                # Requer apenas a identidade estável, não igualdade de campos comerciais voláteis.
                if (
                    customer is None
                    or _positive_id(customer.get("id")) != pair["customer_id"]
                    or customer_phones(customer) != customer_phones(pair["customer"])
                ):
                    reasons.append("CLIENTE_API_MUDOU")
            orders = {oid: farm.get_pedido(oid) for oid in pair["orders"]}
        except FarmaliderError:
            reasons.append("FALHA_API_NA_REVALIDACAO")
            customer, orders = pair["customer"], {}
        related = [
            c
            for c in contacts
            if c["uuid"] not in state["deleted"]
            and c["uuid"] not in (uid, cid)
            and (
                normalize_cpf(get_fields(c).get("cpf")) == pair["cpf"]
                or native_phones(c) & customer_phones(customer or {})
            )
        ] + [canonical, duplicate]
        reasons += validate_pair(
            canonical,
            duplicate,
            customer=customer or {},
            related_contacts=related,
            orders=orders,
            recorded_order_ids=recorded,
            history_owner_ids=owners,
        )
        if (
            set(reasons) - PRESERVABLE
            or not refs(duplicate) <= set(pair["orders"])
            or not recorded <= set(pair["orders"])
        ):
            state["skipped"].append(uid)
            continue
        # Pré-validar todas as escritas antes de excluir esta pessoa.
        requests_for(tables, [pair], {cid: preview_refusals(canonical, pair)})
        canonical = preserve_refusals(weni, canonical, duplicate, names)
        if validate_pair(
            canonical,
            duplicate,
            customer=customer,
            related_contacts=[c for c in related if c["uuid"] != cid] + [canonical],
            orders=orders,
            recorded_order_ids=recorded,
            history_owner_ids=owners,
        ):
            raise ValueError("Proteções não preservadas; exclusão bloqueada.")
        canonicals[cid] = canonical
        proof = {
            "before_duplicate": duplicate,
            "after_canonical": canonical,
            "customer": customer,
            "orders": orders,
            "recorded_order_ids": sorted(recorded, key=int),
            "checked_at": now(),
        }
        proof_path = directory / ("proof-" + uid + "-" + digest(proof)[:12] + ".json")
        save_private(proof_path, proof)
        state.setdefault("proofs", {})[uid] = proof_path.name
        state["deletions"][uid] = "interrupting"
        save_private(state_path, state)
        weni.interrupt_contact_flows(uid)
        recompra_group = groups_index(duplicate, names).get("Recompra FarmaLíder")
        if recompra_group:
            weni.contact_group_action(uid, action="remove", group=recompra_group)
        state["deletions"][uid] = "sending"
        save_private(state_path, state)
        # Mesmo em timeout, uma nova execução somente consulta o resultado.
        try:
            weni.delete_contact(uid)
        except WeniError as exc:
            if (
                exc.status_code is not None
                and 400 <= exc.status_code < 500
                and exc.status_code not in (408, 499)
            ):
                state["deletions"][uid] = "rejected"
                save_private(state_path, state)
            raise
        if exact_contact(weni, uid) is not None:
            raise RuntimeError(
                "A Weni não confirmou a exclusão; aplicação interrompida."
            )
        state["deleted"].append(uid)
        save_private(state_path, state)
        ready.append(pair)
    state["skipped"] = sorted(set(state["skipped"]))
    if ready:
        _, keys = requests_for(tables, ready, canonicals)
        state["delivery_keys"] = sorted(set(state["delivery_keys"]) | keys)
        # Preservar evidência e chaves Delivery antes da primeira retirada de linhas.
        save_private(state_path, state)
        store.cleanup(ready, canonicals, directory)
        from dedup_bi import refresh

        refresh(store, state["deleted"], state["delivery_keys"], directory)
    state["done"] = True
    save_private(state_path, state)
    return set(state["deleted"]), len(state["skipped"])


def run_with_dedup(*, apply, full, quarter, review_path, update):
    """A revisão antecede todas as mutações; a limpeza antecede o checkpoint Weni."""
    from weni_update import _open_farmalider

    settings = load_private_settings()
    removed, skipped = set(), 0
    with execution_lock(DIRECTORY):
        payload = None
        if apply:
            from weni_update import load_review_snapshot

            load_review_snapshot(
                full=full, json_path=Path(review_path), quarter=quarter
            )
            try:
                payload = load_review(review_path, full=full, quarter=quarter)
            except (OSError, ValueError) as exc:
                logging.warning(
                    "Deduplicação sem revisão válida (%s); nenhuma nova exclusão será aplicada. Gere nova revisão na próxima incremental.",
                    type(exc).__name__,
                )
                payload = {
                    "pairs": [],
                    "pending": [{"reason": "REVISAO_DUPLICADOS_PENDENTE"}],
                    "sha256": "sem-novas-exclusoes",
                }
        weni = WeniClient(str(settings.WENI_API_TOKEN), retry_mutations=False)
        farm, store = None, None
        try:
            if not apply:
                cached_contacts = []
                stats = update(set(), cached_contacts)
                contacts = (
                    cached_contacts
                    if cached_contacts
                    else weni.list_contacts(after=None)
                )
                try:
                    names = group_names(weni)
                    populate_group_names(contacts, names)
                    scope, campaign = selected_scope(quarter)
                    farm = _open_farmalider()
                    result = prepare(
                        contacts,
                        farm,
                        lambda: DedupSheets(str(settings.GOOGLE_SPREADSHEET_ID)),
                        scope=scope,
                    )
                except Exception as exc:
                    logging.warning(
                        "Revisão de duplicados pendente (%s); atualização normal preservada para esta carga.",
                        type(exc).__name__,
                    )
                    campaign = ""
                    if quarter is not None:
                        _, campaign = selected_scope(quarter)
                    result = {
                        "pairs": [],
                        "pending": [
                            {
                                "reason": "FALHA_REVISAO_" + type(exc).__name__,
                                "contacts": [],
                            }
                        ],
                    }
                payload = write_review(
                    review_path,
                    result,
                    full=full,
                    quarter=quarter,
                    campaign_id=campaign,
                )
                logging.info(
                    "Duplicados: exclusões propostas=%s; pendências preservadas=%s. Relatório: %s",
                    len(payload["pairs"]),
                    len(payload["pending"]),
                    Path(review_path).with_suffix(".txt"),
                )
                return stats, {
                    "proposed": len(payload["pairs"]),
                    "pending": len(payload["pending"]),
                    "deleted": 0,
                    "skipped": 0,
                }
            if payload is None:
                raise ValueError("Revisão de duplicados ausente.")
            # Retomar lotes incompletos já revisados antes de iniciar lotes novos.
            jobs = []
            for state_file in sorted(DIRECTORY.glob("*/lote-*/state.json")):
                state = json.loads(state_file.read_text(encoding="utf-8"))
                if not state.get("done"):
                    jobs.append((state_file.parent, state["pairs"]))
            root = DIRECTORY / payload["sha256"]
            covered = {p["duplicate_uuid"] for _, pairs in jobs for p in pairs}
            for index in range(0, len(payload["pairs"]), 25):
                folder = root / f"lote-{index // 25:04d}"
                if folder not in {p for p, _ in jobs}:
                    fresh_pairs = [
                        p
                        for p in payload["pairs"][index : index + 25]
                        if p["duplicate_uuid"] not in covered
                    ]
                    if fresh_pairs:
                        jobs.append((folder, fresh_pairs))
                        covered.update(p["duplicate_uuid"] for p in fresh_pairs)
            failed_batches = 0
            # Registrar os lotes revisados antes de abrir conexões. Falhas de
            # serviço não perdem o trabalho pendente da próxima incremental.
            for folder, pairs in jobs:
                path = folder / "state.json"
                if not path.exists():
                    save_private(
                        path,
                        {
                            "pairs": pairs,
                            "deleted": [],
                            "skipped": [],
                            "delivery_keys": [],
                            "done": False,
                            "deletions": {},
                        },
                    )
            if jobs:
                try:
                    store = DedupSheets(str(settings.GOOGLE_SPREADSHEET_ID))
                    farm = _open_farmalider()
                    if farm is None:
                        raise ValueError(
                            "API FarmaLíder indisponível para deduplicação."
                        )
                    contacts = weni.list_contacts(after=None)
                    names = group_names(weni)
                    populate_group_names(contacts, names)
                    with TaskProgress(
                        "Weni: limpeza de duplicados", len(jobs), unit="lotes"
                    ) as progress:
                        for folder, pairs in progress.track(jobs):
                            try:
                                deleted, conflicts = apply_batch(
                                    pairs, weni, farm, store, contacts, names, folder
                                )
                            except Exception as exc:
                                state_path = folder / "state.json"
                                state = json.loads(
                                    state_path.read_text(encoding="utf-8")
                                )
                                state["last_error"] = {
                                    "type": type(exc).__name__,
                                    "at": now(),
                                }
                                save_private(state_path, state)
                                deleted = set(state["deleted"])
                                conflicts = len(pairs) - len(deleted)
                                failed_batches += 1
                                logging.warning(
                                    "Deduplicação: lote pendente (%s); nova conferência na próxima incremental. A carga Weni continuará.",
                                    type(exc).__name__,
                                )
                            removed |= deleted
                            skipped += conflicts
                            contacts = [c for c in contacts if c["uuid"] not in deleted]
                except Exception as exc:
                    failed_batches += len(jobs)
                    logging.warning(
                        "Deduplicação indisponível (%s); lotes preservados para a próxima incremental. A carga Weni continuará.",
                        type(exc).__name__,
                    )
            stats = update(removed, None)
            return stats, {
                "proposed": len(payload["pairs"]),
                "pending": len(payload["pending"]),
                "deleted": len(removed),
                "skipped": skipped,
                "failed_batches": failed_batches,
            }
        finally:
            weni.close()
            if farm is not None:
                farm.close()
            if store is not None:
                store.close()
