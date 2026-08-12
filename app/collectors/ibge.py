"""IBGE: lista de municípios (autocomplete), população estimada e PIB municipal."""
import unicodedata

import httpx

from .base import get_json, resultado

LOCALIDADES = "https://servicodados.ibge.gov.br/api/v1/localidades/municipios"
AGREGADOS = "https://servicodados.ibge.gov.br/api/v3/agregados"

# cache em memória da lista de municípios (estável, ~5570 itens)
_municipios: list[dict] | None = None


def _sem_acento(texto: str) -> str:
    return unicodedata.normalize("NFKD", texto).encode("ascii", "ignore").decode().lower()


async def listar_municipios(client: httpx.AsyncClient) -> list[dict]:
    global _municipios
    if _municipios is None:
        bruto = await get_json(client, LOCALIDADES, params={"view": "nivelado"})
        _municipios = [
            {
                "cod_ibge": str(m["municipio-id"]),
                "nome": m["municipio-nome"],
                "uf": m["UF-sigla"],
            }
            for m in bruto
        ]
    return _municipios


async def buscar_municipios(client: httpx.AsyncClient, consulta: str, limite: int = 12) -> list[dict]:
    consulta_norm = _sem_acento(consulta)
    municipios = await listar_municipios(client)
    encontrados = [m for m in municipios if consulta_norm in _sem_acento(m["nome"])]
    # prioriza começo do nome
    encontrados.sort(key=lambda m: (not _sem_acento(m["nome"]).startswith(consulta_norm), m["nome"]))
    return encontrados[:limite]


def _valor_agregado(payload) -> tuple[float | None, str | None]:
    """Extrai (valor, período) da estrutura de resposta da API de agregados."""
    try:
        serie = payload[0]["resultados"][0]["series"][0]["serie"]
        periodo, valor = sorted(serie.items())[-1]
        if valor in ("...", "-", None):
            return None, periodo
        return float(valor), periodo
    except (KeyError, IndexError, TypeError, ValueError):
        return None, None


async def coletar_contexto(client: httpx.AsyncClient, cod_ibge: str) -> dict:
    """População estimada (agregado 6579) e PIB municipal (agregado 5938, var. 37)."""
    try:
        pop_raw = await get_json(
            client,
            f"{AGREGADOS}/6579/periodos/-1/variaveis/9324",
            params={"localidades": f"N6[{cod_ibge}]"},
        )
        pib_raw = await get_json(
            client,
            f"{AGREGADOS}/5938/periodos/-1/variaveis/37",
            params={"localidades": f"N6[{cod_ibge}]"},
        )
        populacao, ano_pop = _valor_agregado(pop_raw)
        pib_mil, ano_pib = _valor_agregado(pib_raw)  # em R$ mil
        pib_per_capita = None
        if populacao and pib_mil:
            pib_per_capita = round(pib_mil * 1000 / populacao, 2)
        return resultado(
            "ibge",
            {
                "populacao": populacao,
                "ano_populacao": ano_pop,
                "pib_mil_reais": pib_mil,
                "ano_pib": ano_pib,
                "pib_per_capita": pib_per_capita,
            },
        )
    except Exception as exc:  # noqa: BLE001 - coletor tolerante a falha
        return resultado("ibge", erro=f"{type(exc).__name__}: {exc}")
