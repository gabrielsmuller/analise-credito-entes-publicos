"""Comportamento de pagamento do ente - o que mais importa para um fornecedor.

Responde à pergunta que a CAPAG não responde: *este ente paga suas contas?*

Fontes (SICONFI):
- RGF Anexo 05 - Disponibilidade de Caixa e Restos a Pagar. Publicado apenas no
  3º quadrimestre (fechamento do exercício), então a série é anual.
- RREO Anexo 07 - Restos a pagar inscritos, pagos, cancelados e saldo.
- RREO Anexo 01 - Despesas liquidadas x efetivamente pagas.
"""
import asyncio
import re
from datetime import date

import httpx

from .base import resultado
from .siconfi_base import buscar_rgf_ano, buscar_rreo

# Agregados do Anexo 05 por origem de recurso. "Não vinculados" é o dinheiro
# discricionário - o que sobra para pagar fornecedor comum, sem carimbo.
NAO_VINCULADOS = r"total dos recursos n[ãa]o vinculados"
TOTAL_GERAL = r"^total \(iv\)"


def _norm(texto) -> str:
    return re.sub(r"\s+", " ", str(texto or "")).strip().lower()


def _valor(itens: list[dict], cod_conta: str, padrao_conta: str) -> float | None:
    alvo = cod_conta.lower()
    for item in itens:
        if _norm(item.get("cod_conta")) == alvo and re.search(padrao_conta, _norm(item.get("conta"))):
            try:
                return float(item["valor"])
            except (TypeError, ValueError):
                continue
    return None


def _soma(*valores) -> float | None:
    presentes = [v for v in valores if v is not None]
    return round(sum(presentes), 2) if presentes else None


async def _rgf_anexo05(client: httpx.AsyncClient, cod_ibge: str, ano: int) -> dict | None:
    """Disponibilidade de caixa e obrigações do fechamento de um exercício.

    O Anexo 05 só existe no fechamento (3º quadrimestre no completo, 2º semestre
    no simplificado), então buscamos apenas o último período de cada modalidade.
    """
    brutos, _, _ = await buscar_rgf_ano(client, cod_ibge, ano, apenas_fechamento=True)
    itens = [i for i in brutos if _norm(i.get("anexo")) == "rgf-anexo 05"]
    if not itens:
        return None

    def bloco(padrao: str) -> dict:
        rp_liquidados = _soma(
            _valor(itens, "RestosAPagarLiquidadosENaoPagosDoExercicio", padrao),
            _valor(itens, "RestosAPagarLiquidadosENaoPagosDeExerciciosAnteriores", padrao),
        )
        demais = _valor(itens, "DemaisObrigacoesFinanceiras", padrao)
        rp_nao_liquidados = _soma(
            _valor(itens, "RestosAPagarEmpenhadosENaoLiquidadosDoExercicio", padrao),
            _valor(itens, "RestosAPagarEmpenhadosENaoLiquidadosDeExerciciosAnteriores", padrao),
        )
        bruta = _valor(itens, "DisponibilidadeDeCaixaBruta", padrao)
        obrigacoes = _soma(rp_liquidados, demais, rp_nao_liquidados)
        cobertura = round(bruta / obrigacoes, 3) if bruta is not None and obrigacoes else None
        return {
            "disponibilidade_bruta": bruta,
            "restos_a_pagar_processados": rp_liquidados,  # liquidados e não pagos
            "restos_a_pagar_nao_processados": rp_nao_liquidados,  # empenhados, não liquidados
            "demais_obrigacoes_financeiras": demais,
            "obrigacoes_totais": obrigacoes,
            "disponibilidade_liquida": _valor(itens, "DisponibilidadeDeCaixaLiquida", padrao),
            "disponibilidade_liquida_apos_rp": _valor(itens, "DisponibilidadeDeCaixaLiquidaAposRP", padrao),
            "indice_cobertura": cobertura,
        }

    return {"ano": ano, "nao_vinculados": bloco(NAO_VINCULADOS), "total": bloco(TOTAL_GERAL)}


