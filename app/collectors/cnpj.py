"""BrasilAPI: dados cadastrais do CNPJ do órgão (situação, natureza jurídica)."""
import httpx

from .base import get_json, resultado

BRASILAPI = "https://brasilapi.com.br/api/cnpj/v1"


async def coletar_cnpj(client: httpx.AsyncClient, cnpj: str | None) -> dict:
    if not cnpj:
        return resultado("cnpj", erro="CNPJ não informado")
    cnpj = "".join(filter(str.isdigit, cnpj))
    try:
        d = await get_json(client, f"{BRASILAPI}/{cnpj}")
        return resultado(
            "cnpj",
            {
                "razao_social": d.get("razao_social"),
                "situacao_cadastral": d.get("descricao_situacao_cadastral"),
                "natureza_juridica": d.get("natureza_juridica"),
                "data_inicio_atividade": d.get("data_inicio_atividade"),
                "municipio": d.get("municipio"),
                "uf": d.get("uf"),
            },
        )
    except Exception as exc:  # noqa: BLE001
        return resultado("cnpj", erro=f"{type(exc).__name__}: {exc}")
