# Copyright (C) Agustín Giménez Bernad (gusmanb), original LogicAnalyzer
# Copyright (C) 2026 Julian Decker
#
# Part of PiPiLogicAnalyzer, based on his LogicAnalyzer firmware;
# the changes are described in firmware/README.md.
#
# SPDX-License-Identifier: GPL-3.0-or-later

# Windows counterpart of firmware/build_all.sh: builds the firmware for every board (and turbo
# variant) into .\publish. The images are named PiPiLogicAnalyzer_<BOARD_TYPE>[_Turbo].uf2, which
# the application uses to recognise the board when flashing.
#
#   .\publish.ps1                            all boards
#   .\publish.ps1 BOARD_PICO BOARD_PICO_2    selected boards
#
# Uses the Pico SDK 2.1.1 and the tools installed by the "Raspberry Pi Pico" VS Code extension.

param (
    [string[]]$Boards
)

$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot

$allBoards = @("BOARD_PICO", "BOARD_PICO_2", "BOARD_PICO_W", "BOARD_PICO_W_WIFI", "BOARD_PICO_2_W", "BOARD_PICO_2_W_WIFI", "BOARD_ZERO", "BOARD_INTERCEPTOR")
if (-not $Boards) {
    $Boards = $allBoards
}

# CMakeLists.txt silently falls back to the Pico for unknown boards
foreach ($board in $Boards) {
    if ($board -notin $allBoards) {
        throw "Unknown board $board, known: $($allBoards -join ' ')"
    }
}

# Path to the build settings file
$buildSettingsFile = "PiPiLogicAnalyzer_Build_Settings.cmake"
$buildSettingsBackup = "$buildSettingsFile.orig"

# Tools of the VS Code extension
$picoHome = "${env:USERPROFILE}/.pico-sdk"
$cmakeBinPath = "$picoHome/cmake/v3.31.5/bin"
$ninjaPath = "$picoHome/ninja/v1.12.1"
$picoSdkPath = "$picoHome/sdk/2.1.1"
$picoToolchainPath = "$picoHome/toolchain/14_2_Rel1"
$picoToolchainBinPath = "$picoToolchainPath/bin"
$picoToolPath = "$picoHome/picotool/2.1.1/picotool"

# A leftover copy means an earlier run was stopped before it could restore the settings
if (Test-Path -Path $buildSettingsBackup) {
    throw "$buildSettingsBackup exists from an interrupted run. Move it back over $buildSettingsFile or delete it, then start again."
}

# Create or clear the publish directory
$publishDir = Join-Path $PSScriptRoot "publish"
if (-not (Test-Path -Path $publishDir)) {
    New-Item -ItemType Directory -Path $publishDir | Out-Null
} else {
    Remove-Item -Recurse -Force "$publishDir\*"
}
# Separate from .\build, so the VS Code build keeps its board in the cache
$buildDir = Join-Path $publishDir ".build"

# Set environment variables
$env:PICO_SDK_PATH = $picoSdkPath
$env:PICO_TOOLCHAIN_PATH = $picoToolchainPath

# Add paths to $env:Path only if they are not already set
$pathsToAdd = @($picoToolchainBinPath, $picoToolPath, $cmakeBinPath, $ninjaPath)
foreach ($path in $pathsToAdd) {
    if (-not ($path -in ($env:Path -split ";" | ForEach-Object { $_.Trim() }))) {
        $env:Path = "$path;$env:Path"
    }
}

$processorCount = [Environment]::ProcessorCount
$settings = Get-Content $buildSettingsFile
Copy-Item -Path $buildSettingsFile -Destination $buildSettingsBackup
$built = 0
$failed = 0

try {
    foreach ($boardType in $Boards) {
        foreach ($turboMode in @("0", "1")) {
            # Skip turbo mode for the W variants (CMakeLists.txt refuses them, the Pico 2 W ones included)
            if ($turboMode -eq "1" -and ($boardType -like "BOARD_PICO_W*" -or $boardType -like "BOARD_PICO_2_W*")) {
                continue
            }

            $binaryName = "PiPiLogicAnalyzer_$boardType"
            if ($turboMode -eq "1") {
                $binaryName = "${binaryName}_Turbo"
            }
            Write-Host "==> $binaryName"

            # Update the build settings file
            $content = $settings -replace '^set\(BOARD_TYPE .*\)', "set(BOARD_TYPE `"$boardType`")"
            $content = $content -replace '^set\(TURBO_MODE .*\)', "set(TURBO_MODE $turboMode)"
            Set-Content $buildSettingsFile $content

            # The board is cached by CMake, every variant needs a fresh build directory
            Remove-Item -Recurse -Force $buildDir -ErrorAction SilentlyContinue

            & cmake -S . -B $buildDir -G "Ninja"
            if ($LASTEXITCODE -eq 0) {
                & cmake --build $buildDir --config Release -- -j $processorCount
            }

            $uf2File = Join-Path $buildDir "PiPiLogicAnalyzer.uf2"
            if ($LASTEXITCODE -eq 0 -and (Test-Path -Path $uf2File)) {
                Move-Item -Path $uf2File -Destination "$publishDir\$binaryName.uf2"
                $built++
            } else {
                Write-Host "Error: $binaryName failed"
                $failed++
            }
        }
    }
}
finally {
    Move-Item -Force -Path $buildSettingsBackup -Destination $buildSettingsFile
    Remove-Item -Recurse -Force $buildDir -ErrorAction SilentlyContinue
}

Write-Host ""
Write-Host "$built image(s) in $publishDir, $failed failed."
if ($failed -gt 0) {
    exit 1
}
