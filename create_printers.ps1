<#
.SYNOPSIS
  Creates the machine-wide logical printers CoolPrint needs.

.DESCRIPTION
  Clones an existing, working Canon printer (driver + port are copied from it),
  then sets paper size and duplex on every copy. Finally reads the settings back
  as SYSTEM (the account the service runs under) and compares them.
  Safe to re-run: existing printers are only re-configured.

.PARAMETER Source
  Existing printer to copy driver and port from (default: Canon_Long).
.PARAMETER NoA3
  Only A4 printers (Canon_Long / Canon_Short / Canon_None).
.PARAMETER NoVerify
  Skip the read-back check as SYSTEM.

  Names must match config.ini ([printer] name_*, [paper] a3_*).
#>
param(
    [string]$Source = 'Canon_Long',
    [switch]$NoA3,
    [switch]$NoVerify
)
$ErrorActionPreference = 'Stop'

$me = [Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
if (-not $me.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Host 'Run as administrator.' -ForegroundColor Red
    exit 1
}

$src = Get-Printer -Name $Source -ErrorAction SilentlyContinue
if (-not $src) {
    Write-Host "Source printer '$Source' not found. Installed printers:" -ForegroundColor Red
    Get-Printer | Format-Table Name, DriverName, PortName -AutoSize
    Write-Host 'Pass the right one: create_printers.bat -Source "<name>"'
    exit 1
}
Write-Host "Source: $($src.Name)  driver='$($src.DriverName)'  port='$($src.PortName)'"

$plan = @(
    @{ Name = 'Canon_Long';     Paper = 'A4'; Duplex = 'TwoSidedLongEdge'  },
    @{ Name = 'Canon_Short';    Paper = 'A4'; Duplex = 'TwoSidedShortEdge' },
    @{ Name = 'Canon_None';     Paper = 'A4'; Duplex = 'OneSided'          },
    @{ Name = 'Canon_A3_Long';  Paper = 'A3'; Duplex = 'TwoSidedLongEdge'  },
    @{ Name = 'Canon_A3_Short'; Paper = 'A3'; Duplex = 'TwoSidedShortEdge' },
    @{ Name = 'Canon_A3_None';  Paper = 'A3'; Duplex = 'OneSided'          }
)
if ($NoA3) { $plan = $plan | Where-Object { $_.Paper -eq 'A4' } }

$failed = @()
foreach ($p in $plan) {
    try {
        if (-not (Get-Printer -Name $p.Name -ErrorAction SilentlyContinue)) {
            Add-Printer -Name $p.Name -DriverName $src.DriverName -PortName $src.PortName
            Write-Host "[created]    $($p.Name)"
        } else {
            Write-Host "[exists]     $($p.Name)"
        }
        Set-PrintConfiguration -PrinterName $p.Name -PaperSize $p.Paper -DuplexingMode $p.Duplex
        Write-Host "[configured] $($p.Name): $($p.Paper), $($p.Duplex)"
    } catch {
        Write-Host "[FAILED]     $($p.Name): $($_.Exception.Message)" -ForegroundColor Red
        $failed += $p.Name
    }
}

if (-not $NoVerify) {
    Write-Host ''
    Write-Host 'Reading settings back as SYSTEM (what the service sees)...'
    $chk = Join-Path $env:ProgramData 'coolprint_check.ps1'
    $res = Join-Path $env:ProgramData 'coolprint_check.txt'
    $task = 'CoolPrintCheck'
    Remove-Item $res -ErrorAction SilentlyContinue
    $names = ($plan | ForEach-Object { "'$($_.Name)'" }) -join ','
    Set-Content -Path $chk -Value @(
        "`$out = '$res'",
        "foreach (`$n in @($names)) {",
        '  try { $c = Get-PrintConfiguration -PrinterName $n; "$n|$($c.PaperSize)|$($c.DuplexingMode)" | Add-Content $out }',
        '  catch { "$n|ERROR|$($_.Exception.Message)" | Add-Content $out }',
        '}'
    )
    try {
        & schtasks /Create /TN $task /SC ONCE /ST 00:00 /RU SYSTEM /RL HIGHEST /F `
            /TR "powershell.exe -NoProfile -ExecutionPolicy Bypass -File $chk" | Out-Null
        & schtasks /Run /TN $task | Out-Null
        for ($i = 0; $i -lt 40; $i++) {
            Start-Sleep -Milliseconds 750
            if ((Test-Path $res) -and (@(Get-Content $res).Count -ge $plan.Count)) { break }
        }
        if (-not (Test-Path $res)) {
            Write-Host 'No answer from the SYSTEM check; verify by hand (Printer properties -> Advanced -> Printing Defaults).' -ForegroundColor Yellow
        } else {
            $got = @{}
            foreach ($line in Get-Content $res) { $f = $line -split '\|', 3; $got[$f[0]] = $f }
            foreach ($p in $plan) {
                $g = $got[$p.Name]
                if ($g -and $g[1] -eq $p.Paper -and $g[2] -eq $p.Duplex) {
                    Write-Host ("[OK]       {0,-15} {1}, {2}" -f $p.Name, $g[1], $g[2]) -ForegroundColor Green
                } else {
                    $seen = if ($g) { "$($g[1]), $($g[2])" } else { 'no data' }
                    Write-Host ("[MISMATCH] {0,-15} wanted {1}, {2}; SYSTEM sees {3}" -f $p.Name, $p.Paper, $p.Duplex, $seen) -ForegroundColor Red
                    $failed += $p.Name
                }
            }
        }
    } finally {
        & schtasks /Delete /TN $task /F 2>$null | Out-Null
        Remove-Item $chk, $res -ErrorAction SilentlyContinue
    }
}

Write-Host ''
if ($failed.Count) {
    Write-Host ("Fix by hand: Printer properties -> Advanced -> Printing Defaults for: " + (($failed | Select-Object -Unique) -join ', ')) -ForegroundColor Yellow
    exit 2
}
Write-Host 'Done. Make sure these names match config.ini, then restart the CoolPrint service.' -ForegroundColor Green
