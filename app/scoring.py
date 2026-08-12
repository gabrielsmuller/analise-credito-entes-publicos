"""Scorecard determinístico de crédito do ente público.

Todos os pesos e faixas estão neste arquivo, visíveis e auditáveis. A IA não
participa do cálculo - apenas redige o dossiê a partir do resultado.

O score é normalizado pelas dimensões com dado disponível: fonte indisponível
não penaliza nem beneficia, mas fica registrada como lacuna.

Prioridade de pesos: o bloco de **comportamento de pagamento** (liquidez,
restos a pagar, execução de pagamento e tendência = 45 pontos) pesa mais que a
CAPAG (30). A razão é que a CAPAG responde "este ente pode tomar dívida nova
com garantia da União?", enquanto a pergunta de um fornecedor é "este ente paga
a minha nota fiscal?" - perguntas relacionadas, mas não iguais.

O que NÃO é pontuado, de propósito:
- Resultado orçamentário (receitas realizadas - despesas liquidadas): compara
  grandezas de regimes diferentes num corte parcial do exercício, quando as
  receitas entram distribuídas e as despesas se concentram no fim do ano. Um
  saldo positivo em junho é o esperado, não mérito. Fica como contexto.
- Contratos do PNCP: a classificação por texto do objeto produz falsos
  positivos e o volume vem amostrado. Serve como inteligência comercial.
- Repasses federais: fluxo naturalmente irregular; qualquer faixa fixa seria
  um indicador falso.
"""

# Limites da LRF usados como referência nas faixas:
# - Despesa com pessoal (município, poder executivo): limite 54% da RCL, alerta 48,6%
# - Dívida Consolidada Líquida (município): limite 120% da RCL

SEMAFORO_VERDE = 70
SEMAFORO_AMARELO = 45
# Meio da faixa amarela (45–69): separa o amarelo "alto" do "baixo" para a
# leitura de risco. O semáforo oficial não muda - só o rótulo textual fica mais
# fino, porque uma faixa de 25 pontos é larga demais para um único rótulo.
AMARELO_MEIO = (SEMAFORO_VERDE + SEMAFORO_AMARELO) // 2  # 57


def _classificar_risco(score, semaforo, confiavel) -> dict:
    """Rótulo de risco derivado do semáforo, com o amarelo subdividido.

    Mantém o semáforo como a autoridade (verde/amarelo/vermelho), mas dá à IA e
    à tela um rótulo que reflete a posição dentro da faixa - um amarelo 68 e um
    amarelo 46 são perfis de risco diferentes.
    """
    if not confiavel:
        return {
            "rotulo": "risco não avaliável",
            "tom": "neutro",
            "nota": "dados insuficientes para uma leitura de risco confiável",
        }
    if semaforo == "verde":
        return {"rotulo": "risco favorável", "tom": "bom", "nota": ""}
    if semaforo == "vermelho":
        return {"rotulo": "risco elevado", "tom": "ruim", "nota": ""}
    # amarelo: separa alto (mais perto do verde) de baixo (mais perto do vermelho)
    if score is not None and score >= AMARELO_MEIO:
        return {"rotulo": "risco moderado", "tom": "medio", "nota": ""}
    return {"rotulo": "risco moderado a elevado", "tom": "medio", "nota": ""}


def _dim(nome: str, pontos, maximo: int, detalhe: str) -> dict:
    return {"dimensao": nome, "pontos": pontos, "maximo": maximo, "detalhe": detalhe}


def _brl(valor) -> str:
    """Formata em real no padrão brasileiro.

    Aplicar .replace(",", ".") na frase inteira estraga tanto o separador
    decimal quanto as vírgulas do texto - a troca tem que ser só no número.
    """
    if valor is None:
        return "-"
    return "R$ " + f"{float(valor):,.2f}".replace(",", "§").replace(".", ",").replace("§", ".")


def _num_br(valor, casas: int = 0) -> str:
    if valor is None:
        return "-"
    return f"{float(valor):,.{casas}f}".replace(",", "§").replace(".", ",").replace("§", ".")


