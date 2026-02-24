# ============================================================
# setup_and_translate_windows.ps1
# ------------------------------------------------------------
# Objetivo: instalar dependências (Python deps + Tesseract) e
# rodar a tradução do PDF (página a página) para pt-BR/pt.
#
# Uso recomendado (PowerShell):
#   Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass -Force
#   .\setup_and_translate_windows.ps1 -Pdf "input\meu.pdf"
#
# Requisitos:
# - Windows 10/11
# - Winget (geralmente já vem no Windows moderno)
# - (Opcional) Docker Desktop para rodar LibreTranslate local
# ============================================================

[CmdletBinding()]
param(
  [Parameter(Mandatory=$true)]
  [string]$Pdf,

  [string]$Out = "",

  [ValidateSet("opusmt","translategemma","libretranslate","mymemory","dummy")]
  [string]$Translator = "opusmt",

  # LibreTranslate (self-host)
  [string]$LibreTranslateUrl = "http://127.0.0.1:5000",
  [string]$LibreTranslateApiKey = "",

  # Opus-MT (local / HuggingFace)
  [string]$OpusMTModel = "Helsinki-NLP/opus-mt-tc-big-en-pt",
  [ValidateSet("auto","cpu","cuda")]
  [string]$OpusMTDevice = "auto",
  [int]$OpusMTNumBeams = 4,
  [int]$OpusMTBatchSize = 4,
  [int]$OpusMTMaxInputTokens = 384,
  [int]$OpusMTMaxNewTokens = 512,
  [string]$OpusMTHfCacheDir = "",


# (Compat) Se você tinha scripts antigos que passavam -LibreTranslatePort, mantenha funcionando.
# Se for >0, substitui a porta em $LibreTranslateUrl (ex.: 5000).
[int]$LibreTranslatePort = 0,

# TranslateGemma (OpenAI-compatible; recomendado via Docker Model Runner)
[string]$TranslateGemmaUrl = "http://127.0.0.1:12434/engines/v1",
[string]$TranslateGemmaModel = "aistaging/translategemma-vllm:27B",
[int]$TranslateGemmaTimeoutSec = 120,
[switch]$SetupTranslateGemmaModelRunner,
[int]$DockerModelRunnerTcpPort = 12434,

  # LibreTranslate (startup / performance)
  # Na 1ª execução, o LibreTranslate pode levar vários minutos para baixar modelos.
  [int]$LibreTranslateWaitSeconds = 900,
  # Para reduzir o tempo de startup, carregue apenas os idiomas necessários (ex.: "en,pt")
  [string]$LibreTranslateLoadOnly = "",

  # Idiomas (tradução)
  [string]$SourceLang = "en",
  [string]$TargetLang = "pb",

  # OCR (Tesseract)
  [string]$OcrLang = "eng",
  [ValidateSet("fast","best")]
  # Qualidade > tempo: por padrão use tessdata_best.
  [string]$TessdataVariant = "best",
  [int]$OcrTimeoutSec = 180,

  # Renderização
  [ValidateSet("pdf_overlay","pdf_overlay_original","raster")]
#    Opção 1 (padrão): `pdf_overlay` (qualidade/legibilidade: rasteriza o fundo e elimina a camada de texto inglês)
  [string]$RenderMode = "pdf_overlay",
  [ValidateSet("jpg","png")]
  [string]$ImageFormat = "jpg",
  [int]$JpgQuality = 90,
  # Qualidade > tempo: por padrão usa DPI 300.
  [int]$Dpi = 300,

  # Intervalo de páginas (0-based)
  [int]$StartPage = -1,
  [int]$EndPage = -1,

  # Flags úteis
  [switch]$NoTranslateImages,     # se passado, NÃO traduz texto dentro de figuras/imagens
  [switch]$OcrPreprocess,         # se passado, aplica pré-processamento antes do OCR
  [switch]$NoResume,              # se passado, não reaproveita work/out_pages
  [switch]$NoKeepWork,            # se passado, apaga work/ ao final
  [switch]$StartLibreTranslateDocker, # tenta subir LibreTranslate via Docker se não estiver rodando

  # PATH / persistência
  [switch]$PersistToolsPath
)

$ErrorActionPreference = "Stop"

