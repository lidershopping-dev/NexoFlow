from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from proxy_config import configure_proxy


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Atualiza campos de contatos da Weni com base em mensagens, "
            "flows e confirmação de pedidos da FarmaLíder."
        )
    )
    parser.add_argument(
        "--require-complete",
        action="store_true",
        help="mantido por compatibilidade; falhas isoladas ficam pendentes para a próxima incremental",
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--check-only", action="store_true", help="testa acessos sem alterar dados"
    )
    mode.add_argument(
        "--dry-run", action="store_true", help="monta e audita o plano sem gravar"
    )
    mode.add_argument(
        "--apply", action="store_true", help="audita e aplica as alterações"
    )
    parser.add_argument(
        "--full",
        action="store_true",
        help="processa o histórico completo em vez da janela incremental",
    )
    parser.add_argument(
        "--quarter",
        type=int,
        choices=(1, 2, 3, 4),
        help="na carga completa, processa somente a Parte 1/4, 2/4, 3/4 ou 4/4 da base congelada",
    )
    parser.add_argument(
        "--review-out",
        help="grava snapshot JSON e relatório TXT para revisão humana",
    )
    parser.add_argument(
        "--require-review",
        help=(
            "na carga completa, aplica exatamente o snapshot revisado e "
            "faz controle de conflito por contato"
        ),
    )
    parser.add_argument(
        "--recompra",
        action="store_true",
        help="executa a rotina de recompra, com revisão própria e sem mudar os demais processos",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if args.quarter is not None and not args.full:
        raise SystemExit("--quarter exige --full.")
    if args.recompra:
        if args.quarter is not None:
            raise SystemExit(
                "Recompra não usa partes: membros expirados precisam ser verificados em cada execução."
            )
        from run_recompra import main as recompra_main

        arguments = [
            "--check-only"
            if args.check_only
            else "--dry-run"
            if args.dry_run
            else "--apply"
        ]
        if args.full:
            arguments.append("--full")
        if args.require_complete:
            arguments.append("--require-complete")
        if args.review_out:
            arguments += ["--review-out", args.review_out]
        if args.require_review:
            arguments += ["--require-review", args.require_review]
        return recompra_main(arguments)
    status = configure_proxy()

    from config import WENI_UPDATE_LOG_FILE
    from logging_config import configure_logging
    from weni_update import _invalid_cpf_summary, check_update_connections, run_update

    configure_logging(WENI_UPDATE_LOG_FILE)
    logging.info("NexoFlow Weni iniciado.")
    logging.info("Proxy: %s", status.safe_summary)

    try:
        if args.check_only:
            result = check_update_connections()
            weni = result.get("weni", {})
            farm = result.get("farmalider", {})
            print("\n=== Conexões do NexoFlow Weni ===")
            print("[OK] Weni: contatos e mensagens acessíveis.")
            if weni.get("opt_in_group_found"):
                print("[OK] Grupo Opt-In LGPD localizado.")
            else:
                print("[AVISO] Grupo Opt-In LGPD não foi localizado.")
            if weni.get("opt_out_group_found"):
                print("[OK] Grupo Opt-Out LGPD localizado.")
            else:
                print("[AVISO] Grupo Opt-Out LGPD não foi localizado.")
            missing_fields = list(weni.get("missing_multichannel_fields") or [])
            if missing_fields:
                print(
                    "[ERRO] Campos multicanal ausentes na Weni: "
                    + ", ".join(missing_fields)
                )
                print(
                    "       Crie-os como campos Texto antes de executar o NexoFlow Weni."
                )
                return 1
            print("[OK] Campos multicanal da Weni localizados.")
            missing_order_fields = list(weni.get("missing_order_fields") or [])
            if missing_order_fields:
                print(
                    "[ERRO] Campos de pedido obrigatórios ausentes na Weni: "
                    + ", ".join(missing_order_fields)
                )
                print("       Crie todos os campos antes de executar o NexoFlow Weni.")
                print("       O NexoFlow não grava snapshot parcial de pedido.")
                return 1
            print("[OK] Campos obrigatórios de pedido localizados.")
            missing_google_ads = list(weni.get("missing_google_ads_fields") or [])
            if missing_google_ads:
                print(
                    "[AVISO] Campos de atribuição Google Ads ausentes: "
                    + ", ".join(missing_google_ads)
                )
                print(
                    "        O NexoFlow continuará funcionando, mas a atribuição Google Ads ficará parcial até esses campos existirem."
                )
            else:
                print("[OK] Campos de atribuição Google Ads localizados.")
            if farm.get("ok"):
                print("[OK] FarmaLíder API acessível para confirmação de pedidos.")
            else:
                print(
                    "[AVISO] FarmaLíder indisponível/desabilitada; compras não serão atualizadas."
                )
            return 0

        review_out = Path(args.review_out) if args.review_out else None
        require_review = Path(args.require_review) if args.require_review else None
        from weni_dedup import run_with_dedup

        default_review = (
            Path(f"logs/weni_update_review_full_25_parte_{args.quarter}.json")
            if args.quarter is not None
            else Path(
                "logs/weni_update_review_full.json"
                if args.full
                else "logs/weni_update_review.json"
            )
        )
        if args.dry_run and review_out is None:
            review_out = default_review
        if args.apply and args.full and require_review is None:
            require_review = default_review

        dedup_review = require_review or review_out or default_review
        stats, dedup = run_with_dedup(
            apply=args.apply,
            full=args.full,
            quarter=args.quarter,
            review_path=dedup_review,
            update=lambda removed, contacts: run_update(
                apply=args.apply,
                full=args.full,
                review_out=review_out,
                require_review=require_review,
                quarter=args.quarter,
                deduplicated_uuids=removed,
                dedup_contacts=contacts,
            ),
        )
        print("\n=== Resumo NexoFlow Weni ===")
        if args.quarter is not None:
            print(f"Carga completa 25%: Parte {args.quarter}/4")
        print(f"Mensagens recebidas analisadas: {stats.messages_incoming}")
        print(f"Contatos modificados consultados: {stats.contacts_modified}")
        print(f"Contatos considerados: {stats.contacts_considered}")
        print(f"Contatos com alterações: {stats.plans_with_changes}")
        print(f"Campos propostos: {stats.field_changes}")
        print(f"Ações de grupo: {stats.group_actions}")
        print(f"Pedidos confirmados pela FarmaLíder: {stats.purchase_confirmed}")
        print(
            f"Contatos com pedido validado (inclusive sem alteração): {stats.orders_validated}"
        )
        print(
            f"Contatos com pedido cancelado por pagamento 0: {stats.orders_cancelled_payment_zero}"
        )
        print(
            f"Contatos com pedido pendente de pagamento: {stats.orders_pending_payment}"
        )
        if stats.invalid_cpf_checked:
            print("CPFs inválidos: " + _invalid_cpf_summary(stats))
            print(
                "Contagens por contato, incluindo cancelados; não são vendas nem pedidos únicos."
            )
        if stats.payment_unmapped_codes:
            print(
                "Pagamento não identificado por código: "
                f"{dict(sorted(stats.payment_unmapped_codes.items()))}"
            )
        print(
            f"Duplicados: propostas={dedup['proposed']}; excluídos={dedup['deleted']}; pendências={dedup['pending']}; conflitos={dedup['skipped']}; lotes pendentes={dedup.get('failed_batches', 0)}."
        )
        if args.dry_run:
            print("Modo: DRY-RUN. Nenhuma alteração foi gravada.")
            if review_out is not None:
                print(f"Snapshot de revisão: {review_out}")
                print(f"Relatório humano: {review_out.with_suffix('.txt')}")
                if args.full:
                    print("Revise o relatório antes de autorizar a carga completa.")
        else:
            print(f"Contatos atualizados: {stats.contacts_updated}")
            if stats.contacts_conflicted:
                print(f"Contatos ignorados por conflito: {stats.contacts_conflicted}")
            if stats.contacts_already_satisfied:
                print(
                    f"Contatos já no estado revisado: {stats.contacts_already_satisfied}"
                )
            if args.full and require_review is not None:
                print(
                    "Modo: APPLY SNAPSHOT CONGELADO. Novas mensagens/contatos "
                    "ficam para a próxima execução incremental."
                )
            else:
                print("Modo: APPLY. Alterações auditadas e gravadas.")
        if (
            stats.orders_api_failed
            or stats.contacts_conflicted
            or stats.contacts_failed
            or dedup["skipped"]
            or dedup.get("failed_batches", 0)
        ):
            print(
                "[AVISO] Carga Weni concluída com pendências. Os contatos que falharam serão revalidados na próxima incremental."
            )
        return 0
    except Exception as exc:
        logging.exception("NexoFlow Weni falhou: %s", exc)
        print(f"\n[ERRO] {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
