param(
  [string]$MySqlExe = "mysql",
  [string]$HostName = "127.0.0.1",
  [int]$Port = 3306,
  [string]$User = "root",
  [string]$Database = "",
  [Parameter(Mandatory = $true)]
  [string[]]$SqlFile
)

$ErrorActionPreference = "Stop"

foreach ($file in $SqlFile) {
  if (-not (Test-Path -LiteralPath $file)) {
    throw "SQL file not found: $file"
  }
}

$databaseArg = @()
if (-not [string]::IsNullOrWhiteSpace($Database)) {
  $databaseArg = @($Database)
}

foreach ($file in $SqlFile) {
  $resolved = (Resolve-Path -LiteralPath $file).Path
  Write-Host "Importing SQL file: $resolved" -ForegroundColor Cyan

  Get-Content -LiteralPath $resolved -Raw -Encoding UTF8 |
    & $MySqlExe `
      -h $HostName `
      -P $Port `
      -u $User `
      -p `
      --default-character-set=utf8mb4 `
      @databaseArg
}

Write-Host "Import finished." -ForegroundColor Green