# Força UTF-8 no console (evita caracteres quebrados no PowerShell 5.1)
try { [Console]::InputEncoding = [System.Text.Encoding]::UTF8 } catch {}
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch {}
try { $OutputEncoding = [System.Text.Encoding]::UTF8 } catch {}

function Write-Step($msg) {
  Write-Host ""
  Write-Host "==> $msg" -ForegroundColor Cyan
}

function Ensure-Winget {
  if (-not (Get-Command winget -ErrorAction SilentlyContinue)) {
    throw "winget não encontrado. Atualize/instale o App Installer da Microsoft Store e tente novamente."
  }
}

function Ensure-Python {
  if (Get-Command python -ErrorAction SilentlyContinue) { return }
  Write-Step "Python não encontrado. Tentando instalar Python 3.11 via winget..."
  Ensure-Winget
  winget install -e --id Python.Python.3.11 --accept-package-agreements --accept-source-agreements
  if (-not (Get-Command python -ErrorAction SilentlyContinue)) {
    throw "Falha ao instalar Python automaticamente. Instale Python 3.11+ manualmente e garanta que 'python' funcione no terminal."
  }
}

function Ensure-Venv($Root) {
  $VenvPath = Join-Path $Root ".venv"
  $Py = Join-Path $VenvPath "Scripts\python.exe"
  if (Test-Path $Py) { return $Py }

  Write-Step "Criando ambiente virtual (.venv)..."
  python -m venv $VenvPath

  if (-not (Test-Path $Py)) {
    throw "Falha ao criar venv. Verifique a instalação do Python."
  }
  return $Py
}

function Pip-Install($Py, $RequirementsPath) {
  Write-Step "Instalando dependências Python (pip)..."
  & $Py -m pip install --upgrade pip
  & $Py -m pip install -r $RequirementsPath
}

function Ensure-TorchCpu($Py) {
  # PyTorch é necessário para tradutores locais (Opus-MT/HF). No Windows,
  # instalar pelo index default pode puxar wheels CUDA inesperadas.
  # O index cpu do PyTorch garante um wheel compatível e mais leve.
  Write-Step "Garantindo PyTorch (CPU) para Opus-MT..."
  & $Py -m pip install --upgrade --index-url https://download.pytorch.org/whl/cpu torch
  if ($LASTEXITCODE -ne 0) {
    throw "Falha ao instalar PyTorch CPU (torch)."
  }
}

function Add-ToPath($Dir, [bool]$Persist) {
  if (-not (Test-Path $Dir)) { return }
  if ($env:PATH -notlike "*$Dir*") {
    $env:PATH = "$Dir;$env:PATH"
  }

  if ($Persist) {
    $currentUserPath = [Environment]::GetEnvironmentVariable("PATH", "User")
    if ($null -eq $currentUserPath) { $currentUserPath = "" }
    if ($currentUserPath -notlike "*$Dir*") {
      [Environment]::SetEnvironmentVariable("PATH", "$Dir;$currentUserPath", "User")
    }
  }
}

function Ensure-Tesseract {
  # 1) tenta achar no PATH
  $cmd = Get-Command tesseract -ErrorAction SilentlyContinue
  if ($cmd) {
    return Split-Path $cmd.Source -Parent
  }

  # 2) tenta local padrão
  $defaultDir = "C:\Program Files\Tesseract-OCR"
  $defaultExe = Join-Path $defaultDir "tesseract.exe"
  if (Test-Path $defaultExe) {
    Add-ToPath $defaultDir $PersistToolsPath
    return $defaultDir
  }

  # 3) instala via winget (UB-Mannheim)
  Write-Step "Tesseract OCR não encontrado. Instalando via winget (UB-Mannheim.TesseractOCR)..."
  Ensure-Winget
  winget install -e --id UB-Mannheim.TesseractOCR --accept-package-agreements --accept-source-agreements

  if (Test-Path $defaultExe) {
    Add-ToPath $defaultDir $PersistToolsPath
    return $defaultDir
  }

  # 4) tenta novamente no PATH
  $cmd2 = Get-Command tesseract -ErrorAction SilentlyContinue
  if ($cmd2) {
    return Split-Path $cmd2.Source -Parent
  }

  throw "Não consegui localizar o Tesseract após instalação. Procure por 'tesseract.exe' e ajuste manualmente."
}

