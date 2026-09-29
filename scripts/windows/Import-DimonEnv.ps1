# Dot-sourced by the Windows service launchers. Loads apps/dimon_live/.env into
# the process environment; explicit shell variables keep precedence, matching
# how the application itself treats that file.

$dimonEnvFile = Join-Path $PSScriptRoot "..\..\apps\dimon_live\.env"
if (Test-Path -LiteralPath $dimonEnvFile -PathType Leaf) {
    foreach ($line in Get-Content -LiteralPath $dimonEnvFile) {
        if ($line -match '^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$') {
            $name = $Matches[1]
            $value = $Matches[2].Trim('"', "'")
            if ($value -and -not [Environment]::GetEnvironmentVariable($name)) {
                [Environment]::SetEnvironmentVariable($name, $value)
            }
        }
    }
}
