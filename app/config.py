"""Configuração central: chaves de API e caminhos."""
import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent

# No Lambda o disco do código (/var/task) é somente leitura e só /tmp é gravável;
# além disso /tmp é efêmero (some quando o container esfria). Por isso os caches
# de município/CAPAG vão para /tmp lá, e o histórico das análises não fica em
# arquivo nenhum - vai para o DynamoDB (ver db.py).
NO_LAMBDA = bool(os.getenv("AWS_LAMBDA_FUNCTION_NAME"))
DATA_DIR = Path("/tmp/data") if NO_LAMBDA else BASE_DIR / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)

load_dotenv(BASE_DIR / ".env")

# Armazenamento do histórico: SQLite local por padrão; DynamoDB no Lambda (ou
# quando USAR_DYNAMO estiver setado, útil para testar o backend Dynamo local).
USAR_DYNAMO = NO_LAMBDA or os.getenv("USAR_DYNAMO", "").strip().lower() in ("1", "true", "sim")
DYNAMO_TABELA = os.getenv("DYNAMO_TABELA", "analise-credito").strip()
# O Lambda injeta AWS_REGION automaticamente; local usamos sa-east-1.
AWS_REGION = os.getenv("AWS_REGION", "sa-east-1").strip()

# Chave gratuita - cadastro em https://portaldatransparencia.gov.br/api-de-dados/cadastrar-email
TRANSPARENCIA_API_KEY = os.getenv("TRANSPARENCIA_API_KEY", "").strip()

# Provedor da redação do dossiê: "openai" ou "anthropic".
LLM_PROVIDER = os.getenv("LLM_PROVIDER", "openai").strip().lower()

# Cada SDK lê sua própria variável de ambiente (OPENAI_API_KEY / ANTHROPIC_API_KEY).
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-5.6-terra")
ANTHROPIC_MODEL = os.getenv("ANTHROPIC_MODEL", "claude-opus-5")

# Acesso ao app. Se APP_SENHA estiver definida, todas as rotas exigem login.
# Deixar vazio só faz sentido rodando em 127.0.0.1 (uso local, uma pessoa).
APP_SENHA = os.getenv("APP_SENHA", "").strip()
APP_USUARIOS = [
    u.strip() for u in os.getenv("APP_USUARIOS", "licitacoes").split(",") if u.strip()
]

DB_PATH = DATA_DIR / "analises.db"
CAPAG_CACHE = DATA_DIR / "capag_municipios.json"