function Download-File($Url, $OutPath) {
  $OutDir = Split-Path $OutPath -Parent
  if (-not (Test-Path $OutDir)) { New-Item -ItemType Directory -Force -Path $OutDir | Out-Null }

  if (Test-Path $OutPath) { return }
  Write-Host "Baixando: $Url"
  Invoke-WebRequest -Uri $Url -OutFile $OutPath
}

function Ensure-Tessdata($Root, $Variant, $Langs) {
  $tessDir = Join-Path $Root "tessdata"
  if (-not (Test-Path $tessDir)) { New-Item -ItemType Directory -Force -Path $tessDir | Out-Null }

  # Importante: o Tesseract usa TESSDATA_PREFIX para localizar *.traineddata
  $env:TESSDATA_PREFIX = $tessDir

  $repo = if ($Variant -eq "best") { "tessdata_best" } else { "tessdata_fast" }
  foreach ($lang in $Langs) {
    $fname = "$lang.traineddata"
    $out = Join-Path $tessDir $fname
    $url = "https://github.com/tesseract-ocr/$repo/raw/main/$fname"
    Download-File $url $out
  }
  return $tessDir
}


function Normalize-LibreTranslateLang($Lang) {
  if ([string]::IsNullOrWhiteSpace($Lang)) { return $Lang }
  $l = ($Lang -as [string]).Trim().ToLower().Replace('_','-')
  # Alias PT-BR: 'pb'/'pt-br' -> 'pt' (LibreTranslate geralmente não diferencia BR/PT)
  if ($l -eq 'pb' -or $l -eq 'pt-br' -or $l -eq 'ptbr' -or $l -eq 'ptbrasil') { return 'pt' }
  # Normaliza variantes (ex.: en-US -> en)
  if ($l -match '-') { return ($l.Split('-')[0]) }
  return $l
}

function Test-LibreTranslate($Url, $SourceLang, $TargetLang, $ApiKey) {
  # Usa Invoke-RestMethod (não depende do engine do Internet Explorer no PowerShell 5.1)
  # Importante: /health pode responder antes dos modelos estarem prontos.
  # Para evitar rodar 500+ páginas e descobrir no final, fazemos um teste real em /translate.
  $base = $Url.TrimEnd("/")

  # 1) Testa endpoints simples
  $candidates = @(
    ($base + "/health"),
    ($base + "/languages")
  )
  $okBasic = $false
  foreach ($u in $candidates) {
    try {
      $null = Invoke-RestMethod -Uri $u -Method Get -TimeoutSec 5
      $okBasic = $true
      break
    } catch {
      # tenta o próximo endpoint
    }
  }
  if (-not $okBasic) { return $false }

  # 2) Teste de tradução
  try {
    $payload = @{
      q = "This is a translation test."
      source = (Normalize-LibreTranslateLang $SourceLang)
      target = (Normalize-LibreTranslateLang $TargetLang)
      format = "text"
    }
    if (-not [string]::IsNullOrWhiteSpace($ApiKey)) {
      $payload.api_key = $ApiKey
    }

    $json = $payload | ConvertTo-Json -Depth 5
    $resp = Invoke-RestMethod -Uri ($base + "/translate") -Method Post -TimeoutSec 20 -ContentType "application/json" -Body $json
    if ($resp -and $resp.translatedText -and ($resp.translatedText -ne $payload.q)) {
      return $true
    }
  } catch {
    # ainda não pronto
  }
  return $false
}

