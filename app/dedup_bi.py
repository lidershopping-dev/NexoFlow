"""Reparo mínimo do BI após deduplicação, preservando os fatos financeiros."""

from __future__ import annotations

import hashlib
import json
import math
from copy import deepcopy
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from uuid import uuid4

from gspread.exceptions import APIError

from analytics import (  # noqa: E402
    BI_CHANNEL_COLUMNS,
    BI_CONVERSION_COLUMNS,
    BI_ORDERS_COLUMNS,
    BI_SUMMARY_COLUMNS,
    build_bi_channel_records,
    build_bi_conversion_records,
    build_bi_order_rows,
    build_bi_summary_record,
)
from dedup_io import save_private
from delivery_tickets import DELIVERY_ORDER_COLUMNS, build_delivery_order_records  # noqa: E402
from google_sheets import GoogleSheetsStore, column_letter  # noqa: E402
from looker_bi import DATE_COLUMNS, SCHEMAS, build_looker_tables, opaque_key  # noqa: E402

SOURCE_TITLES = (
    "Base_Contatos",
    "Pedidos_FarmaLider",
    "Itens_Pedidos_FarmaLider",
    "Tickets_Delivery_Weni",
)
DETAIL_TITLES = (
    "BI_Publico",
    "BI_Clientes",
    "BI_Clientes_Vendas",
    "BI_Itens",
    "BI_Qualidade",
)
TARGET_TITLES = (
    *DETAIL_TITLES,
    "BI_Pedidos",
    "BI_Delivery_Pedidos",
    "BI_Resumo_Diario",
    "BI_Canais",
    "BI_Conversao",
)
KEYS = {
    "BI_Publico": "contato_chave",
    "BI_Clientes": "cliente_chave",
    "BI_Clientes_Vendas": "venda_chave",
    "BI_Itens": "item_chave",
    "BI_Qualidade": None,
    "BI_Pedidos": "pedido_chave",
    "BI_Delivery_Pedidos": "pedido_chave",
    "BI_Resumo_Diario": "data",
    "BI_Canais": "chave",
    "BI_Conversao": "chave",
}
ALLOWED = {
    "BI_Clientes": {"cliente", "compra_pendente_validacao", "atualizado_em"},
    "BI_Clientes_Vendas": {"cliente", "atualizado_em"},
    "BI_Itens": {"atualizado_em"},
    "BI_Pedidos": {
        "cliente",
        "cpf",
        "canal",
        "cidade",
        "estado",
        "cep",
        "bairro",
        "idade",
        "cliente_chave",
        "genero",
        "atualizado_em",
    },
    "BI_Delivery_Pedidos": {
        "vinculo_atendimento",
        "quantidade_atendimentos",
        "atualizado_em",
    },
    "BI_Qualidade": {
        "atualizado_em",
        "contatos_base",
        "contatos_com_idade",
        "cobertura_idade_pct",
        "contatos_com_cidade",
        "contatos_com_genero",
        "anuncios_disponiveis",
        "contatos_com_compra_pendente_validacao",
    },
}
SUMMARY_PROTECTED = {
    name
    for name in BI_SUMMARY_COLUMNS
    if name.startswith(("faturamento", "pedidos", "unidades", "ticket"))
}


