"""Lista os modelos disponíveis para a chave OpenAI configurada no .env."""
import os

from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

modelos = sorted(m.id for m in client.models.list())
interessantes = [m for m in modelos if m.startswith(("gpt", "o1", "o3", "o4", "chatgpt"))]

print(f"{len(modelos)} modelos acessíveis; candidatos para geração de texto:\n")
for m in interessantes:
    print("  ", m)