def _pct_br(valor, casas: int = 1, sinal: bool = False) -> str:
    if valor is None:
        return "-"
    texto = _num_br(abs(float(valor)), casas)
    if sinal:
        return f"{'+' if float(valor) >= 0 else '-'}{texto}%"
    return f"{'-' if float(valor) < 0 else ''}{texto}%"


def _pontuar_capag(capag: dict) -> dict:
    maximo = 30
    if not capag.get("ok"):
        return _dim("Nota CAPAG (Tesouro Nacional)", None, maximo, capag.get("erro") or "indisponível")
    d = capag["dados"]
    nota = (d.get("nota_capag") or "").strip().upper()
    tabela = {"A": 30, "B": 22, "C": 8, "D": 0}
    pontos = tabela.get(nota[:1] if nota else "", None)
    if pontos is not None:
        return _dim(
            "Nota CAPAG (Tesouro Nacional)", pontos, maximo, f"nota {nota} ({d.get('referencia', '')})"
        )

    # Sem nota consolidada, mas os indicadores parciais são informação real:
    # tratá-los como "sem dado" descartaria sinal negativo relevante. Um ente
    # sem CAPAG costuma ser justamente o que não entregou dados ao Tesouro.
    parciais = {
        "endividamento": (d.get("nota_endividamento") or "").strip().upper(),
        "poupança corrente": (d.get("nota_poupanca_corrente") or "").strip().upper(),
        "liquidez": (d.get("nota_liquidez") or "").strip().upper(),
    }
    validos = {k: v[:1] for k, v in parciais.items() if v[:1] in tabela}
    if not validos:
        return _dim(
            "Nota CAPAG (Tesouro Nacional)",
            None,
            maximo,
            f"sem nota e sem indicadores parciais (informado: '{nota or 'vazio'}')",
        )
    media = sum(tabela[v] for v in validos.values()) / len(validos)
    detalhe = ", ".join(f"{k}: {v}" for k, v in validos.items())
    return _dim(
        "Nota CAPAG (Tesouro Nacional)",
        round(media),
        maximo,
        f"sem nota consolidada; estimado pelos indicadores parciais ({detalhe})",
    )


def _pontuar_sancoes(transp: dict) -> dict:
    maximo = 8
    if not transp.get("ok"):
        return _dim("Sanções CEIS/CNEP/CEPIM", None, maximo, transp.get("erro") or "indisponível")
    d = transp["dados"]
    total = (d.get("ceis_total") or 0) + (d.get("cnep_total") or 0) + (d.get("cepim_total") or 0)
    if total == 0:
        return _dim("Sanções CEIS/CNEP/CEPIM", maximo, maximo, "nenhuma sanção ativa encontrada")
    return _dim("Sanções CEIS/CNEP/CEPIM", 0, maximo, f"{total} registro(s) de sanção encontrados")


def _pontuar_convenios(conv: dict) -> dict:
    maximo = 8
    nome = "Convênios federais (adimplência)"
    if not conv.get("ok"):
        return _dim(nome, None, maximo, conv.get("erro") or "indisponível")
    d = conv["dados"]
    # Guarda de esquema: análises antigas trazem outra estrutura. Sem isso, os
    # campos ausentes viram 0 e o scorecard afirma "nenhum inadimplente" - uma
    # informação falsa com cara de fato, em vez de uma lacuna declarada.
    if "inadimplentes_total" not in d:
        return _dim(
            nome, None, maximo, "análise coletada em formato anterior - reprocessar para avaliar"
        )

    inad_total = d.get("inadimplentes_total") or 0
    inad_recentes = d.get("inadimplentes_recentes") or 0
    suspensas = d.get("suspensas_total") or 0

    if inad_total == 0 and suspensas == 0:
        return _dim(nome, maximo, maximo, "nenhum convênio inadimplente")

    # Duas dimensões importam: recência (é conduta da gestão atual?) e volume
    # (é caso isolado ou falha sistêmica?). Uma pendência de 2009 num município
    # com centenas de convênios é resíduo histórico; 149 pendências somando
    # R$ 1,4 bilhão é outra coisa, mesmo que nenhuma seja recente.
    valor = d.get("valor_inadimplente") or 0
    if inad_recentes >= 3 or inad_total >= 20:
        pontos = 0
        leitura = f"{inad_total} convênio(s) inadimplente(s) somando {_brl(valor)}"
    elif inad_recentes >= 1 or inad_total >= 5:
        pontos = 2
        leitura = f"{inad_total} inadimplente(s) ({inad_recentes} nos últimos 5 anos), {_brl(valor)}"
    elif inad_total >= 1:
        pontos = 6
        leitura = (
            f"{inad_total} pendência(s) histórica(s) de {_brl(valor)} "
            "(vigência encerrada há mais de 5 anos)"
        )
    else:
        pontos = maximo
        leitura = "nenhum inadimplente"

    if suspensas:
        pontos = max(0, pontos - 1)
        leitura += f"; {suspensas} com inadimplência suspensa"
    if d.get("inadimplentes_truncado"):
        leitura += " (contagem truncada no teto de paginação)"

    return _dim(nome, pontos, maximo, leitura)


