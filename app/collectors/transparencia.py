"""Portal da Transparência: sanções, transferências federais recebidas e convênios.

Requer chave gratuita (header `chave-api-dados`) - cadastro em
https://portaldatransparencia.gov.br/api-de-dados/cadastrar-email
Sem a chave, os coletores devolvem status "pendente" e a análise segue normalmente.
"""
from datetime import date

import httpx

from ..config import TRANSPARENCIA_API_KEY
from .base import get_json, resultado

BASE = "https://api.portaldatransparencia.gov.br/api-de-dados"
MAX_PAGINAS = 15


def _sem_chave(fonte: str) -> dict:
    return resultado(
        fonte,
        erro="chave da API não configurada (TRANSPARENCIA_API_KEY no .env) - "
        "cadastro gratuito em portaldatransparencia.gov.br/api-de-dados/cadastrar-email",
    )


async def _paginar(
    client: httpx.AsyncClient, endpoint: str, params: dict, max_paginas: int = MAX_PAGINAS
) -> list[dict]:
    """Percorre as páginas do endpoint até esgotar ou atingir o teto de segurança."""
    headers = {"chave-api-dados": TRANSPARENCIA_API_KEY}
    itens: list[dict] = []
    for pagina in range(1, max_paginas + 1):
        lote = await get_json(
            client, f"{BASE}/{endpoint}", params={**params, "pagina": pagina}, headers=headers
        )
        if not isinstance(lote, list) or not lote:
            break
        itens.extend(lote)
        if len(lote) < 15:  # página incompleta => última
            break
    return itens


def _so_digitos(valor) -> str:
    return "".join(ch for ch in str(valor or "") if ch.isdigit())


def _identificadores(item: dict) -> set[str]:
    """Todos os CNPJ/CPF que aparecem no registro de sanção, só dígitos."""
    ids = set()
    # "pessoa"/"sancionado" no CEIS e CNEP; "pessoaJuridica" no CEPIM
    for chave in ("pessoa", "sancionado", "pessoaJuridica"):
        bloco = item.get(chave) or {}
        if isinstance(bloco, dict):
            for campo in (
                "cnpj", "cnpjFormatado", "cpfFormatado", "numeroInscricaoSocial", "codigoFormatado",
            ):
                doc = _so_digitos(bloco.get(campo))
                if doc:
                    ids.add(doc)
    return ids


async def _sancoes(client: httpx.AsyncClient, endpoint: str, cnpj: str) -> list[dict]:
    """Consulta um cadastro de sanções filtrando pelo CNPJ.

    ATENÇÃO: o nome do parâmetro difere entre os cadastros - CEIS e CNEP usam
    `codigoSancionado`, CEPIM usa `cnpjSancionado`. Um nome errado não gera erro:
    a API simplesmente ignora o filtro e devolve a primeira página de TODAS as
    sanções do país. Por isso conferimos o CNPJ de cada registro devolvido.
    """
    param = "cnpjSancionado" if endpoint == "cepim" else "codigoSancionado"
    itens = await _paginar(client, endpoint, {param: cnpj})
    return [i for i in itens if cnpj in _identificadores(i)]


async def coletar_transparencia(client: httpx.AsyncClient, cnpj: str | None) -> dict:
    if not TRANSPARENCIA_API_KEY:
        return _sem_chave("transparencia")
    if not cnpj:
        return resultado("transparencia", erro="CNPJ não informado - consulta de sanções não realizada")
    cnpj = "".join(filter(str.isdigit, cnpj))
    try:
        ceis = await _sancoes(client, "ceis", cnpj)
        cnep = await _sancoes(client, "cnep", cnpj)
        cepim = await _sancoes(client, "cepim", cnpj)

        def texto(valor) -> str:
            if isinstance(valor, dict):
                return str(valor.get("descricao") or valor.get("nome") or "")
            return str(valor or "")

        def resumir(lista: list[dict]) -> list[dict]:
            return [
                {
                    "tipo": texto(i.get("tipoSancao")),
                    "orgao": texto(i.get("orgaoSancionador")),
                    "inicio": i.get("dataInicioSancao"),
                    "fim": i.get("dataFimSancao"),
                    "valor_multa": i.get("valorMulta"),
                    "motivo": texto(i.get("motivo")),
                    "processo": i.get("numeroProcesso"),
                }
                for i in lista[:10]
            ]

        return resultado(
            "transparencia",
            {
                "ceis_total": len(ceis),
                "cnep_total": len(cnep),
                "cepim_total": len(cepim),
                "ceis": resumir(ceis),
                "cnep": resumir(cnep),
                "cepim": resumir(cepim),
            },
        )
    except Exception as exc:  # noqa: BLE001
        return resultado("transparencia", erro=f"{type(exc).__name__}: {exc}")


