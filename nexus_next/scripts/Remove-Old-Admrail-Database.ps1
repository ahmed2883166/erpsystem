param()
$ErrorActionPreference = "Stop"
$script:Psql = $null

function Read-PlainSecret([string]$Prompt) {
    $Secure = Read-Host $Prompt -AsSecureString
    $Ptr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($Secure)
    try { return [Runtime.InteropServices.Marshal]::PtrToStringBSTR($Ptr) }
    finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($Ptr) }
}
function Invoke-PsqlScalar([string]$Sql) {
    $Output = & $script:Psql -X -q -v ON_ERROR_STOP=1 -h 127.0.0.1 -p 5432 -U postgres -d postgres -Atc $Sql 2>&1
    if ($LASTEXITCODE -ne 0) { throw "PostgreSQL query failed: $($Output -join ' ')" }
    return ($Output -join [Environment]::NewLine).Trim()
}
function Quote-PgIdentifier([string]$Name) { return '"' + $Name.Replace('"','""') + '"' }

try {
    Write-Host "Admrail ERP - PostgreSQL database cleanup" -ForegroundColor Cyan
    Write-Host "Select one user database. System databases are excluded and protected." -ForegroundColor Yellow
    Write-Host "No database or role is deleted without separate exact confirmation." -ForegroundColor Yellow

    $Service = Get-Service -Name "postgresql-x64-16" -ErrorAction SilentlyContinue
    if (-not $Service) { throw "PostgreSQL 16 Windows service was not found." }
    if ($Service.Status -ne "Running") { throw "PostgreSQL 16 service is not running. Start it and retry." }
    $Cmd = Get-Command "psql.exe" -ErrorAction SilentlyContinue
    if ($Cmd) { $script:Psql = $Cmd.Source }
    else {
        $Path = "C:\Program Files\PostgreSQL\16\bin\psql.exe"
        if (-not (Test-Path -LiteralPath $Path)) { throw "psql.exe was not found. Install PostgreSQL Command Line Tools." }
        $script:Psql = $Path
    }

    $AdminPassword = Read-PlainSecret "Enter PostgreSQL administrator (postgres) password"
    if ([string]::IsNullOrWhiteSpace($AdminPassword)) { throw "The PostgreSQL password cannot be empty." }
    $env:PGPASSWORD = $AdminPassword
    $null = Invoke-PsqlScalar "SELECT current_user;"
    $AdminPassword = $null

    $DbJson = Invoke-PsqlScalar @"
SELECT COALESCE(json_agg(json_build_object(
 'name', d.datname, 'owner', r.rolname,
 'size', pg_size_pretty(pg_database_size(d.datname)),
 'encoding', pg_encoding_to_char(d.encoding)
) ORDER BY d.datname), '[]'::json)::text
FROM pg_database d JOIN pg_roles r ON r.oid=d.datdba
WHERE NOT d.datistemplate AND d.datname <> 'postgres';
"@
    $Databases = @($DbJson | ConvertFrom-Json)
    if ($Databases.Count -eq 0 -or -not $Databases[0].name) {
        Write-Host "No user databases found. Nothing to delete." -ForegroundColor Green
        exit 0
    }

    Write-Host "`nAvailable user databases:" -ForegroundColor Cyan
    for ($i=0; $i -lt $Databases.Count; $i++) {
        $Db=$Databases[$i]
        Write-Host ("[{0}] {1} | Owner: {2} | Size: {3} | Encoding: {4}" -f ($i+1),$Db.name,$Db.owner,$Db.size,$Db.encoding)
    }
    $Choice=Read-Host "Enter the number of the OLD database to delete, or Enter to cancel"
    if ([string]::IsNullOrWhiteSpace($Choice)) { Write-Host "Cancelled. Nothing deleted."; exit 0 }
    if ($Choice -notmatch '^\d+$') { throw "Enter a valid database number." }
    $Index=[int]$Choice-1
    if ($Index -lt 0 -or $Index -ge $Databases.Count) { throw "Database number is outside the list." }
    $SelectedDb=[string]$Databases[$Index].name
    if ($SelectedDb -in @("postgres","template0","template1") -or $SelectedDb.StartsWith("template")) {
        throw "System/template databases are protected."
    }

    $EscapedDb=$SelectedDb.Replace("'","''")
    $RoleJson=Invoke-PsqlScalar @"
SELECT COALESCE(json_agg(json_build_object('name',role_name,'relationship',relationship) ORDER BY role_name),'[]'::json)::text
FROM (
 SELECT r.rolname AS role_name, 'database owner'::text AS relationship
 FROM pg_database d JOIN pg_roles r ON r.oid=d.datdba WHERE d.datname='$EscapedDb'
 UNION
 SELECT DISTINCT r.rolname, 'database-level privilege'::text
 FROM pg_database d
 CROSS JOIN LATERAL aclexplode(COALESCE(d.datacl,acldefault('d',d.datdba))) a
 JOIN pg_roles r ON r.oid=a.grantee
 WHERE d.datname='$EscapedDb' AND a.grantee<>0
) related;
"@
    $RelatedRoles=@($RoleJson | ConvertFrom-Json)
    Write-Host "`nSelected database: $SelectedDb" -ForegroundColor Yellow
    Write-Host ("Owner: {0} | Size: {1}" -f $Databases[$Index].owner,$Databases[$Index].size)
    Write-Host "Related roles:"
    if ($RelatedRoles.Count -eq 0 -or -not $RelatedRoles[0].name) { Write-Host "  (none listed)" }
    else { foreach($Role in $RelatedRoles) { Write-Host ("  - {0} ({1})" -f $Role.name,$Role.relationship) } }

    Write-Host "`nWARNING: all data inside '$SelectedDb' will be permanently deleted." -ForegroundColor Red
    Write-Host "If records may be needed, cancel and take a PostgreSQL backup first." -ForegroundColor Red
    $Confirm=Read-Host "Type the exact database name '$SelectedDb' to permanently delete it"
    if ($Confirm -cne $SelectedDb) { Write-Host "Confirmation mismatch. Nothing deleted."; exit 0 }

    $QuotedDb=Quote-PgIdentifier $SelectedDb
    $DropOutput=& $script:Psql -X -q -v ON_ERROR_STOP=1 -h 127.0.0.1 -p 5432 -U postgres -d postgres -c "DROP DATABASE $QuotedDb WITH (FORCE);" 2>&1
    if ($LASTEXITCODE -ne 0) { throw "Could not delete '$SelectedDb': $($DropOutput -join ' ')" }
    Write-Host "Database '$SelectedDb' deleted." -ForegroundColor Green

    $RoleNames=@($RelatedRoles | ForEach-Object { [string]$_.name } | Where-Object { $_ -and -not $_.StartsWith("pg_") } | Select-Object -Unique)
    if ($RoleNames.Count -gt 0) {
        Write-Host "`nRoles are NOT deleted automatically. Select one only if you know it is no longer used:" -ForegroundColor Yellow
        for($i=0;$i -lt $RoleNames.Count;$i++){ Write-Host ("[{0}] {1}" -f ($i+1),$RoleNames[$i]) }
        $RoleChoice=Read-Host "Role number to attempt removal, or Enter to keep all roles"
        if (-not [string]::IsNullOrWhiteSpace($RoleChoice)) {
            if ($RoleChoice -notmatch '^\d+$') { throw "Invalid role number. Database was deleted; no role was removed." }
            $ri=[int]$RoleChoice-1
            if($ri -lt 0 -or $ri -ge $RoleNames.Count){ throw "Role number outside list. Database was deleted; no role was removed." }
            $SelectedRole=$RoleNames[$ri]
            $EscapedRole=$SelectedRole.Replace("'","''")
            $Info=Invoke-PsqlScalar "SELECT json_build_object('superuser',rolsuper,'replication',rolreplication,'bypassrls',rolbypassrls,'login',rolcanlogin)::text FROM pg_roles WHERE rolname='$EscapedRole';" | ConvertFrom-Json
            if(-not $Info -or $Info.superuser -or $Info.replication -or $Info.bypassrls -or -not $Info.login) {
                throw "Selected role is privileged or is not a normal login role. It was kept."
            }
            $OtherDbCount=[int](Invoke-PsqlScalar "SELECT count(*) FROM pg_database WHERE datdba=(SELECT oid FROM pg_roles WHERE rolname='$EscapedRole');")
            if($OtherDbCount -gt 0){ throw "Role '$SelectedRole' owns another database. It was kept." }
            $RoleConfirm=Read-Host "Type exactly 'DELETE ROLE $SelectedRole' to attempt role removal"
            if($RoleConfirm -ceq "DELETE ROLE $SelectedRole"){
                $QuotedRole=Quote-PgIdentifier $SelectedRole
                $RoleOutput=& $script:Psql -X -q -v ON_ERROR_STOP=1 -h 127.0.0.1 -p 5432 -U postgres -d postgres -c "DROP ROLE $QuotedRole;" 2>&1
                if($LASTEXITCODE -ne 0){ Write-Host "Role kept: PostgreSQL reports dependencies or use by other objects. No other objects were removed." -ForegroundColor Yellow }
                else{ Write-Host "Role '$SelectedRole' removed." -ForegroundColor Green }
            } else { Write-Host "Role confirmation mismatch. Role kept." -ForegroundColor Yellow }
        }
    }
    Write-Host "`nCleanup finished. PostgreSQL and all other databases remain installed." -ForegroundColor Green
    Write-Host "You can now run Setup-Admrail.cmd to create the new Admrail ERP v2 database." -ForegroundColor Green
    Read-Host "Press Enter to close"
}
catch {
    Write-Host "`nCLEANUP STOPPED: $($_.Exception.Message)" -ForegroundColor Red
    Write-Host "Any completed database deletion is permanent. Review the message before retrying." -ForegroundColor Yellow
    Read-Host "Press Enter to close"
    exit 1
}
finally { Remove-Item Env:PGPASSWORD -ErrorAction SilentlyContinue }