def _faixa(valor, faixas: list[tuple[float, int]], pontos_alem: int = 0) -> int:
    """faixas: lista de (limite_superior, pontos), avaliada em ordem."""
    for limite, pontos in faixas:
        if valor < limite:
            return pontos
    return pontos_alem


def _pontuar_fiscal(siconfi: dict) -> list[dict]:
    if not siconfi.get("ok"):
        motivo = siconfi.get("erro") or "indisponível"
        return [
            _dim("Despesa com pessoal (% RCL)", None, 10, motivo),
            _dim("Dívida consolidada líquida (% RCL)", None, 10, motivo),
        ]
    d = siconfi["dados"]
    dims = []

    rgf = d.get("rgf") or {}
    pessoal = rgf.get("pessoal_pct_rcl")
    if pessoal is None:
        dims.append(_dim("Despesa com pessoal (% RCL)", None, 10, "não localizado no RGF"))
    else:
        pontos = _faixa(pessoal, [(48.6, 10), (54.0, 6), (60.0, 3)], 0)
        ref = rgf.get("referencia", "")
        dims.append(
            _dim("Despesa com pessoal (% RCL)", pontos, 10, f"{_pct_br(pessoal)} da RCL ({ref}); limite LRF: 54%")
        )

    dcl = rgf.get("dcl_pct_rcl")
    if dcl is None:
        dims.append(_dim("Dívida consolidada líquida (% RCL)", None, 10, "não localizado no RGF"))
    else:
        pontos = _faixa(dcl, [(60.0, 10), (100.0, 6), (120.0, 3)], 0)
        ref = rgf.get("referencia", "")
        # DCL negativa significa que o ente tem mais ativo financeiro que dívida
        # (credor líquido). Exibir "-35,8%" ao lado de "limite: 120%" sem explicar
        # sugere problema para quem não é da área, quando é o oposto.
        if dcl < 0:
            leitura = f"{_pct_br(dcl)} da RCL ({ref}) - posição muito favorável: credor líquido"
        else:
            leitura = f"{_pct_br(dcl)} da RCL ({ref}); limite máximo: 120%"
        dims.append(_dim("Dívida consolidada líquida (% RCL)", pontos, 10, leitura))

    return dims


