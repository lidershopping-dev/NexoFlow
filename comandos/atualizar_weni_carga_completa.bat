@echo off
chcp 65001 >nul
setlocal EnableExtensions DisableDelayedExpansion
for %%I in ("%~dp0..") do set "NEXOFLOW_HOME=%%~fI"
set "PYTHONPATH=%NEXOFLOW_HOME%;%NEXOFLOW_HOME%\app;%PYTHONPATH%"
set "PYTHONIOENCODING=utf-8"
cd /d "%NEXOFLOW_HOME%\"
title NexoFlow Weni - Carga Completa
set "NEXOFLOW_LAUNCHER=1"
set "WORKFLOW_MODE="
set "STRICT_ARG="
if /i "%~1"=="--fluxo-completo" (
    set "WORKFLOW_MODE=1"
    set "STRICT_ARG=--require-complete"
    set "OPCAO=0"
    goto :opcao_escolhida
)

cls
echo ==========================================================
echo NexoFlow Weni - Carga Completa
echo ==========================================================
echo.
echo Escolha o tamanho da carga:
echo.
echo   [0] 100%% - carga completa tradicional
echo   [1] Parte 1/4 - primeiros 25%%
echo   [2] Parte 2/4 - segundos 25%%
echo   [3] Parte 3/4 - terceiros 25%%
echo   [4] Parte 4/4 - últimos 25%%
echo.
echo As partes 25%% NÃO são incrementais.
echo Elas usam a mesma base congelada e devem ser executadas em ordem.
echo Cada parte gera seu próprio dry-run, relatório e snapshot revisável.
echo.
set "OPCAO="
set /p "OPCAO=Opção: " < CON


:opcao_escolhida
set "PART="
set "PART_ARG="
set "REVIEW_JSON=logs\weni_update_review_full.json"
set "REVIEW_TXT=logs\weni_update_review_full.txt"

if "%OPCAO%"=="0" goto :configurado
if "%OPCAO%"=="1" set "PART=1"
if "%OPCAO%"=="2" set "PART=2"
if "%OPCAO%"=="3" set "PART=3"
if "%OPCAO%"=="4" set "PART=4"
if not defined PART (
    echo [ERRO] Opção inválida.
    call :aguardar_saida
    exit /b 2
)

set "PART_ARG=--quarter %PART%"
set "REVIEW_JSON=logs\weni_update_review_full_25_parte_%PART%.json"
set "REVIEW_TXT=logs\weni_update_review_full_25_parte_%PART%.txt"

:configurado
if not exist "%NEXOFLOW_HOME%\scripts\confirmar_revisao_weni.ps1" (
    echo [ERRO] Componente de revisão ausente: scripts\confirmar_revisao_weni.ps1
    echo Reinstale o Setup atualizado do NexoFlow 1.0 antes de continuar.
    set "RC=3"
    goto :erro
)
if not exist "%NEXOFLOW_HOME%\scripts\resultado_carga_weni.ps1" (
    echo [ERRO] Componente de resultado ausente. Reinstale o Setup atualizado.
    set "RC=3"
    goto :erro
)
echo.
if defined PART (
    echo [INFO] Carga completa Weni 25%% - Parte %PART%/4.
) else (
    echo [INFO] Carga completa Weni tradicional - 100%%.
)
echo.
echo SEGURANÇA NexoFlow 1.0 - ATENDIMENTO 24H:
echo   1. check-up
echo   2. teste de acesso
echo   3. dry-run + auditoria + snapshot congelado
echo   4. revisão humana
echo   5. apply EXATAMENTE do snapshot revisado
echo   6. validação individual antes de cada contato
echo.
if defined PART (
    echo Na carga 25%%, somente os contatos da Parte %PART%/4 entram no plano.
    echo A próxima parte reutiliza o mesmo manifesto congelado.
) else (
    echo Esta rotina reanalisa 100%% da base disponível.
)
echo.

call "%NEXOFLOW_HOME%\comandos\check-up.bat"
set "RC=%ERRORLEVEL%"
if not "%RC%"=="0" goto :erro

call "%NEXOFLOW_HOME%\comandos\testar_update_weni.bat"
set "RC=%ERRORLEVEL%"
if not "%RC%"=="0" goto :erro

if exist "%REVIEW_JSON%" del /q "%REVIEW_JSON%" >nul 2>&1
if exist "%REVIEW_TXT%" del /q "%REVIEW_TXT%" >nul 2>&1

echo.
echo [ETAPA 3/6] Dry-run + auditoria + snapshot congelado
echo ----------------------------------------------------------
".venv\Scripts\python.exe" app\run_update_weni.py --dry-run --full %PART_ARG% %STRICT_ARG% --review-out "%REVIEW_JSON%"
set "RC=%ERRORLEVEL%"
if not "%RC%"=="0" goto :erro

if not exist "%REVIEW_JSON%" (
    echo [ERRO] Snapshot de revisão não foi gerado: %REVIEW_JSON%
    set "RC=3"
    goto :erro
)
if not exist "%REVIEW_TXT%" (
    echo [ERRO] Relatório de revisão não foi gerado: %REVIEW_TXT%
    set "RC=3"
    goto :erro
)

