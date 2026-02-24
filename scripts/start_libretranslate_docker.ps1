# ============================================================
# start_libretranslate_docker.ps1
# ------------------------------------------------------------
# Sobe o LibreTranslate local via Docker (Windows PowerShell).
#
# Requisitos:
# - Docker Desktop instalado e EM EXECUÇÃO.
#
# Observação importante:
# - Na 1ª execução, o LibreTranslate pode levar VÁRIOS MINUTOS para baixar modelos.
# - Para reduzir o tempo de startup e o tamanho do download, use LT_LOAD_ONLY.
#   (ex.: "en,pt" para inglês -> português).
# ============================================================

[CmdletBinding()]
param(
  [string]$Url = "http://127.0.0.1:5000",
  [string]$LoadOnly = "en,pt",
  [int]$WaitSeconds = 900
)

$ErrorActionPreference = "Stop"

# Força UTF-8 no console
try { [Console]::InputEncoding = [System.Text.Encoding]::UTF8 } catch {}
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch {}
try { $OutputEncoding = [System.Text.Encoding]::UTF8 } catch {}

function Test-LibreTranslate($BaseUrl) {
  $base = $BaseUrl.TrimEnd("/")
  $candidates = @(
    ($base + "/health"),
    ($base + "/languages")
  )
  foreach ($u in $candidates) {
    try {
      $null = Invoke-RestMethod -Uri $u -Method Get -TimeoutSec 5
      return $true
    } catch { }
  }
  return $false
}

if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
  throw "Docker não encontrado. Instale Docker Desktop e tente novamente."
}

try {
  docker info | Out-Null
} catch {
  throw "Docker está instalado, mas o engine não respondeu. Abra o Docker Desktop e aguarde ficar 'Running'."
}

$name = "libretranslate"

$exists = docker ps -a --format "{{.Names}}" | Select-String -Pattern "^$name$" -Quiet
if ($exists) {
  docker start $name | Out-Null
} else {
  if (-not [string]::IsNullOrWhiteSpace($LoadOnly)) {
    docker run -d --name $name --restart unless-stopped -p 5000:5000 -e "LT_LOAD_ONLY=$LoadOnly" libretranslate/libretranslate | Out-Null
  } else {
    docker run -d --name $name --restart unless-stopped -p 5000:5000 libretranslate/libretranslate | Out-Null
  }
}

Write-Host "Aguardando LibreTranslate ficar pronto em $Url ..."
if ($WaitSeconds -lt 60) { $WaitSeconds = 60 }

for ($i=0; $i -lt $WaitSeconds; $i++) {
  if (Test-LibreTranslate $Url) {
    Write-Host "✅ LibreTranslate OK em $Url"
    Write-Host "Para parar: docker stop libretranslate"
    exit 0
  }
  if (($i % 10) -eq 0) {
    Write-Host ("  ... inicializando (" + $i + "s/" + $WaitSeconds + "s)")
  }
  Start-Sleep -Seconds 1
}

Write-Host "❌ Não respondeu após $WaitSeconds segundos."
Write-Host "Dica: veja logs com: docker logs -f libretranslate"
exit 1
