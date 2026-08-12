"""Configuração central: chaves de API e caminhos."""
import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)

load_dotenv(BASE_DIR / ".env")

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
