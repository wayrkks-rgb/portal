$ErrorActionPreference = "Stop"

$python = Get-Command python.exe -ErrorAction SilentlyContinue
if ($null -eq $python) {
    throw "python.exe was not found in PATH. Install CPython 3.13 and add it to PATH."
}

& $python.Source -c "import sys; raise SystemExit(0 if sys.version_info[:2] == (3, 13) else 1)"
if ($LASTEXITCODE -ne 0) {
    throw "Python 3.13 is required."
}

$powerShellVersion = $PSVersionTable.PSVersion.ToString()
$powerCli = Get-Module -ListAvailable VMware.VimAutomation.Core | Sort-Object Version -Descending | Select-Object -First 1

Write-Host "Python:" (& $python.Source --version)
Write-Host "PowerShell:" $powerShellVersion
if ($null -eq $powerCli) {
    Write-Warning "VMware.VimAutomation.Core was not found. DEMO works, but POWERCLI collection will not."
} else {
    Write-Host "PowerCLI module:" $powerCli.Name $powerCli.Version
}
