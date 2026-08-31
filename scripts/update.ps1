[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$UpstreamRepository = "https://github.com/Mai-with-u/MaiBot.git"
$LatestReleaseApi = "https://api.github.com/repos/Mai-with-u/MaiBot/releases/latest"

$git = Get-Command git -ErrorAction SilentlyContinue
if ($null -eq $git) {
    throw "git was not found. Install Git and ensure it is available on PATH."
}
$uv = Get-Command uv -ErrorAction SilentlyContinue
if ($null -eq $uv) {
    throw "uv was not found. Install uv and ensure it is available on PATH."
}

Push-Location $ProjectRoot
try {
    $branch = (& $git.Source branch --show-current).Trim()
    if ($LASTEXITCODE -ne 0 -or $branch -ne "deployment") {
        throw "Updates must run on the deployment branch. Current branch: $branch"
    }

    $worktreeChanges = & $git.Source status --porcelain --untracked-files=all
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to inspect the worktree status."
    }
    if ($worktreeChanges) {
        throw "The worktree has tracked or trackable changes; refusing to update."
    }

    $upstreamUrl = (& $git.Source remote get-url upstream).Trim()
    if ($LASTEXITCODE -ne 0 -or $upstreamUrl -ne $UpstreamRepository) {
        throw "upstream must point to $UpstreamRepository. Current URL: $upstreamUrl"
    }

    $release = Invoke-RestMethod -Uri $LatestReleaseApi -Headers @{ "User-Agent" = "maibot-yelin-updater" } -TimeoutSec 30
    $tag = [string]$release.tag_name
    if (-not $tag -or $release.draft -or $release.prerelease -or $tag -notmatch '^v?\d+\.\d+\.\d+$') {
        throw "GitHub latest Release is not a stable semantic version."
    }

    Write-Host "Fetching stable MaiBot release $tag ..."
    & $git.Source fetch upstream "refs/tags/$tag`:refs/tags/$tag"
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to fetch upstream tag $tag."
    }

    & $git.Source merge --no-edit $tag
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to merge $tag. Resolve the conflict before continuing."
    }

    & $uv.Source sync --frozen
    if ($LASTEXITCODE -ne 0) {
        throw "The stable release was merged, but uv sync --frozen failed."
    }

    Write-Host "MaiBot was updated to stable release $tag. The deployment branch was not pushed."
}
finally {
    Pop-Location
}