echo.
echo ==========================================================
echo REVISÃO HUMANA OBRIGATÓRIA
echo ==========================================================
if defined PART echo Parte revisada: %PART%/4
echo O relatório será aberto no Bloco de Notas.
echo Confira especialmente LGPD, idade/maioridade, grupos, CPF, pedidos e exclusões de duplicados.
echo Encerre outras cargas de Analytics, Planilhas e Recompra antes de aplicar.
echo O plano fica congelado enquanto você revisa.
echo O CMD ficará aguardando a confirmação enquanto você revisa.
echo Depois de concluir, feche o Bloco de Notas e volte ao CMD.
echo.
rem Não usar START /WAIT: o Bloco de Notas moderno pode usar processo intermediário
rem e encerrar/alterar a espera do console. A confirmação abaixo bloqueia no CON.
start "" notepad.exe "%CD%\%REVIEW_TXT%"
set "RC=%ERRORLEVEL%"
if not "%RC%"=="0" goto :erro

echo.
echo [ETAPA 4/6] Confirmação humana
echo ----------------------------------------------------------
echo [INFO] O CMD permanecerá aguardando uma confirmação em janela separada.
echo Digite CARGA COMPLETA REVISADA nessa janela somente depois de revisar o relatório.
set "CONFIRM_PART="
if defined PART set "CONFIRM_PART=-Part %PART%"
"%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" -NoLogo -NoProfile -STA -ExecutionPolicy Bypass -File "%NEXOFLOW_HOME%\scripts\confirmar_revisao_weni.ps1" %CONFIRM_PART%
set "RC=%ERRORLEVEL%"
if "%RC%"=="2" goto :cancelado
if not "%RC%"=="0" goto :erro

echo.
"%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" -NoLogo -NoProfile -STA -ExecutionPolicy Bypass -File "%NEXOFLOW_HOME%\scripts\resultado_carga_weni.ps1" -RecordOnly -Stage "APPLY_INICIADO" -Snapshot "%REVIEW_JSON%"
set "RC=%ERRORLEVEL%"
if not "%RC%"=="0" goto :erro
echo [ETAPA 5/6] Aplicando snapshot congelado com validação por contato
echo ----------------------------------------------------------
".venv\Scripts\python.exe" app\run_update_weni.py --apply --full %PART_ARG% %STRICT_ARG% --require-review "%REVIEW_JSON%"
set "RC=%ERRORLEVEL%"
"%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" -NoLogo -NoProfile -STA -ExecutionPolicy Bypass -File "%NEXOFLOW_HOME%\scripts\resultado_carga_weni.ps1" -RecordOnly -Stage "APPLY_RETORNOU" -ResultCode %RC% -Snapshot "%REVIEW_JSON%"
if not "%RC%"=="0" goto :erro

echo.
if defined WORKFLOW_MODE goto :recompra_fim
if defined PART if not "%PART%"=="4" goto :recompra_fim
rem Recompra é uma etapa independente, com auditoria e revisão próprias.
".venv\Scripts\python.exe" -c "import sys,secrets_config as s; sys.exit(0 if getattr(s,'RECOMPRA_ENABLED',False) else 10)"
if not "%ERRORLEVEL%"=="0" goto :recompra_fim
call "%NEXOFLOW_HOME%\comandos\atualizar_recompra.bat"
if errorlevel 1 echo [AVISO] Carga Weni concluída; recompra pendente. Consulte logs\nexoflow_recompra.log.
:recompra_fim
echo [ETAPA 6/6] Carga concluída
echo ==========================================================
if defined PART (
    echo [OK] Parte %PART%/4 concluída.
    if not "%PART%"=="4" echo Execute a próxima parte quando desejar.
    if "%PART%"=="4" echo [OK] As quatro partes Weni foram concluídas nesta campanha.
) else (
    echo [OK] Carga completa 100%% concluída.
)
echo Relatório: %REVIEW_TXT%
echo Snapshot:  %REVIEW_JSON%
echo Manifesto 25%%: logs\full_load_25_manifest.json
echo ==========================================================
set "RC=0"
set "RESULT_STAGE=CONCLUIDO"
call :resultado
exit /b 0

:cancelado
echo.
echo [INFO] Operação cancelada. Nenhuma alteração foi aplicada.
echo O snapshot permanece disponível para consulta. Execute novamente para gerar uma nova revisão.
set "RC=2"
set "RESULT_STAGE=CANCELADO"
call :resultado
exit /b 2

:erro
echo.
echo [ERRO] Código de saída: %RC%
echo [ERRO] Processo interrompido. Nenhuma etapa posterior deve ser presumida como concluída.
if defined PART echo [INFO] A Parte %PART%/4 NÃO deve ser presumida como concluída.
echo Consulte logs\nexoflow_update_weni.log.
set "RESULT_STAGE=ERRO"
call :resultado
exit /b %RC%

:resultado
if defined WORKFLOW_MODE exit /b 0
echo [INFO] O resultado permanecerá no terminal. Digite EXIT quando quiser fechar.
if not exist "%NEXOFLOW_HOME%\scripts\resultado_carga_weni.ps1" (
    call :aguardar_saida
    exit /b
)
"%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" -NoLogo -NoProfile -STA -ExecutionPolicy Bypass -File "%NEXOFLOW_HOME%\scripts\resultado_carga_weni.ps1" -ResultCode %RC% -Stage "%RESULT_STAGE%" -Snapshot "%REVIEW_JSON%"
call :aguardar_saida
exit /b 0

:aguardar_saida
if defined WORKFLOW_MODE exit /b 0
if defined NEXOFLOW_TERMINAL exit /b 0
setlocal EnableDelayedExpansion
:aguardar_exit
set "WENI_EXIT="
set /p "WENI_EXIT=Digite EXIT para fechar esta janela: " < CON
if /i "!WENI_EXIT!"=="EXIT" goto :saida_confirmada
goto :aguardar_exit
:saida_confirmada
endlocal
exit /b 0
