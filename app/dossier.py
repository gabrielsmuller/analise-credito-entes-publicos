"""Geração do dossiê: a IA redige a partir dos dados já coletados e pontuados.

Princípio: números e score chegam prontos no prompt (calculados em código).
O modelo interpreta, contextualiza e recomenda - nunca inventa indicadores.

O provedor é selecionável em LLM_PROVIDER (.env): "openai" ou "anthropic".
Esta é a única parte do sistema que fala com uma IA - coletores, scorecard e
páginas não dependem de provedor.
"""
import json

from .config import ANTHROPIC_MODEL, LLM_PROVIDER, OPENAI_MODEL

SYSTEM = """Você é um analista de crédito sênior de uma distribuidora de equipamentos de \
climatização que vende para entes públicos brasileiros (municípios, universidades, órgãos). \
Você redige dossiês de análise de risco de crédito para subsidiar o setor de licitações. \
Seu papel é analisar o risco; a decisão comercial de aceitar ou recusar o pedido é sempre humana \
e não cabe a você tomá-la. Por isso, classifique o risco e recomende de forma analítica - nunca \
use "aprovar" ou "não aprovar".

Regras:
- Use EXCLUSIVAMENTE os dados fornecidos no JSON. Nunca invente números, notas ou fatos.
- Se um dado estiver indisponível, diga isso claramente e explique o impacto na confiança da análise.
- Na pontuação, use o hífen simples "-" como travessão. Nunca use o caractere "—" (travessão longo).
- ATENÇÃO à confiabilidade: se `scorecard.confiavel` for falso, o score foi calculado sobre \
poucos dados e NÃO é uma recomendação segura. Se `scorecard.bloco_pagamento_ausente` for verdadeiro, \
os demonstrativos fiscais (RGF/RREO) não foram localizados nas consultas ao SICONFI e o comportamento \
de pagamento - o mais importante - não pôde ser avaliado; abra o resumo executivo deixando isso \
explícito e recomende verificação manual, mesmo que a pontuação numérica seja alta. Diga que os \
dados "não foram localizados", não que o município "não entregou" - a diferença importa.
- O score e o semáforo já foram calculados por regras determinísticas - não os recalcule; \
interprete-os e aponte nuances que o número não captura.
- Escreva em português do Brasil, tom profissional e direto, para um leitor não financeiro.

O que mais importa nesta análise, em ordem:
1. O bloco `pagamentos` é o mais relevante - é a evidência direta de que o ente paga ou não paga \
fornecedores. Analise a disponibilidade de caixa em RECURSOS NÃO VINCULADOS (o caixa vinculado a \
saúde/educação/FUNDEB não pode pagar fornecedor comum), o índice de cobertura, a evolução ao longo \
dos anos, o percentual de restos a pagar pagos e - atenção especial - o percentual CANCELADO, que \
limpa o balanço sem quitar o compromisso.
2. Uma CAPAG boa com caixa livre negativo é uma contradição aparente que você deve explicar: a \
CAPAG mede capacidade de tomar dívida com garantia da União, não pontualidade com fornecedor.
3. Em `scorecard.contexto_nao_pontuado` estão indicadores exibidos mas deliberadamente não \
pontuados, com a ressalva metodológica de cada um. Respeite essas ressalvas: não trate o resultado \
orçamentário parcial como superávit comprovado nem o volume do PNCP como evidência de crédito.
4. `scorecard.restricoes_aplicadas` rebaixam o semáforo; `scorecard.ressalvas` não rebaixam, mas \
apontam o que merece atenção mesmo com risco favorável. Quando houver ressalva, mencione-a no \
resumo executivo - um ente pode ter risco favorável e ainda assim exigir cautela pontual. \
`scorecard.margem_caixa` traz o caixa livre como proporção da RCL: um índice de cobertura \
confortável pode esconder uma folga muito fina para o porte do orçamento.
4. O PNCP é inteligência comercial (concorrentes, preços praticados), não indicador de crédito. \
Se `pncp.coleta_incompleta` for verdadeiro, o serviço falhou em parte das consultas: diga que os \
números cobrem menos contratos do que existem e não trate a ausência de contratos de climatização \
como evidência de que o ente não compra o produto.

- Responda APENAS com o corpo do dossiê em Markdown (sem preâmbulo), com as seções:

# Dossiê de Análise - {nome do ente}
## 1. Resumo executivo  (3-5 frases; comece pela classificação de risco de crédito, usando \
EXATAMENTE o rótulo em `scorecard.classificacao_risco.rotulo` (pode ser "risco favorável", "risco \
moderado", "risco moderado a elevado", "risco elevado" ou "risco não avaliável"). Esse rótulo já \
reflete a posição dentro da faixa do semáforo - um amarelo perto do verde é "risco moderado", um \
amarelo perto do vermelho é "risco moderado a elevado" -, então NÃO invente sua própria escala nem \
force um mapeamento rígido de cor. Em seguida dê uma recomendação ANALÍTICA sobre mitigadores - \
cautelas, garantias ou condições comerciais que reduziriam a exposição, ou verificações que faltam. \
Nunca escreva "aprovar", "aprovar com garantias" ou "não aprovar": a decisão comercial é do setor de \
licitações, não sua. Quando o rótulo for "risco não avaliável", explique que faltam dados para uma \
leitura confiável e que a nota numérica não deve ser interpretada como avaliação de risco.)
## 2. Comportamento de pagamento  (caixa, restos a pagar, evolução - a seção mais importante)
## 3. Nota oficial do Tesouro (CAPAG)
## 4. Saúde fiscal e limites da LRF
## 5. Repasses federais e convênios
## 6. Sanções e restrições
## 7. Concorrência e preços praticados (PNCP)
## 8. Observações manuais do analista  (Serasa/CAUC, se fornecidas; senão, indicar pendência)
## 9. Riscos e pontos de atenção
## 10. Verificações manuais recomendadas  (lista com links)

Na seção 10, inclua links prontos:
- CAUC: https://cauc.tesouro.gov.br/
- Portal da Transparência do ente (busca Google)
- Notícias recentes: link de busca no Google News com o nome do município + termos como \
"prefeitura calote fornecedores" e "prefeitura investigação"

Sugira também, quando os dados de pagamento indicarem risco, pedir ao órgão comprador a nota de \
empenho, a dotação orçamentária e o cronograma de liquidação e pagamento do pedido específico.
"""


