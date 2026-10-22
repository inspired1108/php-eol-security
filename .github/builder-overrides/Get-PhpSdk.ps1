function Get-PhpSdk {
    <#
    .SYNOPSIS
        php-eol-security replacement for the builder's Get-PhpSdk: locked SDK, pre-seeded PGO inputs.
    .DESCRIPTION
        The SDK is the commit-verified checkout of php-sdk-binary-tools made by
        ".github/scripts/locked_inputs.py fetch" instead of the tag archive, which is not byte-stable.
        The PGO work directory is seeded with the locked nginx, MariaDB, training scripts and
        Composer, which phpsdk_pgo then uses instead of downloading them.
    #>
    [OutputType()]
    param (
    )
    begin {
        if (-not $env:LOCKED_INPUTS_DIR) {
            throw 'LOCKED_INPUTS_DIR is not set, refusing to download the PHP SDK'
        }
    }
    process {
        $index = Get-Content -LiteralPath (Join-Path $env:LOCKED_INPUTS_DIR 'index.json') -Raw | ConvertFrom-Json -AsHashtable
        Copy-Item -LiteralPath (Join-Path $env:LOCKED_INPUTS_DIR $index.sdk.path) -Destination php-sdk -Recurse
        Write-Host "Locked PHP SDK at commit $($index.sdk.commit)"

        foreach ($seed in $index.seeds) {
            $path = Join-Path $env:LOCKED_INPUTS_DIR $seed.path
            $sha256 = (Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash.ToLowerInvariant()
            if ($sha256 -ne $seed.sha256) {
                throw "$($seed.target): SHA-256 $sha256, expected $($seed.sha256)"
            }
            $target = Join-Path 'php-sdk\pgo\work' $seed.target
            New-Item -ItemType Directory -Force -Path (Split-Path $target -Parent) > $null
            Copy-Item -LiteralPath $path -Destination $target
            Write-Host "Locked PGO input $($seed.target) ($sha256)"
        }
    }
    end {
    }
}
