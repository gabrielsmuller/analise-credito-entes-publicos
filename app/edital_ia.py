"""Análise de editais (piloto): a IA lê o edital, extrai os dados e dá uma visão geral.

Diferente do score de crédito - que é 100% determinístico sobre dados oficiais -,
aqui a leitura do edital é necessariamente interpretativa. Para manter o que dá
para manter auditável:
  - a IA transcreve os fatos (datas, valores, itens, prazos, exigências) em JSON;
  - a IA dá nota e justificativa por critério de atratividade, mas a NOTA FINAL é
    a média ponderada calculada aqui, com pesos visíveis (PESOS_CRITERIOS);
  - a situação do prazo (aberto/encerrado) é calculada pela data, não pela IA.
"""
import json
from datetime import date, timedelta

from .config import ANTHROPIC_MODEL, LLM_PROVIDER, OPENAI_MODEL
from .dossier import IA_RETRIES, IA_TIMEOUT, _extrair_json

# Editais muito longos são cortados para caber no contexto do modelo com folga.
# O edital de exemplo (~90 páginas) tem ~134 mil caracteres.
MAX_CARACTERES = 400_000

# Critérios de atratividade para uma distribuidora de climatização. A IA dá
# 0-10 em cada um; o peso fica aqui, no código, para ser auditável e ajustável.
PESOS_CRITERIOS = {
    "aderencia_portfolio": ("Aderência ao portfólio", 25),
    "condicoes_pagamento": ("Condições de pagamento", 20),
    "prazo_logistica": ("Prazo e logística de entrega", 15),
    "exigencias": ("Exigências técnicas e de habilitação", 15),
    "riscos_contratuais": ("Riscos contratuais (multas, garantias)", 15),
    "porte_oportunidade": ("Porte da oportunidade", 10),
}

# Checklist fixo: a IA responde SEMPRE estas perguntas (sim / não / não consta),
# em vez de escolher livremente o que listar - o que fazia itens aparecerem numa
# leitura e sumirem na seguinte. "favoravel" é a resposta boa para o fornecedor
# (colore a tela); None = informativo, sem juízo.
CHECKLIST = [
    ("registro_precos", "É ata de registro de preços (sem obrigação de compra)?", None),
    ("qualificacao_economica", "Exige balanço, índices contábeis ou capital/PL mínimo?", "não"),
    ("atestado_tecnico", "Exige atestado de capacidade técnica?", "não"),
    ("amostra", "Exige amostra, catálogo ou prova de conceito?", "não"),
    ("visita_tecnica", "Exige visita técnica?", "não"),
    ("garantia_proposta", "Exige garantia de proposta?", "não"),
    ("garantia_contratual", "Exige garantia contratual (caução, seguro, fiança)?", "não"),
    ("instalacao", "Instalação inclusa no fornecimento?", None),
    ("entrega_parcelada", "Entrega parcelada ou sob demanda?", None),
    ("reajuste", "Prevê reajuste de preço?", "sim"),
    ("beneficio_me_epp", "Exclusividade, cota ou preferência para ME/EPP?", None),
    ("subcontratacao", "Permite subcontratação?", None),
    ("consorcio", "Permite participação em consórcio?", None),
]
# Perguntas de prazo/valor: a resposta é um detalhe, não sim/não.
CHECKLIST_INFO = [
    ("validade_proposta", "Validade mínima da proposta"),
    ("vigencia", "Vigência do contrato ou da ata"),
]
RESPOSTAS = {"sim": "sim", "nao": "não", "não": "não", "nao consta": "não consta", "não consta": "não consta"}