async def _rreo_anexo07(client: httpx.AsyncClient, cod_ibge: str, ano: int, periodos: tuple[int, ...]):
    """Restos a pagar do exercício: inscritos, pagos, cancelados e saldo."""
    for periodo in periodos:
        itens = await buscar_rreo(client, cod_ibge, ano, periodo, "RREO-Anexo 07")
        if not itens:
            continue

        # a linha "TOTAL (III) = (I + II)" consolida todos os poderes/órgãos;
        # sem esse filtro pegaríamos linhas parciais de um órgão isolado
        def val(cod: str) -> float | None:
            return _valor(itens, cod, r"^total \(iii\)")

        pref_proc = "RestosAPagarProcessadosENaoProcessadosLiquidados"
        inscritos_proc = _soma(
            val(f"{pref_proc}InscritosEmExerciciosAnteriores"),
            val(f"{pref_proc}InscritosEmExercicioAnterior"),
        )
        pagos_proc = val(f"{pref_proc}Pagos")
        cancelados_proc = val(f"{pref_proc}Cancelados")

        inscritos_nproc = _soma(
            val("RestosAPagarNaoProcessadosInscritosEmExerciciosAnteriores"),
            val("RestosAPagarNaoProcessadosInscritosEmExercicioAnterior"),
        )
        pagos_nproc = val("RestosAPagarNaoProcessadosPagos")
        cancelados_nproc = val("RestosAPagarNaoProcessadosCancelados")

        def pct(parte, total):
            return round(parte / total * 100, 1) if parte is not None and total else None

        return {
            "ano": ano,
            "referencia": f"{periodo}º bimestre/{ano}",
            # Restos a pagar são quitados ao longo do exercício: um corte no 3º
            # bimestre mostra naturalmente menos pago que um fechamento. Comparar
            # tendência entre um ano em curso e anos fechados é comparar grandezas
            # diferentes, então marcamos quais estão fechados.
            "exercicio_fechado": periodo == 6,
            "processados": {
                "inscritos": inscritos_proc,
                "pagos": pagos_proc,
                "cancelados": cancelados_proc,
                "saldo": val(f"{pref_proc}APagar"),
                "pct_pago": pct(pagos_proc, inscritos_proc),
                "pct_cancelado": pct(cancelados_proc, inscritos_proc),
            },
            "nao_processados": {
                "inscritos": inscritos_nproc,
                "pagos": pagos_nproc,
                "cancelados": cancelados_nproc,
                "saldo": val("RestosAPagarNaoProcessadosAPagar"),
                "pct_pago": pct(pagos_nproc, inscritos_nproc),
                "pct_cancelado": pct(cancelados_nproc, inscritos_nproc),
            },
            "saldo_total": val("SaldoTotal"),
        }
    return None


async def _rreo_liquidadas_x_pagas(client: httpx.AsyncClient, cod_ibge: str):
    """Quanto do que já foi liquidado (dívida reconhecida) foi de fato pago."""
    hoje = date.today()
    for ano in (hoje.year, hoje.year - 1):
        for periodo in (6, 5, 4, 3, 2, 1):
            itens = await buscar_rreo(client, cod_ibge, ano, periodo, "RREO-Anexo 01")
            if not itens:
                continue

            def col(padrao_coluna: str) -> float | None:
                for i in itens:
                    if _norm(i.get("cod_conta")) == "totaldespesas" and re.search(
                        padrao_coluna, _norm(i.get("coluna"))
                    ):
                        try:
                            return float(i["valor"])
                        except (TypeError, ValueError):
                            continue
                return None

            liquidadas = col(r"despesas liquidadas at[ée] o bimestre")
            pagas = col(r"despesas pagas at[ée] o bimestre")
            if liquidadas is None and pagas is None:
                continue
            return {
                "referencia": f"{periodo}º bimestre/{ano}",
                "empenhadas": col(r"despesas empenhadas at[ée] o bimestre"),
                "liquidadas": liquidadas,
                "pagas": pagas,
                "a_pagar": round(liquidadas - pagas, 2) if liquidadas and pagas else None,
                "pct_pago": round(pagas / liquidadas * 100, 1) if liquidadas and pagas else None,
            }
    return None


async def coletar_pagamentos(client: httpx.AsyncClient, cod_ibge: str) -> dict:
    hoje = date.today()
    # o Anexo 05 sai no fechamento do exercício; o ano corrente ainda não tem
    anos_fechados = [hoje.year - 1, hoje.year - 2, hoje.year - 3]
    try:
        tarefas = [_rgf_anexo05(client, cod_ibge, ano) for ano in anos_fechados]
        tarefas.append(_rreo_anexo07(client, cod_ibge, hoje.year, (6, 5, 4, 3, 2, 1)))
        tarefas.append(_rreo_anexo07(client, cod_ibge, hoje.year - 1, (6,)))
        tarefas.append(_rreo_anexo07(client, cod_ibge, hoje.year - 2, (6,)))
        tarefas.append(_rreo_liquidadas_x_pagas(client, cod_ibge))

        resultados = await asyncio.gather(*tarefas, return_exceptions=True)

        def limpo(valor):
            return None if isinstance(valor, BaseException) else valor

        caixa = [limpo(r) for r in resultados[:3]]
        caixa = [c for c in caixa if c]
        restos = [limpo(r) for r in resultados[3:6]]
        restos = [r for r in restos if r]
        execucao = limpo(resultados[6])

        if not caixa and not restos and not execucao:
            return resultado(
                "pagamentos", erro="RGF Anexo 05 / RREO Anexo 07 não localizados para este ente"
            )

        return resultado(
            "pagamentos",
            {
                "caixa_por_ano": caixa,  # mais recente primeiro
                "restos_a_pagar_por_ano": restos,
                "execucao_pagamento": execucao,
            },
        )
    except Exception as exc:  # noqa: BLE001
        return resultado("pagamentos", erro=f"{type(exc).__name__}: {exc}")