def _contexto_nao_pontuado(dados: dict) -> list[dict]:
    """Indicadores exibidos sem virar pontuação, com a ressalva do porquê."""
    itens = []

    siconfi = dados.get("siconfi", {})
    orc = (siconfi.get("dados") or {}).get("rreo_orcamento") or {} if siconfi.get("ok") else {}
    receitas, saldo = orc.get("receitas_realizadas"), orc.get("resultado")
    if receitas and saldo is not None:
        pct = saldo / receitas * 100
        itens.append(
            {
                "indicador": "Resultado orçamentário",
                "valor": f"{_pct_br(pct, sinal=True)} sobre receitas realizadas ({orc.get('referencia', '')})",
                "ressalva": "Compara receitas realizadas com despesas liquidadas num corte parcial "
                "do exercício. As receitas entram distribuídas ao longo do ano e as despesas se "
                "concentram no segundo semestre, então saldo positivo no meio do ano é o padrão "
                "esperado - não evidência de superávit. Só é conclusivo no fechamento.",
            }
        )

    pncp = dados.get("pncp", {})
    if pncp.get("ok"):
        p = pncp["dados"]
        itens.append(
            {
                "indicador": "Contratos no PNCP",
                "valor": f"{_num_br(p.get('total_contratos'))} contratos em {p.get('periodo')}",
                "ressalva": "Serve como inteligência comercial (quem já vendeu, por quanto). "
                "Não entra no score: a classificação por texto do objeto gera falsos positivos e "
                "o volume vem amostrado. Além disso, contrato assinado não é pagamento efetuado.",
            }
        )

    transf = dados.get("transferencias", {})
    if transf.get("ok"):
        t = transf["dados"]
        itens.append(
            {
                "indicador": "Repasses federais",
                "valor": f"{_brl(t.get('total_recebido'))} em {t.get('meses_com_repasse')} meses",
                "ressalva": "Fluxo naturalmente irregular entre meses; qualquer faixa fixa de "
                "pontuação produziria um indicador falso. Útil para ler a regularidade do caixa.",
            }
        )

    return itens


