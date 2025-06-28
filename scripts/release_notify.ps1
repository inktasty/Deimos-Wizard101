param(
    [Parameter(Mandatory=$true)]
    [string]$WebhookUrl = "",
    
    [Parameter(Mandatory=$true)]
    [string]$VersionTag = "3.11.0",
    
    [Parameter(Mandatory=$true)]
    [string]$Repository = "https://github.com/Deimos-Wizard101/Deimos-Wizard101",
    
    [Parameter(Mandatory=$true)]
    [string]$Changelog = "Testing testing 123",
    
    [Parameter(Mandatory=$true)]
    [string]$UserId = "263123145333014530"
)

Write-Host "Sending simplified Discord notification for release $VersionTag..."

try {
    # Truncate and sanitize changelog for Discord
    $changelogText = $Changelog
    if ($changelogText.Length -gt 1800) {
        $changelogText = $changelogText.Substring(0, 1800) + "..."
    }

    $changelogClean = $changelogText -replace '"', "'" -replace '[\r\n]+', ' | ' -replace '\\', '/'

    # Construct simple message
    $content = "<@$UserId> A new release is out: **$VersionTag**`nRepo: $Repository/releases/tag/$VersionTag`n`n**Changes:**`n$changelogClean"

    $payload = @{ content = $content } | ConvertTo-Json -Depth 2

    # Send the webhook
    Invoke-RestMethod -Uri $WebhookUrl -Method Post -Body $payload -ContentType "application/json"
    Write-Host "✅ Simple Discord notification sent successfully"

} catch {
    Write-Error "❌ Failed to send Discord notification: $($_.Exception.Message)"
    exit 1
}
