# Libera a porta 8000 no Firewall do Windows apenas para a rede local.
# Precisa ser executado UMA VEZ, como administrador.
#
# O escopo fica restrito a redes privadas e a faixas de IP locais: mesmo que a
# maquina va para uma rede publica (um wifi de hotel, por exemplo), a regra nao
# passa a valer ali.

$ErrorActionPreference = "Stop"
$nome = "Analise de Credito - Entes Publicos (LAN)"

$atual = Get-NetFirewallRule -DisplayName $nome -ErrorAction SilentlyContinue
if ($atual) {
    Write-Host "Regra ja existe. Removendo para recriar..." -ForegroundColor Yellow
    Remove-NetFirewallRule -DisplayName $nome
}

New-NetFirewallRule `
    -DisplayName $nome `
    -Direction Inbound `
    -Action Allow `
    -Protocol TCP `
    -LocalPort 8000 `
    -Profile Private `
    -RemoteAddress LocalSubnet | Out-Null

Write-Host "Regra criada: porta 8000 liberada apenas para a rede local privada." -ForegroundColor Green
Write-Host "Para remover depois: Remove-NetFirewallRule -DisplayName '$nome'"