def _pontuar_pagamento(pag: dict) -> list[dict]:
    """Bloco de comportamento de pagamento - o mais pesado do scorecard.

    Usa os recursos NÃO VINCULADOS: é o caixa sem carimbo, o que de fato sobra
    para pagar fornecedor comum. O caixa vinculado (saúde, educação, FUNDEB)
    infla o total mas não pode ser usado livremente.
    """
    nomes = [
        ("Liquidez para fornecedores", 20),
        ("Pagamento de restos a pagar", 12),
        ("Execução de pagamento (liquidado x pago)", 8),
        ("Tendência da liquidez (3 anos)", 5),
    ]
    if not pag.get("ok"):
        motivo = pag.get("erro") or "indisponível"
        return [_dim(nome, None, peso, motivo) for nome, peso in nomes]

    d = pag["dados"]
    caixa = d.get("caixa_por_ano") or []
    restos = d.get("restos_a_pagar_por_ano") or []
    execucao = d.get("execucao_pagamento") or {}
    dims = []

    # 1) Cobertura das obrigações pelo caixa disponível
    if caixa:
        ano = caixa[0]
        nv = ano["nao_vinculados"]
        cobertura = nv.get("indice_cobertura")
        liquida = nv.get("disponibilidade_liquida_apos_rp")
        if cobertura is None:
            dims.append(_dim(nomes[0][0], None, 20, "disponibilidade de caixa não localizada"))
        else:
            pontos = _faixa(-cobertura, [(-1.5, 20), (-1.0, 15), (-0.8, 8), (-0.5, 4)], 0)
            situacao = "cobre" if cobertura >= 1 else "NÃO cobre"
            dims.append(
                _dim(
                    nomes[0][0],
                    pontos,
                    20,
                    f"caixa {situacao} as obrigações: índice {_num_br(cobertura, 2)}"
                    f" (disponibilidade líquida após restos a pagar: {_brl(liquida)})"
                    f" - fechamento de {ano['ano']}, recursos não vinculados",
                )
            )
    else:
        dims.append(_dim(nomes[0][0], None, 20, "RGF Anexo 05 não disponível"))

    # 2) Restos a pagar: nível de pagamento, cancelamento e trajetória
    #    12 pontos = 6 (quanto paga) + 3 (quanto cancela) + 3 (para onde vai)
    if restos:
        r = restos[0]
        proc, nproc = r["processados"], r["nao_processados"]
        pct_pago = proc.get("pct_pago")
        pct_cancel = nproc.get("pct_cancelado")
        if pct_pago is None and pct_cancel is None:
            dims.append(_dim(nomes[1][0], None, 12, "restos a pagar não localizados"))
        else:
            pontos = 0
            partes = []
            if pct_pago is not None:
                pontos += _faixa(-pct_pago, [(-95, 6), (-85, 4), (-70, 2)], 0)
                partes.append(f"{_pct_br(pct_pago)} dos processados pagos")
            if pct_cancel is not None:
                # cancelar resto a pagar limpa o balanço sem quitar o compromisso
                pontos += _faixa(pct_cancel, [(5, 3), (15, 2), (25, 1)], 0)
                partes.append(f"{_pct_br(pct_cancel)} cancelados")

            # trajetória: só entre exercícios fechados, para não comparar um
            # corte parcial do ano corrente com fechamentos completos
            fechados = [
                (x["ano"], x["processados"].get("pct_pago"))
                for x in restos
                if x.get("exercicio_fechado") and x["processados"].get("pct_pago") is not None
            ]
            if len(fechados) < 2:
                pontos += 2  # sem série comparável: não premia nem pune
                partes.append("sem série de exercícios fechados para avaliar tendência")
            else:
                recente, antigo = fechados[0][1], fechados[-1][1]
                serie = " → ".join(f"{a}: {_pct_br(p)}" for a, p in reversed(fechados))
                queda = antigo - recente
                if queda <= 5:
                    pontos += 3
                    leitura = "estável ou em melhora"
                elif queda <= 20:
                    pontos += 1
                    leitura = "em queda"
                else:
                    leitura = "em forte queda"
                partes.append(f"pagamento {leitura} ({serie})")

            dims.append(_dim(nomes[1][0], pontos, 12, f"{r['referencia']}: " + "; ".join(partes)))
    else:
        dims.append(_dim(nomes[1][0], None, 12, "RREO Anexo 07 não disponível"))

    # 3) Do que já virou dívida líquida e certa, quanto saiu do caixa
    pct = execucao.get("pct_pago")
    if pct is None:
        dims.append(_dim(nomes[2][0], None, 8, "despesas liquidadas/pagas não localizadas"))
    else:
        pontos = _faixa(-pct, [(-95, 8), (-90, 6), (-80, 3)], 0)
        dims.append(
            _dim(nomes[2][0], pontos, 8, f"{_pct_br(pct)} das despesas liquidadas foram pagas ({execucao['referencia']})")
        )

    # 4) A direção importa tanto quanto o nível
    coberturas = [(a["ano"], a["nao_vinculados"].get("indice_cobertura")) for a in caixa]
    coberturas = [(ano, c) for ano, c in coberturas if c is not None]
    if len(coberturas) < 2:
        dims.append(_dim(nomes[3][0], None, 5, "série histórica insuficiente"))
    else:
        atual, antigo = coberturas[0][1], coberturas[-1][1]
        serie = " → ".join(f"{ano}: {_num_br(c, 2)}" for ano, c in reversed(coberturas))

        # Direção, por variação absoluta. Percentual não serve: dividir por base
        # negativa ou próxima de zero inverte o sinal e classifica melhora como
        # deterioração (sair de -0,60 para 0,09 melhorou, mas a razão dá -114%).
        delta = atual - antigo
        if delta >= -0.10:
            pontos, leitura = 5, "estável ou em melhora"
        elif delta >= -0.50:
            pontos, leitura = 2, "em deterioração"
        else:
            pontos, leitura = 0, "em forte deterioração"

        # Histórico limita a direção: subir depois de anos submerso é recuperação
        # recente, não solidez. Um ente que passou 2 dos 3 anos sem cobrir as
        # obrigações não merece nota cheia só por ter melhorado no último.
        anos_abaixo = sum(1 for _, c in coberturas if c < 1.0)
        teto = {0: 5, 1: 3, 2: 3}.get(anos_abaixo, 1)
        if teto < pontos:
            pontos = teto
            if anos_abaixo == len(coberturas):
                leitura += (
                    f", mas sem cobrir as obrigações em nenhum dos {len(coberturas)} anos"
                )
            else:
                leitura += (
                    f", mas {anos_abaixo} de {len(coberturas)} anos sem cobrir as obrigações"
                    " - recuperação recente, não histórico consolidado"
                )
        dims.append(_dim(nomes[3][0], pontos, 5, f"{leitura} ({serie})"))

    return dims


