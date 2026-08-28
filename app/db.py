"""Camada de persistência: escolhe o backend conforme o ambiente.

- Local / notebook: SQLite (arquivo, zero configuração) - ver db_sqlite.py.
- Serverless / Lambda: DynamoDB (o disco do Lambda é efêmero) - ver db_dynamo.py.

A escolha é feita por config.USAR_DYNAMO. As duas implementações expõem a
mesma interface, então o resto do app (main.py) não sabe qual está em uso.
"""
from .config import USAR_DYNAMO

if USAR_DYNAMO:
    from .db_dynamo import (
        atualizar_job,
        buscar_analise,
        buscar_job,
        contar_mensagens,
        criar_job,
        listar_analises,
        listar_mensagens,
        salvar_analise,
        salvar_mensagens,
    )
else:
    from .db_sqlite import (
        atualizar_job,
        buscar_analise,
        buscar_job,
        contar_mensagens,
        criar_job,
        listar_analises,
        listar_mensagens,
        salvar_analise,
        salvar_mensagens,
    )

__all__ = [
    "salvar_analise",
    "buscar_analise",
    "listar_mensagens",
    "salvar_mensagens",
    "contar_mensagens",
    "listar_analises",
    "criar_job",
    "buscar_job",
    "atualizar_job",
]
