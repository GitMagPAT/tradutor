param(
  [string]$Base = ".",
  [string]$Pdf = "input\meu_arquivo.pdf",
  [string]$Out = "",
  [ValidateSet("opusmt","libretranslate","translategemma","mymemory","dummy")]
  [string]$Translator = "opusmt",
  [string]$SourceLang = "en",
  [string]$TargetLang = "pb",
  [int]$Dpi = 300,
  [ValidateSet("pdf_overlay","pdf_overlay_original","raster")]
  [string]$RenderMode = "pdf_overlay",
  [int]$StartPage = 0,
  [int]$EndPage = 22
)

$ErrorActionPreference = "Stop"
Set-Location $Base

if (-not $Out) {
  $stamp = Get-Date -Format "yyyyMMdd_HHmmss"
  $Out = "output\meu_arquivo_ptbr_$stamp.pdf"
}

$setupFile = "setup_and_translate_windows.ps1"
if (-not (Test-Path $setupFile)) {
  throw "Arquivo '$setupFile' não encontrado em $Base"
}

$conflicts = Select-String -Path $setupFile -Pattern '^(<<<<<<< |=======|>>>>>>> )' -ErrorAction SilentlyContinue
if ($conflicts) {
  Write-Host "[WARN] Marcadores de conflito detectados em $setupFile. Tentando restaurar..." -ForegroundColor Yellow

  git rev-parse --is-inside-work-tree *> $null
  if ($LASTEXITCODE -ne 0) {
    throw "Este diretório não é um repositório Git. Não consigo restaurar automaticamente o script."
  }

  git restore --source=HEAD --worktree --staged -- $setupFile *> $null
  $conflicts = Select-String -Path $setupFile -Pattern '^(<<<<<<< |=======|>>>>>>> )' -ErrorAction SilentlyContinue

  if ($conflicts) {
    git fetch origin *> $null
    git checkout origin/main -- $setupFile *> $null
    $conflicts = Select-String -Path $setupFile -Pattern '^(<<<<<<< |=======|>>>>>>> )' -ErrorAction SilentlyContinue
  }

  if ($conflicts) {
    throw "Ainda há conflito em $setupFile após tentativa de reparo automático."
  }

  Write-Host "[OK] Script restaurado sem conflitos." -ForegroundColor Green
}

Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass -Force

$setupParams = @{
  Pdf = $Pdf
  Out = $Out
  Translator = $Translator
  SourceLang = $SourceLang
  TargetLang = $TargetLang
  Dpi = $Dpi
  RenderMode = $RenderMode
  StartPage = $StartPage
  EndPage = $EndPage
  PersistToolsPath = $true
}

Write-Host "[INFO] Executando setup com parâmetros estáveis (sem multiline com crase)..." -ForegroundColor Cyan
& .\$setupFile @setupParams
if ($LASTEXITCODE -ne 0) {
  throw "Falha na tradução (exit code $LASTEXITCODE). Veja logs em work\\logs."
}

Write-Host "[OK] PDF gerado em: $Out" -ForegroundColor Green
