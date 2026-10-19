"""NexoFlow 1.0: pedidos de clientes de atendimentos marcados no Weni Chats."""

from __future__ import annotations

import argparse
import logging
import os
import shutil
import tempfile
from datetime import UTC, datetime
from pathlib import Path


def _validate_review_destination(path: Path, *, archive_existing: bool) -> None:
    if path.exists() and not archive_existing:
        from weni_update import PlanAuditError

        raise PlanAuditError(
            "Já existe uma revisão Delivery. Para retomar, use --apply --require-review com esse arquivo. "
            "Para gerar outra revisão preservando a anterior, acrescente --archive-review ao --dry-run."
        )


def _archive_delivery_review(path: Path) -> Path:
    """Preserva a revisão anterior antes de substituir qualquer arquivo."""
    archive_root = path.parent / "delivery_historico"
    archive_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    archive = Path(
        tempfile.mkdtemp(
            prefix=f"{path.stem}_{datetime.now(UTC):%Y%m%dT%H%M%SZ}_",
            dir=archive_root,
        )
    )
    for source in (
        path,
        path.with_suffix(".txt"),
        path.parent / "privado" / f"{path.stem}_conflitos.json",
    ):
        if source.exists():
            copied = archive / source.name
            shutil.copy2(source, copied)
            if os.name != "nt":
                copied.chmod(0o600)
    return archive


