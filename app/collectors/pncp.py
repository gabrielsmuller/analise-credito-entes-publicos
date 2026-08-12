"""PNCP (Portal Nacional de Contratações Públicas): contratos do órgão.

API pública de consulta: https://pncp.gov.br/api/consulta/v1/contratos
Busca os contratos publicados nos últimos 24 meses (duas janelas de 1 ano) e
destaca os relacionados a climatização/ar-condicionado.
"""
import re
from datetime import date, timedelta

import httpx

from .base import get_json, resultado

CONTRATOS = "https://pncp.gov.br/api/consulta/v1/contratos"

# Termos com fronteira de palavra. A busca por substring simples produzia falsos
# positivos: uma permissão de uso para exploração de chope entrou como
# "climatização" porque o texto longo do contrato mencionava refrigeração.
TERMOS_CLIMATIZACAO = {
    "ar-condicionado": r"ar[\s-]?condicionad[oa]s?",
    "condicionador de ar": r"condicionador(?:es)? de ar",
    "climatização": r"climatiza\w*",
    "refrigeração": r"refrigera(?:ção|cao|dor\w*|ções|coes)",
    "split": r"\bsplits?\b",
    "VRF/VRV": r"\bvr[fv]\b",
    "chiller": r"\bchillers?\b",
    "fancoil": r"\bfan[\s-]?coils?\b",
}

# Tipos de contrato que não são aquisição de equipamento: mesmo citando
# refrigeração no texto, não representam compra de climatização.
EXCLUSOES = re.compile(
    r"permiss[ãa]o (remunerada )?de uso|concess[ãa]o (de uso|de servi[çc]o)|cess[ãa]o de uso"
    r"|loca[çc][ãa]o de (im[óo]vel|espa[çc]o|[áa]rea)|explora[çc][ãa]o comercial",
    re.IGNORECASE,
)


def _classificar_climatizacao(objeto: str) -> str | None:
    """Devolve o termo que classificou o contrato, ou None.

    Expor o termo torna a classificação auditável: o analista vê por que cada
    contrato entrou na lista e identifica na hora um enquadramento indevido.
    """
    if not objeto or EXCLUSOES.search(objeto):
        return None
    for rotulo, padrao in TERMOS_CLIMATIZACAO.items():
        if re.search(padrao, objeto, re.IGNORECASE):
            return rotulo
    return None


MAX_PAGINAS = 4  # 4 páginas de 500 = amostra de até 2000 contratos por janela


async def _contratos_periodo(
    client: httpx.AsyncClient, cnpj: str, ini: date, fim: date
) -> tuple[list[dict], int, list[str]]:
    """Devolve (contratos obtidos, total de registros no período, falhas).

    O PNCP é instável: a mesma consulta responde em 2s ou estoura 504 em 70s,
    sem relação com o tamanho da página. Por isso uma página que falha não
    aborta a coleta - ficamos com o que já veio e registramos a falha, já que
    dado parcial de concorrência vale muito mais que nenhum dado.
    """
    contratos: list[dict] = []
    total_registros = 0
    falhas: list[str] = []
    pagina = 1
    while pagina <= MAX_PAGINAS:
        try:
            payload = await get_json(
                client,
                CONTRATOS,
                params={
                    "dataInicial": ini.strftime("%Y%m%d"),
                    "dataFinal": fim.strftime("%Y%m%d"),
                    "cnpjOrgao": cnpj,
                    "pagina": pagina,
                    "tamanhoPagina": 500,
                },
                tentativas=6,
                espera_base=3,
            )
        except Exception as exc:  # noqa: BLE001 - falha de página não derruba a coleta
            falhas.append(
                f"{ini:%m/%Y}–{fim:%m/%Y} página {pagina}: {type(exc).__name__}"
            )
            break
        contratos.extend(payload.get("data") or [])
        total_registros = payload.get("totalRegistros") or len(contratos)
        if pagina >= (payload.get("totalPaginas") or 1):
            break
        pagina += 1
    return contratos, total_registros, falhas


