# Builds the Nori Android app (an APK, installed by hand: not on any store).
#   .\build.ps1              build the release APK, print its path, size and version
#   .\build.ps1 -Test        also run the JVM unit tests first
#   .\build.ps1 -Upload      build, then copy the APK into the running Nori container so Settings -> Devices can offer it
#                            (docker compose cp ... nori:/app/data/phone_app/nori-companion.apk, plus version.txt)
# The tools (JDK 17, Android SDK, Gradle) live in NORI_ANDROID_TOOLS (default D:\Dev\android-tools); nothing is installed system-wide.
param([switch]$Upload, [switch]$Test)

$tools = if ($env:NORI_ANDROID_TOOLS) { $env:NORI_ANDROID_TOOLS } else { 'D:\Dev\android-tools' }
$env:JAVA_HOME = Join-Path $tools 'jdk'
$env:ANDROID_HOME = Join-Path $tools 'sdk'
$env:ANDROID_SDK_ROOT = $env:ANDROID_HOME
$env:GRADLE_USER_HOME = Join-Path $tools 'gradle-home'
$env:Path = "$env:JAVA_HOME\bin;" + $env:Path

Push-Location $PSScriptRoot
try {
    if ($Test) {
        & .\gradlew.bat testDebugUnitTest --no-daemon
        if ($LASTEXITCODE -ne 0) { throw "the unit tests failed" }
    }
    & .\gradlew.bat assembleRelease --no-daemon
    if ($LASTEXITCODE -ne 0) { throw "the build failed" }
    $apk = Get-ChildItem -Recurse app\build\outputs\apk\release -Filter *.apk | Select-Object -First 1
    if (-not $apk) { throw "no APK was produced" }
    $version = (Select-String -Path version.properties -Pattern '^versionName=(.+)$').Matches[0].Groups[1].Value.Trim()
    Write-Host "built $($apk.FullName) ($([math]::Round($apk.Length / 1MB, 1)) MB), version $version"
    if ($Upload) {
        # The app runs from the main checkout (this folder may be a worktree of it).
        $repo = if ($env:NORI_COMPOSE_DIR) { $env:NORI_COMPOSE_DIR } else { Split-Path -Parent $PSScriptRoot }
        Push-Location $repo
        try {
            $tmp = Join-Path $env:TEMP 'nori-phone-app'
            New-Item -ItemType Directory -Force $tmp | Out-Null
            Copy-Item $apk.FullName (Join-Path $tmp 'nori-companion.apk') -Force
            Set-Content -Path (Join-Path $tmp 'version.txt') -Value $version -NoNewline
            docker compose exec -T nori mkdir -p /app/data/phone_app
            docker compose cp (Join-Path $tmp 'nori-companion.apk') nori:/app/data/phone_app/nori-companion.apk
            docker compose cp (Join-Path $tmp 'version.txt') nori:/app/data/phone_app/version.txt
            Write-Host "uploaded: Settings -> Devices now offers version $version"
        } finally { Pop-Location }
    }
} finally { Pop-Location }
