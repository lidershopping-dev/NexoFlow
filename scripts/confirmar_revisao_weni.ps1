[CmdletBinding()]
param(
    [string]$Part = "",
    [string]$Expected = "CARGA COMPLETA REVISADA",
    [string]$Title = "NexoFlow Weni - Revisão Humana"
)

$ErrorActionPreference = "Stop"

function Show-WeniReviewDialog {
    param([string]$Message, [string]$Title)

    Add-Type -AssemblyName Microsoft.VisualBasic
    return [Microsoft.VisualBasic.Interaction]::InputBox($Message, $Title, "")
}

function Confirm-WeniReview {
    param([string]$Part = "", [string]$Expected = "CARGA COMPLETA REVISADA", [string]$Title = "NexoFlow Weni - Revisão Humana")

    try {
        if ($Expected.Equals("ATUALIZAR", [System.StringComparison]::OrdinalIgnoreCase)) {
            $message = @"
O dry-run incremental foi concluído e auditado.

A aplicação inclui as exclusões de duplicados propostas no relatório
e a limpeza das fontes do Google Sheets e do Looker.

Para autorizar as alterações revisadas, digite exatamente:

$expected

Fechar ou cancelar esta janela NÃO aplica nenhuma alteração.
"@
        }
        else {
            $partText = ""
            if ($Part) { $partText = " da Parte $Part/4" }
            $message = @"
Revise o relatório aberto no Bloco de Notas$partText.

Depois de concluir a revisão, digite exatamente:

$expected

Fechar ou cancelar esta janela NÃO aplica nenhuma alteração.
"@
        }
        $response = [string](Show-WeniReviewDialog -Message $message -Title $title)
        if ($response.Trim().Equals($expected, [System.StringComparison]::OrdinalIgnoreCase)) {
            Write-Host "[OK] Revisão humana confirmada."
            return 0
        }
        Write-Host "[INFO] Confirmação humana não recebida. Nenhuma alteração será aplicada."
        return 2
    }
    catch {
        Write-Host "[ERRO] Não foi possível abrir a confirmação humana gráfica."
        Write-Host "[ERRO] Nenhuma alteração será aplicada."
        return 3
    }
}

if ($MyInvocation.InvocationName -ne '.') {
    exit (Confirm-WeniReview -Part $Part -Expected $Expected -Title $Title)
}