async def coletar_pncp(client: httpx.AsyncClient, cnpj: str | None) -> dict:
    if not cnpj:
        return resultado("pncp", erro="CNPJ do órgão não informado - histórico de contratos não consultado")
    cnpj = "".join(filter(str.isdigit, cnpj))
    try:
        hoje = date.today()
        contratos: list[dict] = []
        total_registros = 0
        falhas: list[str] = []
        for janela in range(2):  # 2 janelas de ~1 ano (limite da API por consulta)
            fim = hoje - timedelta(days=365 * janela)
            ini = fim - timedelta(days=364)
            parte, total, erros = await _contratos_periodo(client, cnpj, ini, fim)
            contratos.extend(parte)
            total_registros += total
            falhas.extend(erros)

        if not contratos:
            return resultado(
                "pncp",
                erro="PNCP indisponível - "
                + ("; ".join(falhas) if falhas else "nenhum contrato retornado"),
            )

        def resumo(c: dict) -> dict:
            return {
                "objeto": (c.get("objetoContrato") or "")[:300],
                "valor_global": c.get("valorGlobal"),
                "fornecedor": c.get("nomeRazaoSocialFornecedor"),
                "assinatura": c.get("dataAssinatura"),
                "vigencia_fim": c.get("dataVigenciaFim"),
            }

        # a API pode repetir o mesmo contrato entre janelas de consulta
        vistos = set()
        unicos = []
        for c in contratos:
            chave = (
                c.get("orgaoEntidade", {}).get("cnpj"),
                c.get("anoContrato"),
                c.get("sequencialContrato"),
            )
            if chave in vistos:
                continue
            vistos.add(chave)
            unicos.append(c)
        duplicatas = len(contratos) - len(unicos)
        contratos = unicos

        similares = []
        for c in contratos:
            termo = _classificar_climatizacao(c.get("objetoContrato") or "")
            if termo:
                similares.append({**resumo(c), "termo_identificado": termo})

        # agrupa por fornecedor: 14 linhas idênticas de ata de preço não informam
        # nada além do que "6 contratos, R$ 21 mil" já informa
        por_fornecedor: dict[str, dict] = {}
        for s in similares:
            nome = s["fornecedor"] or "não informado"
            # o mesmo fornecedor aparece com grafias diferentes ("BELMICRO" e
            # "BEL MICRO"); agrupa ignorando espaços e pontuação
            chave_forn = re.sub(r"[^A-Z0-9]", "", nome.upper())
            entrada = por_fornecedor.setdefault(
                chave_forn, {"fornecedor": nome, "contratos": 0, "valor_total": 0.0, "ultimo": None}
            )
            entrada["contratos"] += 1
            entrada["valor_total"] = round(entrada["valor_total"] + (s["valor_global"] or 0), 2)
            if not entrada["ultimo"] or (s["assinatura"] or "") > entrada["ultimo"]:
                entrada["ultimo"] = s["assinatura"]

        valores = [c.get("valorGlobal") or 0 for c in contratos]
        maiores = sorted(contratos, key=lambda c: c.get("valorGlobal") or 0, reverse=True)[:10]

        amostrado = len(contratos) < total_registros
        return resultado(
            "pncp",
            {
                "periodo": "últimos 24 meses",
                "total_contratos": total_registros,
                "contratos_analisados": len(contratos),
                "duplicatas_removidas": duplicatas,
                "amostra_parcial": amostrado,
                "coleta_incompleta": bool(falhas),
                "falhas_coleta": falhas,
                "valor_total_contratado": round(sum(valores), 2),
                "observacao_valor": "valor calculado sobre a amostra analisada" if amostrado else None,
                "climatizacao_total_contratos": len(similares),
                "climatizacao_valor_total": round(sum(s["valor_global"] or 0 for s in similares), 2),
                "climatizacao_por_fornecedor": sorted(
                    por_fornecedor.values(), key=lambda f: f["valor_total"], reverse=True
                ),
                "contratos_climatizacao": sorted(
                    similares, key=lambda s: s["valor_global"] or 0, reverse=True
                )[:15],
                "maiores_contratos": [resumo(c) for c in maiores],
            },
        )
    except Exception as exc:  # noqa: BLE001
        return resultado("pncp", erro=f"{type(exc).__name__}: {exc}")