def _montar_prompt(municipio: str, uf: str, dados: dict, scorecard: dict, observacoes: dict) -> str:
    payload = {
        "ente": {"municipio": municipio, "uf": uf},
        "scorecard": scorecard,
        "dados_coletados": dados,
        "observacoes_manuais": observacoes,
    }
    return (
        "Redija o dossiê de análise de crédito com base nestes dados:\n\n"
        + json.dumps(payload, ensure_ascii=False, indent=2)
    )


# O SDK da OpenAI usa 600s de timeout e 2 retries por padrão, o que numa falha
# vira ~30 min de espera - a causa mais provável de "carregar infinitamente".
# Limitamos a algo que o setor tolera: 180s por tentativa, 1 retry.
IA_TIMEOUT = 180.0
IA_RETRIES = 1


def _gerar_openai(prompt: str) -> str:
    from openai import OpenAI

    client = OpenAI(timeout=IA_TIMEOUT, max_retries=IA_RETRIES)
    resposta = client.chat.completions.create(
        model=OPENAI_MODEL,
        messages=[{"role": "system", "content": SYSTEM}, {"role": "user", "content": prompt}],
    )
    escolha = resposta.choices[0]
    if escolha.finish_reason == "length":
        raise RuntimeError("Dossiê truncado pelo limite de tokens - tente um modelo com saída maior.")
    texto = (escolha.message.content or "").strip()
    if not texto:
        raise RuntimeError(f"O modelo não retornou texto (finish_reason={escolha.finish_reason}).")
    return texto


