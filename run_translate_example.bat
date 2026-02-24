@echo off
REM ============================================================
REM Exemplo simples (Windows) - Tradução de PDF
REM ============================================================
REM 1) Coloque um PDF em inglês dentro da pasta "input"
REM 2) Ajuste o nome abaixo
REM 3) Execute este .bat com duplo clique
REM ============================================================

set "BASE=%~dp0"
cd /d "%BASE%"

REM Ajuste aqui:
REM - Por padrão, usamos o PDF de exemplo incluído no projeto.
set "PDF=input\prototype_english.pdf"

powershell -NoProfile -ExecutionPolicy Bypass -File "%BASE%setup_and_translate_windows.ps1" -Pdf "%PDF%" -StartLibreTranslateDocker

pause
