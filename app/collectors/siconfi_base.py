"""Busca de RGF/RREO no SICONFI, tolerante às duas modalidades de entrega.

Municípios com menos de 50 mil habitantes podem entregar os relatórios na forma
**Simplificada**: RGF semestral (`RGF Simplificado`, periodicidade S) e RREO
`RREO Simplificado`. A estrutura de contas é idêntica à completa - mesmos
`cod_conta` e anexos -, só mudam o `co_tipo_demonstrativo` e, no RGF, a
periodicidade. Quem busca só a modalidade completa não acha nada nesses entes,
o que antes era confundido com "não entregou". Estas funções tentam a completa
primeiro (a mais comum) e caem para a simplificada.
"""
import httpx

from .base import get_json

BASE = "https://apidatalake.tesouro.gov.br/ords/siconfi/tt"

# (co_tipo_demonstrativo, periodicidade, períodos do mais recente ao 1º, rótulo)
RGF_MODALIDADES = [
    ("RGF", "Q", (3, 2, 1), "quadrimestre"),
    ("RGF Simplificado", "S", (2, 1), "semestre"),
]
RREO_MODALIDADES = ("RREO", "RREO Simplificado")


async def buscar_rgf_ano(
    client: httpx.AsyncClient, cod_ibge: str, ano: int, *, apenas_fechamento: bool = False
) -> tuple[list[dict], str | None, str | None]:
    """RGF de um exercício, na modalidade em que o ente entrega.

    Devolve (itens, referência, modalidade). `apenas_fechamento` busca só o
    último período de cada modalidade - usado para o Anexo 05, que só existe no
    fechamento (3º quadrimestre ou 2º semestre).
    """
    for tipo, periodicidade, periodos, rotulo in RGF_MODALIDADES:
        candidatos = periodos[:1] if apenas_fechamento else periodos
        for periodo in candidatos:
            payload = await get_json(
                client,
                f"{BASE}/rgf",
                params={
                    "an_exercicio": ano,
                    "in_periodicidade": periodicidade,
                    "nr_periodo": periodo,
                    "co_tipo_demonstrativo": tipo,
                    "co_esfera": "M",
                    "co_poder": "E",
                    "id_ente": cod_ibge,
                },
            )
            itens = payload.get("items", [])
            if itens:
                simplificado = "Simplificado" in tipo
                return itens, f"{periodo}º {rotulo}/{ano}", ("simplificado" if simplificado else "completo")
    return [], None, None


async def buscar_rreo(
    client: httpx.AsyncClient, cod_ibge: str, ano: int, periodo: int, anexo: str
) -> list[dict]:
    """RREO de um bimestre, tentando a modalidade completa e depois a simplificada.

    A periodicidade do RREO é bimestral nas duas modalidades - muda só o
    `co_tipo_demonstrativo`.
    """
    for tipo in RREO_MODALIDADES:
        payload = await get_json(
            client,
            f"{BASE}/rreo",
            params={
                "an_exercicio": ano,
                "nr_periodo": periodo,
                "co_tipo_demonstrativo": tipo,
                "no_anexo": anexo,
                "id_ente": cod_ibge,
            },
        )
        itens = payload.get("items", [])
        if itens:
            return itens
    return []