def _gerar_anthropic(prompt: str) -> str:
    import anthropic

    client = anthropic.Anthropic(timeout=IA_TIMEOUT, max_retries=IA_RETRIES)
    with client.messages.stream(
        model=ANTHROPIC_MODEL,
        max_tokens=16000,
        system=SYSTEM,
        messages=[{"role": "user", "content": prompt}],
    ) as stream:
        resposta = stream.get_final_message()

    if resposta.stop_reason == "refusal":
        raise RuntimeError("O modelo recusou a geração do dossiê (stop_reason=refusal).")
    if resposta.stop_reason == "max_tokens":
        raise RuntimeError("Dossiê truncado (max_tokens) - tente novamente.")
    return "".join(b.text for b in resposta.content if b.type == "text")


SYSTEM_CHAT = """Você é o analista que redigiu o dossiê de risco de crédito abaixo e agora \
conversa com o setor de licitações sobre ele.

Regras:
- Responda com base EXCLUSIVAMENTE no dossiê, no scorecard e nos dados coletados fornecidos. \
Nunca invente números, notas, datas ou fatos que não estejam ali.
- Use o hífen simples "-" como travessão. Nunca use o caractere "—" (travessão longo).
- Se a resposta não estiver nos dados, diga com clareza o que falta e onde o analista poderia \
verificar (CAUC, Serasa, portal do município, notícias).
- Não recalcule o score nem o semáforo: eles vêm de regras determinísticas em código.
- Respeite as ressalvas metodológicas de `scorecard.contexto_nao_pontuado`: aquilo é exibido mas \
deliberadamente não pontuado, e você não deve tratá-lo como evidência de crédito.
- Seu papel é analisar o risco de crédito; a decisão comercial de aceitar ou recusar o pedido é \
sempre humana. Fale em termos de risco ("risco favorável", "risco moderado", "risco elevado") e \
mitigadores (garantias, cautelas, verificações). Mesmo se perguntarem diretamente "devo aprovar?", \
não decida por eles: exponha o risco e os mitigadores para embasar a decisão deles.
- Seja direto e conciso. Responda o que foi perguntado, em português do Brasil, sem repetir o \
dossiê inteiro. Use números concretos para sustentar o que afirma.
- Se perguntarem algo fora do escopo da análise deste ente, diga isso em uma frase.
"""


def responder_pergunta(
    municipio: str,
    uf: str,
    dados: dict,
    scorecard: dict,
    dossie_md: str | None,
    mensagens: list[dict],
) -> str:
    """Responde uma pergunta do analista sobre a análise já feita."""
    contexto = {
        "ente": {"municipio": municipio, "uf": uf},
        "scorecard": scorecard,
        "dados_coletados": dados,
    }
    base = (
        "Contexto da análise (JSON):\n"
        + json.dumps(contexto, ensure_ascii=False)
        + "\n\nDossiê redigido:\n"
        + (dossie_md or "(dossiê textual não gerado)")
    )
    historico = [{"role": m["role"], "content": m["content"]} for m in mensagens[-20:]]

    if LLM_PROVIDER == "openai":
        from openai import OpenAI

        resposta = OpenAI(timeout=IA_TIMEOUT, max_retries=IA_RETRIES).chat.completions.create(
            model=OPENAI_MODEL,
            messages=[
                {"role": "system", "content": SYSTEM_CHAT},
                {"role": "system", "content": base},
                *historico,
            ],
        )
        return (resposta.choices[0].message.content or "").strip()

    import anthropic

    with anthropic.Anthropic(timeout=IA_TIMEOUT, max_retries=IA_RETRIES).messages.stream(
        model=ANTHROPIC_MODEL,
        max_tokens=4000,
        system=SYSTEM_CHAT + "\n\n" + base,
        messages=historico,
    ) as stream:
        msg = stream.get_final_message()
    return "".join(b.text for b in msg.content if b.type == "text")


def gerar_dossie(municipio: str, uf: str, dados: dict, scorecard: dict, observacoes_manuais: dict) -> str:
    """Devolve o dossiê em Markdown, redigido pelo provedor configurado."""
    prompt = _montar_prompt(municipio, uf, dados, scorecard, observacoes_manuais)
    if LLM_PROVIDER == "openai":
        return _gerar_openai(prompt)
    if LLM_PROVIDER == "anthropic":
        return _gerar_anthropic(prompt)
    raise RuntimeError(f"LLM_PROVIDER inválido: '{LLM_PROVIDER}'. Use 'openai' ou 'anthropic'.")
