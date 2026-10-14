# Water Reminder — install or update (Windows 10/11).
#
#   irm https://raw.githubusercontent.com/mhd-mansoor/water-reminder/main/install.ps1 | iex
#
# Run the same command again any time to update to the latest version.
# It downloads the newest release from GitHub, checks its SHA-256 checksum,
# installs it for your user only (no admin rights), turns on start-at-login
# and starts it. Your history and settings are kept.

& {
    $ErrorActionPreference = 'Stop'
    $ProgressPreference = 'SilentlyContinue'   # makes Invoke-WebRequest much faster
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

    $repo  = 'mhd-mansoor/water-reminder'
    $base  = "https://github.com/$repo/releases/latest/download"
    $asset = 'WaterReminder-Windows.zip'
    $tmp   = Join-Path $env:TEMP ("water-reminder-" + [guid]::NewGuid().ToString('N'))
    New-Item -ItemType Directory -Path $tmp | Out-Null

    function Say($msg) { Write-Host "  $msg" -ForegroundColor Cyan }

    try {
        Write-Host ""
        Write-Host "  Water Reminder installer" -ForegroundColor Blue
        Say "Downloading the latest version..."
        Invoke-WebRequest -UseBasicParsing -Uri "$base/SHA256SUMS.txt" -OutFile "$tmp\SHA256SUMS.txt"
        Invoke-WebRequest -UseBasicParsing -Uri "$base/$asset" -OutFile "$tmp\$asset"

        Say "Checking the download..."
        $line = Get-Content "$tmp\SHA256SUMS.txt" | Where-Object { $_ -match "\s\*?$([regex]::Escape($asset))$" } | Select-Object -First 1
        if (-not $line) { throw "No checksum found for $asset." }
        $expected = ($line -split '\s+')[0].ToLower()
        $actual = (Get-FileHash -Algorithm SHA256 "$tmp\$asset").Hash.ToLower()
        if ($expected -ne $actual) { throw "Checksum mismatch - the download was not installed." }

        Say "Installing..."
        Expand-Archive -Path "$tmp\$asset" -DestinationPath "$tmp\app" -Force
        $exe = Join-Path $tmp 'app\WaterReminder\WaterReminder.exe'
        if (-not (Test-Path $exe)) { throw "WaterReminder.exe is missing from the download." }
        # The app copies itself to %LOCALAPPDATA%\WaterReminder, sets up start-at-login and
        # Start-menu entries, stops any older copy, then starts the installed copy.
        # (WaitForExit waits only for this process, not the copy it starts in the background)
        $p = Start-Process -FilePath $exe -PassThru
        $null = $p.Handle
        if (-not $p.WaitForExit(120000)) { throw "The installer step took too long." }
        if ($p.ExitCode -ne 0) { throw "The app exited with code $($p.ExitCode)." }

        Write-Host ""
        Write-Host "  Done! Water Reminder is running - look for the water drop." -ForegroundColor Green
        Write-Host "  Run this same command any time to update." -ForegroundColor Green
        Write-Host ""
    }
    catch {
        Write-Host ""
        Write-Host "  Install failed: $($_.Exception.Message)" -ForegroundColor Red
        Write-Host "  You can also download it from https://github.com/$repo/releases/latest" -ForegroundColor Yellow
        Write-Host ""
    }
    finally {
        Start-Sleep -Seconds 2
        Remove-Item -Recurse -Force $tmp -ErrorAction SilentlyContinue
    }
}
