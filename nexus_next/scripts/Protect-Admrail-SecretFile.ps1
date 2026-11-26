param(
    [Parameter(Mandatory = $true)]
    [string]$Path
)

$ErrorActionPreference = "Stop"
if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
    throw "Secret configuration file does not exist: $Path"
}

$ResolvedPath = (Resolve-Path -LiteralPath $Path).Path
$CurrentIdentity = [Security.Principal.WindowsIdentity]::GetCurrent().Name
$AllowedPrincipals = @(
    "$($CurrentIdentity):(F)"
    "*S-1-5-18:(F)"
    "*S-1-5-32-544:(F)"
)

& icacls.exe $ResolvedPath /inheritance:r /grant:r $AllowedPrincipals | Out-Null
if ($LASTEXITCODE -ne 0) {
    throw "Could not restrict the Admrail secret file to its owner, SYSTEM, and local Administrators."
}

$Acl = Get-Acl -LiteralPath $ResolvedPath
if (-not $Acl.AreAccessRulesProtected) {
    throw "The Admrail secret file still inherits permissions from its parent directory."
}

$AllowedSids = @(
    [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
    "S-1-5-18"
    "S-1-5-32-544"
)
foreach ($Rule in $Acl.Access) {
    $Sid = $Rule.IdentityReference.Translate([Security.Principal.SecurityIdentifier]).Value
    if ($Sid -notin $AllowedSids) {
        throw "Unexpected principal still has access to the Admrail secret file: $($Rule.IdentityReference)"
    }
}
foreach ($Sid in $AllowedSids) {
    $HasFullControl = $false
    foreach ($Rule in $Acl.Access) {
        $RuleSid = $Rule.IdentityReference.Translate([Security.Principal.SecurityIdentifier]).Value
        if ($RuleSid -eq $Sid -and $Rule.AccessControlType -eq [Security.AccessControl.AccessControlType]::Allow -and
            ($Rule.FileSystemRights -band [Security.AccessControl.FileSystemRights]::FullControl) -eq [Security.AccessControl.FileSystemRights]::FullControl) {
            $HasFullControl = $true
            break
        }
    }
    if (-not $HasFullControl) {
        throw "Required full-control access is missing for security principal $Sid."
    }
}

Write-Output "Admrail secret file permissions are restricted and verified."
