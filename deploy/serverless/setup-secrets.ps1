# Cloud Command Code (serverless) — one-time secret setup. Run it yourself in PowerShell 7 from the repo root:
#   pwsh -File deploy/serverless/setup-secrets.ps1 -WorkerUrl https://cloud-command-code.<you>.workers.dev -Repos thor149/cloud-command-code
# It asks for your secrets with hidden prompts, generates the two internal secrets itself (never displayed),
# stores them in Cloudflare (wrangler secret put) and in each repo's Actions secrets (gh secret set), finds your
# Telegram user ID, and points the Telegram webhook at the Worker. Nothing is written to disk or printed.
param(
    [Parameter(Mandatory = $true)] [string] $WorkerUrl,
    [Parameter(Mandatory = $true)] [string[]] $Repos
)
$ErrorActionPreference = "Stop"
$WorkerUrl = $WorkerUrl.TrimEnd("/")
if ($WorkerUrl -notmatch '^https://[a-z0-9.-]+\.workers\.dev$') { throw "WorkerUrl must look like https://name.account.workers.dev" }
foreach ($r in $Repos) { if ($r -notmatch '^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$') { throw "Repo must be owner/name: $r" } }
$workerDir = Join-Path $PSScriptRoot "..\..\worker" | Resolve-Path

function Read-Secret([string] $label) {
    $secure = Read-Host -AsSecureString $label
    $plain = [System.Net.NetworkCredential]::new("", $secure).Password
    if (-not $plain) { throw "$label is required." }
    return $plain.Trim()
}
function New-RandomSecret { [Convert]::ToHexString([Security.Cryptography.RandomNumberGenerator]::GetBytes(32)).ToLowerInvariant() }
function Set-WorkerSecret([string] $name, [string] $value) {
    Push-Location $workerDir
    try { $value | npx --yes wrangler@latest secret put $name | Out-Null } finally { Pop-Location }
    if ($LASTEXITCODE -ne 0) { throw "wrangler secret put $name failed" }
    Write-Host "  Cloudflare secret $name set"
}

Write-Host "1/5  Secrets (input is hidden)"
$telegram = Read-Secret "Telegram bot token from @BotFather"
if ($telegram -notmatch '^\d+:[A-Za-z0-9_-]{30,}$') { throw "That does not look like a BotFather token." }
$commandCode = Read-Secret "Command Code API key (Command Code Studio -> API Keys)"
$github = Read-Secret "GitHub fine-grained token (repos above, permission Actions: Read and write)"
$ccbotSecret = New-RandomSecret
$webhookSecret = New-RandomSecret

Write-Host "2/5  Your Telegram user ID — send your bot any private message now, then press Enter."
[void] (Read-Host)
$updates = Invoke-RestMethod -Uri "https://api.telegram.org/bot$telegram/getUpdates" -Method Post -Body (@{ allowed_updates = @("message") } | ConvertTo-Json) -ContentType "application/json"
$ids = @($updates.result | Where-Object { $_.message.chat.type -eq "private" } | ForEach-Object { $_.message.from.id } | Sort-Object -Unique)
if ($ids.Count -ne 1) { throw "Expected exactly one private sender, found $($ids.Count). Message the bot (only from your account) and retry." }
$ownerId = [string] $ids[0]
Write-Host "  Owner ID: $ownerId"

Write-Host "3/5  Cloudflare Worker secrets"
Set-WorkerSecret "TELEGRAM_BOT_TOKEN" $telegram
Set-WorkerSecret "TELEGRAM_WEBHOOK_SECRET" $webhookSecret
Set-WorkerSecret "GITHUB_TOKEN" $github
Set-WorkerSecret "CCBOT_SECRET" $ccbotSecret
Set-WorkerSecret "OWNER_ID" $ownerId

Write-Host "4/5  GitHub Actions secrets"
foreach ($repo in $Repos) {
    $commandCode | gh secret set COMMAND_CODE_API_KEY --repo $repo
    $ccbotSecret | gh secret set CCBOT_SECRET --repo $repo
    $WorkerUrl | gh secret set CCBOT_API --repo $repo
    if ($LASTEXITCODE -ne 0) { throw "gh secret set failed for $repo" }
    Write-Host "  ${repo}: COMMAND_CODE_API_KEY, CCBOT_SECRET, CCBOT_API set"
}

Write-Host "5/5  Telegram webhook"
$hook = @{ url = "$WorkerUrl/telegram"; secret_token = $webhookSecret; allowed_updates = @("message"); drop_pending_updates = $true } | ConvertTo-Json
$result = Invoke-RestMethod -Uri "https://api.telegram.org/bot$telegram/setWebhook" -Method Post -Body $hook -ContentType "application/json"
if (-not $result.ok) { throw "setWebhook failed: $($result.description)" }
Write-Host "  Webhook set. Message your bot /start."

Remove-Variable telegram, commandCode, github, ccbotSecret, webhookSecret -ErrorAction SilentlyContinue
