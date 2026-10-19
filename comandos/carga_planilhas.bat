@echo off
chcp 65001 >nul
setlocal EnableExtensions DisableDelayedExpansion
for %%I in ("%~dp0..") do set "NEXOFLOW_HOME=%%~fI"
set "PYTHONPATH=%NEXOFLOW_HOME%;%NEXOFLOW_HOME%\app;%PYTHONPATH%"
set "PYTHONIOENCODING=utf-8"
set "STRICT_ARG="
if defined NEXOFLOW_FULL_WORKFLOW set "STRICT_ARG=--require-complete"
cd /d "%NEXOFLOW_HOME%\"
set "DESTINO=%~1"
if /I "%DESTINO%"=="weni" goto :destino_ok
if /I "%DESTINO%"=="analytics" goto :destino_ok
echo [ERRO] Destino inválido. Use os comandos nexo weni planilhas ou nexo analytics planilhas.
exit /b 1

:destino_ok
title NexoFlow 1.0 - Carga por planilhas
set "REVIEW_JSON=logs\delivery_%DESTINO%_review.json"
set "REVIEW_TXT=logs\delivery_%DESTINO%_review.txt"
set "ARCHIVE_ARG="
if not exist "importacoes" mkdir "importacoes"
if not exist ".venv\Scripts\python.exe" (
    echo [ERRO] Ambiente virtual ausente. Execute instalar.bat primeiro.
    set "RC=1"
    goto :fim
)
echo ==========================================================
echo NexoFlow 1.0 - Carga por planilhas - %DESTINO%
echo ==========================================================
echo Coloque os exports originais na pasta importacoes:
echo   Conversas_Delivery.xlsx
echo   Contatos_Delivery.xlsx
echo.
if exist "%REVIEW_JSON%" goto :revisao_existente

:consultar
echo [1/3] Validando vínculos e consultando pedidos na FarmaLíder...
".venv\Scripts\python.exe" app\run_delivery_tickets.py --dry-run %STRICT_ARG% %ARCHIVE_ARG% --planilhas --target "%DESTINO%" --review-out "%REVIEW_JSON%"
set "RC=%ERRORLEVEL%"
if not "%RC%"=="0" goto :erro_consulta
goto :revisar

:revisao_existente
echo Já existe uma revisão para este destino.
echo [1] Retomar a revisão existente, conferindo o estado atual
echo [2] Gerar nova revisão, preservando a anterior no histórico
echo [3] Cancelar
choice /C 123 /N /M "Escolha uma opção: "
if errorlevel 3 (
    set "RC=2"
    goto :cancelado
)
if errorlevel 2 (
    set "ARCHIVE_ARG=--archive-review"
    goto :consultar
)
if errorlevel 1 goto :revisar
set "RC=2"
goto :cancelado

:revisar
if not exist "%REVIEW_TXT%" (
    echo [ERRO] Relatório de revisão ausente. Gere uma nova revisão preservando a anterior.
    set "RC=1"
    goto :fim
)
echo.
echo [2/3] Revise o relatório no Bloco de Notas.
echo Confira identidade, pedidos, datas, exclusões e destino da carga.
start "" notepad.exe "%CD%\%REVIEW_TXT%" >nul 2>&1
set "RC=%ERRORLEVEL%"
if not "%RC%"=="0" goto :erro_confirmacao
echo O CMD permanecerá aguardando uma confirmação em janela separada.
echo Digite PLANILHAS REVISADAS nessa janela somente depois de revisar o relatório.
"%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" -NoLogo -NoProfile -STA -ExecutionPolicy Bypass -File "%NEXOFLOW_HOME%\scripts\confirmar_revisao_weni.ps1" -Expected "PLANILHAS REVISADAS" -Title "NexoFlow %DESTINO% - Revisão das Planilhas"
set "RC=%ERRORLEVEL%"
if "%RC%"=="2" goto :cancelado
if not "%RC%"=="0" goto :erro_confirmacao
echo.
echo [3/3] Revalidando os contatos e aplicando o snapshot revisado...
".venv\Scripts\python.exe" app\run_delivery_tickets.py --apply --target "%DESTINO%" --require-review "%REVIEW_JSON%"
set "RC=%ERRORLEVEL%"
if "%RC%"=="3" (
    echo [AVISO] Carga parcial. Os contatos aceitos foram preservados; os conflitos não foram aplicados.
    echo Consulte o diagnóstico privado indicado no console e logs\nexoflow_delivery.log.
    echo Execute esta opção novamente e escolha Retomar a revisão existente.
    echo Se o contato continuar divergente, gere e revise um novo snapshot; o anterior será preservado.
    goto :fim
)
if not "%RC%"=="0" (
    echo [ERRO] Aplicação interrompida. Consulte o relatório e logs\nexoflow_delivery.log.
    echo Para conferir e retomar ações pendentes, execute esta opção novamente e escolha Retomar.
    goto :fim
)
echo [OK] Carga por planilhas concluída.
goto :fim

:cancelado
echo [INFO] Operação cancelada. Nenhuma alteração foi aplicada.
goto :fim

:erro_confirmacao
echo [ERRO] Não foi possível confirmar a revisão. Nenhuma alteração foi aplicada.
goto :fim

:erro_consulta
echo [ERRO] Consulta ou auditoria falhou. Nenhuma gravação foi iniciada.

:fim
if not defined NEXOFLOW_LAUNCHER pause
exit /b %RC%
