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

$api = "https://api.telegram.org/bot$telegram"
$botName = (Invoke-RestMethod -Uri "$api/getMe").result.username
# getUpdates only works while no webhook is set; clear any old one (pending messages are kept).
[void] (Invoke-RestMethod -Uri "$api/deleteWebhook" -Method Post)
Write-Host "2/5  Your Telegram user ID"
Write-Host "     Open https://t.me/$botName in Telegram, tap Start (or send 'hi') — this is your NEW bot, not @BotFather."
Write-Host "     Then press Enter here. If you know your numeric Telegram user ID you may type it instead."
$ownerId = $null
for ($attempt = 1; $attempt -le 5 -and -not $ownerId; $attempt++) {
    $typed = ([string] (Read-Host "  Press Enter after messaging @$botName")).Trim()
    $updates = Invoke-RestMethod -Uri "$api/getUpdates" -Method Post -Body (@{ allowed_updates = @("message") } | ConvertTo-Json) -ContentType "application/json"
    $ids = @($updates.result | Where-Object { $_.message.chat.type -eq "private" } | ForEach-Object { [string] $_.message.from.id } | Sort-Object -Unique)
    if ($ids.Count -gt 1) { throw "More than one person has messaged @$botName. Only you should message it; ask Claude how to reset." }
    if ($ids.Count -eq 1) {
        $ownerId = $ids[0]
        if ($typed -and $typed -ne $ownerId) { Write-Host "  Note: you typed $typed, but the message came from $ownerId. Using $ownerId." }
    } elseif ($typed -match '^\d{5,15}$') {
        $ownerId = $typed
        Write-Host "  No message found yet; using the ID you typed. If the bot ignores you later, this ID was wrong."
    } else {
        Write-Host "  No message from you yet (attempt $attempt of 5). Open https://t.me/$botName , tap Start, then press Enter."
    }
}
if (-not $ownerId) { throw "Could not find your Telegram user ID. Message @$botName and run the script again." }
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