async def coletar_transferencias(client: httpx.AsyncClient, cnpj: str | None) -> dict:
    """Histórico mensal de recursos federais recebidos pelo órgão (24 meses).

    É o fluxo de caixa real que entra do governo federal - a parte mais
    previsível da receita e o melhor indicador de regularidade do repasse.
    """
    if not TRANSPARENCIA_API_KEY:
        return _sem_chave("transferencias")
    if not cnpj:
        return resultado("transferencias", erro="CNPJ não informado - histórico de repasses não consultado")
    cnpj = "".join(filter(str.isdigit, cnpj))
    try:
        hoje = date.today()

        def recuar(meses: int) -> tuple[int, int]:
            total = (hoje.year * 12 + hoje.month - 1) - meses
            return total // 12, total % 12 + 1

        # a API rejeita períodos acima de 12 meses, então consultamos em duas janelas
        itens: list[dict] = []
        for inicio, fim in ((23, 12), (11, 0)):
            ai, mi = recuar(inicio)
            af, mf = recuar(fim)
            itens.extend(
                await _paginar(
                    client,
                    "despesas/recursos-recebidos",
                    {
                        "mesAnoInicio": f"{mi:02d}/{ai}",
                        "mesAnoFim": f"{mf:02d}/{af}",
                        "codigoFavorecido": cnpj,
                    },
                )
            )
        if not itens:
            return resultado(
                "transferencias",
                erro="nenhum repasse federal encontrado para este CNPJ nos últimos 24 meses",
            )

        por_mes: dict[str, float] = {}
        por_orgao: dict[str, float] = {}
        for i in itens:
            ano_mes = str(i.get("anoMes") or "")
            valor = float(i.get("valor") or 0)
            if len(ano_mes) == 6:
                rotulo = f"{ano_mes[4:]}/{ano_mes[:4]}"
                por_mes[rotulo] = round(por_mes.get(rotulo, 0) + valor, 2)
            orgao = i.get("nomeOrgaoSuperior") or i.get("nomeOrgao") or "não identificado"
            por_orgao[orgao] = round(por_orgao.get(orgao, 0) + valor, 2)

        serie = [
            {"mes": m, "valor": v}
            for m, v in sorted(por_mes.items(), key=lambda kv: (kv[0][3:], kv[0][:2]))
        ]
        valores = [p["valor"] for p in serie]
        total = round(sum(valores), 2)
        meses_sem_repasse = sum(1 for v in valores if v <= 0)

        return resultado(
            "transferencias",
            {
                "periodo": "últimos 24 meses",
                "total_recebido": total,
                "media_mensal": round(total / len(serie), 2) if serie else None,
                "meses_com_repasse": len(serie),
                "meses_sem_repasse": meses_sem_repasse,
                "serie_mensal": serie,
                "principais_orgaos": sorted(
                    ({"orgao": o, "valor": v} for o, v in por_orgao.items()),
                    key=lambda x: x["valor"],
                    reverse=True,
                )[:8],
            },
        )
    except Exception as exc:  # noqa: BLE001
        return resultado("transferencias", erro=f"{type(exc).__name__}: {exc}")


# Códigos de situação do endpoint de convênios (confirmados contra a API).
SITUACAO_INADIMPLENTE = "2"
SITUACAO_INADIMPLENCIA_SUSPENSA = "6"


def _texto(valor) -> str:
    if isinstance(valor, dict):
        return str(valor.get("descricao") or valor.get("nome") or valor.get("objeto") or "")
    return str(valor or "")


def _resumir_convenio(c: dict) -> dict:
    return {
        "objeto": _texto((c.get("dimConvenio") or {}).get("objeto"))[:200],
        "situacao": _texto(c.get("situacao")),
        "valor": c.get("valor"),
        "valor_liberado": c.get("valorLiberado"),
        "inicio_vigencia": c.get("dataInicioVigencia"),
        "fim_vigencia": c.get("dataFinalVigencia"),
    }


def _anos_desde(data_iso: str | None) -> int | None:
    """Quantos anos se passaram desde a data (formato AAAA-MM-DD)."""
    if not data_iso or len(str(data_iso)) < 4:
        return None
    try:
        return date.today().year - int(str(data_iso)[:4])
    except ValueError:
        return None