SYSTEM_EDITAL = """Você é um analista de licitações de uma distribuidora de equipamentos de \
climatização (ar-condicionado split, cassete, piso-teto, VRF, instalação e manutenção) que vende \
para órgãos públicos brasileiros. Leia o edital e devolva APENAS um JSON válido, sem texto ao redor.

Regras:
- Transcreva fielmente o que está no edital. Nunca invente números, datas ou exigências. Se um \
dado não aparecer, use null (ou lista vazia).
- Números em formato brasileiro ("R$ 128.406,59") viram float (128406.59). Datas em AAAA-MM-DD.
- Use o hífen simples "-"; nunca o travessão longo "—".
- Na avaliação, dê nota de 0 a 10 do ponto de vista da distribuidora (10 = muito favorável), com \
justificativa curta e concreta citando o edital. Não decida se a empresa deve participar - apenas \
avalie; a decisão é do setor de licitações.

O "checklist" deve ter EXATAMENTE estas chaves, todas preenchidas:
__CHAVES__
Em "resposta": "sim" ou "não" quando o edital trata do assunto; "não consta" quando o edital não menciona. Nunca deixe uma chave de fora. Para validade_proposta e vigencia, responda "sim" e ponha o prazo em "detalhe".
Nas perguntas "Exige...?", responda "sim" SOMENTE se o edital obriga o licitante a apresentar ou \
cumprir aquilo. Menções genéricas em cláusulas-padrão de sanção ou pagamento (ex.: "perda da \
garantia de proposta", "descontada da garantia prestada") NÃO são exigência: se o edital não pede \
que a garantia seja apresentada, responda "não" e diga isso no detalhe.
Quando o edital prevê que o órgão PODE exigir do licitante (ex.: "o pregoeiro poderá solicitar \
amostra"), responda "sim" e escreva "a critério do órgão" no detalhe - o fornecedor precisa estar \
preparado para isso.

Formato exato:
{
  "orgao": "nome do órgão licitante",
  "tipo_orgao": "municipio|estado|federal|autarquia|consorcio|outro",
  "municipio": "nome do município do órgão ou null",
  "uf": "sigla ou null",
  "cnpj_orgao": "só dígitos ou null",
  "numero": "ex.: Pregão Eletrônico 021/2026",
  "modalidade": "string",
  "criterio_julgamento": "ex.: menor preço por item",
  "modo_disputa": "string ou null",
  "plataforma": "portal/site da sessão ou null",
  "objeto_resumo": "uma frase",
  "data_abertura": "AAAA-MM-DD ou null",
  "hora_abertura": "HH:MM ou null",
  "prazo_propostas": "até quando enviar propostas, texto curto, ou null",
  "valor_estimado_total": número ou null,
  "valor_sigiloso": true/false,
  "itens": [{"numero": "string", "descricao": "curta", "quantidade": número, "unidade": "string", \
"capacidade_btu": número ou null, "valor_unitario_ref": número ou null, "valor_total_ref": número ou null}],
  "exclusivo_me_epp": true/false/null,
  "instalacao_inclusa": true/false/null,
  "prazo_entrega_dias": inteiro ou null,
  "local_entrega": "string ou null",
  "prazo_pagamento_dias": inteiro ou null,
  "garantia_meses": inteiro ou null,
  "fonte_recursos": "string ou null",
  "prazo_impugnacao_dias_uteis": inteiro (ex.: "até 3 dias úteis antes da sessão" -> 3) ou null,
  "prazo_esclarecimento_dias_uteis": inteiro ou null,
  "checklist": {
    "<chave>": {"resposta": "sim|não|não consta", "detalhe": "curto, com números/prazos ou null", "referencia": "item/cláusula do edital ou null"}
  },
  "pontos_atencao": ["cláusulas incomuns, restritivas ou arriscadas para o fornecedor que NÃO estejam já cobertas pelo checklist"],
  "avaliacao": {
    "aderencia_portfolio": {"nota": 0-10, "justificativa": "..."},
    "condicoes_pagamento": {"nota": 0-10, "justificativa": "..."},
    "prazo_logistica": {"nota": 0-10, "justificativa": "..."},
    "exigencias": {"nota": 0-10, "justificativa": "quanto mais restritivo, menor"},
    "riscos_contratuais": {"nota": 0-10, "justificativa": "quanto mais arriscado, menor"},
    "porte_oportunidade": {"nota": 0-10, "justificativa": "..."}
  },
  "visao_geral_md": "visão geral em Markdown para o setor de licitações, com as seções: \
## Resumo (3-5 frases), ## Objeto e itens, ## Prazos e datas, ## Condições comerciais \
(pagamento, entrega, garantia, instalação), ## Habilitação, ## Pontos de atenção. Seja direto."
}"""


SYSTEM_EDITAL = SYSTEM_EDITAL.replace(
    "__CHAVES__",
    "\n".join(
        [f"- {chave}: {pergunta}" for chave, pergunta, _ in CHECKLIST]
        + [f"- {chave}: {pergunta}" for chave, pergunta in CHECKLIST_INFO]
    ),
)


