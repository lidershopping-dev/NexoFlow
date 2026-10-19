from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

ERRORS: list[str] = []


def fail(message: str) -> None:
    ERRORS.append(message)
    print(f"[ERRO] {message}")


def ok(message: str) -> None:
    print(f"[OK] {message}")


def read(path: str) -> str:
    return (
        (ROOT / path)
        .read_text(encoding="utf-8")
        .replace("comandos\\", "comandos/")
        .replace("app\\", "app/")
    )


def ico_images(path: Path) -> list[bytes]:
    data = path.read_bytes()
    if data[:4] != b"\x00\x00\x01\x00":
        return []
    count = int.from_bytes(data[4:6], "little")
    images: list[bytes] = []
    for index in range(count):
        entry = 6 + (16 * index)
        size = int.from_bytes(data[entry + 8 : entry + 12], "little")
        offset = int.from_bytes(data[entry + 12 : entry + 16], "little")
        images.append(data[offset : offset + size])
    return images


def numeric_config(config_text: str, name: str) -> float | None:
    match = re.search(
        rf"{re.escape(name)}(?:\s*:\s*Final)?\s*=\s*([0-9.]+)",
        config_text,
    )
    return float(match.group(1)) if match else None


def check_farmalider_runtime() -> None:
    config_text = read("app/config.py")
    client_text = read("app/farmalider_client.py")
    main_text = read("app/main.py")
    proxy_text = read("app/proxy_config.py")
    weni_text = read("app/weni_client.py")
    sheets_text = read("app/google_sheets.py")
    analytics_text = read("app/analytics.py")
    weni_update_text = read("app/weni_update.py")
    run_update_text = read("app/run_update_weni.py")

    delay = numeric_config(config_text, "FARMALIDER_REQUEST_DELAY_SECONDS")
    workers = numeric_config(config_text, "FARMALIDER_MAX_WORKERS")
    parallel_min = numeric_config(config_text, "FARMALIDER_PARALLEL_MIN_CPFS")
    batch_size = numeric_config(config_text, "FARMALIDER_BATCH_SIZE")
    cooldown = numeric_config(config_text, "FARMALIDER_429_COOLDOWN_SECONDS")
    max_adaptive_delay = numeric_config(
        config_text,
        "FARMALIDER_MAX_ADAPTIVE_DELAY_SECONDS",
    )
    slow_request = numeric_config(
        config_text,
        "FARMALIDER_SLOW_REQUEST_SECONDS",
    )

    if delay is None:
        fail("FARMALIDER_REQUEST_DELAY_SECONDS não foi encontrado em config.py.")
    elif delay < 0:
        fail("FARMALIDER_REQUEST_DELAY_SECONDS não pode ser negativo.")
    else:
        ok(
            f"Delay global FarmaLíder centralizado em app/config.py ({delay * 1000:g} ms)."
        )

    if workers is None:
        fail("FARMALIDER_MAX_WORKERS não foi encontrado em config.py.")
    elif not 1 <= workers <= 16:
        fail("FARMALIDER_MAX_WORKERS deve ficar entre 1 e 16.")
    else:
        ok(f"Concorrência FarmaLíder limitada a {int(workers)} workers.")

    if parallel_min is None:
        fail("FARMALIDER_PARALLEL_MIN_CPFS não foi encontrado em config.py.")
    elif parallel_min < 1:
        fail("FARMALIDER_PARALLEL_MIN_CPFS deve ser pelo menos 1.")
    elif batch_size is not None and parallel_min > batch_size:
        fail("O limiar de paralelismo não pode ser maior que o lote.")
    else:
        ok(f"Paralelismo FarmaLíder só é ativado a partir de {int(parallel_min)} CPFs.")

    if batch_size is None:
        fail("FARMALIDER_BATCH_SIZE não foi encontrado em config.py.")
    elif batch_size < 1 or batch_size > 5000:
        fail("FARMALIDER_BATCH_SIZE deve ficar entre 1 e 5000.")
    elif workers is not None and batch_size < workers:
        fail("FARMALIDER_BATCH_SIZE não pode ser menor que o número de workers.")
    else:
        ok(f"Lote FarmaLíder limitado a {int(batch_size)} CPFs.")

    if cooldown is None or cooldown <= 0:
        fail("FARMALIDER_429_COOLDOWN_SECONDS deve ser maior que zero.")
    else:
        ok(f"Cooldown global para HTTP 429 configurado ({cooldown:g}s).")

    if max_adaptive_delay is None:
        fail("FARMALIDER_MAX_ADAPTIVE_DELAY_SECONDS não foi encontrado.")
    elif delay is not None and max_adaptive_delay < delay:
        fail("O delay adaptativo máximo não pode ser menor que o delay base.")
    else:
        ok(f"Throttle adaptativo de 429 limitado a {max_adaptive_delay * 1000:g} ms.")

    if slow_request is None or slow_request <= 0:
        fail("FARMALIDER_SLOW_REQUEST_SECONDS deve ser maior que zero.")
    else:
        ok(
            "Diagnóstico de requisições lentas FarmaLíder configurado "
            f"({slow_request:g}s)."
        )

    recovery_delay = numeric_config(
        config_text, "FARMALIDER_RECOVERY_MAX_DELAY_SECONDS"
    )
    server_cooldown = numeric_config(config_text, "FARMALIDER_5XX_COOLDOWN_SECONDS")
    serial_successes = numeric_config(
        config_text, "FARMALIDER_RECOVERY_SERIAL_SUCCESSES"
    )
    parallel_successes = numeric_config(
        config_text, "FARMALIDER_RECOVERY_PARALLEL_SUCCESSES"
    )
    circuit_errors = numeric_config(config_text, "FARMALIDER_CIRCUIT_MIN_5XX")
    circuit_queries = numeric_config(
        config_text, "FARMALIDER_CIRCUIT_MIN_DISTINCT_QUERIES"
    )
    if (
        recovery_delay is None
        or not (delay or 0) <= recovery_delay <= 1
        or server_cooldown is None
        or server_cooldown < 5
        or serial_successes is None
        or serial_successes < 6
        or parallel_successes is None
        or parallel_successes < serial_successes
        or circuit_errors is None
        or circuit_errors < 12
        or circuit_queries is None
        or circuit_queries < 3
    ):
        fail(
            "Recuperação FarmaLíder deve manter cooldown, retorno gradual e circuito por várias consultas."
        )
    else:
        ok(
            "Recuperação serial gradual, cooldown 5xx e circuito antecipado configurados."
        )

    required_client_parts = [
        "threading.local",
        "_rate_lock",
        "threading.Condition",
        "_request_slot",
        "_protect_after_failure",
        "_mark_healthy",
        "cancel_pending_requests",
        "FarmaliderUnavailableError",
        "metrics_snapshot",
        "p95_response_ms",
        "slow_requests",
        "_endpoint_label",
    ]
    missing_client = [item for item in required_client_parts if item not in client_text]
    if missing_client:
        fail(
            "Proteções de concorrência no cliente FarmaLíder incompletas: "
            + ", ".join(missing_client)
        )
    else:
        ok("Cliente FarmaLíder usa sessões por thread e limitador global.")

    preflight_start = client_text.find("def test_connection")
    preflight_text = client_text[preflight_start:] if preflight_start >= 0 else ""
    required_preflight_parts = [
        "classify_network_failure",
        "for attempt in range(1, FARMALIDER_MAX_RETRIES + 1):",
        "if not failure.retryable",
        "_protect_after_failure",
        "_sleep_before_retry",
        '"attempts": attempt',
    ]
    missing_preflight = [
        item for item in required_preflight_parts if item not in preflight_text
    ]
    if missing_preflight:
        fail(
            "Preflight FarmaLíder sem retry/diagnóstico resiliente: "
            + ", ".join(missing_preflight)
        )
    else:
        ok(
            "Preflight FarmaLíder repete falhas transitórias e preserva "
            "falha imediata para causas não recuperáveis."
        )

    route_switch_contract = {
        "app/proxy_config.py": (
            proxy_text,
            ["NetworkRouteRefresh", "_ROUTE_REFRESH_LOCK", "refresh_network_route"],
        ),
        "app/farmalider_client.py": (
            client_text,
            [
                "_sync_base_url_from_environment",
                "_refresh_network_route",
                "refresh_network_route",
            ],
        ),
        "app/weni_client.py": (
            weni_text,
            ["refresh_network_route", "falha recuperável ao acessar Weni"],
        ),
        "app/google_sheets.py": (
            sheets_text,
            ["refresh_network_route", "falha recuperável no Google Sheets"],
        ),
    }
    missing_route_parts: list[str] = []
    for file_name, (source, markers) in route_switch_contract.items():
        for marker in markers:
            if marker not in source:
                missing_route_parts.append(f"{file_name}:{marker}")
    if missing_route_parts:
        fail(
            "Troca dinâmica direta/proxy incompleta: " + ", ".join(missing_route_parts)
        )
    else:
        ok(
            "Carga reconhece troca direta/proxy em Weni, FarmaLíder e "
            "Google Sheets e repete a operação em andamento."
        )

    quarter_contract = {
        "app/analytics.py": (
            analytics_text,
            [
                "FULL_LOAD_25_PARTS = 4",
                "full_load_25_manifest.json",
                "prepare_full_load_quarter",
                "mark_full_load_quarter_complete",
                "contacts_sha256",
            ],
        ),
        "app/main.py": (
            main_text,
            [
                "FULL_SYNC_QUARTER",
                'component="analytics"',
                "carga_completa_25_parte_",
                "ultima_execucao_carga_25_parte",
            ],
        ),
        "app/weni_update.py": (
            weni_update_text,
            [
                "quarter: int | None",
                'component="weni"',
                "selected_uuids",
                "quarter_campaign_id",
            ],
        ),
        "app/run_update_weni.py": (
            run_update_text,
            ["--quarter", "weni_update_review_full_25_parte_"],
        ),
        "comandos/executar_carga_completa.bat": (
            read("comandos/executar_carga_completa.bat"),
            ["Parte 1/4", "Parte 4/4", "FULL_SYNC_QUARTER"],
        ),
        "comandos/atualizar_weni_carga_completa.bat": (
            read("comandos/atualizar_weni_carga_completa.bat"),
            ["Parte 1/4", "Parte 4/4", "--quarter", "full_load_25_manifest.json"],
        ),
    }
    missing_quarter_parts: list[str] = []
    for file_name, (source, markers) in quarter_contract.items():
        for marker in markers:
            if marker not in source:
                missing_quarter_parts.append(f"{file_name}:{marker}")
    if missing_quarter_parts:
        fail("Carga completa 25% incompleta: " + ", ".join(missing_quarter_parts))
    else:
        ok(
            "Carga completa 25% usa quatro partes determinísticas, manifesto "
            "congelado compartilhado e menus próprios em Weni/Analytics."
        )

    required_main_parts = [
        "ThreadPoolExecutor",
        "FARMALIDER_MAX_WORKERS",
        "FARMALIDER_PARALLEL_MIN_CPFS",
        "FARMALIDER_BATCH_SIZE",
        "FARMALIDER_BATCH_ERROR_ABORT_RATE",
    ]
    missing_main = [item for item in required_main_parts if item not in main_text]
    if missing_main:
        fail(
            "Processamento concorrente de CPF incompleto em app/main.py: "
            + ", ".join(missing_main)
        )
    else:
        ok("Processamento de CPF usa concorrência controlada e circuit breaker.")

    tests = read("tests/test_current_integration.py")
    hardcoded_operational_test = re.search(
        r"assertEqual\(\s*config\.FARMALIDER_(?:REQUEST_DELAY_SECONDS|"
        r"MAX_WORKERS|BATCH_SIZE)\s*,\s*[0-9.]+",
        tests,
    )
    if hardcoded_operational_test:
        fail(
            "Teste de integração voltou a fixar um valor operacional da "
            "FarmaLíder. Valide comportamento/faixa, não o número atual."
        )
    else:
        ok("Testes não duplicam valores operacionais da FarmaLíder.")

    weni_tests = read("tests/test_weni_update.py")
    required_isolation = [
        '"weni_update._load_state"',
        "side_effect=lambda *args, **kwargs: {}",
    ]
    missing_isolation = [item for item in required_isolation if item not in weni_tests]
    if missing_isolation:
        fail(
            "Testes do NexoFlow Weni podem consumir o estado operacional real: "
            + ", ".join(missing_isolation)
        )
    else:
        ok("Testes do NexoFlow Weni isolam o checkpoint operacional real.")

    checkup = read("comandos/check-up.bat")
    quiet_runner = read("app/run_checkup_tests.py")
    required_quiet_runner = [
        "app/run_checkup_tests.py",
        "logging.disable(logging.CRITICAL)",
        'warnings.simplefilter("ignore")',
        "TextTestRunner(verbosity=0)",
        "result.wasSuccessful()",
    ]
    quiet_source = checkup + "\n" + quiet_runner
    missing_quiet = [item for item in required_quiet_runner if item not in quiet_source]
    if missing_quiet:
        fail(
            "Check-up não está usando o runner silencioso de testes: "
            + ", ".join(missing_quiet)
        )
    else:
        ok("Check-up oculta ruído esperado dos mocks e mantém falhas reais visíveis.")

    active_docs = ["README.md", "docs/README VSCODE.md"]
    stale_docs: list[str] = []
    stale_patterns = [
        re.compile(r"sem\s+paralelismo", re.IGNORECASE),
        re.compile(r"consultas\s+sequenciais", re.IGNORECASE),
    ]
    for filename in active_docs:
        path = ROOT / filename
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8")
        if any(pattern.search(text) for pattern in stale_patterns):
            stale_docs.append(filename)

    if stale_docs:
        fail(
            "Documentação ativa ainda descreve processamento sequencial: "
            + ", ".join(stale_docs)
        )
    else:
        ok("Documentação ativa reflete a concorrência controlada.")