async def coletar_convenios(client: httpx.AsyncClient, cod_ibge: str, cnpj: str | None = None) -> dict:
    """Convênios do município com a União, focado na adimplência.

    A `situacao` é sinal direto de credibilidade administrativa: convênio
    inadimplente significa que o ente não prestou contas de recurso federal,
    o que trava novos repasses e indica desorganização na gestão.

    Dois cuidados que a API exige:

    1. O filtro de situação é aplicado no servidor. Paginar tudo e filtrar aqui
       não funciona: um município grande tem milhares de convênios (o Rio de
       Janeiro passa de 1.800) e qualquer teto de paginação trunca a contagem,
       devolvendo um número que parece um fato mas é artefato do teto.

    2. `codigoIBGE` significa "convenente **localizado** neste município", não
       "o convenente **é** este município". Sem filtrar pelo CNPJ da prefeitura,
       entram convênios de ONGs, fundações e empresas sediadas na cidade - no
       Rio de Janeiro, 149 convênios inadimplentes de terceiros seriam
       atribuídos à prefeitura.
    """
    if not TRANSPARENCIA_API_KEY:
        return _sem_chave("convenios")
    if not cnpj:
        return resultado(
            "convenios", erro="CNPJ da prefeitura não resolvido - convênios não consultados"
        )
    cnpj = "".join(filter(str.isdigit, cnpj))

    def e_da_prefeitura(c: dict) -> bool:
        doc = (c.get("convenente") or {}).get("cnpjFormatado") or ""
        return "".join(filter(str.isdigit, doc)) == cnpj

    try:
        brutos_inad = await _paginar(
            client, "convenios", {"codigoIBGE": cod_ibge, "situacao": SITUACAO_INADIMPLENTE}
        )
        brutos_susp = await _paginar(
            client,
            "convenios",
            {"codigoIBGE": cod_ibge, "situacao": SITUACAO_INADIMPLENCIA_SUSPENSA},
        )
        brutos_amostra = await _paginar(
            client, "convenios", {"codigoIBGE": cod_ibge}, max_paginas=4
        )

        inadimplentes_raw = [c for c in brutos_inad if e_da_prefeitura(c)]
        suspensas_raw = [c for c in brutos_susp if e_da_prefeitura(c)]
        amostra_raw = [c for c in brutos_amostra if e_da_prefeitura(c)]
        descartados = (len(brutos_inad) - len(inadimplentes_raw)) + (
            len(brutos_susp) - len(suspensas_raw)
        )

        if not inadimplentes_raw and not suspensas_raw and not amostra_raw:
            return resultado(
                "convenios",
                {
                    "inadimplentes_total": 0,
                    "inadimplentes_recentes": 0,
                    "suspensas_total": 0,
                    "observacao": "nenhum convênio federal registrado para a prefeitura",
                },
            )

        inadimplentes = [_resumir_convenio(c) for c in inadimplentes_raw]
        for reg in inadimplentes:
            reg["anos_desde_vigencia"] = _anos_desde(reg["fim_vigencia"])
        suspensas = [_resumir_convenio(c) for c in suspensas_raw]

        # inadimplência recente pesa diferente de pendência histórica não resolvida
        recentes_inad = [
            r for r in inadimplentes
            if r["anos_desde_vigencia"] is not None and r["anos_desde_vigencia"] <= 5
        ]

        return resultado(
            "convenios",
            {
                "inadimplentes_total": len(inadimplentes),
                "inadimplentes_recentes": len(recentes_inad),
                "inadimplentes_truncado": len(brutos_inad) >= MAX_PAGINAS * 15,
                "descartados_outras_entidades": descartados,
                "valor_inadimplente": round(sum(float(c.get("valor") or 0) for c in inadimplentes_raw), 2),
                "suspensas_total": len(suspensas),
                "inadimplentes": sorted(
                    inadimplentes, key=lambda r: str(r["fim_vigencia"] or ""), reverse=True
                )[:15],
                "inadimplencias_suspensas": suspensas[:10],
                "recentes": sorted(
                    (_resumir_convenio(c) for c in amostra_raw),
                    key=lambda r: str(r["inicio_vigencia"] or ""),
                    reverse=True,
                )[:10],
            },
        )
    except Exception as exc:  # noqa: BLE001
        return resultado("convenios", erro=f"{type(exc).__name__}: {exc}")