def digest(value):
    def canonical(v):
        if isinstance(v, float):
            if not math.isfinite(v):
                raise ValueError("Número não finito no estado a verificar.")
            return int(v) if v.is_integer() else v
        if isinstance(v, list):
            return [canonical(item) for item in v]
        if isinstance(v, dict):
            return {k: canonical(item) for k, item in v.items()}
        return v

    return hashlib.sha256(
        json.dumps(
            canonical(value), ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode()
    ).hexdigest()


def save(path, value):
    save_private(path, value)


def trimmed(values):
    result = [list(r) for r in values]
    for r in result:
        while r and r[-1] in ("", None):
            r.pop()
    while result and not result[-1]:
        result.pop()
    return result


def records(values):
    if not values:
        raise ValueError("Tabela sem cabeçalho.")
    headers = values[0]
    if len(headers) != len(set(headers)) or any(not h for h in headers):
        raise ValueError("Cabeçalho vazio ou duplicado.")
    return [
        {
            h: str(row[i]) if i < len(row) and row[i] is not None else ""
            for i, h in enumerate(headers)
        }
        for row in values[1:]
        if any(v not in ("", None) for v in row)
    ]


def day(value):
    if isinstance(value, (int, float)):
        return (date(1899, 12, 30) + timedelta(days=int(value))).isoformat()
    if isinstance(value, str) and value.isdigit() and 20000 <= int(value) <= 100000:
        return (date(1899, 12, 30) + timedelta(days=int(value))).isoformat()
    return date.fromisoformat(str(value)[:10]).isoformat()


def native_value(header, value):
    if header in DATE_COLUMNS | {"data_snapshot"} and value not in ("", None):
        if isinstance(value, (int, float)):
            return value
        return (date.fromisoformat(str(value)[:10]) - date(1899, 12, 30)).days
    return "" if value is None else value


def equal(header, a, b):
    a, b = native_value(header, a), native_value(header, b)
    if a == b:
        return True
    # A API retorna int/float para células numéricas; não converter texto de
    # nomes/IDs arbitrários nem ignorar diferenças monetárias pequenas.
    if isinstance(a, (int, float)) or isinstance(b, (int, float)):
        try:
            return Decimal(str(a)) == Decimal(str(b))
        except InvalidOperation:
            return False
    return False


def removed_set(payload):
    if isinstance(payload, dict):
        payload = payload.get("removed_uuids", payload.get("pairs", []))
    return {str(p if isinstance(p, str) else p["duplicate_uuid"]) for p in payload}


def prepare_targets(
    sources, current, controls, *, removed_uuids, updated_at, snapshot_date=None
):
    """Builder puro. Não altera objetos recebidos nem usa rede/arquivo."""
    base, orders, items, tickets = (records(sources[title]) for title in SOURCE_TITLES)
    for rows in (base, orders, items, tickets):
        if any(r.get("contact_uuid") in removed_uuids for r in rows):
            raise ValueError(
                "UUID retirado ainda consta em uma fonte; conclua a limpeza antes do BI."
            )
    for rows, key in (
        (base, "contact_uuid"),
        (orders, "pedido_chave"),
        (items, "item_chave"),
    ):
        keys = [r[key] for r in rows if r.get(key)]
        if len(keys) != len(set(keys)):
            raise ValueError("Fonte com chave duplicada; reparo interrompido.")
    # Preservar o dia da última fotografia já publicada: higiene não é carga.
    summary_header = current["BI_Resumo_Diario"][0]
    pos = summary_header.index("data")
    previous_days = [
        day(r[pos])
        for r in current["BI_Resumo_Diario"][1:]
        if len(r) > pos and r[pos] not in ("", None)
    ]
    chosen_day = snapshot_date or max(previous_days)
    if chosen_day not in previous_days:
        raise ValueError("A higiene só pode corrigir uma fotografia já existente.")
    previous_quality = records(current["BI_Qualidade"])
    history_start = controls.get("historico_observado_desde") or (
        previous_quality[0].get("historico_observado_desde", "")
        if previous_quality
        else ""
    )
    try:
        history_start = day(history_start) if history_start else ""
    except ValueError:
        raise ValueError(
            "Início histórico inválido; preserve a fonte para revisão."
        ) from None
    looker = build_looker_tables(
        base,
        orders,
        items,
        snapshot_date=chosen_day,
        updated_at=updated_at,
        history_observed_since=history_start,
    )
    schemas = {title: SCHEMAS[title] for title in DETAIL_TITLES}
    generated = {title: looker[title] for title in DETAIL_TITLES}
    schemas.update(
        {
            "BI_Pedidos": BI_ORDERS_COLUMNS,
            "BI_Delivery_Pedidos": DELIVERY_ORDER_COLUMNS,
            "BI_Resumo_Diario": BI_SUMMARY_COLUMNS,
            "BI_Canais": BI_CHANNEL_COLUMNS,
            "BI_Conversao": BI_CONVERSION_COLUMNS,
        }
    )
    generated["BI_Pedidos"] = build_bi_order_rows(
        base, orders, items, updated_at=updated_at
    )
    delivery = build_delivery_order_records(tickets, orders, updated_at=updated_at)
    generated["BI_Delivery_Pedidos"] = [
        [r.get(h, "") for h in DELIVERY_ORDER_COLUMNS] for r in delivery
    ]
    summary = build_bi_summary_record(
        base, orders, items, snapshot_date=chosen_day, updated_at=updated_at
    )
    generated["BI_Resumo_Diario"] = [[summary[h] for h in BI_SUMMARY_COLUMNS]]
    generated["BI_Canais"] = [
        [r.get(h, "") for h in BI_CHANNEL_COLUMNS]
        for r in build_bi_channel_records(
            base, snapshot_date=chosen_day, updated_at=updated_at
        )
    ]
    generated["BI_Conversao"] = [
        [r.get(h, "") for h in BI_CONVERSION_COLUMNS]
        for r in build_bi_conversion_records(
            base, orders, snapshot_date=chosen_day, updated_at=updated_at
        )
    ]
    targets = {}
    for title, rows in generated.items():
        expected, actual = schemas[title], current[title][0]
        if len(expected) != len(actual) or set(actual) != set(expected):
            raise ValueError(f"Schema incompatível em {title}.")
        typed = GoogleSheetsStore._typed_bi_rows(expected, rows)
        mapped = [{h: row[i] for i, h in enumerate(expected)} for row in typed]
        targets[title] = [actual] + [[r[h] for h in actual] for r in mapped]
    return targets, chosen_day


def diff_table(
    title, before, desired, *, removed_uuids, allowed_delivery_removals=frozenset()
):
    """Plano mínimo e validação financeira por chave, com índices físicos."""
    headers, key = before[0], KEYS[title]
    key_col = headers.index(key) if key else None

    def index(values):
        result = {}
        for rowno, row in enumerate(values[1:], 2):
            if not any(v not in ("", None) for v in row):
                continue
            padded = row + [""] * (len(headers) - len(row))
            identity = native_value(key, padded[key_col]) if key else "single"
            if identity in result or not identity:
                raise ValueError(f"Chave vazia/duplicada em {title}.")
            result[identity] = (rowno, padded[: len(headers)])
        return result

    old, new = index(before), index(desired)
    additions = set(new) - set(old)
    removals = set(old) - set(new)
    snapshots = title in {"BI_Resumo_Diario", "BI_Canais", "BI_Conversao"}
    if snapshots:
        # Desired contém somente o dia do corte; manter todas as outras datas.
        removals = set()
    if additions:
        raise ValueError(
            f"Novas chaves em {title}; confirme uma carga separada, não higiene."
        )
    if removals and title == "BI_Publico":
        allowed = {opaque_key("contato", uid) for uid in removed_uuids}
        if not removals <= allowed:
            raise ValueError("BI_Publico contém remoção sem UUID comprovado.")
    elif removals and title == "BI_Delivery_Pedidos":
        if not removals <= set(allowed_delivery_removals):
            raise ValueError(
                "Remoção Delivery sem pedido comprovadamente ligado aos tickets retirados."
            )
    elif removals:
        raise ValueError(f"Remoção de fatos financeiros em {title} bloqueada.")
    updates = []
    final = deepcopy(before)
    for identity in new:
        rowno, previous = old[identity]
        proposed = new[identity][1]
        changed = {
            h
            for h, a, b in zip(headers, previous, proposed, strict=True)
            if h != "atualizado_em" and not equal(h, a, b)
        }
        if not changed:
            continue
        allowed = ALLOWED.get(title)
        if title == "BI_Resumo_Diario" and changed & SUMMARY_PROTECTED:
            raise ValueError(
                "Valores financeiros do resumo mudariam; higiene interrompida."
            )
        if allowed is not None and changed - allowed:
            raise ValueError(
                f"Campos fora do reparo de identidade mudariam em {title}: {sorted(changed - allowed)}"
            )
        # Atualizar apenas células alteradas e o marcador de atualização dessa
        # linha. Preservar números, fórmulas e formatos das demais células.
        fields = changed | ({"atualizado_em"} if "atualizado_em" in headers else set())
        for h in fields:
            col = headers.index(h)
            value = proposed[col]
            updates.append(
                {"row": rowno, "column": col + 1, "header": h, "value": value}
            )
            while len(final[rowno - 1]) <= col:
                final[rowno - 1].append("")
            final[rowno - 1][col] = value
    deletions = sorted((old[k][0] for k in removals), reverse=True)
    for rowno in deletions:
        del final[rowno - 1]
    return {
        "updates": updates,
        "deletions": deletions,
        "before": before,
        "after": final,
        "before_hash": digest(trimmed(before)),
        "after_hash": digest(trimmed(final)),
    }


def cell_value(value):
    if isinstance(value, bool):
        return {"boolValue": value}
    if isinstance(value, (float, int)):
        if not math.isfinite(value):
            raise ValueError("Número não finito.")
        return {"numberValue": value}
    return {"stringValue": str(value)}


def requests_for(plan):
    requests = []
    for table in plan["tables"].values():
        for update in table["updates"]:
            requests.append(
                {
                    "updateCells": {
                        "range": {
                            "sheetId": table["sheet_id"],
                            "startRowIndex": update["row"] - 1,
                            "endRowIndex": update["row"],
                            "startColumnIndex": update["column"] - 1,
                            "endColumnIndex": update["column"],
                        },
                        "rows": [
                            {
                                "values": [
                                    {"userEnteredValue": cell_value(update["value"])}
                                ]
                            }
                        ],
                        "fields": "userEnteredValue",
                    }
                }
            )
    for table in plan["tables"].values():
        for row in table["deletions"]:
            requests.append(
                {
                    "deleteDimension": {
                        "range": {
                            "sheetId": table["sheet_id"],
                            "dimension": "ROWS",
                            "startIndex": row - 1,
                            "endIndex": row,
                        }
                    }
                }
            )
    return requests


def read_values(ws, *, formatted=False):
    width = column_letter(ws.col_count)
    values = []
    batches = max(1, (ws.row_count + 3999) // 4000)
    print(
        f"BI: leitura {ws.title} | lotes={batches} | linhas_alocadas={ws.row_count}",
        flush=True,
    )
    for batch_number, first in enumerate(range(1, ws.row_count + 1, 4000), 1):
        last = min(ws.row_count, first + 3999)
        chunk = ws.get(
            f"A{first}:{width}{last}",
            value_render_option="FORMATTED_VALUE" if formatted else "UNFORMATTED_VALUE",
        )
        values.extend(
            [list(row) for row in chunk]
            + [[] for _ in range(last - first + 1 - len(chunk))]
        )
        if batch_number == 1 or batch_number % 3 == 0 or batch_number == batches:
            print(
                f"BI: leitura {ws.title} | lote={batch_number}/{batches} | linhas_lidas={last}",
                flush=True,
            )
    return trimmed(values)


def read_state(hub, operations, layout):
    print("BI: conferindo metadados e destinos de armazenamento.", flush=True)
    hub_sheets = {ws.title: ws for ws in hub.worksheets()}
    ops_sheets = {ws.title: ws for ws in operations.worksheets()}
    routing = (
        hub_sheets["NexoFlow_Arquivos"].get("A1:B5")
        if "NexoFlow_Arquivos" in hub_sheets
        else None
    )
    from sheets_storage import SheetsStorage

    if (
        routing is not None
        and SheetsStorage.from_values(routing, layout.dashboard) != layout
    ):
        raise ValueError("Destinos de armazenamento divergentes.")
    controls = read_values(ops_sheets["Controle"])
    sources = {
        title: read_values(ops_sheets[title], formatted=True) for title in SOURCE_TITLES
    }
    current = {title: read_values(hub_sheets[title]) for title in TARGET_TITLES}
    return sources, current, controls, hub_sheets


def backup_native(hub, tables, backup_dir):
    for title, table in tables.items():
        rows = sorted({x["row"] for x in table["updates"]} | set(table["deletions"]))
        ranges = [
            f"'{title}'!A{row}:{column_letter(len(table['before'][0]))}{row}"
            for row in rows
        ]
        if ranges:
            print(
                f"BI: backup nativo {title} | linhas={len(rows)} | lotes={(len(ranges) + 39) // 40}",
                flush=True,
            )
        for start in range(0, len(ranges), 40):
            response = hub.fetch_sheet_metadata(
                params={
                    "ranges": ranges[start : start + 40],
                    "includeGridData": True,
                    "fields": "sheets(properties(sheetId,title),data(startRow,startColumn,rowData(values(userEnteredValue,userEnteredFormat,dataValidation))))",
                }
            )
            save(backup_dir / f"native-{title}-{start}.json", response)
            # A escrita prevista usa valores, nunca substitui fórmulas.
            formulas = set()
            for sheet in response.get("sheets", []):
                for block in sheet.get("data", []):
                    for ri, row in enumerate(
                        block.get("rowData", []), block.get("startRow", 0) + 1
                    ):
                        for ci, cell in enumerate(
                            row.get("values", []), block.get("startColumn", 0) + 1
                        ):
                            if "formulaValue" in cell.get("userEnteredValue", {}):
                                formulas.add((ri, ci))
            if any((u["row"], u["column"]) in formulas for u in table["updates"]):
                raise ValueError(
                    f"Fórmula encontrada em célula a editar de {title}; revisão necessária."
                )


def refresh(store, removed_uuids, delivery_keys, directory):
    """Cada plano é imutável; reenvio de transação incerta nunca é automático."""
    directory = Path(directory)
    path, receipt_path = directory / "bi_plan.json", directory / "bi_receipt.json"
    hub = store.hub
    operations = store.books[store.layout.operations]
    sources, current, controls, handles = read_state(hub, operations, store.layout)
    receipt = (
        json.loads(receipt_path.read_text(encoding="utf-8"))
        if receipt_path.exists()
        else {}
    )
    if path.exists():
        plan = json.loads(path.read_text(encoding="utf-8"))
        if (
            plan["hub"] != store.layout.dashboard
            or plan["operations"] != store.layout.operations
        ):
            raise ValueError("Plano BI pertence a outros arquivos.")
        changed = (
            digest(sources) != plan["sources_hash"]
            or digest(controls) != plan["controls_hash"]
        )
        if changed:
            if receipt.get("state") == "sending":
                if all(
                    digest(trimmed(current[t])) == plan["tables"][t]["after_hash"]
                    for t in TARGET_TITLES
                ):
                    save(receipt_path, {"state": "verified", "plan_hash": digest(plan)})
                    return
                raise ValueError(
                    "Fontes mudaram e o envio BI anterior não foi confirmado; manter pendente."
                )
            # Nenhum envio incerto: uma carga posterior pode ter atualizado o BI.
            # Arquivar provas antigas e preparar índices novos a partir da leitura atual.
            archive = directory / ("bi_previous_" + uuid4().hex)
            archive.mkdir(parents=True, mode=0o700)
            for previous in [
                path,
                receipt_path,
                directory / "bi_sources.json",
                *directory.glob("native-*.json"),
            ]:
                if previous.exists():
                    previous.replace(archive / previous.name)
            receipt = {}
    if not path.exists():
        if receipt:
            raise ValueError("Comprovante BI sem plano; preservar para revisão.")
        mapping = {r[0]: str(r[1]) for r in controls[1:] if len(r) >= 2}
        now = datetime.now(UTC).isoformat(timespec="seconds")
        desired, cutoff = prepare_targets(
            sources, current, mapping, removed_uuids=set(removed_uuids), updated_at=now
        )
        plan = {
            "hub": store.layout.dashboard,
            "operations": store.layout.operations,
            "snapshot_date": cutoff,
            "sources_hash": digest(sources),
            "controls_hash": digest(controls),
            "tables": {},
        }
        for title in TARGET_TITLES:
            table = diff_table(
                title,
                current[title],
                desired[title],
                removed_uuids=set(removed_uuids),
                allowed_delivery_removals=frozenset(delivery_keys),
            )
            table["sheet_id"] = handles[title].id
            plan["tables"][title] = table
        save(directory / "bi_sources.json", {"sources": sources, "Controle": controls})
        backup_native(hub, plan["tables"], directory)
        save(path, plan)
    hashes = {t: digest(trimmed(current[t])) for t in TARGET_TITLES}
    if all(hashes[t] == plan["tables"][t]["after_hash"] for t in TARGET_TITLES):
        save(receipt_path, {"state": "verified", "plan_hash": digest(plan)})
        return
    if receipt.get("state") == "sending":
        raise RuntimeError(
            "Resultado BI anterior incerto; reenvio de exclusões bloqueado."
        )
    if any(
        hashes[t] != plan["tables"][t]["before_hash"]
        or handles[t].id != plan["tables"][t]["sheet_id"]
        for t in TARGET_TITLES
    ):
        raise ValueError("BI mudou; índices antigos não serão reutilizados.")
    requests = requests_for(plan)
    if len(json.dumps(requests, ensure_ascii=False).encode()) > 1_500_000:
        raise ValueError(
            "Lote BI excede 1,5 MB; preserve os comprovantes para revisão."
        )
    if digest(read_values(operations.worksheet("Controle"))) != plan["controls_hash"]:
        raise ValueError(
            "Controle mudou antes da gravação do BI; carga simultânea bloqueada."
        )
    save(receipt_path, {"state": "sending", "plan_hash": digest(plan)})
    if requests:
        try:
            hub.batch_update({"requests": requests})
        except APIError as exc:
            if (
                400 <= exc.response.status_code < 500
                and exc.response.status_code not in (408, 499)
            ):
                save(receipt_path, {"state": "rejected", "plan_hash": digest(plan)})
            raise
    handles = {w.title: w for w in hub.worksheets()}
    for title in TARGET_TITLES:
        if (
            digest(trimmed(read_values(handles[title])))
            != plan["tables"][title]["after_hash"]
        ):
            raise RuntimeError(
                "BI divergiu após a limpeza. Preserve o comprovante; não repita índices."
            )
    if digest(read_values(operations.worksheet("Controle"))) != plan["controls_hash"]:
        raise RuntimeError(
            "Controle mudou durante a limpeza; confira cargas simultâneas."
        )
    save(receipt_path, {"state": "verified", "plan_hash": digest(plan)})