def _pontuar_contexto(ibge: dict) -> list[dict]:
    if not ibge.get("ok"):
        motivo = ibge.get("erro") or "indisponível"
        return [
            _dim("Porte populacional", None, 8, motivo),
            _dim("PIB per capita", None, 7, motivo),
        ]
    d = ibge["dados"]
    dims = []
    pop = d.get("populacao")
    if pop is None:
        dims.append(_dim("Porte populacional", None, 4, "população não disponível"))
    else:
        pontos = 4 if pop >= 100_000 else (3 if pop >= 30_000 else (2 if pop >= 10_000 else 1))
        dims.append(_dim("Porte populacional", pontos, 4, f"{_num_br(pop)} habitantes ({d.get('ano_populacao')})"))
    pib_pc = d.get("pib_per_capita")
    if pib_pc is None:
        dims.append(_dim("PIB per capita", None, 3, "PIB não disponível"))
    else:
        pontos = 3 if pib_pc >= 30_000 else (2 if pib_pc >= 20_000 else 1)
        dims.append(_dim("PIB per capita", pontos, 3, f"{_brl(pib_pc)}/hab ({d.get('ano_pib')})"))
    return dims


# Agrupamento das dimensões para leitura rápida no painel.
BLOCOS = {
    "Comportamento de pagamento": [
        "Liquidez para fornecedores",
        "Pagamento de restos a pagar",
        "Execução de pagamento (liquidado x pago)",
        "Tendência da liquidez (3 anos)",
    ],
    "Saúde fiscal": [
        "Nota CAPAG (Tesouro Nacional)",
        "Despesa com pessoal (% RCL)",
        "Dívida consolidada líquida (% RCL)",
    ],
    "Conformidade": ["Convênios federais (adimplência)", "Sanções CEIS/CNEP/CEPIM"],
    "Contexto socioeconômico": ["Porte populacional", "PIB per capita"],
}


# Caixa livre como proporção da RCL. Complementa o índice de cobertura, que
# responde "as obrigações estão cobertas?" mas não "a folga é relevante para o
# tamanho da operação?". O Rio de Janeiro cobre suas obrigações (1,20x) com uma
# folga de R$ 295 mi - que é 0,8% de uma RCL de R$ 37 bi, ou seja, muito fina.
#
# Entra como ressalva, não como dimensão: pontuar de novo o mesmo caixa seria
# dupla contagem com "Liquidez para fornecedores".
FAIXAS_MARGEM = [
    (5.0, "folga forte", "bom", None),
    (2.0, "confortável", "bom", None),
    (1.0, "margem moderada", "medio", "margem de caixa moderada frente ao porte do orçamento"),
    (0.0, "folga apertada", "medio", "baixa margem relativa de caixa frente ao porte do orçamento"),
    (None, "negativa", "ruim", None),  # abaixo de zero já vira restrição
]


def _classificar_margem_caixa(dados: dict) -> dict | None:
    pag = dados.get("pagamentos", {})
    if not pag.get("ok") or not (pag["dados"].get("caixa_por_ano") or []):
        return None
    ano = pag["dados"]["caixa_por_ano"][0]
    livre = ano["nao_vinculados"].get("disponibilidade_liquida_apos_rp")
    rcl = ((dados.get("siconfi", {}).get("dados") or {}).get("rgf") or {}).get("rcl_ajustada")
    if livre is None or not rcl:
        return None

    pct = livre / rcl * 100
    for limite, rotulo, tom, ressalva in FAIXAS_MARGEM:
        if limite is None or pct >= limite:
            return {
                "pct": round(pct, 2),
                "valor": livre,
                "rcl": rcl,
                "ano": ano["ano"],
                "faixa": rotulo,
                "tom": tom,
                "ressalva": ressalva,
            }
    return None