def normalizar_checklist(bruto: dict | None) -> list[dict]:
    """Garante todas as perguntas, na ordem fixa, com resposta padronizada.

    Chave que a IA omitiu vira "não consta" marcada como não verificada, em vez
    de simplesmente sumir da tela - a ausência fica visível.
    """
    bruto = bruto or {}
    saida = []
    for chave, pergunta, favoravel in [*CHECKLIST, *((c, p, None) for c, p in CHECKLIST_INFO)]:
        item = bruto.get(chave) if isinstance(bruto.get(chave), dict) else {}
        resposta = RESPOSTAS.get(str(item.get("resposta") or "").strip().lower())
        if resposta is None or resposta == "não consta" or favoravel is None:
            tom = "neutro"
        else:
            tom = "bom" if resposta == favoravel else "medio"
        saida.append({
            "chave": chave,
            "pergunta": pergunta,
            "resposta": resposta or "não verificado",
            "detalhe": item.get("detalhe"),
            "referencia": item.get("referencia"),
            "tom": tom,
            "info": chave in dict(CHECKLIST_INFO),
        })
    return saida


def recuar_dias_uteis(data: date, dias: int) -> date:
    """Data N dias úteis antes (seg-sex). Não conhece feriados - ver prazos_edital."""
    atual = data
    while dias > 0:
        atual -= timedelta(days=1)
        if atual.weekday() < 5:
            dias -= 1
    return atual


def prazos_edital(dados: dict | None, hoje: date | None = None) -> list[dict]:
    """Prazos-limite antes da sessão, calculados a partir da data de abertura."""
    dados = dados or {}
    try:
        abertura = date.fromisoformat(str(dados.get("data_abertura"))[:10])
    except ValueError:
        return []
    hoje = hoje or date.today()
    prazos = []
    for chave, rotulo in (
        ("prazo_esclarecimento_dias_uteis", "Pedir esclarecimentos"),
        ("prazo_impugnacao_dias_uteis", "Impugnar o edital"),
    ):
        try:
            n = int(dados.get(chave))
        except (TypeError, ValueError):
            continue
        limite = recuar_dias_uteis(abertura, n)
        restam = (limite - hoje).days
        if restam > 0:
            situacao, tom = f"faltam {restam} dia{'s' if restam != 1 else ''}", ("bom" if restam >= 3 else "medio")
        elif restam == 0:
            situacao, tom = "vence hoje", "medio"
        else:
            situacao, tom = "prazo encerrado", "ruim"
        prazos.append({"rotulo": rotulo, "data": limite.isoformat(), "dias_uteis": n,
                       "situacao": situacao, "tom": tom})
    return prazos


def _chamar_json(system: str, usuario: str, timeout: float | None = None, retries: int | None = None) -> dict:
    """Chamada que devolve JSON. timeout/retries próprios servem à matriz de exigências,
    que prefere dividir um bloco lento a repetir o mesmo pedido."""
    timeout = IA_TIMEOUT if timeout is None else timeout
    retries = IA_RETRIES if retries is None else retries
    if LLM_PROVIDER == "openai":
        from openai import OpenAI

        resp = OpenAI(timeout=timeout, max_retries=retries).chat.completions.create(
            model=OPENAI_MODEL,
            response_format={"type": "json_object"},
            messages=[{"role": "system", "content": system}, {"role": "user", "content": usuario}],
        )
        escolha = resp.choices[0]
        if escolha.finish_reason == "length":
            raise RuntimeError("análise do edital truncada pelo limite de tokens")
        return _extrair_json(escolha.message.content or "")

    import anthropic

    with anthropic.Anthropic(timeout=timeout, max_retries=retries).messages.stream(
        model=ANTHROPIC_MODEL,
        max_tokens=16000,
        system=system,
        messages=[{"role": "user", "content": usuario}],
    ) as stream:
        msg = stream.get_final_message()
    return _extrair_json("".join(b.text for b in msg.content if b.type == "text"))


def _texto_limitado(texto: str) -> str:
    if len(texto) <= MAX_CARACTERES:
        return texto
    return texto[:MAX_CARACTERES] + "\n\n[... edital cortado: texto excede o limite de análise ...]"


