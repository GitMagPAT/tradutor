# ============================================================
# start_libretranslate_python.ps1
# ------------------------------------------------------------
# Sobe o LibreTranslate via Python (SEM Docker).
#
# Observação:
# - Isso pode baixar modelos e pode levar alguns minutos na 1ª vez.
# - Recomendado usar a venv do projeto.
#
# Dica de performance:
# - Use --load-only (LoadOnly) para baixar apenas os idiomas necessários.
#   Ex.: "en,pt" para inglês -> português.
# ============================================================

[CmdletBinding()]
param(
  [string]$Host = "127.0.0.1",
  [int]$Port = 5000,
  [string]$LoadOnly = "en,pt"
)

$ErrorActionPreference = "Stop"

# Força UTF-8 no console
try { [Console]::InputEncoding = [System.Text.Encoding]::UTF8 } catch {}
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch {}
try { $OutputEncoding = [System.Text.Encoding]::UTF8 } catch {}

$ROOT = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $ROOT

$activate = Join-Path $ROOT ".venv\Scripts\Activate.ps1"
if (-not (Test-Path $activate)) {
  throw "Não encontrei .venv. Rode primeiro o setup_and_translate_windows.ps1 para criar o ambiente."
}

. $activate

pip install --upgrade pip
pip install libretranslate

Write-Host "Iniciando LibreTranslate em http://$Host`:$Port ..."
if (-not [string]::IsNullOrWhiteSpace($LoadOnly)) {
  Write-Host "Usando --load-only $LoadOnly (reduz download / startup)."
  libretranslate --host $Host --port $Port --load-only $LoadOnly
} else {
  libretranslate --host $Host --port $Port
}