def check_farmalider_auth() -> None:
    client_text = read("app/farmalider_client.py")

    authorization_assignment = re.search(
        r"headers\s*\[\s*[\"']Authorization[\"']\s*\]\s*=",
        client_text,
    )
    authorization_mapping = re.search(
        r"[\"']Authorization[\"']\s*:\s*",
        client_text,
    )
    bearer_value = re.search(
        r"[\"']Bearer\s+[^\"']+",
        client_text,
        re.IGNORECASE,
    )

    if authorization_assignment or authorization_mapping or bearer_value:
        fail(
            "app/farmalider_client.py parece configurar Authorization/Bearer; "
            "a integração atual deve usar somente token via QueryString."
        )
    elif re.search(
        r"headers\.pop\(\s*[\"']Authorization[\"']",
        client_text,
    ):
        ok(
            "FarmaLíder não envia Authorization/Bearer; "
            "o header é removido defensivamente."
        )
    else:
        ok("FarmaLíder não configura Authorization/Bearer no cliente.")

    token_query = re.search(
        r"params\s*=.*?[\"']token[\"']|"
        r"[\"']token[\"']\s*:\s*",
        client_text,
        re.DOTALL,
    )
    if not token_query:
        fail("Não encontrei o parâmetro token no cliente FarmaLíder.")
    else:
        ok("Token FarmaLíder presente como parâmetro de QueryString.")


