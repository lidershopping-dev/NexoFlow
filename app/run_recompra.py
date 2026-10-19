"""Rotina incremental de recompra, sempre com revisão antes dos POSTs."""

import argparse
import json
import logging
from pathlib import Path

from requests import RequestException


def build_parser():
    from config import FARMALIDER_MAX_WORKERS

    parser = argparse.ArgumentParser(
        description="NexoFlow 1.0 — Recompra FarmaLíder D+25"
    )
    parser.add_argument(
        "--require-complete",
        action="store_true",
        help="interrompe o fluxo se houver consultas de pedido incompletas",
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--check-only",
        action="store_true",
        help="audita campos, grupos, campanha e fluxo; somente GET",
    )
    mode.add_argument(
        "--dry-run",
        action="store_true",
        help="consulta clientes e propõe alterações sem gravar na Weni",
    )
    mode.add_argument(
        "--apply", action="store_true", help="revalida e aplica o snapshot revisado"
    )
    mode.add_argument(
        "--check-review",
        action="store_true",
        help="confere integridade e dia da revisão local, sem acessar APIs",
    )
    mode.add_argument(
        "--diagnose-state",
        action="store_true",
        help="resume estado, pendências e auditoria sem acessar APIs nem gravar arquivos",
    )
    parser.add_argument(
        "--full",
        action="store_true",
        help="auditoria de toda a base; mesmas regras do incremental, sem retroativos",
    )
    parser.add_argument("--contact", help="UUID Weni para auditoria de um contato")
    parser.add_argument(
        "--workers",
        type=int,
        choices=range(1, FARMALIDER_MAX_WORKERS + 1),
        default=FARMALIDER_MAX_WORKERS,
        help="consultas FarmaLíder simultâneas no dry-run; mantém o limitador global da API",
    )
    parser.add_argument(
        "--review-out", type=Path, default=Path("logs/recompra_review.json")
    )
    parser.add_argument(
        "--require-review", type=Path, help="snapshot que foi revisado antes do apply"
    )
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if (args.apply or args.check_review) and not args.require_review:
        parser.error("--apply e --check-review exigem --require-review.")
    from recompra import (
        REVIEW_EXPIRED_EXIT_CODE,
        ReviewExpiredError,
        diagnose_state,
        ensure_review_current,
        finalize_review,
        now_local,
        read_review,
        safe_error_frames,
    )

    if args.diagnose_state:
        try:
            print(json.dumps(diagnose_state(), ensure_ascii=False, indent=2))
            return 0
        except (RuntimeError, OSError, ValueError) as exc:
            print("[ERRO] " + str(exc))
            return 1
    review = None
    if args.apply or args.check_review:
        try:
            review = read_review(args.require_review)
            ensure_review_current(review, now_local())
        except ReviewExpiredError as exc:
            print("[REVISÃO EXPIRADA] " + str(exc))
            return REVIEW_EXPIRED_EXIT_CODE
        except Exception as exc:
            print(
                "[ERRO] "
                + (
                    str(exc)
                    if isinstance(exc, RuntimeError)
                    else "Não foi possível validar a revisão. Gere um novo dry-run."
                )
            )
            return 1
        if args.check_review:
            print(
                "[OK] Integridade e dia da revisão conferidos: " + review["day"] + "."
            )
            return 0
    from proxy_config import configure_proxy

    configure_proxy()
    from logging_config import configure_logging

    configure_logging(Path("logs/nexoflow_recompra.log"))
    import secrets_config as secrets
    from recompra import (
        STATE,
        apply_review,
        build_plans,
        inspect_structure,
        load_state,
        make_review,
        now_local,
        read_review,
        write_review,
    )
    from weni_client import WeniClient
    from weni_update import _open_farmalider

    weni = None
    farm = None
    try:
        if not getattr(secrets, "WENI_API_TOKEN", ""):
            raise RuntimeError("Token Weni não configurado.")
        # Uma resposta perdida de POST não autoriza repetir inclusão nem
        # atravessar a virada do dia em uma tentativa interna do cliente.
        weni = WeniClient(secrets.WENI_API_TOKEN, retry_mutations=False)
        structure = inspect_structure(weni)
        for error in structure["errors"]:
            print("[PENDÊNCIA] " + error)
        print(
            "[INFO] Diagnóstico somente leitura: grupo, evento +25 dias, campos, fluxo e fuso."
        )
        if args.check_only:
            return 1 if structure["errors"] else 0
        farm = _open_farmalider()
        if farm is None:
            raise RuntimeError(
                "FarmaLíder precisa estar disponível para confirmar a compra."
            )
        if args.apply:
            result = apply_review(review, weni, farm)
            print("[OK] Recompra: " + str(result))
            return 3 if result.get("conflitos") else 0
        state = load_state(STATE)
        now = now_local()
        if args.contact:
            contact = weni.get_contact(uuid=args.contact)
            if not contact:
                raise RuntimeError("Contato não localizado.")
            contacts = [contact]
        else:
            # O relógio e a API FarmaLíder podem mudar sem modified_on na Weni.
            # Uma varredura de contatos é necessária para detectar D+26, novas
            # compras e recusas em todos os membros, mesmo no incremental.
            contacts = weni.iter_contacts()
        pagination = {}
        plans = build_plans(
            contacts,
            farm,
            structure,
            state,
            now=now,
            workers=args.workers,
            pagination_stats=pagination,
        )
        review = make_review(plans, structure, now=now, state=state)
        review["pagination"] = pagination
        # Os planos usam o relógio capturado antes da varredura. Não publicar
        # esses planos sob a data nova se a geração atravessou a meia-noite.
        ensure_review_current(review, now_local())
        finalize_review(review, now_local())
        write_review(review, args.review_out)
        ensure_review_current(review, now_local())
        if args.require_complete and any(
            str(plan.get("reason", "")).startswith("ERRO_")
            or str(plan.get("order_resolution_status", "")).startswith("ERRO_")
            for plan in plans
        ):
            print(
                "[AVISO] Recompra com consultas incompletas. O fluxo completo foi interrompido antes da aplicação."
            )
            return 3
        print(
            "[OK] Dry-run concluído; nenhuma gravação na Weni. Revise "
            + str(args.review_out.with_suffix(".txt"))
        )
        print(
            "[INFO] Grupo: 0 a 25 dias. Envio: exclusivamente pela campanha Weni no D+25."
        )
        return 1 if structure["errors"] else 0
    except ReviewExpiredError as exc:
        logging.warning("Recompra: %s", exc)
        print("[REVISÃO EXPIRADA] " + str(exc))
        return REVIEW_EXPIRED_EXIT_CODE if exc.allow_refresh else 1
    except Exception as exc:
        logging.error(
            "Recompra falhou: tipo=%s; errno=%s; winerror=%s; origem=%s.",
            type(exc).__name__,
            getattr(exc, "errno", None),
            getattr(exc, "winerror", None),
            " > ".join(safe_error_frames(exc)),
        )
        print(
            "[ERRO] "
            + (
                "Falha ao acessar um arquivo local da Recompra "
                f"({type(exc).__name__}, errno={getattr(exc, 'errno', None)}, "
                f"winerror={getattr(exc, 'winerror', None)}). "
                "Verifique as permissões da pasta logs e se há bloqueio por outro processo "
                "ou antivírus. Preserve o estado e a auditoria; gere uma nova revisão "
                "antes de retomar."
                if isinstance(exc, OSError) and not isinstance(exc, RequestException)
                else str(exc)
                if isinstance(exc, (RuntimeError, ValueError))
                else "Falha de API. Consulte o diagnóstico seguro."
            )
        )
        return 1
    finally:
        if farm:
            farm.close()
        if weni:
            weni.close()


if __name__ == "__main__":
    raise SystemExit(main())
