# Sobe o app para acesso pela rede local (LAN).
#
# Diferente do modo local, aqui o servidor escuta em todas as interfaces, então
# qualquer máquina da rede alcança a porta. Por isso o script exige senha: sem
# ela, qualquer um na rede abriria o sistema, leria os dossiês e dispararia
# análises que consomem crédito de IA.

$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot

if (-not (Test-Path ".env")) {
    Write-Host "ERRO: arquivo .env nao encontrado. Copie o .env.example e preencha as chaves." -ForegroundColor Red
    exit 1
}

$senha = (Get-Content .env | Where-Object { $_ -match '^\s*APP_SENHA\s*=\s*(.+)$' })
if (-not $senha) {
    Write-Host "ERRO: defina APP_SENHA no .env antes de expor o app na rede." -ForegroundColor Red
    Write-Host "      Sem senha, qualquer maquina da rede teria acesso total." -ForegroundColor Red
    exit 1
}

$porta = 8000
$ips = Get-NetIPAddress -AddressFamily IPv4 |
    Where-Object { $_.IPAddress -notlike "127.*" -and $_.IPAddress -notlike "169.254.*" } |
    Select-Object -ExpandProperty IPAddress

Write-Host ""
Write-Host "  Analise de Credito - Entes Publicos" -ForegroundColor Cyan
Write-Host "  Acesse de outra maquina da rede em:" -ForegroundColor Cyan
foreach ($ip in $ips) { Write-Host "      http://${ip}:${porta}" -ForegroundColor Green }
Write-Host "  (login pedido pelo navegador; usuario padrao: licitacoes)"
Write-Host "  Para encerrar: Ctrl+C"
Write-Host ""

& .\.venv\Scripts\uvicorn.exe app.main:app --host 0.0.0.0 --port $porta