def check_google_sheets_retry_if_configured() -> None:
    config_text = read("app/config.py")
    sheets_text = read("app/google_sheets.py")

    required_config = [
        "GOOGLE_SHEETS_WRITE_MAX_RETRIES",
        "GOOGLE_SHEETS_READ_MAX_RETRIES",
        "GOOGLE_SHEETS_CONNECT_TIMEOUT_SECONDS",
        "GOOGLE_SHEETS_READ_TIMEOUT_SECONDS",
        "GOOGLE_SHEETS_MAX_BATCH_PAYLOAD_BYTES",
    ]
    missing_config = [item for item in required_config if item not in config_text]
    if missing_config:
        fail(
            "Proteção Google Sheets ausente em app/config.py: "
            + ", ".join(missing_config)
        )
        return

    required = [
        "_read_with_retry",
        "_write_with_retry",
        "set_timeout",
        "RequestException",
        "GOOGLE_SHEETS_WRITE_DELAY_SECONDS",
        "GOOGLE_SHEETS_WRITE_MAX_RETRIES",
        "GOOGLE_SHEETS_READ_MAX_RETRIES",
        "SCHEMA_CONTROL_KEY",
        "schema_fingerprint",
        "load_base_uuids",
        "sales_metric_row_count",
        "_normalize_table",
        "_adaptive_batches",
        "upsert_contacts_detailed",
        "load_base_snapshot",
        "copy.deepcopy(batch)",
        "copy.deepcopy(updates)",
        "sem mudança=%s",
    ]
    missing = [item for item in required if item not in sheets_text]
    if missing:
        fail("Proteção de escrita do Google Sheets incompleta: " + ", ".join(missing))
    else:
        ok(
            "Google Sheets protegido com timeout/retry de leitura, "
            "retry de escrita e otimizações de carga."
        )

    main_text = read("app/main.py")
    required_observability = [
        "GOOGLE_SHEETS_PROGRESS_EVERY_RECORDS",
        "TaskProgress(",
        "gravação confirmada",
        "request_progress",
        "etapa 1/4 - Base_Contatos + Historico_Alteracoes",
        "leitura recuperada em %s na tentativa",
        "falha de rede/timeout durante escrita",
        "_headers_cache",
        "_read_header_bounded",
        "leitura redundante de cabeçalho dispensada",
    ]
    observability_source = config_text + "\n" + sheets_text + "\n" + main_text
    missing_observability = [
        item for item in required_observability if item not in observability_source
    ]
    if missing_observability:
        fail(
            "Google Sheets não contém observabilidade/resiliência do NexoFlow 1.0: "
            + ", ".join(missing_observability)
        )
    else:
        ok(
            "Google Sheets informa progresso de carga e retries recuperados "
            "sem períodos silenciosos longos."
        )

    if "self.history.row_values(1)" in sheets_text:
        fail(
            "Historico_Alteracoes voltou a reler a linha inteira do cabeçalho "
            "durante a carga."
        )
    else:
        ok("Histórico reutiliza schema validado e não relê cabeçalho redundante.")

    bi_required = [
        "SHEET_BI_RESUMO_DIARIO",
        "SHEET_BI_CANAIS",
        "SHEET_BI_CONVERSAO",
        "SHEET_BI_VENDAS",
        "SHEET_BI_ADS",
        "SHEET_BI_PEDIDOS",
        "SHEET_BI_STATUS",
        "BI_SUMMARY_COLUMNS",
        "BI_CHANNEL_COLUMNS",
        "BI_CONVERSION_COLUMNS",
        "BI_SALES_COLUMNS",
        "BI_ADS_COLUMNS",
        "BI_ORDERS_COLUMNS",
        "BI_STATUS_COLUMNS",
        "build_bi_summary_record",
        "build_bi_channel_records",
        "build_bi_conversion_records",
        "build_bi_sales_rows",
        "build_bi_ads_rows",
        "build_bi_order_rows",
        "opt_in_lgpd",
        "maior_de_18_anos",
        "BI_Publico",
        "BI_Clientes_Vendas",
        "BI_Produtos_ABC",
        "BI_Qualidade",
        "replace_looker_tables",
        "_replace_summary_atomically",
    ]
    bi_source = (
        config_text
        + "\n"
        + sheets_text
        + "\n"
        + main_text
        + "\n"
        + read("app/analytics.py")
        + "\n"
        + read("app/looker_bi.py")
    )
    missing_bi = [item for item in bi_required if item not in bi_source]
    if missing_bi:
        fail("Camada BI/Looker Studio 1.0 incompleta: " + ", ".join(missing_bi))
    else:
        ok(
            "Camada BI/Looker Studio contém as tabelas operacionais e comerciais, com tipos numéricos, cobertura de itens e migração financeira atômica."
        )

    diagnostico_text = read("app/diagnostico.py")
    if "validate_structure=True" not in diagnostico_text:
        fail(
            "app/diagnostico.py deve forçar validação completa da estrutura "
            "do Google Sheets."
        )
    else:
        ok("Diagnóstico força validação completa da estrutura do Sheets.")