def _agrupar_em_blocos(dimensoes: list[dict]) -> list[dict]:
    blocos = []
    for nome, membros in BLOCOS.items():
        dims = [d for d in dimensoes if d["dimensao"] in membros]
        avaliadas = [d for d in dims if d["pontos"] is not None]
        possivel = sum(d["maximo"] for d in avaliadas)
        obtido = sum(d["pontos"] for d in avaliadas)
        blocos.append(
            {
                "bloco": nome,
                "pontos": obtido if avaliadas else None,
                "maximo": possivel if avaliadas else sum(d["maximo"] for d in dims),
                "peso_total": sum(d["maximo"] for d in dims),
                "pct": round(obtido / possivel * 100) if possivel else None,
                "dimensoes": dims,
            }
        )
    return blocos


def calcular_scorecard(dados: dict) -> dict:
    """dados: dict {fonte: resultado_do_coletor}."""
    dimensoes = [
        *_pontuar_pagamento(dados.get("pagamentos", {})),
        _pontuar_capag(dados.get("capag", {})),
        *_pontuar_fiscal(dados.get("siconfi", {})),
        _pontuar_convenios(dados.get("convenios", {})),
        _pontuar_sancoes(dados.get("transparencia", {})),
        *_pontuar_contexto(dados.get("ibge", {})),
    ]

    avaliadas = [d for d in dimensoes if d["pontos"] is not None]
    lacunas = [d["dimensao"] for d in dimensoes if d["pontos"] is None]
    possivel = sum(d["maximo"] for d in avaliadas)
    obtido = sum(d["pontos"] for d in avaliadas)
    score = round(obtido / possivel * 100) if possivel else None

    if score is None:
        semaforo = "indefinido"
    elif score >= SEMAFORO_VERDE:
        semaforo = "verde"
    elif score >= SEMAFORO_AMARELO:
        semaforo = "amarelo"
    else:
        semaforo = "vermelho"

    restricoes = []
    nota_capag = ""
    if dados.get("capag", {}).get("ok"):
        nota_capag = (dados["capag"]["dados"].get("nota_capag") or "").strip().upper()[:1]
    if nota_capag == "D" and semaforo != "indefinido":
        semaforo = "vermelho"
        restricoes.append("CAPAG D força semáforo vermelho")
    elif nota_capag == "C" and semaforo == "verde":
        semaforo = "amarelo"
        restricoes.append("CAPAG C impede semáforo verde")

    transp = dados.get("transparencia", {})
    if transp.get("ok"):
        d = transp["dados"]
        if (d.get("ceis_total") or 0) + (d.get("cnep_total") or 0) + (d.get("cepim_total") or 0) > 0:
            if semaforo == "verde":
                semaforo = "amarelo"
            restricoes.append("existência de sanções impede semáforo verde")

    conv = dados.get("convenios", {})
    if conv.get("ok"):
        cd = conv["dados"]
        recentes = cd.get("inadimplentes_recentes") or 0
        valor_inad = cd.get("valor_inadimplente") or 0
        # Contagem acumulada não bloqueia sozinha: um município grande mantém
        # centenas de convênios e acumula pendências antigas por idade, não por
        # conduta. O que pesa é inadimplência recente e o tamanho do valor em
        # aberto frente ao orçamento - R$ 14 mi numa RCL de R$ 13 bi é ruído;
        # R$ 1,4 bi numa RCL de R$ 37 bi é material.
        rcl = ((dados.get("siconfi", {}).get("dados") or {}).get("rgf") or {}).get("rcl_ajustada")
        pct_rcl = (valor_inad / rcl * 100) if rcl else None

        motivo = None
        if recentes >= 3:
            motivo = f"{recentes} convênios inadimplentes nos últimos 5 anos"
        elif pct_rcl is not None and pct_rcl >= 1.0:
            motivo = (
                f"convênios inadimplentes somam {_brl(valor_inad)}, "
                f"{_pct_br(pct_rcl)} da receita corrente líquida"
            )
        if motivo:
            if semaforo == "verde":
                semaforo = "amarelo"
            restricoes.append(f"{motivo} impede semáforo verde")

    # CAUC é a fonte autoritativa sobre estar ou não impedido de receber
    # transferências voluntárias da União - mais direto que inferir dos convênios.
    obs = dados.get("observacoes_manuais", {})
    if obs.get("ok") and (obs.get("dados") or {}).get("cauc_situacao") == "com_pendencias":
        if semaforo == "verde":
            semaforo = "amarelo"
        restricoes.append("pendências no CAUC (informado pelo analista) impedem semáforo verde")

    # caixa discricionário negativo no fechamento: o ente terminou o exercício
    # devendo mais do que tinha, e é desse caixa que sai o pagamento a fornecedor
    pag = dados.get("pagamentos", {})
    if pag.get("ok") and (pag["dados"].get("caixa_por_ano") or []):
        nv = pag["dados"]["caixa_por_ano"][0]["nao_vinculados"]
        liquida = nv.get("disponibilidade_liquida_apos_rp")
        if liquida is not None and liquida < 0:
            if semaforo == "verde":
                semaforo = "amarelo"
            restricoes.append(
                "disponibilidade de caixa líquida negativa em recursos não vinculados "
                "impede semáforo verde"
            )

    cobertura = round(possivel / sum(d["maximo"] for d in dimensoes) * 100)

    # Confiabilidade: um score alto sobre pouca informação passa falsa segurança.
    # O caso crítico é quando falta o bloco de pagamento (o mais pesado, 45 pts),
    # que é justamente a evidência direta de que o ente paga fornecedores - sem
    # ele, o score vem de dimensões periféricas (CAPAG, porte, sanções) e não diz
    # o que mais importa. Quando isso acontece, o score não é confiável e o
    # semáforo não passa de amarelo, independentemente da pontuação.
    bloco_pagamento_ausente = all(
        d["pontos"] is None
        for d in dimensoes
        if d["dimensao"] in BLOCOS["Comportamento de pagamento"]
    )
    confiavel = cobertura >= 60 and not bloco_pagamento_ausente
    if not confiavel and semaforo == "verde":
        semaforo = "amarelo"
    if bloco_pagamento_ausente:
        restricoes.append(
            "os demonstrativos fiscais (RGF/RREO) não foram localizados nas consultas automáticas "
            "ao SICONFI, então o comportamento de pagamento - o bloco mais importante - não pôde "
            "ser avaliado; o score reflete apenas dados periféricos e não deve ser lido como aprovação"
        )
    elif cobertura < 60:
        restricoes.append(
            f"cobertura de dados baixa ({cobertura}%): o score reflete informação parcial"
        )

    # Ressalvas acompanham o semáforo sem rebaixá-lo: sinalizam o que merece
    # atenção mesmo quando os critérios de aprovação foram atendidos.
    ressalvas = []
    margem = _classificar_margem_caixa(dados)
    if margem and margem["ressalva"]:
        ressalvas.append(
            f"{margem['ressalva']}: caixa livre de {_brl(margem['valor'])} "
            f"equivale a {_pct_br(margem['pct'], 1)} da RCL ({margem['faixa']}, {margem['ano']})"
        )

    return {
        "score": score,
        "semaforo": semaforo,
        "confiavel": confiavel,
        "classificacao_risco": _classificar_risco(score, semaforo, confiavel),
        "bloco_pagamento_ausente": bloco_pagamento_ausente,
        "cobertura_dados_pct": cobertura,
        "dimensoes": dimensoes,
        "blocos": _agrupar_em_blocos(dimensoes),
        "contexto_nao_pontuado": _contexto_nao_pontuado(dados),
        "lacunas": lacunas,
        "restricoes_aplicadas": restricoes,
        "ressalvas": ressalvas,
        "margem_caixa": margem,
        "faixas": {"verde": f">= {SEMAFORO_VERDE}", "amarelo": f">= {SEMAFORO_AMARELO}", "vermelho": f"< {SEMAFORO_AMARELO}"},
    }