def main():
    parser = argparse.ArgumentParser(
        description="Reconcilia as tags Pedido, Pedido is deleted (legada, inclusive com data de exclusão) e Pedido finalizado do Weni Chats com a FarmaLíder."
    )
    parser.add_argument(
        "--require-complete",
        action="store_true",
        help="interrompe o fluxo se houver consultas de pedido incompletas",
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--dry-run",
        action="store_true",
        help="consulta todo o histórico encerrado e gera revisão, sem gravar nas integrações",
    )
    mode.add_argument(
        "--apply",
        action="store_true",
        help="aplica o snapshot revisado na Weni e no Google Sheets",
    )
    parser.add_argument(
        "--review-out", type=Path, default=Path("logs/delivery_review.json")
    )
    parser.add_argument(
        "--require-review", type=Path, help="snapshot Delivery previamente revisado"
    )
    parser.add_argument(
        "--archive-review",
        action="store_true",
        help="preserva a revisão anterior antes de gerar uma nova; somente com --dry-run",
    )
    parser.add_argument(
        "--planilhas",
        action="store_true",
        help="usa Conversas_Delivery.xlsx e Contatos_Delivery.xlsx, sem token do Chats",
    )
    parser.add_argument(
        "--import-dir",
        type=Path,
        default=Path("importacoes"),
        help="pasta dos dois exports XLSX",
    )
    parser.add_argument(
        "--target",
        choices=["weni", "analytics", "both"],
        help="destino da carga; fica congelado no relatório de revisão",
    )
    args = parser.parse_args()
    if args.apply and args.require_review is None:
        parser.error("--apply exige --require-review com o snapshot revisado.")
    if args.archive_review and not args.dry_run:
        parser.error("--archive-review exige --dry-run.")
    if os.name == "nt":
        from proxy_config import configure_proxy

        configure_proxy()
    import secrets_config as secrets
    from delivery_tickets import (
        apply_delivery_review,
        reconcile_tickets,
        select_tickets,
        write_delivery_review,
    )
    from google_sheets import GoogleSheetsStore
    from logging_config import configure_logging
    from weni_chats_client import WeniChatsClient
    from weni_client import WeniClient
    from weni_update import _available_weni_fields, _open_farmalider

    configure_logging(Path("logs/nexoflow_delivery.log"))
    weni, chats, farm = None, None, None
    try:
        if args.dry_run:
            _validate_review_destination(
                args.review_out, archive_existing=args.archive_review
            )
        if not getattr(secrets, "WENI_API_TOKEN", ""):
            raise RuntimeError("Token da Weni não configurado.")
        imported = None
        cutoff = datetime.now(UTC)
        if args.dry_run and args.planilhas:
            from delivery_spreadsheets import read_delivery_spreadsheets

            imported = read_delivery_spreadsheets(args.import_dir, cutoff=cutoff)
        elif args.dry_run:
            # Validate Chats configuration before listing the entire Weni base or querying FarmaLíder.
            chats = WeniChatsClient(
                str(getattr(secrets, "WENI_CHATS_API_BASE_URL", "")),
                str(getattr(secrets, "WENI_CHATS_API_TOKEN", "")),
            )
        # A timeout can follow a committed group removal. Resume from the
        # reviewed snapshot after reading the current state; never repeat POST.
        weni = WeniClient(secrets.WENI_API_TOKEN, retry_mutations=False)
        if args.apply:
            spreadsheet_id = str(getattr(secrets, "GOOGLE_SPREADSHEET_ID", ""))

            def store_factory():
                if not spreadsheet_id:
                    raise RuntimeError("ID da planilha Google não configurado.")
                return GoogleSheetsStore(spreadsheet_id)

            result = apply_delivery_review(
                args.require_review,
                weni,
                store_factory=store_factory,
                target=args.target,
            )
            if result["conflicts"]:
                print("[AVISO] Reconciliação parcial:", result)
                if result.get("conflict_report"):
                    print(
                        "[INFO] Diagnóstico privado dos conflitos:",
                        result["conflict_report"],
                    )
                print(
                    "[INFO] Contatos aceitos foram preservados. Retome o mesmo snapshot pelo menu de planilhas; "
                    "divergências persistentes exigem nova revisão."
                )
                return 3
            print("[OK] Reconciliação aplicada:", result)
            return 0
        farm = _open_farmalider()
        if farm is None:
            raise RuntimeError(
                "FarmaLíder deve estar configurada para confirmar os pedidos."
            )
        if imported is not None:
            from delivery_spreadsheets import validate_current_contacts

            tickets, _, import_report = imported
            contacts = weni.list_contacts() if tickets else []
            tickets, contacts, rejected = validate_current_contacts(
                tickets,
                contacts,
                refresh_contact=lambda uid: weni.get_contact(uuid=uid),
            )
            import_report["current_contact_rejections"] = rejected
            logging.info(
                "Delivery planilhas: %s; exclusões atuais=%s.",
                import_report["summary"],
                rejected,
            )
        else:
            if chats is None:
                raise RuntimeError(
                    "Configure a API de atendimentos antes de continuar."
                )
            rooms = chats.list_closed_rooms(cutoff=cutoff)
            tickets, rejected = select_tickets(rooms, cutoff=cutoff)
            contacts = weni.list_contacts() if tickets else []
        logging.info(
            "Delivery: tickets selecionados=%s; exclusões por motivo=%s.",
            len(tickets),
            rejected,
        )
        available = _available_weni_fields(weni)
        plans, delivery = reconcile_tickets(
            tickets,
            contacts,
            farm,
            captured_at=cutoff.isoformat(),
            available_fields=available,
            order_id_field=""
            if imported is not None
            else str(getattr(secrets, "WENI_CHATS_ORDER_ID_FIELD", "")),
        )
        delivery["selection_rejections"] = rejected
        delivery["target"] = args.target or "both"
        if imported is not None:
            delivery["spreadsheet_import"] = import_report
        _validate_review_destination(
            args.review_out, archive_existing=args.archive_review
        )
        if args.review_out.exists():
            archive = _archive_delivery_review(args.review_out)
            print("[INFO] Revisão anterior preservada em", archive)
        write_delivery_review(plans, delivery, args.review_out)
        if args.require_complete and any(
            str(row.get("resultado_validacao", "")).startswith("ERRO_")
            for row in delivery["tickets"]
        ):
            print(
                "[AVISO] Consulta de pedidos parcial. Nenhuma aplicação foi autorizada pelo fluxo completo."
            )
            return 3
        print(f"[OK] Revisão gerada em {args.review_out.with_suffix('.txt')}.")
        print(
            f"Tickets: {len(tickets)} | clientes: {len(plans)} | pedidos únicos: {len(delivery['orders'])}."
        )
        print(
            "[INFO] Nenhuma gravação na Weni ou no Google Sheets. Revise o relatório antes de aplicar."
        )
        print(
            "[INFO] Sem ID de pedido no ticket, a existência de pedido do cliente não comprova atribuição ao atendimento."
        )
        return 0
    except Exception as exc:
        # SDK errors can contain private/authenticated URLs; never print the original exception text.
        from delivery_spreadsheets import DeliverySpreadsheetError
        from weni_chats_client import WeniChatsError
        from weni_client import WeniError
        from weni_update import PlanAuditError

        if isinstance(
            exc, (WeniChatsError, DeliverySpreadsheetError, WeniError, PlanAuditError)
        ):
            logging.error("%s", exc)
        else:
            logging.error(
                "Reconciliação Delivery interrompida (%s). Confira as configurações e o relatório privado; uma aplicação parcial deve ser retomada com o mesmo snapshot.",
                type(exc).__name__,
            )
        return 1
    finally:
        for client in (weni, chats, farm):
            if client is not None:
                client.close()


if __name__ == "__main__":
    raise SystemExit(main())