def check_windows_lifecycle() -> None:
    required = [
        "NexoFlow Analytics.exe",
        "NexoFlow Weni.exe",
        "NexoFlow Terminal.exe",
        "desinstalar_nexoflow.cmd",
        "comandos/executar_carga_segura.bat",
        "comandos/atualizar_weni.bat",
        "comandos/atualizar_weni_dry_run.bat",
        "comandos/atualizar_weni_carga_completa.bat",
        "comandos/testar_update_weni.bat",
        "app/run_update_weni.py",
        "app/run_terminal.py",
        "app/weni_update.py",
        "app/contact_enrichment.py",
        "comandos/criar_atalho_desktop.bat",
        "installer/install.ps1",
        "installer/uninstall.ps1",
        "assets/NexoFlow.ico",
        "assets/NexoFlow_Weni.ico",
        "assets/NexoFlow_Terminal.ico",
        "assets/NexoFlow_Recompra.ico",
        "assets/NexoFlow_Planilha.ico",
        "assets/installer.ico",
        "assets/uninstaller.ico",
        "docs/README INSTALACAO.md",
        "docs/Documentacao NexoFlow.md",
        "docs/Documentacao NexoFlow.html",
        "docs/ASSINATURA WINDOWS.md",
    ]
    missing = [name for name in required if not (ROOT / name).exists()]
    if missing:
        fail("Ciclo Windows incompleto: " + ", ".join(missing))
        return

    chain = read("comandos/executar_carga_segura.bat")
    expected_calls = [
        "comandos/check-up.bat",
        "comandos/testar_conexoes.bat",
        "comandos/executar_manual.bat",
    ]
    missing_calls = [name for name in expected_calls if name not in chain]
    if missing_calls:
        fail("Cadeia do launcher incompleta: " + ", ".join(missing_calls))
    elif chain.count('if not "%RC%"=="0"') < 3:
        fail("Launcher deve bloquear a próxima etapa quando houver erro.")
    else:
        ok("Launcher executa check-up, conexões e sincronização com bloqueio por erro.")

    for filename in expected_calls:
        text = read(filename)
        if "NEXOFLOW_LAUNCHER" not in text:
            fail(f"{filename} não suporta execução encadeada pelo launcher.")
            return
    ok("BATs individuais preservam uso manual e suportam o launcher.")

    batch_files = sorted([*ROOT.glob("*.bat"), *(ROOT / "comandos").glob("*.bat")])
    batch_bom = [
        path.name
        for path in batch_files
        if path.read_bytes().startswith(b"\xef\xbb\xbf")
    ]
    if batch_bom:
        fail(
            "Arquivos BAT não podem usar UTF-8 BOM no CMD do Windows: "
            + ", ".join(batch_bom)
        )
        return
    ok("Arquivos BAT usam codificação compatível com cmd.exe, sem UTF-8 BOM inicial.")

    launcher_bytes = (ROOT / "NexoFlow Analytics.exe").read_bytes()
    update_launcher_bytes = (ROOT / "NexoFlow Weni.exe").read_bytes()
    terminal_bytes = (ROOT / "NexoFlow Terminal.exe").read_bytes()
    if (
        not launcher_bytes.startswith(b"MZ")
        or not update_launcher_bytes.startswith(b"MZ")
        or not terminal_bytes.startswith(b"MZ")
    ):
        fail("Executáveis Windows esperados não possuem cabeçalho PE/MZ.")
    elif (
        b"NexoFlowOperationLock" not in launcher_bytes
        or b"NexoFlowOperationLock" not in update_launcher_bytes
    ):
        fail("Executáveis operacionais não compartilham a trava de instância esperada.")
    elif b"comandos/atualizar_weni.bat" not in update_launcher_bytes:
        fail("NexoFlow Weni.exe não aponta para atualizar_weni.bat.")
    elif any(
        b"NexoFlowNativeLauncher=1.0" not in data
        for data in (launcher_bytes, update_launcher_bytes, terminal_bytes)
    ):
        fail(
            "Launchers Windows não possuem a revisão nativa de segurança do NexoFlow 1.0."
        )
    elif any(
        b"CreateProcessW" not in data
        for data in (launcher_bytes, update_launcher_bytes, terminal_bytes)
    ):
        fail(
            "Launchers Windows precisam iniciar processos pela API nativa/CreateProcessW."
        )
    elif any(
        b"\x00system\x00" in data
        for data in (launcher_bytes, update_launcher_bytes, terminal_bytes)
    ):
        fail("Launchers Windows não podem importar system().")
    elif b"app/run_terminal.py" not in terminal_bytes:
        fail("NexoFlow Terminal.exe não aponta diretamente para run_terminal.py.")
    elif b"powershell.exe" in launcher_bytes or b"-ExecutionPolicy" in launcher_bytes:
        fail(
            "Analytics não deve conter lógica de PowerShell/ExecutionPolicy que pertence ao Setup/Uninstall."
        )
    elif (
        b"powershell.exe" in update_launcher_bytes
        or b"-ExecutionPolicy" in update_launcher_bytes
    ):
        fail(
            "Update Weni não deve conter lógica de PowerShell/ExecutionPolicy que pertence ao Setup/Uninstall."
        )
    else:
        ok(
            "Analytics, NexoFlow Weni e Terminal usam launchers nativos sem system(); a desinstalação usa script PowerShell auditável com autoelevação."
        )

    if b'requestedExecutionLevel level="asInvoker"' not in launcher_bytes:
        fail("Launcher manual deve usar manifest asInvoker.")
    elif b'requestedExecutionLevel level="asInvoker"' not in update_launcher_bytes:
        fail("Launcher Update Weni deve usar manifest asInvoker.")
    elif b'requestedExecutionLevel level="asInvoker"' not in terminal_bytes:
        fail("NexoFlow Terminal deve usar manifest asInvoker.")
    elif any(
        b".rsrc" not in data
        for data in (launcher_bytes, update_launcher_bytes, terminal_bytes)
    ):
        fail("Executáveis devem conter recursos/ícones embutidos.")
    else:
        ok(
            "Manifests de privilégio e recursos dos três executáveis operacionais estão presentes."
        )

    version_markers = [
        "Grupo Líder".encode("utf-16le"),
        "1.0.0".encode("utf-16le"),
        "ProductVersion".encode("utf-16le"),
        "FileDescription".encode("utf-16le"),
    ]
    if any(marker not in launcher_bytes for marker in version_markers):
        fail("Executável Analytics não contém VERSIONINFO completo do NexoFlow 1.0.")
        return
    if any(marker not in update_launcher_bytes for marker in version_markers):
        fail(
            "Executável NexoFlow Weni não contém VERSIONINFO completo do NexoFlow 1.0."
        )
        return
    if any(marker not in terminal_bytes for marker in version_markers):
        fail(
            "Executável NexoFlow Terminal não contém VERSIONINFO completo do NexoFlow 1.0."
        )
        return
    ok(
        "Executáveis Windows operacionais possuem VERSIONINFO de produto/empresa/versão."
    )

    optional_uninstaller = ROOT / "NexoFlow Uninstall.exe"
    if optional_uninstaller.exists():
        optional_bytes = optional_uninstaller.read_bytes()
        if not optional_bytes.startswith(b"MZ"):
            fail("NexoFlow Uninstall.exe opcional não possui cabeçalho PE/MZ.")
            return
        if b"NexoFlowNativeLauncher=1.0" not in optional_bytes:
            fail(
                "NexoFlow Uninstall.exe opcional não está identificado como NexoFlow 1.0."
            )
            return
        if "1.0.0.0".encode("utf-16le") not in optional_bytes:
            fail("NexoFlow Uninstall.exe opcional não contém VERSIONINFO 1.0.0.0.")
            return
        ok(
            "NexoFlow Uninstall.exe opcional está presente e identificado como release 1.0."
        )
    else:
        ok(
            "NexoFlow Uninstall.exe opcional não está presente; isso não reprova o check-up."
        )

    install = read("installer/install.ps1")
    uninstall = read("installer/uninstall.ps1")
    for required_text in [
        "payload manifest.json",
        "function Get-Sha256File",
        "secrets_config.py",
        "credentials",
        "logs",
        "CurrentVersion\\Uninstall\\NexoFlow",
    ]:
        if required_text not in install:
            fail(f"Instalador não contém controle obrigatório: {required_text}")
            return
    # O instalador atual não usa mais WinGet/Python 3.12. A auditoria deve
    # verificar o fallback local 3.14.8 e a detecção de runtimes 3.11+ reais.
    if "Python.Python.3.12" in install:
        fail("Instalador voltou a depender do identificador legado Python.Python.3.12.")
        return
    for required_text in [
        "python-3.14.8-amd64.exe",
        "python3.14.exe",
        "pythoncore-*-64",
        "pymanager.exe",
        "list --format=exe",
        "NEXOFLOW_PYTHON_EXE",
        "Get-CompatiblePython",
        "Get-Command $commandName -All",
        "sys.executable",
        "Python 3.11 ou superior",
        "Confirm-PythonInstall",
        "Install-PythonIfNeeded",
        "Start-Process -FilePath $BundledPythonInstaller",
        "NÃO instalará Python 3.12",
        "NÃO usará WinGet",
        "NexoFlow Analytics.lnk",
        "NexoFlow Weni.lnk",
        "NexoFlow Weni.exe",
        "NexoFlow Terminal.exe",
        "NexoFlow Terminal.lnk",
        "NexoFlow_Terminal.ico",
        "NexoFlow_Recompra.ico",
        "NexoFlow_Planilha.ico",
        "docs/Documentacao NexoFlow.html",
    ]:
        if required_text not in install:
            fail(
                f"Instalador não contém o fluxo obrigatório de requisito Python/documentação: {required_text}"
            )
            return
    install_bytes = (ROOT / "installer" / "install.ps1").read_bytes()
    uninstall_script_bytes = (ROOT / "installer" / "uninstall.ps1").read_bytes()
    if not install_bytes.startswith(
        b"\xef\xbb\xbf"
    ) or not uninstall_script_bytes.startswith(b"\xef\xbb\xbf"):
        fail(
            "Scripts PowerShell do instalador/desinstalador devem usar UTF-8 com BOM para preservar acentuação no Windows PowerShell."
        )
        return
    ok(
        "Instalador reconhece Python 3.11+ (incluindo 3.14) e usa scripts PowerShell com UTF-8 BOM."
    )
    for required_text in [
        "Start-Transcript",
        "NexoFlow-Setup.log",
        "Stop-InstalledNexoFlowProcesses",
        "taskkill.exe",
        "function Remove-DirectoryWithRetry",
        "function Stage-ExistingInstall",
        "function Restore-PreviousInstall",
        "$attempt -le $Attempts",
        "Ensure-Administrator",
        "-Verb RunAs",
    ]:
        if required_text not in install:
            fail(
                f"Setup 1.0 não contém a proteção/diagnóstico obrigatório: {required_text}"
            )
            return
    setup_source = read("native_launcher/setup.go")
    if (
        "if rc != 0" not in setup_source
        or "waitEnter()" not in setup_source
        or "NexoFlow-Setup.log" not in setup_source
    ):
        fail(
            "Launcher do Setup deve manter a janela aberta em falha e indicar o log persistente."
        )
        return
    ok(
        "Setup mantém diagnóstico visível, gera log persistente e trata arquivos em uso na atualização."
    )
    for required_text in [
        "function Ensure-InstalledPayloadFile",
        "Write-Step 'Validando componentes instalados'",
        "Restaurando a partir do payload validado",
        "Get-Sha256File -Path $target",
        "installer\\uninstall.ps1",
        "desinstalar_nexoflow.cmd",
        "Verifique antivírus/EDR e tente novamente",
    ]:
        if required_text not in install:
            fail(
                f"Setup não valida/repara executáveis obrigatórios no destino: {required_text}"
            )
            return
    ok(
        "Setup valida e repara launchers operacionais e o desinstalador baseado em script após a instalação."
    )
    for required_text in [
        "function Install-OptionalUninstaller",
        "NexoFlow Uninstall.exe",
        "OptionalUninstallerValidated",
        "Como ele é opcional, o Setup continuará normalmente",
        "Where-Object { $_.Name -notin @('payload manifest.json','NexoFlow Uninstall.exe') }",
    ]:
        if required_text not in install:
            fail(
                f"Setup não trata o launcher opcional de desinstalação de forma tolerante: {required_text}"
            )
            return
    if "'NexoFlow Uninstall.exe')) {\n        Ensure-InstalledPayloadFile" in install:
        fail(
            "NexoFlow Uninstall.exe não pode fazer parte da validação obrigatória no destino."
        )
        return
    ok(
        "NexoFlow Uninstall.exe é distribuído opcionalmente e não reprova Setup/check-up quando bloqueado por antivírus/EDR."
    )
    ok(
        "Instalador valida integridade, trata o requisito Python, preserva dados privados e registra o ciclo Windows."
    )

    for required_text in [
        "YesNoCancel",
        "NexoFlow Data",
        "secrets_config.py",
        "credentials",
        "logs",
        "Stage2",
        "Get-CimInstance Win32_Process",
        "Stop-Process",
        "Remove-InstallDirectory",
        "NexoFlow Weni.lnk",
        "NexoFlow Terminal.lnk",
        "NexoFlow-Uninstall-Stage2.ps1",
        "Set-Location -LiteralPath $tempWorkingDir",
        "-WorkingDirectory $tempWorkingDir",
        "$attempt -le 60",
        "attrib.exe -R -S -H",
    ]:
        if required_text not in uninstall:
            fail(f"Desinstalador não contém controle obrigatório: {required_text}")
            return
    if "cmd.exe /d /s /c start" in install or 'cd /d ""{0}"" &&' in install:
        fail("Registro de desinstalação não deve depender de CMD intermediário.")
        return
    if (
        "$uninstallCommand =" not in install
        or "$powershellExe" not in install
        or "installer\\uninstall.ps1" not in install
    ):
        fail(
            "Registro de desinstalação deve apontar para installer\\uninstall.ps1 via Windows PowerShell."
        )
        return
    if (
        "function Ensure-Administrator" not in uninstall
        or "-Verb RunAs" not in uninstall
    ):
        fail("Desinstalador em script deve solicitar elevação administrativa via UAC.")
        return
    if "-WindowStyle Hidden" in uninstall:
        fail("Stage2 do desinstalador não deve ser executado oculto.")
        return
    if r"cmd\.exe|powershell\.exe|pwsh\.exe" in uninstall:
        fail(
            "Desinstalador não deve usar a antiga expressão que encerrava terminais CMD/PowerShell genéricos."
        )
        return
    ok(
        "Desinstalador em script preserva dados opcionalmente, autoeleva via UAC, usa Stage2 visível/determinístico e restringe processos bloqueadores."
    )

    install_bat = read("instalar.bat")
    if (
        "NEXOFLOW_INSTALLER" not in install_bat
        or "where python.exe" not in install_bat
        or "where python3.14.exe" not in install_bat
        or "where py.exe" not in install_bat
        or "sys.version_info >= (3,11)" not in install_bat
        or "struct.calcsize('P')*8 == 64" not in install_bat
    ):
        fail(
            "instalar.bat não está preparado para Python 3.11+ x64 e execução não interativa pelo instalador."
        )
    else:
        ok("Preparação do ambiente suporta Python 3.11+ x64, instalador e uso manual.")

    for icon in [
        "NexoFlow.ico",
        "NexoFlow_Weni.ico",
        "NexoFlow_Terminal.ico",
        "NexoFlow_Recompra.ico",
        "NexoFlow_Planilha.ico",
        "installer.ico",
        "uninstaller.ico",
    ]:
        data = (ROOT / "assets" / icon).read_bytes()
        if len(data) < 1024 or not data.startswith(b"\x00\x00\x01\x00"):
            fail(f"Ícone Windows inválido ou incompleto: {icon}")
            return
    ok("Ícones Windows do app, instalador e desinstalador estão válidos.")

    desktop_shortcuts = read("comandos/criar_atalho_desktop.bat")
    if "assets\\NexoFlow_Weni.ico" not in desktop_shortcuts:
        fail("Atalho desktop do Update Weni deve usar o ícone dedicado.")
        return
    if "$updateIcon = Join-Path $InstallDir 'assets\\NexoFlow_Weni.ico'" not in install:
        fail("Instalador deve preparar o ícone dedicado do Update Weni.")
        return
    if "-IconPath $updateIcon" not in install:
        fail("Instalador deve aplicar o ícone dedicado aos atalhos do NexoFlow Weni.")
        return
    if "assets\\NexoFlow_Terminal.ico" not in desktop_shortcuts:
        fail("Atalho desktop do NexoFlow Terminal deve usar o ícone dedicado.")
        return
    if (
        "$terminalIcon = Join-Path $InstallDir 'assets\\NexoFlow_Terminal.ico'"
        not in install
        or "-IconPath $terminalIcon" not in install
    ):
        fail(
            "Instalador deve aplicar o ícone dedicado aos atalhos do NexoFlow Terminal."
        )
        return
    ok(
        "Ícones dedicados do NexoFlow Weni e NexoFlow Terminal estão presentes e aplicados aos atalhos."
    )

    if "-ExecutionPolicy Bypass -Command" in desktop_shortcuts:
        fail(
            "Criação manual de atalhos não deve usar ExecutionPolicy Bypass para comando inline."
        )
        return
    terminal_runner = read("app/run_terminal.py")
    if (
        b"app/run_terminal.py" not in terminal_bytes
        or "configure_proxy()" not in terminal_runner
    ):
        fail(
            "NexoFlow Terminal precisa iniciar app/run_terminal.py e carregar o proxy do projeto antes de abrir PowerShell."
        )
        return
    if (
        "NEXOFLOW_HOME" not in terminal_runner
        or "NEXOFLOW_TERMINAL" not in terminal_runner
        or "VIRTUAL_ENV" not in terminal_runner
    ):
        fail("NexoFlow Terminal precisa preparar diretório/ambiente Python do projeto.")
        return
    ok("NexoFlow Terminal prepara proxy e ambiente local sem expor tokens de API.")

    update_launcher = (ROOT / "NexoFlow Weni.exe").read_bytes()
    if b"NexoFlowWeniIconRevision=1.0" not in update_launcher:
        fail(
            "Executável NexoFlow Weni não contém a revisão binária de ícone do NexoFlow 1.0."
        )
        return
    if b"NexoFlowTerminalIconRevision=1.0" not in terminal_bytes:
        fail(
            "Executável NexoFlow Terminal não contém a revisão binária de ícone do NexoFlow 1.0."
        )
        return
    update_icon_images = ico_images(ROOT / "assets" / "NexoFlow_Weni.ico")
    terminal_icon_images = ico_images(ROOT / "assets" / "NexoFlow_Terminal.ico")
    analytics_icon_images = ico_images(ROOT / "assets" / "NexoFlow.ico")
    if not update_icon_images or not all(
        image in update_launcher for image in update_icon_images
    ):
        fail("Executável NexoFlow Weni não incorpora o ícone dedicado nos recursos PE.")
        return
    if not terminal_icon_images or not all(
        image in terminal_bytes for image in terminal_icon_images
    ):
        fail(
            "Executável NexoFlow Terminal não incorpora o ícone dedicado nos recursos PE."
        )
        return
    if any(image in update_launcher for image in analytics_icon_images):
        fail("Executável NexoFlow Weni ainda incorpora o ícone do Analytics.")
        return
    if any(image in terminal_bytes for image in analytics_icon_images):
        fail("Executável NexoFlow Terminal ainda incorpora o ícone do Analytics.")
        return
    ok(
        "Executáveis NexoFlow Weni e NexoFlow Terminal incorporam somente seus ícones dedicados."
    )

    if (
        "function Refresh-NexoFlowIconCache" not in install
        or "SHChangeNotify" not in install
        or "-ClearIconCache" not in install
        or "-show" not in install
        or "Refresh-NexoFlowIconCache -Executables @($appExe, $updateExe, $recompraExe, $terminalExe, $planilhaExe, $uninstallExe)"
        not in install
    ):
        fail(
            "Instalador precisa invalidar/recarregar os ícones do Windows após instalar Analytics, Weni, Terminal, Planilha e Uninstall."
        )
        return
    ok(
        "Instalador força o Explorer a reler os ícones internos de todos os executáveis do NexoFlow."
    )

    active_docs = [
        read("README.md"),
        read("docs/README VSCODE.md"),
        read("docs/README INSTALACAO.md"),
        read("docs/README TERMINAL.md"),
        read("docs/Documentacao NexoFlow.md"),
    ]
    if not all("NexoFlow" in text for text in active_docs):
        fail("Documentação operacional ativa não está alinhada à marca NexoFlow.")
    elif any("Weni WhatsApp Analytics" in text for text in active_docs):
        fail("Documentação operacional ativa ainda contém a marca anterior.")
    else:
        ok("Documentação operacional ativa está alinhada à marca NexoFlow.")


