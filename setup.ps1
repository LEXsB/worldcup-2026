<#
.SYNOPSIS
    Setup automatizado del proyecto worldcup_calendar:
    crea el repo en GitHub, sube los archivos, agrega los secrets
    y dispara la primera ejecución del workflow.

.DESCRIPTION
    Requiere tener instalado:
      - git           (https://git-scm.com/)
      - gh CLI        (https://cli.github.com/) — ejecuta 'gh auth login' antes.

.PARAMETER RepoName
    Nombre del repositorio que se creará bajo tu cuenta.

.PARAMETER Private
    Si se incluye, el repo se crea privado (por defecto es público —
    necesario para que el visor pueda leer el raw URL sin auth).

.EXAMPLE
    .\setup.ps1 -RepoName worldcup-2026

.EXAMPLE
    .\setup.ps1 -RepoName worldcup-2026 -Private
#>

[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$RepoName,

    [switch]$Private
)

$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot

function Test-Command {
    param([string]$Name)
    $null -ne (Get-Command $Name -ErrorAction SilentlyContinue)
}

# ---------- Pre-flight ----------
Write-Host "==> Verificando dependencias..." -ForegroundColor Cyan
foreach ($cmd in @('git', 'gh')) {
    if (-not (Test-Command $cmd)) {
        Write-Error "Falta '$cmd' en el PATH. Instala y reintenta."
        exit 1
    }
}

# Validar que gh esté autenticado
$ghAuth = gh auth status 2>&1
if ($LASTEXITCODE -ne 0) {
    Write-Error "gh CLI no está autenticado. Ejecuta primero: gh auth login"
    exit 1
}
Write-Host "    git + gh CLI OK." -ForegroundColor Green

# ---------- Pedir secrets ----------
Write-Host ""
Write-Host "==> Datos de configuración" -ForegroundColor Cyan
Write-Host "    (Si no tienes API key todavía, regístrate gratis en:" -ForegroundColor DarkGray
Write-Host "     https://www.football-data.org/client/register )" -ForegroundColor DarkGray

$apiKeySecure = Read-Host "Pega tu FOOTBALL_DATA_API_KEY" -AsSecureString
if ($apiKeySecure.Length -eq 0) {
    Write-Error "La API key es obligatoria."
    exit 1
}
$apiKey = [System.Net.NetworkCredential]::new('', $apiKeySecure).Password

Write-Host ""
Write-Host "    Telegram (opcional) — deja vacío si no quieres notificaciones." -ForegroundColor DarkGray
$tgToken = Read-Host "TELEGRAM_BOT_TOKEN (opcional)"
$tgChat  = ""
if ($tgToken) {
    $tgChat = Read-Host "TELEGRAM_CHAT_ID"
    if (-not $tgChat) {
        Write-Warning "Sin chat_id no se enviarán notificaciones. Continúo sin Telegram."
        $tgToken = ""
    }
}

# ---------- git init / commit ----------
Write-Host ""
Write-Host "==> Inicializando repo local..." -ForegroundColor Cyan
if (-not (Test-Path .git)) {
    git init | Out-Null
    git branch -M main
}

git add . | Out-Null
$pendingChanges = git status --porcelain
if ($pendingChanges) {
    git commit -m "feat: initial WC2026 calendar app" | Out-Null
    Write-Host "    Commit inicial creado." -ForegroundColor Green
} else {
    Write-Host "    Sin cambios pendientes." -ForegroundColor DarkGray
}

# ---------- Crear repo en GitHub ----------
Write-Host ""
Write-Host "==> Creando repo en GitHub..." -ForegroundColor Cyan
$visibility = if ($Private) { '--private' } else { '--public' }

# Verificar si el repo remoto 'origin' ya existe
$hasOrigin = git remote 2>$null | Select-String -Pattern '^origin$' -Quiet
if (-not $hasOrigin) {
    gh repo create $RepoName $visibility --source=. --remote=origin --push
    if ($LASTEXITCODE -ne 0) {
        Write-Error "Falló la creación del repo."
        exit 1
    }
    Write-Host "    Repo creado y push hecho." -ForegroundColor Green
} else {
    Write-Host "    Remote 'origin' ya configurado. Saltando creación." -ForegroundColor DarkGray
    git push -u origin main
}

# ---------- Subir secrets ----------
Write-Host ""
Write-Host "==> Configurando secrets..." -ForegroundColor Cyan
$apiKey | gh secret set FOOTBALL_DATA_API_KEY --body -
Write-Host "    FOOTBALL_DATA_API_KEY  ✓" -ForegroundColor Green

if ($tgToken) {
    $tgToken | gh secret set TELEGRAM_BOT_TOKEN --body -
    $tgChat  | gh secret set TELEGRAM_CHAT_ID   --body -
    Write-Host "    TELEGRAM_BOT_TOKEN     ✓" -ForegroundColor Green
    Write-Host "    TELEGRAM_CHAT_ID       ✓" -ForegroundColor Green
}

# ---------- Disparar primer run ----------
Write-Host ""
Write-Host "==> Disparando primera ejecución del workflow..." -ForegroundColor Cyan
gh workflow run update-results.yml
Start-Sleep -Seconds 3
gh run list --workflow=update-results.yml --limit 1

# ---------- Mostrar URL raw para el visor ----------
$repoFull = gh repo view --json nameWithOwner -q .nameWithOwner
$rawUrl   = "https://raw.githubusercontent.com/$repoFull/main/data/matches.json"

Write-Host ""
Write-Host "============================================================" -ForegroundColor Yellow
Write-Host " SETUP COMPLETO" -ForegroundColor Yellow
Write-Host "============================================================" -ForegroundColor Yellow
Write-Host ""
Write-Host " Edita viewer\index.html y pega esta URL en DATA_URL:" -ForegroundColor White
Write-Host ""
Write-Host "   $rawUrl" -ForegroundColor Cyan
Write-Host ""
Write-Host " Luego haz commit + push para que el visor lo recoja." -ForegroundColor White
Write-Host ""
if ($Private) {
    Write-Warning "Tu repo es PRIVADO: el raw URL no será accesible sin auth."
    Write-Warning "Considera hacerlo público o publicar el JSON con GitHub Pages."
}
Write-Host " Sigue el progreso del workflow en:" -ForegroundColor White
Write-Host "   https://github.com/$repoFull/actions" -ForegroundColor Cyan
Write-Host ""
