"""Registro oficial de municípios do Tesouro Nacional (SICONFI).

Traz código IBGE, nome, UF, população e **CNPJ** de cada ente. É a fonte
autoritativa para resolver o CNPJ da prefeitura a partir do município - melhor
que pedir para o analista digitar, que é fonte de erro silencioso: um CNPJ
errado não falha, apenas devolve dados de outra entidade.

A lista é estável (muda quando um município é criado), então fica em cache
local por 30 dias.
"""
import json
import re
import time
import unicodedata

import httpx

from ..config import DATA_DIR
from .base import get_json, resultado

ENTES = "https://apidatalake.tesouro.gov.br/ords/siconfi/tt/entes"
CACHE = DATA_DIR / "entes_municipios.json"
CACHE_TTL = 30 * 24 * 3600

_memoria: dict | None = None


def _sem_acento(texto: str) -> str:
    return unicodedata.normalize("NFKD", texto).encode("ascii", "ignore").decode().lower()


async def _baixar(client: httpx.AsyncClient) -> dict:
    """Percorre a paginação do ORDS até trazer todos os municípios."""
    municipios: dict[str, dict] = {}
    offset = 0
    while offset < 20000:  # trava de segurança; o país tem ~5.570 municípios
        payload = await get_json(client, ENTES, params={"offset": offset, "limit": 1000})
        itens = payload.get("items", [])
        if not itens:
            break
        for i in itens:
            if str(i.get("esfera") or "").upper() != "M":
                continue
            cod = str(i.get("cod_ibge") or "")
            cnpj = re.sub(r"\D", "", str(i.get("cnpj") or ""))
            if len(cod) < 6 or len(cnpj) != 14:
                continue
            municipios[cod] = {
                "cod_ibge": cod,
                "nome": (i.get("ente") or "").strip(),
                "uf": (i.get("uf") or "").strip(),
                "cnpj": cnpj,
                "populacao": i.get("populacao"),
            }
        if not payload.get("hasMore"):
            break
        offset += len(itens)
    return municipios


async def carregar(client: httpx.AsyncClient) -> dict[str, dict]:
    """Devolve o mapa cod_ibge -> dados do município, usando cache local."""
    global _memoria
    if _memoria is not None:
        return _memoria

    if CACHE.exists():
        cache = json.loads(CACHE.read_text(encoding="utf-8"))
        if time.time() - cache.get("baixado_em", 0) < CACHE_TTL and cache.get("municipios"):
            _memoria = cache["municipios"]
            return _memoria

    municipios = await _baixar(client)
    if not municipios:
        raise ValueError("registro de entes do SICONFI veio vazio")
    CACHE.write_text(
        json.dumps({"baixado_em": time.time(), "municipios": municipios}, ensure_ascii=False),
        encoding="utf-8",
    )
    _memoria = municipios
    return _memoria


async def buscar(client: httpx.AsyncClient, consulta: str, limite: int = 12) -> list[dict]:
    """Busca municípios por nome, já devolvendo o CNPJ."""
    municipios = await carregar(client)
    alvo = _sem_acento(consulta.strip())
    if len(alvo) < 2:
        return []
    achados = [m for m in municipios.values() if alvo in _sem_acento(m["nome"])]
    achados.sort(key=lambda m: (not _sem_acento(m["nome"]).startswith(alvo), m["nome"]))
    return achados[:limite]


async def resolver_cnpj(client: httpx.AsyncClient, cod_ibge: str) -> str | None:
    try:
        return (await carregar(client)).get(str(cod_ibge), {}).get("cnpj")
    except Exception:  # noqa: BLE001 - resolução indisponível não derruba a análise
        return None


async def coletar_ente(client: httpx.AsyncClient, cod_ibge: str) -> dict:
    try:
        m = (await carregar(client)).get(str(cod_ibge))
        if not m:
            return resultado("ente", erro=f"município {cod_ibge} não consta no registro do SICONFI")
        return resultado("ente", m)
    except Exception as exc:  # noqa: BLE001
        return resultado("ente", erro=f"{type(exc).__name__}: {exc}")