def check_weni_update_safety() -> None:
    client_text = read("app/weni_client.py")
    update_text = read("app/weni_update.py")
    runner_text = read("app/run_update_weni.py")
    batch_text = read("comandos/atualizar_weni.bat")

    for required in [
        "WENI_MESSAGES_API_URL",
        "update_contact_fields",
        "contact_group_action",
        "list_messages",
    ]:
        if required not in client_text:
            fail(
                f"Cliente Weni não contém recurso obrigatório do Update Weni: {required}"
            )
            return
    ok("Cliente Weni possui leitura de mensagens, escrita de fields e ações de grupo.")
    for optimized in [
        "_resolve_required_groups",
        "urn_contact_cache",
        "_close_audit_handles",
        "buffering=1",
    ]:
        if optimized not in update_text:
            fail(f"Otimização segura do Update Weni ausente: {optimized}")
            return
    ok(
        "Update Weni reduz leituras auxiliares e I/O de auditoria sem relaxar proteções de escrita."
    )

    for required in [
        "ALLOWED_WRITE_FIELDS",
        "audit_plans",
        "purchase_confirmed",
        "WENI_UPDATE_OPT_IN_GROUP",
        "WENI_UPDATE_OPT_OUT_GROUP",
        "last_success_utc",
        "_append_audit",
        "update_contact_fields",
        "contact_group_action",
    ]:
        if required not in update_text:
            fail(f"Update Weni está sem proteção obrigatória: {required}")
            return

    enrichment_text = read("app/contact_enrichment.py")
    for required in [
        '"cpf"',
        "_is_valid_cpf",
        "_direct_cpf",
        "_standalone_cpf",
        "CPF_CONTEXT_PATTERNS",
    ]:
        if required not in enrichment_text:
            fail(f"Detecção segura de CPF em mensagens está incompleta: {required}")
            return
    if '"cpf",' not in update_text or "CPF inválido" not in update_text:
        fail("Update Weni precisa autorizar e auditar CPF antes da escrita.")
        return
    ok("Update Weni detecta CPF válido em mensagens e audita a escrita do campo cpf.")

    urn_utils_text = read("app/urn_utils.py")
    for required in [
        'CHANNEL_PRIORITY = ("whatsapp", "instagram", "facebook")',
        "contact_urn_whatsapp",
        "contact_urn_instagram",
        "contact_urn_facebook",
        "resolve_contact_urns",
        "messenger",
    ]:
        if required not in urn_utils_text:
            fail(f"Resolução multicanal de URNs está incompleta: {required}")
            return
    for required in [
        '"contact_urn_whatsapp"',
        '"contact_urn_instagram"',
        '"contact_urn_facebook"',
        '"contact_canal_principal"',
        "_sync_contact_channel_fields",
        "REVIEW_SCHEMA_VERSION = 6",
    ]:
        if required not in update_text:
            fail(
                f"Update Weni não contém a identificação multicanal obrigatória: {required}"
            )
            return
    analytics_text = read("app/analytics.py")
    for required in [
        '"contact_urn_whatsapp"',
        '"contact_urn_instagram"',
        '"contact_urn_facebook"',
        '"contact_canal_principal"',
        "resolve_contact_urns",
    ]:
        if required not in analytics_text:
            fail(
                f"Analytics não expõe a identificação multicanal obrigatória: {required}"
            )
            return
    ok(
        "URNs multicanal usam a Weni como fonte, com principal determinístico e campos separados por canal."
    )
    ad_text = read("app/ad_attribution.py")
    for required in [
        'GOOGLE_ADS_ORIGIN = "Google Ads"',
        "gclid",
        "gbraid",
        "wbraid",
        "Performance Max",
        "Display/Banner",
        "YouTube",
        "extract_google_ads_attribution",
    ]:
        if required not in ad_text:
            fail(f"Atribuição Google Ads está incompleta: {required}")
            return
    for required in [
        "_sync_google_ads_attribution",
        "GOOGLE_ADS_WRITE_FIELDS",
        "_missing_google_ads_fields",
    ]:
        if required not in update_text:
            fail(
                f"NexoFlow Weni não contém integração Google Ads obrigatória: {required}"
            )
            return
    for required in [
        "primeiro_anuncio_tipo_campanha",
        "primeiro_anuncio_canal_destino",
        "ultimo_anuncio_tipo_campanha",
        "ultimo_anuncio_canal_destino",
    ]:
        if required not in ad_text:
            fail(f"Mapeamento Google Ads não contém campo obrigatório: {required}")
            return
    for required in [
        "primeiro_anuncio_tipo_campanha",
        "primeiro_anuncio_canal_destino",
        "ultimo_anuncio_tipo_campanha",
        "ultimo_anuncio_canal_destino",
        "is_ads_contact",
    ]:
        if required not in analytics_text:
            fail(
                f"Analytics não expõe metadados Google Ads/canal de destino: {required}"
            )
            return
    google_docs = read("docs/GOOGLE ADS.md")
    for required in [
        "Google Ads",
        "gclid",
        "gbraid",
        "wbraid",
        "Performance Max",
        "Search",
        "Display/Banner",
        "YouTube",
    ]:
        if required not in google_docs:
            fail(f"Documentação Google Ads incompleta: {required}")
            return
    ok(
        "Atribuição Google Ads usa evidência explícita e mantém origem do anúncio separada do canal/URN."
    )
    client_text = read("app/weni_client.py")
    config_text = read("app/config.py")
    run_update_text = read("app/run_update_weni.py")
    for required in [
        "WENI_FIELDS_API_URL",
        "def list_fields",
    ]:
        source = config_text + "\n" + client_text
        if required not in source:
            fail(f"Preflight multicanal da Weni está incompleto: {required}")
            return
    for required in [
        "REQUIRED_MULTICHANNEL_FIELDS",
        "_ensure_multichannel_fields",
        "_missing_multichannel_fields",
    ]:
        if required not in update_text:
            fail(f"Preflight multicanal do Update Weni está incompleto: {required}")
            return
    if "Campos multicanal ausentes na Weni" not in run_update_text:
        fail("CLI do NexoFlow Weni não informa campos multicanal ausentes.")
        return
    ok(
        "Preflight valida os campos multicanal na Weni antes de processar/aplicar o plano."
    )

    for required in [
        "REQUIRED_ORDER_FIELDS",
        "_missing_order_fields",
        "_ensure_order_fields",
        "pedido_valor_produtos",
        "pedido_forma_pagamento",
    ]:
        if required not in update_text:
            fail(f"Preflight de pedidos da Weni está incompleto: {required}")
            return
    if "Campos de pedido obrigatórios ausentes na Weni" not in run_update_text:
        fail("CLI do NexoFlow Weni não informa campos obrigatórios de pedido ausentes.")
        return
    ok("Preflight exige o contrato completo de campos pedido_* antes do apply.")

    integration_text = read("app/farmalider_integration.py")
    analytics_text = read("app/analytics.py")
    for required in [
        "_ultimo_pedido_locator_id",
        "FARMALIDER_PEDIDO_ID_WENI",
        "FARMALIDER_ULTIMO_PEDIDO_LOCATOR",
        "valor_total_faturamento",
        "farmalider_frete",
    ]:
        source = integration_text + "\n" + analytics_text
        if required not in source:
            fail(
                f"Arquitetura de recuperação/frete dos pedidos está incompleta: {required}"
            )
            return
    ok(
        "Pedidos usam pedido_id/ultimo_pedido como localizadores e separam produtos, frete e faturamento total."
    )

    if "Opt-In LGPD" not in update_text or "Opt-Out LGPD" not in update_text:
        fail("Update Weni precisa sincronizar os grupos Opt-In LGPD e Opt-Out LGPD.")
        return
    ok("Update Weni sincroniza os grupos Opt-In/Opt-Out conforme opt_in_lgpd.")

    # Verifica o fluxo de execução, não a definição do helper de escrita.
    execution = update_text[update_text.index("def run_update(") :]
    write_pos = execution.find("_execute_contact_plan(")
    audit_pos = execution.rfind("audit_plans(", 0, write_pos)
    state_pos = execution.rfind("_save_state(")
    if audit_pos < 0 or write_pos < 0 or audit_pos > write_pos:
        fail("Auditoria do Update Weni precisa ocorrer antes de qualquer escrita.")
        return
    if state_pos < write_pos:
        fail("Checkpoint do Update Weni não pode avançar antes das escritas.")
        return
    ok(
        "Update Weni audita antes de escrever; falhas isoladas ficam pendentes para a próxima incremental."
    )

    if (
        "--dry-run" not in runner_text
        or "--apply" not in runner_text
        or "--check-only" not in runner_text
    ):
        fail("app/run_update_weni.py precisa oferecer check-only, dry-run e apply.")
        return

    dry_pos = batch_text.find("app/run_update_weni.py --dry-run")
    apply_pos = batch_text.find("app/run_update_weni.py --apply")
    if (
        dry_pos < 0
        or apply_pos < 0
        or dry_pos > apply_pos
        or "Digite ATUALIZAR" not in batch_text
    ):
        fail(
            "comandos/atualizar_weni.bat precisa executar dry-run antes de apply e exigir confirmação manual."
        )
        return
    ok(
        "Launcher Update Weni exige check-up, dry-run/auditoria e confirmação antes do apply."
    )

    full_batch_text = read("comandos/atualizar_weni_carga_completa.bat")
    for required in [
        "--review-out",
        "weni_update_review_full.txt",
        "CARGA COMPLETA REVISADA",
        "--require-review",
    ]:
        if required not in full_batch_text:
            fail(f"Carga completa está sem revisão humana verificável: {required}")
            return
    if full_batch_text.find("--review-out") > full_batch_text.find("--require-review"):
        fail("Carga completa deve gerar o snapshot revisável antes do apply protegido.")
        return
    confirmation_text = read("scripts/confirmar_revisao_weni.ps1")
    if (
        "CARGA COMPLETA REVISADA" not in confirmation_text
        or "-STA" not in full_batch_text
        or 'if "%RC%"=="2" goto :cancelado' not in full_batch_text
        or 'if not "%RC%"=="0" goto :erro' not in full_batch_text
    ):
        fail(
            "Revisão Weni deve aguardar confirmação, preservar erros e incluir o componente nos pacotes."
        )
        return
    # O build é validado no repositório; a instalação contém apenas scripts de uso.
    if (ROOT / "scripts/build_release.ps1").is_file():
        release_text = read("scripts/build_release.ps1")
        if (
            '"scripts/confirmar_revisao_weni.ps1"' not in release_text
            or "payload/scripts/confirmar_revisao_weni.ps1" not in release_text
        ):
            fail(
                "Build deve incluir e validar o componente de revisão Weni nos pacotes."
            )
            return
    for required in [
        "write_review_snapshot",
        "load_review_snapshot",
        "_prepare_frozen_plan_for_apply",
        "_plan_digest",
        "REVIEW_SCHEMA_VERSION = 6",
        "frozen_full_apply",
        "conflict_skipped",
        "pending_conflict_uuids",
        "apply and full and require_review is None",
    ]:
        if required not in update_text:
            fail(f"Update Weni está sem proteção do snapshot congelado: {required}")
            return
    for forbidden in [
        "Recalculando e comparando com o plano revisado",
        "apply SOMENTE se o plano for identico ao revisado",
    ]:
        if forbidden in full_batch_text:
            fail(
                f"Carga completa ainda contém fluxo antigo incompatível com atendimento 24h: {forbidden}"
            )
            return
    ok(
        "Carga completa usa snapshot congelado revisado, valida conflito por contato e não recalcula o histórico no apply."
    )