def calcular_nota(avaliacao: dict | None) -> dict:
    """Média ponderada das notas por critério (0-10). Determinística."""
    criterios = []
    soma = peso_total = 0
    for chave, (rotulo, peso) in PESOS_CRITERIOS.items():
        item = (avaliacao or {}).get(chave) or {}
        try:
            nota = max(0.0, min(10.0, float(item.get("nota"))))
        except (TypeError, ValueError):
            nota = None
        criterios.append(
            {"chave": chave, "rotulo": rotulo, "peso": peso, "nota": nota,
             "justificativa": item.get("justificativa")}
        )
        if nota is not None:
            soma += nota * peso
            peso_total += peso
    final = round(soma / peso_total, 1) if peso_total else None
    if final is None:
        faixa = None
    elif final >= 7:
        faixa = "alta"
    elif final >= 5:
        faixa = "média"
    else:
        faixa = "baixa"
    return {"nota": final, "faixa": faixa, "criterios": criterios}


def situacao_prazo(data_abertura: str | None, hoje: date | None = None) -> dict | None:
    """Quanto falta (ou há quanto passou) a sessão pública - calculado, não pela IA."""
    if not data_abertura:
        return None
    try:
        abertura = date.fromisoformat(str(data_abertura)[:10])
    except ValueError:
        return None
    dias = (abertura - (hoje or date.today())).days
    if dias > 0:
        return {"dias": dias, "tom": "bom" if dias >= 5 else "medio",
                "texto": f"faltam {dias} dia{'s' if dias != 1 else ''} para a sessão"}
    if dias == 0:
        return {"dias": 0, "tom": "medio", "texto": "a sessão é hoje"}
    return {"dias": dias, "tom": "ruim",
            "texto": f"sessão realizada há {-dias} dia{'s' if dias != -1 else ''}"}


def analisar_edital(texto: str) -> dict:
    """Extrai os dados do edital, a avaliação e a visão geral (uma chamada à IA)."""
    dados = _chamar_json(SYSTEM_EDITAL, "Analise o edital a seguir:\n\n" + _texto_limitado(texto))
    dados["nota_atratividade"] = calcular_nota(dados.get("avaliacao"))
    dados["checklist"] = normalizar_checklist(dados.get("checklist"))
    return dados


SYSTEM_CHAT_EDITAL = """Você é o analista de licitações de uma distribuidora de climatização e \
conversa com o setor de licitações sobre o edital abaixo, que você já analisou.

Regras:
- Responda com base EXCLUSIVAMENTE no texto do edital e na análise fornecidos. Cite o item/cláusula \
do edital quando possível (ex.: "item 20.1"). Nunca invente números, datas ou exigências.
- Se a resposta não estiver no edital, diga isso claramente e sugira onde verificar (esclarecimento \
ao pregoeiro, portal da licitação, termo de referência).
- A decisão de participar e o preço a ofertar são do setor de licitações: exponha fatos, riscos e \
alternativas, sem decidir por eles.
- Use o hífen simples "-"; nunca o travessão longo "—". Seja direto e em português do Brasil."""


def responder_pergunta_edital(texto: str, dados: dict | None, historico: list[dict]) -> str:
    """Responde uma pergunta sobre o edital.

    O texto integral vai sempre primeiro e idêntico entre perguntas: assim o cache
    de prompt do provedor reaproveita esse prefixo longo e barateia o chat.
    """
    contexto = (
        "TEXTO INTEGRAL DO EDITAL:\n" + _texto_limitado(texto)
        + "\n\nANÁLISE JÁ FEITA (JSON):\n" + json.dumps(dados or {}, ensure_ascii=False)
    )
    mensagens = [{"role": m["role"], "content": m["content"]} for m in historico[-20:]]

    if LLM_PROVIDER == "openai":
        from openai import OpenAI

        resp = OpenAI(timeout=IA_TIMEOUT, max_retries=IA_RETRIES).chat.completions.create(
            model=OPENAI_MODEL,
            messages=[
                {"role": "system", "content": contexto},
                {"role": "system", "content": SYSTEM_CHAT_EDITAL},
                *mensagens,
            ],
        )
        return (resp.choices[0].message.content or "").strip()

    import anthropic

    with anthropic.Anthropic(timeout=IA_TIMEOUT, max_retries=IA_RETRIES).messages.stream(
        model=ANTHROPIC_MODEL,
        max_tokens=4000,
        system=[
            {"type": "text", "text": contexto, "cache_control": {"type": "ephemeral"}},
            {"type": "text", "text": SYSTEM_CHAT_EDITAL},
        ],
        messages=mensagens,
    ) as stream:
        msg = stream.get_final_message()
    return "".join(b.text for b in msg.content if b.type == "text")