function Ensure-LibreTranslate-Docker($Url) {
  # Só tenta se URL for local (caso mais comum)
  if ($Url -notmatch "localhost" -and $Url -notmatch "127\.0\.0\.1") {
    Write-Host "LibreTranslateUrl não parece local ($Url). Vou assumir que você já tem um servidor rodando."
    return
  }

  if (Test-LibreTranslate $Url $SourceLang $TargetLang $LibreTranslateApiKey) { return }

  Write-Step "LibreTranslate não respondeu em $Url."
  if (-not $StartLibreTranslateDocker) {
    Write-Host "Dica: Rode com -StartLibreTranslateDocker (requer Docker Desktop) ou inicie o LibreTranslate manualmente."
    return
  }

  if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    throw "Docker não encontrado. Instale Docker Desktop (ou mude -Translator para mymemory)."
  }

  # Se o Docker Desktop não estiver rodando, 'docker info' costuma falhar.
  try {
    docker info | Out-Null
  } catch {
    throw "Docker está instalado, mas o engine não respondeu. Abra o Docker Desktop e aguarde ficar 'Running', depois tente novamente."
  }

  # Dica de performance: carregar só os idiomas necessários reduz MUITO o tempo de startup.
  # Referência: --load-only / LT_LOAD_ONLY (docs do LibreTranslate).
  $loadOnly = $LibreTranslateLoadOnly
  if ([string]::IsNullOrWhiteSpace($loadOnly)) {
    $langs = @($SourceLang, $TargetLang) `
      | ForEach-Object { Normalize-LibreTranslateLang (($_ -as [string]).Trim()) } `
      | Where-Object { $_ -and $_ -ne "auto" } `
      | Select-Object -Unique
    if ($langs.Count -gt 0) { $loadOnly = ($langs -join ",") }
  }

  if (-not [string]::IsNullOrWhiteSpace($loadOnly)) {
    Write-Host "LibreTranslate: usando LT_LOAD_ONLY=$loadOnly (reduz tempo de startup / download de modelos)."
    Write-Host "Obs.: se você já tem um container antigo 'libretranslate', remova-o para aplicar LT_LOAD_ONLY:"
    Write-Host "  docker rm -f libretranslate"
  } else {
    Write-Host "LibreTranslate: LT_LOAD_ONLY não configurado (na 1ª execução pode baixar muitos modelos e demorar)."
  }

  Write-Step "Tentando subir LibreTranslate via Docker..."
  $name = "libretranslate"

  $existing = docker ps -a --format "{{.Names}}" | Select-String -Pattern "^$name$" -Quiet
  if ($existing) {
    docker start $name | Out-Null
  } else {
    if (-not [string]::IsNullOrWhiteSpace($loadOnly)) {
      docker run -d --name $name --restart unless-stopped -p 5000:5000 -e "LT_LOAD_ONLY=$loadOnly" libretranslate/libretranslate | Out-Null
    } else {
      docker run -d --name $name --restart unless-stopped -p 5000:5000 libretranslate/libretranslate | Out-Null
    }
  }

  Write-Host "Aguardando LibreTranslate ficar pronto..."
  $maxWait = $LibreTranslateWaitSeconds
  if ($maxWait -lt 60) { $maxWait = 60 }

  for ($i=0; $i -lt $maxWait; $i++) {
    if (Test-LibreTranslate $Url $SourceLang $TargetLang $LibreTranslateApiKey) {
      Write-Host "LibreTranslate OK em $Url"
      return
    }
    if (($i % 10) -eq 0) {
      Write-Host ("  ... inicializando (" + $i + "s/" + $maxWait + "s). Na 1ª vez pode levar vários minutos para baixar modelos.")
    }
    Start-Sleep -Seconds 1
  }

  throw "LibreTranslate não respondeu após $maxWait segundos. Dica: veja logs com 'docker logs -f libretranslate' e/ou aumente -LibreTranslateWaitSeconds."
}

# -----------------------------
function Ensure-TranslateGemma-ModelRunner {
  param(
    [Parameter(Mandatory=$true)][string]$ModelName,
    [Parameter(Mandatory=$true)][string]$BaseUrl,
    [int]$TcpPort = 12434,
    [switch]$DoSetup
  )

  # Verifica Docker
  try { docker version | Out-Null } catch { throw "Docker não encontrado ou não está rodando. Instale/abra o Docker Desktop." }

  # Verifica se o CLI suporta 'docker model'
  $hasModelCmd = $true
  try { docker model --help | Out-Null } catch { $hasModelCmd = $false }
  if (-not $hasModelCmd) {
    throw "Seu Docker não tem o comando 'docker model'. Atualize o Docker Desktop para uma versão que suporte Docker Model Runner e habilite o recurso."
  }

  if ($DoSetup) {
    Write-Step "Habilitando Docker Model Runner (se disponível via CLI)..."
    $hasDockerDesktop = $true
    try { docker desktop --help | Out-Null } catch { $hasDockerDesktop = $false }

    if ($hasDockerDesktop) {
      try {
        docker desktop enable model-runner --tcp $TcpPort | Out-Null
      } catch {
        Write-Host "[WARN] Não consegui habilitar o Model Runner via 'docker desktop'. Habilite manualmente no Docker Desktop e exponha TCP na porta $TcpPort." -ForegroundColor Yellow
      }
    } else {
      Write-Host "[WARN] Comando 'docker desktop' não encontrado. Habilite manualmente o Docker Model Runner no Docker Desktop e exponha TCP na porta $TcpPort." -ForegroundColor Yellow
    }

    Write-Step "Baixando modelo TranslateGemma (isso pode ser grande)..."
    docker model pull $ModelName
  }

  # Health check do endpoint (OpenAI-compatible)
  $modelsUrl = ($BaseUrl.TrimEnd("/")) + "/models"
  try {
    Invoke-RestMethod -Method GET -Uri $modelsUrl -TimeoutSec 15 | Out-Null
    Write-Host "Docker Model Runner OK: $modelsUrl" -ForegroundColor Green
  } catch {
    Write-Host "[WARN] Não consegui acessar $modelsUrl. Verifique se o Docker Model Runner está habilitado e exposto via TCP (porta $TcpPort)." -ForegroundColor Yellow
  }
}

# Começo do script
# -----------------------------

$ROOT = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $ROOT

Write-Step "Validando caminhos (input/output/work)..."
if (-not (Test-Path "input")) { New-Item -ItemType Directory -Force -Path "input" | Out-Null }
if (-not (Test-Path "output")) { New-Item -ItemType Directory -Force -Path "output" | Out-Null }
if (-not (Test-Path "work")) { New-Item -ItemType Directory -Force -Path "work" | Out-Null }

# Resolve PDF path
$PdfPath = $Pdf
if (-not [System.IO.Path]::IsPathRooted($PdfPath)) {
  $PdfPath = Join-Path $ROOT $PdfPath
}
if (-not (Test-Path $PdfPath)) {
  throw "PDF não encontrado: $PdfPath"
}

# Resolve Out path
if ([string]::IsNullOrWhiteSpace($Out)) {
  $stem = [System.IO.Path]::GetFileNameWithoutExtension($PdfPath)
  $Out = Join-Path $ROOT ("output\" + $stem + "_ptbr.pdf")
} elseif (-not [System.IO.Path]::IsPathRooted($Out)) {
  $Out = Join-Path $ROOT $Out
}

Write-Step "Garantindo Python..."
Ensure-Python

Write-Step "Garantindo venv (.venv) + instalando dependências..."
$PY = Ensure-Venv $ROOT
Pip-Install $PY (Join-Path $ROOT "requirements.txt")

if ($Translator -eq "opusmt") {
  # Dependência pesada (torch) é instalada explicitamente, CPU-only.
  Ensure-TorchCpu $PY

  # Exporta configurações para o Python sem precisar adicionar flags novas no CLI.
  $env:OPUSMT_MODEL = $OpusMTModel
  $env:OPUSMT_DEVICE = $OpusMTDevice
  $env:OPUSMT_NUM_BEAMS = "$OpusMTNumBeams"
  $env:OPUSMT_BATCH_SIZE = "$OpusMTBatchSize"
  $env:OPUSMT_MAX_INPUT_TOKENS = "$OpusMTMaxInputTokens"
  $env:OPUSMT_MAX_NEW_TOKENS = "$OpusMTMaxNewTokens"

  if ([string]::IsNullOrWhiteSpace($OpusMTHfCacheDir)) {
    $OpusMTHfCacheDir = Join-Path $ROOT "hf_cache"
  }
  if (-not (Test-Path $OpusMTHfCacheDir)) { New-Item -ItemType Directory -Force -Path $OpusMTHfCacheDir | Out-Null }
  $env:OPUSMT_HF_CACHE_DIR = $OpusMTHfCacheDir
}

Write-Step "Garantindo Tesseract OCR..."
$tessDir = Ensure-Tesseract
Add-ToPath $tessDir $PersistToolsPath
$tessExe = Join-Path $tessDir "tesseract.exe"
if (Test-Path $tessExe) {
  $env:TESSERACT_CMD = $tessExe
}

Write-Step "Baixando tessdata ($TessdataVariant) para idiomas: $OcrLang + osd"
# Sempre baixa "osd" (útil para orientação) + cada idioma do parâmetro (pode ser eng+por)
$langs = @("osd")
foreach ($part in $OcrLang.Split("+")) {
  if ($part.Trim().Length -gt 0) { $langs += $part.Trim() }
}
$tessdataPrefix = Ensure-Tessdata $ROOT $TessdataVariant $langs
$env:TESSDATA_PREFIX = $tessdataPrefix

# Compat: permitir scripts antigos com -LibreTranslatePort
if ($LibreTranslatePort -gt 0) {
  $LibreTranslateUrl = "http://127.0.0.1:$LibreTranslatePort"
}

if ($Translator -eq "translategemma") {
  # OBS: TranslateGemma via Docker Model Runner (OpenAI-compatible).
  # Para vLLM, normalmente requer GPU e (no Windows) WSL2.
  Ensure-TranslateGemma-ModelRunner -ModelName $TranslateGemmaModel -BaseUrl $TranslateGemmaUrl -TcpPort $DockerModelRunnerTcpPort -DoSetup:$SetupTranslateGemmaModelRunner
}
if ($Translator -eq "libretranslate") {
  Ensure-LibreTranslate-Docker $LibreTranslateUrl
}

Write-Step "Rodando pipeline de tradução..."
$cmd = @(
  "-m","app",
  "--pdf",$PdfPath,
  "--out",$Out,
  "--source-lang",$SourceLang,
  "--target-lang",$TargetLang,
  "--dpi",$Dpi,
  "--ocr-lang",$OcrLang,
  "--ocr-timeout-sec",$OcrTimeoutSec,
  "--translator",$Translator,
  "--render-mode",$RenderMode,
  "--image-format",$ImageFormat,
  "--jpg-quality",$JpgQuality,
  "--tesseract-cmd",$env:TESSERACT_CMD,
  "--tessdata-prefix",$env:TESSDATA_PREFIX
)

if ($Translator -eq "libretranslate" -and -not [string]::IsNullOrWhiteSpace($LibreTranslateUrl)) {
  $cmd += @("--libretranslate-url",$LibreTranslateUrl)
}
if ($Translator -eq "libretranslate" -and -not [string]::IsNullOrWhiteSpace($LibreTranslateApiKey)) {
  $cmd += @("--libretranslate-api-key",$LibreTranslateApiKey)
}
if ($Translator -eq "translategemma" -and -not [string]::IsNullOrWhiteSpace($TranslateGemmaUrl)) {
  $cmd += @("--translategemma-url",$TranslateGemmaUrl)
}
if ($Translator -eq "translategemma" -and -not [string]::IsNullOrWhiteSpace($TranslateGemmaModel)) {
  $cmd += @("--translategemma-model",$TranslateGemmaModel)
}
if ($Translator -eq "translategemma" -and $TranslateGemmaTimeoutSec -gt 0) {
  $cmd += @("--translategemma-timeout-sec",$TranslateGemmaTimeoutSec)
}
if ($Translator -eq "mymemory") {
  # opcional: você pode passar -mymemory-email no próprio script do PowerShell,
  # mas aqui mantemos simples: se quiser, use config.yaml ou .env
}

if ($NoTranslateImages) {
  $cmd += @("--no-translate-images")
}
if ($OcrPreprocess) {
  $cmd += @("--ocr-preprocess")
}
if ($NoResume) {
  $cmd += @("--no-resume")
}
if ($NoKeepWork) {
  $cmd += @("--no-keep-work")
}

if ($StartPage -ge 0) { $cmd += @("--start-page",$StartPage) }
if ($EndPage -ge 0) { $cmd += @("--end-page",$EndPage) }

Write-Host ""
Write-Host "Comando Python:" -ForegroundColor DarkGray
Write-Host ("  " + $PY + " " + ($cmd -join " ")) -ForegroundColor DarkGray
Write-Host ""

& $PY @cmd
if ($LASTEXITCODE -ne 0) {
  throw "Falha ao executar o pipeline (exit code $LASTEXITCODE). Veja o traceback acima e os logs em work\logs."
}

Write-Step "Concluído!"
Write-Host "Saída: $Out"