def check_terminal_cli() -> None:
    cli_path = ROOT / "app/nexo_cli.py"
    cmd_path = ROOT / "nexo.cmd"
    terminal_doc = ROOT / "docs/README TERMINAL.md"
    if not cli_path.exists() or not cmd_path.exists() or not terminal_doc.exists():
        fail(
            "CLI amigável do NexoFlow Terminal está incompleta (app/nexo_cli.py/nexo.cmd/documentação)."
        )
        return
    if cmd_path.read_bytes().startswith(b"\xef\xbb\xbf"):
        fail("nexo.cmd não pode usar UTF-8 BOM porque é executado pelo cmd.exe.")
        return
    cli = read("app/nexo_cli.py")
    cmd = read("nexo.cmd")
    runner = read("app/run_terminal.py")
    docs = (
        read("docs/README TERMINAL.md")
        + "\n"
        + read("README.md")
        + "\n"
        + read("docs/Documentacao NexoFlow.md")
    )
    for required in [
        'VERSION = "1.0"',
        "nexo api weni contato",
        "nexo api farmalider pedido",
        "nexo logs weni --seguir",
        "def _api_weni",
        "def _api_farmalider",
        "def _menu",
    ]:
        source = cli + "\n" + docs
        if required not in source:
            fail(
                f"CLI/documentação do Terminal está sem recurso obrigatório: {required}"
            )
            return
    if (
        "app/nexo_cli.py" not in cmd
        or "%NEXO_HOME%.venv\\Scripts\\python.exe" not in cmd
    ):
        fail("nexo.cmd não prioriza a .venv nem chama nexo_cli.py.")
        return
    if "path_parts = [str(BASE_DIR)]" not in runner or "nexo ajuda" not in runner:
        fail(
            "app/run_terminal.py precisa colocar C:\\NexoFlow no PATH e orientar sobre nexo ajuda."
        )
        return
    forbidden = ["update_contact_fields(", "contact_group_action(", "_post_json("]
    api_region = cli[cli.find("def _api_weni") : cli.find("def _menu")]
    if any(item in api_region for item in forbidden):
        fail(
            "Família nexo api deve permanecer somente leitura, sem métodos de escrita da Weni."
        )
        return
    if "docs/README TERMINAL.md" not in read(
        "README.md"
    ) or "docs/README TERMINAL.md" not in read("docs/Documentacao NexoFlow.md"):
        fail("README/documentação geral precisam apontar para README TERMINAL.md.")
        return
    ok(
        "NexoFlow Terminal oferece CLI amigável, ajuda integrada e consultas API somente leitura."
    )


