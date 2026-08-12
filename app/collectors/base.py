"""Infraestrutura comum dos coletores: HTTP com retry e resultado padronizado.

Cada coletor devolve um dict {"fonte", "ok", "dados", "erro"} - uma fonte fora
do ar nunca derruba a análise inteira, apenas vira seção "indisponível".
"""
import asyncio

import httpx

TIMEOUT = httpx.Timeout(90.0, connect=15.0)  # PNCP e Tesouro podem levar >30s por consulta
HEADERS = {"User-Agent": "analise-credito-entes-publicos/1.0"}


def resultado(fonte: str, dados=None, erro: str | None = None) -> dict:
    return {"fonte": fonte, "ok": erro is None, "dados": dados, "erro": erro}


RETENTAVEIS = (429, 500, 502, 503, 504)


async def get_json(
    client: httpx.AsyncClient,
    url: str,
    *,
    params=None,
    headers=None,
    tentativas=5,
    espera_base=1,
):
    """GET com retry exponencial; devolve o JSON ou levanta a última exceção.

    O PNCP e o data lake do Tesouro devolvem 502/504 com alguma frequência sob
    carga, então vale esperar mais em vez de desistir cedo. Quando o serviço
    está congestionado, esperar pouco só repete a falha - daí `espera_base`
    permitir dar mais folga em fontes sabidamente instáveis.
    """
    ultima_exc = None
    for tentativa in range(tentativas):
        try:
            resp = await client.get(url, params=params, headers=headers)
            resp.raise_for_status()
            return resp.json()
        except (httpx.HTTPStatusError, httpx.TransportError) as exc:
            ultima_exc = exc
            if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code not in RETENTAVEIS:
                raise
            if tentativa < tentativas - 1:
                await asyncio.sleep(espera_base * (2**tentativa))
    raise ultima_exc


def novo_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=TIMEOUT, headers=HEADERS, follow_redirects=True)
