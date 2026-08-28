"""Infraestrutura comum dos coletores: HTTP com retry e resultado padronizado.

Cada coletor devolve um dict {"fonte", "ok", "dados", "erro"} - uma fonte fora
do ar nunca derruba a análise inteira, apenas vira seção "indisponível".
"""
import asyncio

import httpx

# 45s por request é folgado para o Tesouro/PNCP sem deixar uma request pendurada
# consumir sozinha o teto do coletor. O download grande da CAPAG usa timeout
# próprio (ver capag.py).
TIMEOUT = httpx.Timeout(45.0, connect=10.0)
HEADERS = {"User-Agent": "analise-credito-entes-publicos/1.0"}


def resultado(fonte: str, dados=None, erro: str | None = None) -> dict:
    return {"fonte": fonte, "ok": erro is None, "dados": dados, "erro": erro}


# Teto de tempo por coletor. Uma fonte lenta ou pendurada (SICONFI instável, o
# XLSX da CAPAG, o PNCP em 504) não pode travar a análise inteira: passado o
# teto, ela vira "indisponível" e o resto segue. Generoso o bastante para não
# cortar coletas legítimas (CAPAG e PNCP levam ~100s em dia ruim).
TETO_COLETOR = 150.0


async def com_teto(fonte: str, coro, teto: float = TETO_COLETOR) -> dict:
    """Executa um coletor com limite de tempo; nunca deixa a análise pendurada."""
    try:
        return await asyncio.wait_for(coro, timeout=teto)
    except asyncio.TimeoutError:
        return resultado(
            fonte, erro=f"tempo esgotado ({teto:.0f}s) - fonte lenta ou fora do ar no momento"
        )
    except Exception as exc:  # noqa: BLE001 - qualquer falha vira fonte indisponível
        return resultado(fonte, erro=f"{type(exc).__name__}: {exc}")


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