def check_portuguese_encoding() -> None:
    # PowerShell 5.1 defaults to ANSI without a BOM; CMD needs an explicit page.
    paths = list((ROOT / "installer").glob("*.ps1")) + list(
        (ROOT / "scripts").glob("*.ps1")
    )
    for path in paths:
        if not path.read_bytes().startswith(b"\xef\xbb\xbf"):
            fail(f"PowerShell sem UTF-8 com BOM: {path.relative_to(ROOT)}")
            return
    for path in (
        list(ROOT.glob("*.bat"))
        + list(ROOT.glob("*.cmd"))
        + list((ROOT / "comandos").glob("*.bat"))
    ):
        if "chcp 65001" not in path.read_text(encoding="utf-8").lower():
            fail(f"CMD sem seleção explícita de UTF-8: {path.name}")
            return
    for name in (
        "docs/Documentacao NexoFlow.html",
        "docs/docs/Documentacao NexoFlow.html",
    ):
        path = ROOT / name
        if path.exists() and 'lang="pt-BR"' not in path.read_text(encoding="utf-8"):
            fail(f"Documentação HTML sem idioma português brasileiro: {name}")
            return
        if path.exists() and "\ufffd" in path.read_text(encoding="utf-8"):
            fail(f"Documentação HTML contém caracteres de substituição: {name}")
            return
    ok("Interface Windows e documentação usam UTF-8 e português brasileiro (pt-BR).")


def main() -> int:
    print("=== Auditoria de consistência do projeto ===")
    check_farmalider_runtime()
    check_farmalider_auth()
    check_google_sheets_retry_if_configured()
    check_windows_lifecycle()
    check_terminal_cli()
    check_weni_update_safety()
    check_portuguese_encoding()

    print()
    if ERRORS:
        print(f"[ERRO] Auditoria encontrou {len(ERRORS)} problema(s).")
        return 1

    print("[OK] Auditoria de consistência passou.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
# NexoFlow 1.0
