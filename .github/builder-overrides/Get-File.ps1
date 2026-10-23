Function Get-File {
    <#
    .SYNOPSIS
        php-eol-security replacement for the builder's Get-File: serves locked inputs only.
    .DESCRIPTION
        Every URL the builder requests must have been fetched and checked against inputs.lock.json
        by ".github/scripts/locked_inputs.py fetch", which writes $env:LOCKED_INPUTS_DIR\index.json.
        The file is hashed again before use; any other URL fails the build.
        The exception is the Visual Studio installers ("signed" in the lock), which change with
        every VS release: they are downloaded here and accepted only with a valid Authenticode
        signature by the locked signer.
    .PARAMETER Url
        The primary URL to download the file from.
    .PARAMETER FallbackUrl
        An optional fallback URL to use if the primary URL is not locked.
    .PARAMETER OutFile
        The output file path; without it, an object with the file's text as Content is returned.
    .PARAMETER Retries
        Unused, kept for compatibility.
    .PARAMETER TimeoutSec
        Unused, kept for compatibility.
    #>
    [OutputType()]
    param (
        [Parameter(Mandatory = $true, Position=0, HelpMessage='Primary URL to download the file from')]
        [ValidateNotNullOrEmpty()]
        [string] $Url,

        [Parameter(Mandatory = $false, Position=1, HelpMessage='Fallback URL to use if the primary URL fails')]
        [string] $FallbackUrl,

        [Parameter(Mandatory = $false, Position=2, HelpMessage='Output file path for the downloaded content')]
        [string] $OutFile = '',

        [Parameter(Mandatory = $false, Position=3, HelpMessage='Number of retries for download attempts')]
        [int] $Retries = 3,

        [Parameter(Mandatory = $false, Position=4, HelpMessage='Timeout in seconds for each download attempt')]
        [int] $TimeoutSec = 0
    )

    if (-not $env:LOCKED_INPUTS_DIR) {
        throw "LOCKED_INPUTS_DIR is not set, refusing to download $Url"
    }
    $lock = Get-Content -LiteralPath (Join-Path $env:LOCKED_INPUTS_DIR 'index.json') -Raw | ConvertFrom-Json -AsHashtable
    $signed = $lock.signed[$Url]
    if ($null -ne $signed) {
        if ($OutFile -eq '') {
            throw "${Url}: signed inputs are executables, an output file is required"
        }
        $temp = Join-Path ([System.IO.Path]::GetTempPath()) ([System.Guid]::NewGuid().ToString() + [System.IO.Path]::GetExtension($Url))
        try {
            Invoke-WebRequest -Uri $Url -OutFile $temp -TimeoutSec $TimeoutSec -UseBasicParsing
            $signature = Get-AuthenticodeSignature -LiteralPath $temp
            if ($signature.Status -ne 'Valid' -or $signature.SignerCertificate.Subject -ne $signed.signer) {
                throw "${Url}: signature $($signature.Status) by '$($signature.SignerCertificate.Subject)', locked signer '$($signed.signer)'"
            }
            Write-Host "Signed input $Url (signer $($signature.SignerCertificate.Subject), thumbprint $($signature.SignerCertificate.Thumbprint), sha256 $((Get-FileHash -LiteralPath $temp -Algorithm SHA256).Hash.ToLowerInvariant()))"
            Copy-Item -LiteralPath $temp -Destination $OutFile -Force
            if ($env:SIGNED_INPUTS_LOG) {
                [ordered]@{
                    url = $Url
                    signer = $signature.SignerCertificate.Subject
                    thumbprint = $signature.SignerCertificate.Thumbprint
                    sha256 = (Get-FileHash -LiteralPath $temp -Algorithm SHA256).Hash.ToLowerInvariant()
                } | ConvertTo-Json -Compress | Add-Content -LiteralPath $env:SIGNED_INPUTS_LOG
            }
        } finally {
            Remove-Item -LiteralPath $temp -Force -ErrorAction SilentlyContinue
        }
        return
    }

    $index = $lock.files
    $entry = $index[$Url]
    if ($null -eq $entry -and $FallbackUrl) {
        $entry = $index[$FallbackUrl]
    }
    if ($null -eq $entry) {
        throw "$Url is not a locked input (inputs.lock.json), refusing to download it"
    }

    $path = Join-Path $env:LOCKED_INPUTS_DIR $entry.path
    $sha256 = (Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($sha256 -ne $entry.sha256) {
        throw "${Url}: SHA-256 $sha256, locked $($entry.sha256)"
    }
    Write-Host "Locked input $Url ($sha256)"

    if ($OutFile -ne '') {
        Copy-Item -LiteralPath $path -Destination $OutFile -Force
    } else {
        return [PSCustomObject]@{ StatusCode = 200; Content = [System.IO.File]::ReadAllText($path) }
    }
}
