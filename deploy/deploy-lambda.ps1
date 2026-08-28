# Reempacota o app e atualiza a funcao Lambda 'analise-credito' (perfil vertys).
# Uso:  powershell -ExecutionPolicy Bypass -File deploy\deploy-lambda.ps1
#
# Pre-requisitos: AWS CLI configurado com o perfil 'vertys' e a .venv do projeto
# criada (python 3.11+). O empacotamento baixa wheels Linux (manylinux/cp312),
# entao roda a partir do Windows sem Docker.

$ErrorActionPreference = "Stop"
# Configuraveis por variavel de ambiente (com padroes). Ajuste conforme a sua conta.
$Regiao  = if ($env:AWS_REGION)   { $env:AWS_REGION }   else { "sa-east-1" }
$Perfil  = if ($env:AWS_PROFILE)  { $env:AWS_PROFILE }  else { "default" }
$Funcao  = if ($env:FUNCTION_NAME){ $env:FUNCTION_NAME }else { "analise-credito" }

$Raiz    = Split-Path -Parent $PSScriptRoot          # raiz do projeto
$Py      = Join-Path $Raiz ".venv\Scripts\python.exe"
$Reqs    = Join-Path $Raiz "requirements-lambda.txt"
$Build   = Join-Path $env:TEMP "analise-lambda-build"
$Pacote  = Join-Path $Build "package"
$Zip     = Join-Path $Build "function.zip"

if (-not (Test-Path $Py)) { throw "Nao achei a .venv em $Py. Crie com: python -m venv .venv" }

Write-Host "=== 1. limpando build anterior ===" -ForegroundColor Cyan
Remove-Item -Recurse -Force $Build -ErrorAction SilentlyContinue | Out-Null
New-Item -ItemType Directory -Force -Path $Pacote | Out-Null

Write-Host "=== 2. instalando dependencias (wheels Linux cp312) ===" -ForegroundColor Cyan
& $Py -m pip install -r $Reqs --target $Pacote `
    --platform manylinux2014_x86_64 --python-version 3.12 --implementation cp `
    --only-binary=:all: --upgrade --quiet
if ($LASTEXITCODE -ne 0) { throw "pip install falhou" }

Write-Host "=== 3. copiando app/ e gerando o zip ===" -ForegroundColor Cyan
& $Py - $Raiz $Pacote $Zip @'
import shutil, os, sys, zipfile
raiz, pacote, zip_path = sys.argv[1], sys.argv[2], sys.argv[3]
dst = os.path.join(pacote, "app")
shutil.rmtree(dst, ignore_errors=True)
shutil.copytree(
    os.path.join(raiz, "app"), dst,
    ignore=lambda d, n: [x for x in n if x in ("__pycache__", "data", ".env") or x.endswith(".pyc")],
)
with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
    for root, _, files in os.walk(pacote):
        for f in files:
            if f.endswith(".pyc") or "__pycache__" in root:
                continue
            full = os.path.join(root, f)
            z.write(full, os.path.relpath(full, pacote).replace("\\", "/"))
print("zip:", round(os.path.getsize(zip_path) / 1e6, 1), "MB")
'@
if ($LASTEXITCODE -ne 0) { throw "empacotamento falhou" }

Write-Host "=== 4. atualizando a funcao Lambda ===" -ForegroundColor Cyan
aws lambda update-function-code --function-name $Funcao `
    --zip-file "fileb://$Zip" --profile $Perfil --region $Regiao `
    --query "LastUpdateStatus" --output text
aws lambda wait function-updated --function-name $Funcao --profile $Perfil --region $Regiao

Write-Host "=== pronto. Codigo atualizado em $Funcao ===" -ForegroundColor Green
Write-Host "URL do app: veja o endpoint do seu API Gateway (aws apigatewayv2 get-apis)."
