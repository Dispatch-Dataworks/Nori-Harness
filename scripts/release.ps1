# Cuts a GitHub release for the VERSION in the repo root, and attaches the Android app to it.
#   .\scripts\release.ps1 -Notes "what changed"          build the APK (unit tests first), then tag + publish with the APK attached
#   .\scripts\release.ps1 -NotesFile notes.md            same, notes from a file (they are what people read in the Updates tab)
#   .\scripts\release.ps1                                same, with GitHub's generated notes
#   .\scripts\release.ps1 -SkipTests                     skip the Android unit tests (the build still runs)
#   .\scripts\release.ps1 -NoApk                         a release with no APK (a server-only change you do not want to rebuild for)
#   .\scripts\release.ps1 -DryRun                        do everything except publish, and print the command
# A version with a dash in it (0.13.0-beta) is published as a pre-release. The APK is named nori-companion-<versionName>.apk, where versionName is
# android_app\version.properties (bump it there whenever the app changed: Android only updates in place when versionCode goes up).
# The APK is signed with the local debug key (see android_app\README.md), which is why this runs here and not in CI: the key must be the same one every time.
# Needs: gh (logged in), and for the APK the tools in NORI_ANDROID_TOOLS (see android_app\build.ps1). Commit and push first; this refuses to release anything else.
param([string]$Notes = "", [string]$NotesFile = "", [switch]$SkipTests, [switch]$NoApk, [switch]$DryRun)

$ErrorActionPreference = 'Stop'
$repo = Split-Path -Parent $PSScriptRoot
Push-Location $repo
try {
    $version = (Get-Content (Join-Path $repo 'VERSION') -Raw).Trim()
    if ($version -notmatch '^\d+\.\d+\.\d+([-.][0-9A-Za-z.-]+)?$') { throw "VERSION '$version' does not look like a version (e.g. 0.13.0-beta)" }
    $tag = "v$version"
    $pre = $version.Contains('-')

    # Only release what is committed and pushed: the release must describe a commit anyone can fetch.
    if (git status --porcelain) { throw "the working tree has uncommitted changes: commit them first" }
    git fetch --quiet origin
    $branch = (git rev-parse --abbrev-ref HEAD).Trim()
    if ((git rev-parse HEAD).Trim() -ne (git rev-parse "origin/$branch").Trim()) { throw "HEAD is not the same as origin/${branch}: push (or pull) first" }
    if (git tag --list $tag) { throw "the tag $tag already exists: bump VERSION" }
    gh release view $tag 2>$null | Out-Null
    if ($LASTEXITCODE -eq 0) { throw "a release $tag already exists: bump VERSION" }
    $global:LASTEXITCODE = 0

    $assets = @()
    if (-not $NoApk) {
        $props = Get-Content (Join-Path $repo 'android_app\version.properties') -Raw
        if ($props -notmatch '(?m)^versionName=(.+)$') { throw "no versionName in android_app\version.properties" }
        $appVersion = $Matches[1].Trim()
        Write-Host "building the Android app $appVersion ..."
        $buildArgs = @(); if (-not $SkipTests) { $buildArgs += '-Test' }
        & (Join-Path $repo 'android_app\build.ps1') @buildArgs
        $built = Get-ChildItem -Recurse (Join-Path $repo 'android_app\app\build\outputs\apk\release') -Filter *.apk | Select-Object -First 1
        if (-not $built) { throw "the build produced no APK" }
        $out = Join-Path $env:TEMP "nori-release-$version"
        New-Item -ItemType Directory -Force $out | Out-Null
        $apk = Join-Path $out "nori-companion-$appVersion.apk"
        Copy-Item $built.FullName $apk -Force
        $assets += $apk
        Write-Host ("APK: {0} ({1} MB)" -f $apk, [math]::Round((Get-Item $apk).Length / 1MB, 1))
        # building must not have dirtied the tree (it would mean the APK is not from the committed source)
        if (git status --porcelain) { throw "the build changed tracked files: the APK does not match the commit" }
    }

    $ghArgs = @('release', 'create', $tag, '--title', $tag)
    if ($pre) { $ghArgs += '--prerelease' }
    if ($NotesFile) { $ghArgs += @('--notes-file', $NotesFile) } elseif ($Notes) { $ghArgs += @('--notes', $Notes) } else { $ghArgs += '--generate-notes' }
    $ghArgs += $assets
    if ($DryRun) { Write-Host "dry run: would run: gh $($ghArgs -join ' ')"; return }
    & gh @ghArgs
    if ($LASTEXITCODE -ne 0) { throw "gh release create failed" }
    $attached = if ($assets) { " with $($assets.Count) file(s) attached" } else { "" }
    Write-Host "released $tag$attached"
} finally { Pop-Location }
