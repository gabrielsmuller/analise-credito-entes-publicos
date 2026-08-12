"""SICONFI (Tesouro Nacional): indicadores fiscais do RREO e do RGF.

API pública: https://apidatalake.tesouro.gov.br/ords/siconfi/tt/
- RGF (poder Executivo, co_poder=E): o Anexo 06 (Demonstrativo Simplificado)
  traz despesa com pessoal e dívida consolidada líquida em % da RCL ajustada.
- RREO Anexo 01: receitas realizadas e despesas liquidadas até o bimestre.

A busca tolera a modalidade Simplificada/semestral dos municípios pequenos -
ver siconfi_base.py.
"""
import re
from datetime import date

import httpx

from .base import resultado
from .siconfi_base import buscar_rgf_ano, buscar_rreo


def _norm(texto) -> str:
    return re.sub(r"\s+", " ", str(texto or "")).strip().lower()


async def _rgf_mais_recente(client: httpx.AsyncClient, cod_ibge: str):
    """RGF mais recente disponível, completo ou simplificado."""
    hoje = date.today()
    for ano in (hoje.year, hoje.year - 1, hoje.year - 2):
        itens, referencia, _ = await buscar_rgf_ano(client, cod_ibge, ano)
        if itens:
            return referencia, itens
    return None, []


async def _rreo_mais_recente(client: httpx.AsyncClient, cod_ibge: str):
    hoje = date.today()
    for ano in (hoje.year, hoje.year - 1, hoje.year - 2):
        for periodo in (6, 5, 4, 3, 2, 1):
            itens = await buscar_rreo(client, cod_ibge, ano, periodo, "RREO-Anexo 01")
            if itens:
                return f"{periodo}º bimestre/{ano}", itens
    return None, []


def _achar_valor(itens: list[dict], padrao_conta: str, padrao_coluna: str) -> float | None:
    """Busca por padrão em cod_conta + conta (usado no RREO, cujas contas variam)."""
    for item in itens:
        alvo = _norm(item.get("cod_conta")) + " " + _norm(item.get("conta"))
        if re.search(padrao_conta, alvo) and re.search(padrao_coluna, _norm(item.get("coluna"))):
            try:
                return float(item["valor"])
            except (TypeError, ValueError):
                continue
    return None


def _valor_exato(itens: list[dict], cod_conta: str, padrao_coluna: str) -> float | None:
    """Casa cod_conta exatamente - identificador estável do SICONFI, imune a
    mudanças no texto da conta e a colisões entre contas de nome parecido
    (ex.: ReceitaCorrenteLiquidaAjustada vs ...AjustadaParaCalculoDosLimites)."""
    alvo = cod_conta.lower()
    for item in itens:
        if _norm(item.get("cod_conta")) == alvo and re.search(padrao_coluna, _norm(item.get("coluna"))):
            try:
                return float(item["valor"])
            except (TypeError, ValueError):
                continue
    return None


async def coletar_siconfi(client: httpx.AsyncClient, cod_ibge: str) -> dict:
    try:
        dados: dict = {}

        ref_rgf, rgf = await _rgf_mais_recente(client, cod_ibge)
        if rgf:
            dados["rgf"] = {
                "referencia": ref_rgf,
                "rcl_ajustada": _valor_exato(rgf, "ReceitaCorrenteLiquidaAjustada", r"^valor$"),
                "pessoal_valor": _valor_exato(rgf, "DespesaComPessoalTotal", r"^valor$"),
                "pessoal_pct_rcl": _valor_exato(rgf, "DespesaComPessoalTotal", r"% sobre a rcl"),
                # limite legal vem do próprio relatório (54% para municípios), não hardcoded
                "pessoal_limite_pct": _valor_exato(
                    rgf, "LimiteMaximoDespesaComPessoalTotal", r"% sobre a rcl"
                ),
                "pessoal_alerta_pct": _valor_exato(
                    rgf, "LimiteDeAlertaDespesaComPessoalTotal", r"% sobre a rcl"
                ),
                "dcl_valor": _valor_exato(
                    rgf,
                    "DividaConsolidadaLiquidaDemonstrativoSimplificado",
                    r"^valor at[eé] o (quadrimestre|semestre)",
                ),
                "dcl_pct_rcl": _valor_exato(
                    rgf, "DividaConsolidadaLiquidaDemonstrativoSimplificado", r"% sobre a rcl"
                ),
            }
            # Nem todo ente entrega o Anexo 06 (Demonstrativo Simplificado).
            # Sem ele a dívida ficava como "sem dado" e a dimensão simplesmente
            # sumia do score; o Anexo 02 traz os componentes para calcular.
            if dados["rgf"]["dcl_pct_rcl"] is None:
                dcl = _valor_exato(rgf, "DividaConsolidadaLiquida", r"at[eé] o \d+º (quadrimestre|semestre)")
                rcl = _valor_exato(rgf, "RGF2ReceitaCorrenteLiquida", r"at[eé] o \d+º (quadrimestre|semestre)")
                if dcl is not None and rcl:
                    dados["rgf"]["dcl_valor"] = dcl
                    dados["rgf"]["dcl_pct_rcl"] = round(dcl / rcl * 100, 2)
                    dados["rgf"]["dcl_origem"] = "calculado a partir do RGF Anexo 02"

        ref_rreo, rreo = await _rreo_mais_recente(client, cod_ibge)
        if rreo:
            receitas = _achar_valor(rreo, r"\btotalreceitas\b", r"^at[eé] o bimestre")
            despesas = _achar_valor(rreo, r"\btotaldespesas\b", r"liquidadas at[eé] o bimestre")
            if despesas is None:
                despesas = _achar_valor(rreo, r"\btotaldespesas\b", r"empenhadas at[eé] o bimestre")
            dados["rreo_orcamento"] = {
                "referencia": ref_rreo,
                "receitas_realizadas": receitas,
                "despesas_liquidadas": despesas,
                "resultado": round(receitas - despesas, 2) if receitas and despesas else None,
            }

        if not dados:
            return resultado("siconfi", erro="nenhum RGF/RREO encontrado nos últimos 2 anos para este ente")
        return resultado("siconfi", dados)
    except Exception as exc:  # noqa: BLE001
        return resultado("siconfi", erro=f"{type(exc).__name__}: {exc}")
