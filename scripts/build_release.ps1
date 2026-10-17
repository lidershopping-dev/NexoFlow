param(
    [string]$Version = "1.0"
)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"

$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$DistDir = Join-Path $RepoRoot "dist"
$SetupRoot = Join-Path $DistDir "NexoFlow Setup"
$PayloadDir = Join-Path $SetupRoot "payload"
$ProjectRoot = Join-Path $DistDir "NexoFlow"

if (Test-Path -LiteralPath $DistDir) {
    Remove-Item -LiteralPath $DistDir -Recurse -Force
}
New-Item -ItemType Directory -Force -Path $PayloadDir | Out-Null
New-Item -ItemType Directory -Force -Path $ProjectRoot | Out-Null

function Assert-PublicTrackedFiles {
    param([Parameter(Mandatory = $true)][string[]]$Paths)

    # A lista do Git pode conter arquivos adicionados com --force. Falhe antes
    # de copiar qualquer arquivo privado; nunca leia seu conteúdo no build.
    $publicPlaceholders = @(
        "configuracao/secrets_config.example.py",
        "configuracao/secrets_config.example_webhook.txt",
        "app/secrets_config.pyi",
        "credentials/coloque_a_credencial_google_aqui.txt",
        "logs/.gitkeep"
    )
    $privatePatterns = @(
        '(^|/)(\.git|\.venv|venv|__pycache__)(/|$)',
        '(^|/)(credentials|secrets|logs)(/|$)',
        '(^|/)(diagnosticos_privados|importacoes)(/|$)',
        '(^|/)(Conversas_Delivery|Contatos_Delivery)\.xlsx$',
        '(^|/)farmalider_falhas_[^/]*\.jsonl$',
        '(^|/)secrets_config\.py($|\.)',
        '(^|/)\.env($|\.)',
        '(^|/)(google[-_]?credentials|service[-_]?account|credentials|secrets|tokens?)([-_.][^/]*)?\.json$',
        '(^|/)(id_rsa|id_dsa|id_ecdsa|id_ed25519)($|\.)',
        '\.(pem|key|p12|pfx|jks|keystore|ppk|log)(\.[^/]*)?$'
    )
    $unsafe = @()
    foreach ($relative in $Paths) {
        if (-not $relative) { continue }
        $normalized = $relative.Replace("\", "/").ToLowerInvariant()
        if ($normalized -in $publicPlaceholders) { continue }
        foreach ($pattern in $privatePatterns) {
            if ($normalized -match $pattern) {
                $unsafe += $relative
                break
            }
        }
    }
    if ($unsafe.Count -gt 0) {
        throw ("Build bloqueado: arquivos privados estão rastreados no Git. " +
            "Remova-os do índice antes de publicar: " + ($unsafe -join ", "))
    }
}

function Copy-TrackedFiles {
    param([Parameter(Mandatory = $true)][string]$Destination)

    Push-Location $RepoRoot
    try {
        $tracked = & git ls-files
        if ($LASTEXITCODE -ne 0) { throw "git ls-files falhou." }
        Assert-PublicTrackedFiles -Paths $tracked

        foreach ($relative in $tracked) {
            if (-not $relative) { continue }
            if ($relative -like "releases/*") { continue }
            if ($relative -like ".github/*") { continue }
            if ($relative -like "scripts/*" -and $relative -notin @("scripts/confirmar_revisao_weni.ps1", "scripts/resultado_carga_weni.ps1", "scripts/check_release_secrets.py", "scripts/build_documentation.py")) { continue }

            $source = Join-Path $RepoRoot $relative
            if (-not (Test-Path -LiteralPath $source -PathType Leaf)) { continue }

            $target = Join-Path $Destination $relative
            $targetDir = Split-Path -Parent $target
            if ($targetDir) { New-Item -ItemType Directory -Force -Path $targetDir | Out-Null }
            Copy-Item -LiteralPath $source -Destination $target -Force
        }
    }
    finally {
        Pop-Location
    }
}

function Write-PayloadManifest {
    param([Parameter(Mandatory = $true)][string]$Payload)

    $manifestPath = Join-Path $Payload "payload manifest.json"
    $entries = @()

    Get-ChildItem -LiteralPath $Payload -Recurse -File |
        Where-Object { $_.FullName -ne $manifestPath } |
        Sort-Object FullName |
        ForEach-Object {
            $relative = $_.FullName.Substring($Payload.Length).TrimStart("\","/").Replace("\","/")
            $entries += [ordered]@{
                path = $relative
                sha256 = (Get-FileHash -Algorithm SHA256 -LiteralPath $_.FullName).Hash.ToLowerInvariant()
                size = $_.Length
            }
        }

    $manifest = [ordered]@{
        version = $Version
        algorithm = "SHA256"
        files = $entries
    }

    $json = $manifest | ConvertTo-Json -Depth 8
    [IO.File]::WriteAllText($manifestPath, $json + [Environment]::NewLine, (New-Object Text.UTF8Encoding($false)))
}

function Write-Checksums {
    param(
        [Parameter(Mandatory = $true)][string]$Root,
        [Parameter(Mandatory = $true)][string]$OutputFile
    )

    $lines = New-Object System.Collections.Generic.List[string]
    Get-ChildItem -LiteralPath $Root -Recurse -File |
        Where-Object { $_.FullName -ne $OutputFile } |
        Sort-Object FullName |
        ForEach-Object {
            $relative = $_.FullName.Substring($Root.Length).TrimStart("\","/").Replace("\","/")
            $hash = (Get-FileHash -Algorithm SHA256 -LiteralPath $_.FullName).Hash.ToLowerInvariant()
            $lines.Add("$hash  $relative")
        }

    [IO.File]::WriteAllLines($OutputFile, $lines, (New-Object Text.UTF8Encoding($false)))
}

function Assert-ZipEntry {
    param(
        [Parameter(Mandatory = $true)][string]$ZipPath,
        [Parameter(Mandatory = $true)][string]$EntryName
    )

    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $zip = [IO.Compression.ZipFile]::OpenRead($ZipPath)
    try {
        $found = $zip.Entries | Where-Object { $_.FullName -eq $EntryName }
        if (-not $found) { throw "Entrada obrigatória ausente no ZIP: $EntryName" }
    }
    finally {
        $zip.Dispose()
    }
}

Write-Host "==> Compilando launchers com textos em português brasileiro"
$LauncherDir = Join-Path $DistDir "launchers"
New-Item -ItemType Directory -Force -Path $LauncherDir | Out-Null
$launcherTargets = @(
    @{ Name = "NexoFlow Analytics.exe"; Tag = "analytics"; Description = "NexoFlow Analytics — análise de dados" },
    @{ Name = "NexoFlow Weni.exe"; Tag = "updateweni"; Description = "NexoFlow Weni — atualização de contatos" },
    @{ Name = "NexoFlow Recompra.exe"; Tag = "recompra"; Description = "NexoFlow Recompra — grupo FarmaLíder D+25" },
    @{ Name = "NexoFlow Planilha.exe"; Tag = "planilha"; Description = "NexoFlow Planilha — cargas por planilhas" },
    @{ Name = "NexoFlow Terminal.exe"; Tag = "terminal"; Description = "NexoFlow Terminal — consultas e diagnóstico" },
    @{ Name = "NexoFlow Uninstall.exe"; Tag = "uninstall"; Description = "NexoFlow — desinstalação" }
)
Push-Location $RepoRoot
$previousGoModule = $env:GO111MODULE
try {
    $env:GO111MODULE = "off"
    foreach ($target in $launcherTargets) {
        $output = Join-Path $LauncherDir $target.Name
        & go build -trimpath -tags $target.Tag -ldflags "-s -w" -o $output ./native_launcher
        if ($LASTEXITCODE -ne 0) { throw "Falha ao compilar $($target.Name)" }
        & (Join-Path $PSScriptRoot "embed_windows_version.ps1") -ExecutablePath $output -Description $target.Description
    }
}
finally {
    if ($null -eq $previousGoModule) { Remove-Item Env:GO111MODULE -ErrorAction SilentlyContinue }
    else { $env:GO111MODULE = $previousGoModule }
    Pop-Location
}

Write-Host "==> Montando projeto completo NexoFlow $Version"
Copy-TrackedFiles -Destination $ProjectRoot

Write-Host "==> Montando payload do Setup"
Copy-TrackedFiles -Destination $PayloadDir

foreach ($folder in @($ProjectRoot, $PayloadDir)) {
    New-Item -ItemType Directory -Force -Path (Join-Path $folder 'importacoes') | Out-Null
    foreach ($target in $launcherTargets) {
        Copy-Item -LiteralPath (Join-Path $LauncherDir $target.Name) -Destination (Join-Path $folder $target.Name) -Force
    }
}

# Um .ico ao lado do EXE não altera o ícone exibido pelo Explorer.
# Cada launcher deve incorporar seu próprio RT_ICON + RT_GROUP_ICON.
$launcherIcons = @(
    @{ Executable = "NexoFlow Analytics.exe"; Icon = "NexoFlow.ico" },
    @{ Executable = "NexoFlow Weni.exe"; Icon = "NexoFlow_Weni.ico" },
    @{ Executable = "NexoFlow Recompra.exe"; Icon = "NexoFlow_Recompra.ico" },
    @{ Executable = "NexoFlow Planilha.exe"; Icon = "NexoFlow_Planilha.ico" },
    @{ Executable = "NexoFlow Terminal.exe"; Icon = "NexoFlow_Terminal.ico" },
    @{ Executable = "NexoFlow Uninstall.exe"; Icon = "uninstaller.ico" }
)
Write-Host "==> Incorporando ícones PE dos executáveis no projeto e no payload"
foreach ($folder in @($ProjectRoot, $PayloadDir)) {
    foreach ($item in $launcherIcons) {
        & (Join-Path $PSScriptRoot "embed_windows_icon.ps1") `
            -ExecutablePath (Join-Path $folder $item.Executable) `
            -IconPath (Join-Path (Join-Path $RepoRoot "assets") $item.Icon)
    }
}

# O manifest deve refletir os EXEs depois da incorporação dos ícones.
Write-Host "==> Auditando os executáveis recompilados no projeto completo"
Push-Location $ProjectRoot
try {
    & python app/auditar_consistencia.py
    if ($LASTEXITCODE -ne 0) { throw "A auditoria dos arquivos empacotados falhou." }
}
finally { Pop-Location }
Write-PayloadManifest -Payload $PayloadDir

New-Item -ItemType Directory -Force -Path (Join-Path $SetupRoot "installer") | Out-Null
Copy-Item -LiteralPath (Join-Path $RepoRoot "installer\install.ps1") -Destination (Join-Path $SetupRoot "installer\install.ps1") -Force

New-Item -ItemType Directory -Force -Path (Join-Path $SetupRoot "prerequisites") | Out-Null
Copy-Item -LiteralPath (Join-Path $RepoRoot "prerequisites\python-3.14.8-amd64.exe") -Destination (Join-Path $SetupRoot "prerequisites\python-3.14.8-amd64.exe") -Force

$readme = @"
NexoFlow $Version - SETUP

Este pacote corresponde ao NexoFlow $Version.

Extraia TODO o ZIP antes de executar.
Depois execute: NexoFlow Setup.exe

O Setup solicita elevação do Windows automaticamente quando necessário.
Atualizações preservam a instalação anterior para rollback automático se houver falha.

O launcher NexoFlow Uninstall.exe permanece opcional.
Se um antivírus corporativo removê-lo antes ou depois da instalação, o Setup continua funcionando.
A desinstalação permanece disponível por installer\uninstall.ps1 e
desinstalar_nexoflow.cmd.

Não coloque credenciais dentro deste pacote.
"@
[IO.File]::WriteAllText((Join-Path $SetupRoot "LEIA-ME INSTALACAO.txt"), $readme, (New-Object Text.UTF8Encoding($false)))
[IO.File]::WriteAllText((Join-Path $SetupRoot "SETUP VALIDADO.txt"), "NexoFlow $Version - Setup validado automaticamente em Windows para instalação, atualização e ausência do Uninstall opcional.", (New-Object Text.UTF8Encoding($false)))

$signing = @'
param()
Write-Host "Assinatura Authenticode não automatizada neste pacote."
Write-Host "Assine os executáveis somente quando houver certificado corporativo confiável."
'@
[IO.File]::WriteAllText((Join-Path $SetupRoot "Assinar Release.ps1"), $signing, (New-Object Text.UTF8Encoding($true)))

Write-Host "==> Compilando launcher do Setup"
Push-Location $RepoRoot
$oldGo111Module = $env:GO111MODULE
try {
    $env:GO111MODULE = "off"
    & go build -trimpath -tags setup -ldflags "-s -w" -o (Join-Path $SetupRoot "NexoFlow Setup.exe") ./native_launcher
    if ($LASTEXITCODE -ne 0) { throw "Falha ao compilar NexoFlow Setup.exe" }
}
finally {
    if ($null -eq $oldGo111Module) {
        Remove-Item Env:GO111MODULE -ErrorAction SilentlyContinue
    } else {
        $env:GO111MODULE = $oldGo111Module
    }
    Pop-Location
}

Write-Host "==> Incorporando ícone próprio no NexoFlow Setup.exe"
& (Join-Path $PSScriptRoot "embed_windows_version.ps1") `
    -ExecutablePath (Join-Path $SetupRoot "NexoFlow Setup.exe") `
    -Description "NexoFlow — instalação e atualização"
& (Join-Path $PSScriptRoot "embed_windows_icon.ps1") `
    -ExecutablePath (Join-Path $SetupRoot "NexoFlow Setup.exe") `
    -IconPath (Join-Path (Join-Path $RepoRoot "assets") "installer.ico")

Write-Host "==> Gerando checksums internos do Setup"
Write-Checksums -Root $SetupRoot -OutputFile (Join-Path $SetupRoot "checksums SHA256.txt")

$SetupZip = Join-Path $DistDir "NexoFlow-Setup-$Version.zip"
$ProjectZip = Join-Path $DistDir "NexoFlow-$Version.zip"
$ShaFile = Join-Path $DistDir "SHA256SUMS-$Version.txt"

Write-Host "==> Compactando Setup"
Compress-Archive -Path $SetupRoot -DestinationPath $SetupZip -CompressionLevel Optimal -Force

Write-Host "==> Compactando projeto completo"
Compress-Archive -Path $ProjectRoot -DestinationPath $ProjectZip -CompressionLevel Optimal -Force

# Garantir a entrada vazia mesmo em ferramentas que omitam diretórios sem arquivos.
Add-Type -AssemblyName System.IO.Compression.FileSystem
foreach ($item in @(
    @{ Path = $SetupZip; Entry = 'NexoFlow Setup/payload/importacoes/' },
    @{ Path = $ProjectZip; Entry = 'NexoFlow/importacoes/' }
)) {
    $archive = [IO.Compression.ZipFile]::Open($item.Path, [IO.Compression.ZipArchiveMode]::Update)
    try {
        if (-not $archive.GetEntry($item.Entry)) { $archive.CreateEntry($item.Entry) | Out-Null }
    } finally { $archive.Dispose() }
    Assert-ZipEntry -ZipPath $item.Path -EntryName $item.Entry
}


Assert-ZipEntry -ZipPath $SetupZip -EntryName "NexoFlow Setup/NexoFlow Setup.exe"
Assert-ZipEntry -ZipPath $SetupZip -EntryName "NexoFlow Setup/payload/payload manifest.json"
Assert-ZipEntry -ZipPath $SetupZip -EntryName "NexoFlow Setup/payload/NexoFlow Recompra.exe"
Assert-ZipEntry -ZipPath $ProjectZip -EntryName "NexoFlow/NexoFlow Recompra.exe"
Assert-ZipEntry -ZipPath $SetupZip -EntryName "NexoFlow Setup/payload/app/weni_update.py"
foreach ($module in @("weni_dedup", "contact_dedup_guard", "dedup_io", "dedup_sheets", "dedup_bi")) {
    Assert-ZipEntry -ZipPath $SetupZip -EntryName "NexoFlow Setup/payload/app/$module.py"
    Assert-ZipEntry -ZipPath $ProjectZip -EntryName "NexoFlow/app/$module.py"
}
Assert-ZipEntry -ZipPath $SetupZip -EntryName "NexoFlow Setup/payload/app/task_progress.py"
Assert-ZipEntry -ZipPath $ProjectZip -EntryName "NexoFlow/app/task_progress.py"
Assert-ZipEntry -ZipPath $SetupZip -EntryName "NexoFlow Setup/payload/app/observed_orders.py"
Assert-ZipEntry -ZipPath $ProjectZip -EntryName "NexoFlow/app/observed_orders.py"
Assert-ZipEntry -ZipPath $SetupZip -EntryName "NexoFlow Setup/payload/app/analytics.py"
Assert-ZipEntry -ZipPath $SetupZip -EntryName "NexoFlow Setup/payload/app/looker_bi.py"
Assert-ZipEntry -ZipPath $SetupZip -EntryName "NexoFlow Setup/payload/app/run_looker.py"
Assert-ZipEntry -ZipPath $ProjectZip -EntryName "NexoFlow/app/looker_bi.py"
Assert-ZipEntry -ZipPath $SetupZip -EntryName "NexoFlow Setup/payload/scripts/confirmar_revisao_weni.ps1"
Assert-ZipEntry -ZipPath $ProjectZip -EntryName "NexoFlow/scripts/confirmar_revisao_weni.ps1"
Assert-ZipEntry -ZipPath $SetupZip -EntryName "NexoFlow Setup/payload/scripts/resultado_carga_weni.ps1"
Assert-ZipEntry -ZipPath $ProjectZip -EntryName "NexoFlow/scripts/resultado_carga_weni.ps1"
Assert-ZipEntry -ZipPath $SetupZip -EntryName "NexoFlow Setup/payload/scripts/build_documentation.py"
Assert-ZipEntry -ZipPath $ProjectZip -EntryName "NexoFlow/scripts/build_documentation.py"

$setupHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $SetupZip).Hash.ToLowerInvariant()
$projectHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $ProjectZip).Hash.ToLowerInvariant()

$shaLines = @(
    "$setupHash  NexoFlow-Setup-$Version.zip",
    "$projectHash  NexoFlow-$Version.zip"
)
[IO.File]::WriteAllLines($ShaFile, $shaLines, (New-Object Text.UTF8Encoding($false)))

$releaseNotes = @"
# NexoFlow $Version

Distribuição oficial do **NexoFlow 1.0**.

O arquivo `NexoFlow-Setup-$Version.zip` contém o instalador do NexoFlow 1.0 e foi validado automaticamente em Windows para primeira instalação e atualização de uma instalação existente.

Melhorias desta distribuição do NexoFlow 1.0:
- Planilha aceita as constantes booleanas originais do export de conversas no campo is_active, mantendo o filtro de atendimentos encerrados e o bloqueio de outras fórmulas;
- deduplicação integrada às cargas Weni, com revisão de exclusões, CPF oficial, telefone nativo único e propriedade dos pedidos; contatos incorretos saem da Weni e das fontes do Sheets/Looker, preservando vendas e recusas;
- falhas isoladas da Weni geram aviso e pendência, sem cancelar a carga; a próxima incremental revalida os contatos e recupera suas mensagens anteriores quando necessário;
- tabelas comerciais para Looker Studio com histórico de vendas, KPIs e Curva ABC com cobertura de itens; tipos monetários corrigidos e migração financeira atômica;
- check-up corrigido para a codificação padrão do Windows; escritas no Sheets reconciliam inclusões após timeout; validação de código, tipos e dependências antes da publicação;
- Weni com ícone azul e ciano, contato e balão de conversa, seguindo a estética dos outros componentes do NexoFlow;
- Recompra com ícone próprio de sacola e setas circulares; Planilha com ícone circular azul e ciano, proporção alinhada aos demais componentes, nos executáveis e atalhos;
- desinstalação remove os atalhos oficiais de todos os componentes, incluindo Recompra, nos locais de instalação e nos desktops e menus atendidos pelo Setup;
- CMD da Weni permanece aberto após conclusão, erro ou cancelamento, inclusive por outro launcher; fechar o aviso final ou pressionar ENTER não encerra a sessão; use EXIT para fechar;
- progresso Weni a cada 2 segundos com barra de 0% a 100% por etapa, contatos/registros por segundo, tempo decorrido e previsão de término; auditoria JSONL mede bytes efetivamente gravados por segundo; consultas sem total conhecido mostram quantidade e velocidade;
- o NexoFlow não inclui chaves, tokens ou credenciais; use suas próprias credenciais nas integrações, conforme SECURITY.md;
- identificação e documentação padronizadas como NexoFlow 1.0, com os componentes Planilha e Recompra nos índices e instruções de assinatura e desinstalação alinhadas ao Setup;
- recompra processa contatos durante a paginação, consulta a FarmaLíder com até quatro workers e cache limitado por execução, reduzindo leituras e gravações redundantes sem alterar a validação de identidade;
- revisão de recompra expirada na virada do dia gera um novo dry-run uma vez no launcher e exige nova confirmação; se a aplicação já iniciou, interrompe e preserva o estado;
- POSTs da recompra usam tentativa única para evitar repetição de inclusões com resposta incerta; GETs mantêm retentativas e o controle persistente continua protegendo os contatos;
- recompra com grupo de 0 a 25 dias, revisão, controle persistente e proteção de referências; envio D+25 permanece na campanha Weni; confira docs/README RECOMPRA.md antes de habilitar;
- cidade, estado, CEP, bairro e idade dos clientes da Weni na base e BI_Pedidos; CEP preservado como texto e datas de pedido mantidas;
- interface e documentação em português brasileiro, com acentos e cedilha; CMD em UTF-8, PowerShell com BOM e executáveis recompilados com metadados pt-BR;
- carga completa Weni 100% ou em partes mantém o CMD aguardando a revisão humana; cancelamentos não aplicam o snapshot e falhas isoladas ficam pendentes sem interromper os demais contatos;
- componente de confirmação incluído e validado no projeto completo e no Setup;
- de-para de pagamento atualizado: 0 Cancelado; 1 Dinheiro; 2 Débito; 3 Crédito; 4 Ticket; 5 Pix por Link de Pagamento; 6 Pix no POS;
- pagamento 0 cancela o pedido: forma de pagamento Cancelado e status 3 na Weni e no BI, preservando os códigos brutos da API na tabela de pedidos;
- de-para de status atualizado: EmAberto=-2; PendenteDePagamento=-1; EmProcessamento=0; ProntoParaRota=4; EmTransito=1; Concluido=2; Cancelado=3;
- vendas e ultima_compra consideram todos os estados documentados não cancelados (-2, -1, 0, 4, 1 e 2), com validação da API; cada carga do Analytics reconsulta por código os pedidos oficiais já observados somente nos últimos 20 dias, inclusive concluídos e cancelados nesse intervalo; pedidos mais antigos permanecem no histórico, e a consulta por CPF continua identificando compras novas;
- diagnóstico seguro de proxy identifica autenticação 407, bloqueio de túnel 403, TLS, DNS, timeout e conexão recusada; resposta 400/404 da FarmaLíder não é apresentada como autenticação confirmada;
- listas de pedido com leitura CSV preservam vírgulas e aspas nos nomes dos produtos, sem desalinhamento artificial na Weni e no Analytics;
- códigos numéricos de pagamento não documentados são preservados e exibidos como não identificados, com o código recebido, sem inventar a forma de pagamento;
- pagamento sem de-para aparece em INFO uma vez por código; resumo por código e progresso distinguem contatos com pedido validado de alterações propostas e gravadas;
- preparação e auditoria continuam antes de qualquer gravação; listas realmente inconsistentes permanecem bloqueadas;
- cooldown global e recuperação serial após falhas 5xx da FarmaLíder, com retorno gradual da concorrência e redução do intervalo quando a API se recupera;
- HTTP 404 continua representando cliente não encontrado; HTTP 500 permanece uma falha da API;
- zeros à esquerda são restaurados antes da validação quando os verificadores do CPF conferem; CPF válido dispensa comparação de identidade, e CPF inválido exige primeiro nome ou telefone local correspondente, ignorando +55 e DDD;
- consultas por CPF que já falharam não são repetidas na mesma carga; dados previamente confirmados são preservados quando o CPF continua o mesmo;
- cargas incompletas aparecem como parciais e preservam o marco incremental para uma nova tentativa;

- carga por planilhas de conversas e contatos com comandos nexo weni planilhas e nexo analytics planilhas, validação de identidade e revisão humana por destino;
- cargas por planilha Weni e Analytics aguardam confirmação gráfica explícita, mesmo sem entrada de console; fechar o Bloco de Notas não encerra a espera;
- pasta importacoes vazia incluída no Setup e no projeto completo, sem exports XLSX;
- importacoes e seus arquivos sempre preservados na desinstalação, inclusive ao remover configurações, credenciais e logs;
- backups anteriores com conteúdo diferente mantidos em historico_preservado, com validação SHA-256 antes de remover a instalação.

Principais garantias do Setup:
- elevação UAC automática quando necessária;
- rollback automático se uma atualização falhar;
- NexoFlow Uninstall.exe opcional;
- integridade SHA-256 dos componentes obrigatórios;
- preservação de secrets_config.py, credentials e logs em atualizações.

Downloads:
- NexoFlow-Setup-$Version.zip - instalador do NexoFlow 1.0;
- NexoFlow-$Version.zip - projeto completo do NexoFlow 1.0;
- SHA256SUMS-$Version.txt - hashes SHA-256.

Recursos incluídos:
- NexoFlow Analytics;
- NexoFlow Weni;
- NexoFlow Terminal;
- NexoFlow Planilha, com menu para Weni e Analytics, ícone próprio e atalhos;
- integração Weni, FarmaLíder, Google Sheets e Looker Studio;
- tratamento de pedidos, status, LGPD, canais e atribuição de anúncios.
"@
[IO.File]::WriteAllText((Join-Path $DistDir "release-notes.md"), $releaseNotes, (New-Object Text.UTF8Encoding($false)))

Write-Host "==> Artefatos gerados"
Get-ChildItem -LiteralPath $DistDir -File | Select-Object Name,Length
# NexoFlow 1.0
