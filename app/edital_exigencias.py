"""Matriz de exigências do edital: extração exaustiva, deduplicada e verificada.

Cada edital tem estrutura própria, e uma única leitura resumida perde as
exigências de cláusulas específicas (ex.: a especificação técnica de um item).
Aqui o edital é lido em blocos de páginas, em paralelo, com uma instrução só:
listar TUDO o que é exigido, uma linha por exigência, com o trecho literal.
Depois o código:
  - deduplica (editais repetem a mesma cláusula no edital, no TR e na minuta);
  - confere cada trecho citado contra o texto do edital e calcula a página -
    o que não for localizado fica marcado, em vez de passar por fato.
As categorias são fixas, para que editais diferentes caiam na mesma estrutura.
"""
import re
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from difflib import SequenceMatcher

from .config import OPENAI_MODEL, OPENAI_MODEL_EXTRACAO
from .edital_ia import _chamar_json

CATEGORIAS = {
    "participacao": "Participação e credenciamento",
    "proposta": "Proposta e documentos da proposta",
    "habilitacao_juridica": "Habilitação jurídica",
    "habilitacao_fiscal": "Regularidade fiscal, social e trabalhista",
    "habilitacao_economica": "Qualificação econômico-financeira",
    "habilitacao_tecnica": "Qualificação técnica",
    "especificacao_produto": "Especificações do produto",
    "entrega_execucao": "Entrega e execução",
    "recebimento_pagamento": "Recebimento e pagamento",
    "garantia_assistencia": "Garantia e assistência técnica",
    "sancoes": "Sanções e multas",
    "outras": "Outras exigências",
}
# consequências que tiram o licitante da disputa ou do contrato
CONSEQUENCIAS_GRAVES = ("desclassifica", "inabilita", "rescis", "impediment", "inidone")

# Caracteres por chamada. O tempo de resposta cresce com o número de exigências
# devolvidas, e trechos com tabelas de especificação são muito densos: blocos menores
# e mais paralelos evitam que um bloco só segure a análise inteira.
TAMANHO_BLOCO = 16_000
MAX_PARALELO = 8
# Lida em blocos, a matriz aceita bem mais texto que a leitura geral (uma chamada só,
# limitada pelo contexto do modelo). 800 mil caracteres ~ 250 páginas ~ 34 blocos,
# ~7 min no pior caso - dentro do teto de 15 min do Lambda. Acima disso, corta e AVISA.
LIMITE_MATRIZ = 800_000

# Produtos cuja especificação técnica interessa. Editais mistos (o do IFFar tem 55
# itens, ~16 de climatização) geravam centenas de linhas de liquidificador e geladeira -
# e o custo da extração é quase todo o texto gerado. Os demais itens viram uma linha.
ESCOPO_PRODUTOS = (
    "climatização: ar-condicionado (split, cassete, piso-teto, janela, VRF, portátil), "
    "climatizador evaporativo, cortina de ar, desumidificador, ventilação e exaustão, "
    "e peças, acessórios e serviços desses equipamentos"
)

SYSTEM_EXIGENCIAS = """Você recebe UM TRECHO de um edital de licitação (as páginas vêm marcadas \
com "=== página N ==="). Liste TODAS as exigências, obrigações, condições, prazos, especificações \
técnicas e penalidades que o trecho impõe ao licitante ou ao contratado.

Regras:
- Seja exaustivo: cada obrigação vira UMA linha própria. Não resuma, não junte várias exigências \
numa linha, não omita por parecer óbvio ou repetido. Na dúvida, inclua.
- Especificações técnicas de produto: SÓ para itens de __ESCOPO__. Nesses, uma linha por \
requisito, indicando o item (ex.: item 2, "serpentina de cobre"; item 2, "gás refrigerante R32"), \
breve: "exigencia" com no máximo 12 palavras (ex.: "Item 2: gás refrigerante R32") e "trecho" com \
40 a 80 caracteres. Para itens de outros produtos (eletrodomésticos, móveis, etc.), NÃO detalhe a \
especificação técnica: registre só UMA linha por item, ex.: "Item 32: forno micro-ondas 20 L".
- Inclua também regras de julgamento que afetam o licitante: preço máximo, critério de \
inexequibilidade (ex.: "abaixo de 50% do orçado"), motivos de desclassificação e de inabilitação.
- Ignore o que não impõe nada ao fornecedor: justificativas, fundamentos legais genéricos, \
definições, obrigações exclusivas da Administração.
- "exigencia": frase completa e autossuficiente, com os números, prazos e valores do texto.
- "trecho": cópia LITERAL de 40 a 150 caracteres do edital que sustenta a exigência, exatamente \
como está (mesmas palavras, sem corrigir nem reescrever).
- "curto": a exigência em até 8 palavras (ex.: "gás refrigerante R32", "balanço dos 2 últimos \
exercícios", "entrega em 10 dias úteis").
- marca "R" (rotina) se for exigência de rotina, presente em praticamente qualquer pregão da Lei \
14.133 (certidões fiscais e trabalhistas usuais, contrato social, declarações-padrão, regras gerais \
do sistema eletrônico, sanções transcritas da lei sem números próprios). Sem "R" se for particular \
deste edital ou trouxer número, prazo, valor ou condição definidos por ele (especificações técnicas, \
prazos de entrega, índices contábeis, multas com percentuais próprios, catálogo, amostra...). \
Na dúvida, sem "R".
- "documento": só quando a exigência pede que o licitante APRESENTE um documento para participar \
- no credenciamento, junto com a proposta ou na habilitação. Use o nome curto e padronizado \
(ex.: "Balanço patrimonial", "Certidão negativa de falência", "Atestado de capacidade técnica", \
"Catálogo ou ficha técnica do produto", "Prova de regularidade com o FGTS"), sempre o mesmo nome \
para o mesmo documento. NÃO é documento: o que só se usa depois (nota fiscal, recurso, pedido de \
esclarecimento ou impugnação, ordem de fornecimento, manual ou certificado entregue com o produto) \
- nesses casos, null.
- marca "C" (condicional) SOMENTE se a exigência vale apenas para estes casos: cooperativa, MEI, \
empresário individual, sociedade simples, empresa estrangeira, consórcio, empresa em recuperação \
judicial, filial ou agência, ME/EPP que queira o benefício. O licitante típico é uma sociedade \
empresária limitada: o que se pede dela (contrato social, alterações, documentos dos \
administradores) e o que vale para todos (regularidade fiscal, trabalhista, FGTS, inscrições) não \
leva "C".
- Use o hífen simples "-"; nunca o travessão longo.

Devolva APENAS JSON, em formato COMPACTO: cada exigência é uma LISTA de 9 valores, nesta ordem, \
sem nomes de campo:
{"e": [[categoria, item, exigencia, curto, marcas, documento, clausula, consequencia, trecho], ...]}
- categoria: um de __CATEGORIAS__
- item: número do item do objeto, ou null
- marcas: "", "R", "C" ou "RC"
- consequencia: o que acontece se descumprir (desclassificação, inabilitação, multa...), ou null
- documento, clausula: null quando não houver
Exemplo: {"e": [["habilitacao_economica", null, "Apresentar balanço patrimonial dos 2 últimos \
exercícios", "balanço dos 2 últimos exercícios", "", "Balanço patrimonial", "12.1.3", "inabilitação", \
"Balanço patrimonial e demonstrações contábeis dos dois últimos exercícios"]]}""".replace(
    "__CATEGORIAS__", "|".join(CATEGORIAS)
).replace("__ESCOPO__", ESCOPO_PRODUTOS)


def _normalizar(texto: str) -> str:
    t = unicodedata.normalize("NFKD", texto or "").encode("ascii", "ignore").decode().lower()
    t = re.sub(r"-\s*\n\s*", "", t)          # hifenização de fim de linha
    t = re.sub(r"[^a-z0-9%,./]+", " ", t)    # pontuação e quebras viram espaço
    return re.sub(r"\s+", " ", t).strip()


def _shingles(texto_norm: str, n: int = 4) -> set:
    w = texto_norm.split()
    return {" ".join(w[i:i + n]) for i in range(max(1, len(w) - n + 1))}


def paginas_do_texto(texto: str) -> list[str]:
    """Os extratores separam páginas com form feed; sem ele, o texto é uma página só."""
    paginas = texto.split("\f")
    while paginas and not paginas[-1].strip():
        paginas.pop()
    return paginas or [texto]


def _blocos(paginas: list[str]) -> list[list[tuple[int, str]]]:
    """Agrupa páginas em blocos de ~TAMANHO_BLOCO; cada bloco guarda (nº da página, texto)."""
    blocos, atual, tamanho = [], [], 0
    for n, pagina in enumerate(paginas, 1):
        if atual and tamanho + len(pagina) > TAMANHO_BLOCO:
            blocos.append(atual)
            atual, tamanho = [], 0
        atual.append((n, pagina))
        tamanho += len(pagina)
    if atual:
        blocos.append(atual)
    return blocos


def _texto_bloco(bloco: list[tuple[int, str]]) -> str:
    return "\n".join(f"=== página {n} ===\n{texto}" for n, texto in bloco)


def _dividir(bloco: list[tuple[int, str]]) -> list[list[tuple[int, str]]] | None:
    """Parte um bloco ao meio: por páginas, ou pela metade da página se for uma só."""
    if len(bloco) > 1:
        meio = len(bloco) // 2
        return [bloco[:meio], bloco[meio:]]
    n, texto = bloco[0]
    if len(texto) < 4000:
        return None
    corte = texto.rfind("\n", 0, len(texto) // 2)
    corte = corte if corte > 0 else len(texto) // 2
    return [[(n, texto[:corte])], [(n, texto[corte:])]]


def _localizar(trecho: str, paginas_norm: list[str], shingles_pag: list[set]) -> tuple[str, int | None]:
    """Confere o trecho citado contra o edital. Devolve (situação, página)."""
    alvo = _normalizar(trecho)
    if len(alvo) < 15:
        return "nao_localizado", None
    for n, pagina in enumerate(paginas_norm, 1):
        if alvo in pagina:
            return "verificado", n
    # tolera pequenas diferenças (quebra de página no meio da frase, colunas, espaços)
    alvo_sh = _shingles(alvo)
    melhor, pagina_melhor = 0.0, None
    for n, conjunto in enumerate(shingles_pag, 1):
        taxa = len(alvo_sh & conjunto) / len(alvo_sh)
        if taxa > melhor:
            melhor, pagina_melhor = taxa, n
    if melhor >= 0.6:
        return "aproximado", pagina_melhor
    return "nao_localizado", None


_PALAVRAS_VAZIAS = {
    "de", "da", "do", "das", "dos", "o", "a", "os", "as", "e", "com", "para", "em", "no", "na",
    "nos", "nas", "ao", "aos", "ser", "deve", "que", "um", "uma", "por", "pelo", "pela", "se",
}


def _item_norm(valor) -> str | None:
    """Número do item, comparável entre a matriz e a tabela de itens.

    '01', '1', 'Item 1' -> '1'; com lote: '1.3', 'Lote 1 - Item 3' -> '1.3' (sem colidir
    com o item 1). Texto que não é só rótulo de item/lote ('32 aparelhos de ar') -> None.
    """
    texto = _limpar(valor)
    if not texto:
        return None
    t = _normalizar(texto)
    sobra = re.sub(r"\b(lotes?|grupos?|itens|item|n|no|nr|num|numero)\b", " ", t)
    if re.sub(r"[\d\s.,/]+", "", sobra):
        return None
    numeros = re.findall(r"\d+", t)
    if not numeros or len(numeros) > 3:
        return None
    return ".".join(str(int(n)) for n in numeros)


normalizar_item = _item_norm  # público: a tela casa as especificações com a tabela de itens


def _chave_comparacao(texto: str) -> str:
    """Tira da frase o que só muda a redação, para comparar o conteúdo."""
    t = _normalizar(texto)
    t = re.sub(r"[.,/]+(?=\s|$)", " ", t)  # pontuação de fim de palavra; mantém 12.000 e 0,20
    t = re.sub(r"\b(o |os )?(item|itens) \d+( e \d+)*\b", " ", t)
    t = re.sub(r"\b(deverao|devera|devem|deveram|devera ser|deverao ser)\b", "deve", t)
    return re.sub(r"\s+", " ", t).strip()


def _numeros(tokens) -> set:
    return {w for w in tokens if any(c.isdigit() for c in w)}


def _mesma_exigencia(a: str, b: str) -> bool:
    """Quase idênticas, ou uma contida na outra - nunca com números diferentes.

    A trava de números vale nos dois caminhos: frases quase iguais que diferem só
    no número ("prazo de 10 dias" x "prazo de 30 dias") são exigências distintas.
    """
    ta = {w for w in a.split() if w not in _PALAVRAS_VAZIAS}
    tb = {w for w in b.split() if w not in _PALAVRAS_VAZIAS}
    if SequenceMatcher(None, a, b).ratio() >= 0.85:
        return _numeros(ta) == _numeros(tb)
    menor, maior = (ta, tb) if len(ta) <= len(tb) else (tb, ta)
    if len(menor) < 3:
        return False
    if _numeros(menor) - maior:
        return False
    return len(menor & maior) / len(menor) >= 0.9


def _mesmo_rotulo(a: str, b: str) -> bool:
    """Para etiquetas curtas ("tensão 220V ou bivolt" x "220V ou bivolt, 60 Hz").

    Mais tolerante que _mesma_exigencia porque etiquetas têm 2-6 palavras; a
    trava de números continua: "1/4 e 3/8" nunca se funde com "1/4 e 1/2".
    """
    ta = {w for w in a.split() if w not in _PALAVRAS_VAZIAS}
    tb = {w for w in b.split() if w not in _PALAVRAS_VAZIAS}
    menor, maior = (ta, tb) if len(ta) <= len(tb) else (tb, ta)
    if len(menor) < 2:
        return menor == maior and bool(menor)
    if {w for w in menor if any(c.isdigit() for c in w)} - maior:
        return False
    return len(menor & maior) / len(menor) >= 0.75


def _deduplicar(exigencias: list[dict]) -> list[dict]:
    """Funde exigências repetidas da mesma categoria e item (edital, TR e minuta repetem)."""
    unicas: list[dict] = []
    for e in exigencias:
        chave = _chave_comparacao(e["exigencia"])
        par = next(
            (u for u in unicas
             if u["categoria"] == e["categoria"] and u["item"] == e["item"]
             and _mesma_exigencia(u["_chave"], chave)),
            None,
        )
        if par is None:
            unicas.append({**e, "_chave": chave, "clausulas": [c for c in [e["clausula"]] if c]})
            continue
        if e["clausula"] and e["clausula"] not in par["clausulas"]:
            par["clausulas"].append(e["clausula"])
        if len(e["exigencia"]) > len(par["exigencia"]):  # fica a versão mais detalhada
            par["exigencia"], par["_chave"] = e["exigencia"], chave
        if par["verificacao"] != "verificado" and e["verificacao"] == "verificado":
            par["trecho"], par["verificacao"], par["pagina"] = e["trecho"], "verificado", e["pagina"]
        if not par["consequencia"] and e["consequencia"]:
            par["consequencia"], par["grave"] = e["consequencia"], e["grave"]
        # na dúvida, específica: o que não for rotina não pode sumir do destaque
        par["padrao"] = par["padrao"] and e["padrao"]
        par["condicional"] = par["condicional"] and e["condicional"]
        par["documento"] = par["documento"] or e["documento"]
        par["curto"] = par["curto"] or e["curto"]
    for u in unicas:
        u.pop("_chave")
        u.pop("clausula", None)
    return unicas


def _limpar(valor) -> str | None:
    texto = str(valor).strip() if valor is not None else ""
    return texto if texto and texto.lower() not in ("null", "none", "-") else None


# Um bloco denso (ex.: tabela com as especificações de 55 itens) gera centenas de
# linhas e pode passar de 3 minutos - medido: 196s e 148 exigências num bloco de 23 mil
# caracteres. Repetir o mesmo pedido só repete o estouro; o bloco lento é DIVIDIDO e
# as metades são lidas em paralelo. Tudo dentro de um orçamento de tempo que cabe no
# teto de 15 min do Lambda (a leitura geral roda em paralelo a isto).
TIMEOUT_BLOCO = 240
ORCAMENTO_SEGUNDOS = 780
PROFUNDIDADE_MAX = 2


def _ler_bloco(bloco: list[tuple[int, str]], prazo_final: float, profundidade: int = 0):
    """Lê um bloco; se estourar o tempo ou falhar, divide e tenta as metades.

    Devolve (lista de respostas JSON, páginas que não puderam ser lidas).
    """
    restante = prazo_final - time.monotonic()
    paginas = sorted({n for n, _ in bloco})
    if restante < 60:
        return [], paginas
    try:
        resp = _chamar_json(
            SYSTEM_EXIGENCIAS, "Trecho do edital:\n\n" + _texto_bloco(bloco),
            timeout=min(TIMEOUT_BLOCO, restante - 15), retries=0, modelo=OPENAI_MODEL_EXTRACAO,
        )
        return [resp], []
    except Exception:  # noqa: BLE001 - timeout ou resposta inválida: divide e tenta de novo
        partes = _dividir(bloco) if profundidade < PROFUNDIDADE_MAX else None
        if not partes:
            return [], paginas
        with ThreadPoolExecutor(max_workers=len(partes)) as pool:
            resultados = list(pool.map(lambda p: _ler_bloco(p, prazo_final, profundidade + 1), partes))
        respostas = [r for rs, _ in resultados for r in rs]
        falhas = sorted({n for _, fs in resultados for n in fs})
        return respostas, falhas


_CAMPOS_COMPACTOS = ("categoria", "item", "exigencia", "curto", "marcas", "documento",
                     "clausula", "consequencia", "trecho")


def _linhas(resp: dict | None) -> list[dict]:
    """Converte a resposta compacta ({"e": [[...9 valores...]]}) em dicionários.

    O formato compacto existe por custo: sem repetir os nomes dos campos a cada linha,
    a IA gera bem menos texto. Aceita também o formato antigo ({"exigencias": [{...}]}).
    """
    resp = resp or {}
    saida = []
    for linha in resp.get("e") or []:
        if not isinstance(linha, list) or len(linha) < 3:
            continue
        e = dict(zip(_CAMPOS_COMPACTOS, linha + [None] * (len(_CAMPOS_COMPACTOS) - len(linha))))
        marcas = str(e.pop("marcas") or "").upper()
        e["padrao"], e["condicional"] = "R" in marcas, "C" in marcas
        saida.append(e)
    saida.extend(e for e in resp.get("exigencias") or [] if isinstance(e, dict))
    return saida


def extrair_exigencias(texto: str) -> tuple[list[dict], list[int]]:
    """Matriz exaustiva de exigências: leitura em blocos, dedupe e verificação.

    Devolve (exigências, páginas que não puderam ser lidas). Um trecho que falha não
    descarta o resto: as páginas dele ficam listadas para a tela avisar.
    """
    paginas = paginas_do_texto(texto[:LIMITE_MATRIZ])
    blocos = _blocos(paginas)
    prazo_final = time.monotonic() + ORCAMENTO_SEGUNDOS
    with ThreadPoolExecutor(max_workers=MAX_PARALELO) as pool:
        resultados = list(pool.map(lambda b: _ler_bloco(b, prazo_final), blocos))
    respostas = [r for rs, _ in resultados for r in rs]
    paginas_falhas = sorted({n for _, fs in resultados for n in fs})
    if not respostas:
        raise RuntimeError("nenhum trecho do edital pôde ser lido pela IA")

    paginas_norm = [_normalizar(p) for p in paginas]
    shingles_pag = [_shingles(p) for p in paginas_norm]

    brutas = []
    for resp in respostas:
        for e in _linhas(resp):
            if not isinstance(e, dict) or not _limpar(e.get("exigencia")):
                continue
            situacao, pagina = _localizar(e.get("trecho") or "", paginas_norm, shingles_pag)
            consequencia = _limpar(e.get("consequencia"))
            brutas.append({
                "categoria": e.get("categoria") if e.get("categoria") in CATEGORIAS else "outras",
                "item": _item_norm(e.get("item")),
                "exigencia": _limpar(e.get("exigencia")),
                "curto": _limpar(e.get("curto")),
                # só é rotina se a IA disse true explicitamente; ausente ou ambíguo = específica
                "padrao": e.get("padrao") is True,
                "documento": _limpar(e.get("documento")),
                "condicional": e.get("condicional") is True,
                "clausula": _limpar(e.get("clausula")),
                "consequencia": consequencia,
                "grave": bool(consequencia and any(g in _normalizar(consequencia) for g in CONSEQUENCIAS_GRAVES)),
                "trecho": _limpar(e.get("trecho")),
                "verificacao": situacao,
                "pagina": pagina,
            })

    ordem = list(CATEGORIAS)
    unicas = _deduplicar(brutas)
    unicas.sort(key=lambda e: (ordem.index(e["categoria"]), (e["item"] or "").zfill(4), e["pagina"] or 9999))
    revisar_destaques(unicas)
    return unicas, paginas_falhas


SYSTEM_DESTAQUES = """Você é analista de licitações de uma distribuidora que disputa muitos \
pregões eletrônicos da Lei 14.133. Abaixo, exigências extraídas de UM edital, candidatas ao quadro \
"O que este edital pede de diferente" - o que o licitante precisa preparar ou cuidar ESPECIALMENTE \
neste edital, além da rotina.

Mantenha SÓ o que um licitante experiente não encontraria em qualquer pregão: documento incomum, \
índice contábil, capital ou patrimônio mínimo, atestado com quantitativo, amostra, certificação, \
visita, garantia, prazo ou percentual fora do usual, condição particular deste órgão.

Descarte a rotina, mesmo que traga número ou prazo: regras do sistema eletrônico e do SICAF \
(prazos de cadastro, envio de documentos ou readequação em 2 horas), declarações-padrão \
(trabalho infantil, condenação trabalhista, custos trabalhistas, ME/EPP), documentos de praxe \
(contrato social, documentos dos administradores, certidões fiscais e trabalhistas usuais), \
critério legal de inexequibilidade, validade de proposta de até 90 dias, regras tributárias \
gerais (alíquotas, média de tributos recolhidos, EFD, desoneração), data e hora da sessão, valor \
máximo estimado da contratação (já aparece na tabela de itens), e frases sem conteúdo próprio \
("cumprir o prazo assinalado", "observar o item X").
Na dúvida, descarte: o quadro só tem valor se for curto. Um edital de modelo padrão pode não ter \
nenhum destaque.

Devolva APENAS JSON: {"manter": [números das exigências mantidas]} - lista vazia se nenhuma."""


def _numero_da_regra(texto: str) -> bool:
    """Número que é da regra (prazo, valor, índice), e não referência a cláusula ("item 3.4")."""
    ref = r"(n[º°o.]*\s*)?[\dIVX][\d./\-ºª]*"
    sem_referencias = re.sub(
        r"(\b(ite[mn]s?|subite[mn]s?|cl[aá]usulas?|se[cç][aã]o|anexo|art(igo)?s?\.?|incisos?|al[ií]neas?|"
        r"par[aá]grafo|lei|decreto|in|instru[cç][aã]o normativa)|§+)\s*" + ref
        + r"((\s*,\s*|\s+(e|a|ou)\s+)" + ref + r")*",  # listas: "itens 5.4, 5.5 e 5.6"
        " ", texto, flags=re.IGNORECASE)
    sem_referencias = re.sub(r",?\s+de\s+(19|20)\d\d\b", " ", sem_referencias)  # "Lei ..., de 2021"
    return _tem_numero(sem_referencias)


def _candidato_destaque(e: dict) -> bool:
    """Específica, eliminatória e CONCRETA: pede um documento ou traz um número da regra.

    Regras genéricas que a IA às vezes marca como específicas ("obedecer às
    especificações", "preço dentro do máximo") ficam na lista completa.
    """
    documento = e.get("documento")
    if documento and _DOC_GENERICO.match(_normalizar(documento)):
        documento = None
    return (not e.get("padrao") and e["grave"] and not e.get("condicional")
            # produto tem quadro próprio; sanções são consequência prevista em lei, não exigência
            and e["categoria"] not in ("especificacao_produto", "sancoes")
            and bool(documento or _numero_da_regra(e["exigencia"])))


def revisar_destaques(exigencias: list[dict]) -> None:
    """Segunda opinião, no modelo principal, sobre o que é destaque.

    A extração roda no modelo barato, que marca como "específica" quase tudo
    (606 de 705 no IFFar). Revisar só os candidatos custa ~1 centavo: poucas
    dezenas de linhas curtas, resposta de alguns números. Se a revisão falhar,
    nada é marcado e a tela usa só o filtro do código.
    """
    candidatos = [e for e in exigencias if _candidato_destaque(e)]
    if not candidatos:
        return
    linhas = "\n".join(
        f"{i}. [{CATEGORIAS[e['categoria']]}] {e['exigencia']}"
        + (f" (documento: {e['documento']})" if e.get("documento") else "")
        for i, e in enumerate(candidatos, 1)
    )
    try:
        resp = _chamar_json(SYSTEM_DESTAQUES, linhas, timeout=120, retries=1, modelo=OPENAI_MODEL)
        manter = {int(n) for n in (resp or {}).get("manter") or [] if str(n).isdigit()}
    except Exception:
        return
    for i, e in enumerate(candidatos, 1):
        e["destaque"] = i in manter


CATEGORIAS_EXECUCAO = ("entrega_execucao", "recebimento_pagamento", "garantia_assistencia", "sancoes")
FASE_DOCUMENTO = {"proposta": "com a proposta", "participacao": "credenciamento"}


# "Documentos de habilitação", "Documentos originais ou cópias autenticadas": nomes
# genéricos que não dizem QUAL documento - os específicos já aparecem cada um na sua linha.
_DOC_GENERICO = re.compile(
    r"^((os )?documentos?( de habilitacao| originais( ou copias autenticadas)?| exigidos"
    r"| previstos( no edital| no termo de referencia)?| complementares)?"
    r"|declarac(ao|oes)|certid(ao|oes)|comprovante|atestado)$"
)


def _agrupar_documentos(exigencias: list[dict]) -> list[dict]:
    """Uma linha por documento pedido, juntando as cláusulas que falam dele.

    Cada documento traz as exigências específicas sobre ele (ex.: certidão de
    falência "emitida há no máximo 30 dias"); as de rotina só contam.
    """
    docs: list[dict] = []
    for e in exigencias:
        if not e.get("documento") or _DOC_GENERICO.match(_normalizar(e["documento"])):
            continue
        chave = _chave_comparacao(e["documento"])
        doc = next((d for d in docs if SequenceMatcher(None, d["_chave"], chave).ratio() >= 0.8), None)
        if doc is None:
            doc = {"_chave": chave, "nome": e["documento"], "especifico": False, "eliminatorio": False,
                   "condicional": True, "fase": FASE_DOCUMENTO.get(e["categoria"], "habilitação"),
                   "detalhes": [], "clausulas": [], "paginas": []}
            docs.append(doc)
        doc["eliminatorio"] = doc["eliminatorio"] or e["grave"]
        # basta uma menção que valha para todos para o documento não ser condicional
        doc["condicional"] = doc["condicional"] and e.get("condicional", False)
        if not e.get("padrao"):
            doc["especifico"] = True
            doc["detalhes"].append(e)
        for c in e.get("clausulas") or []:
            if c not in doc["clausulas"]:
                doc["clausulas"].append(c)
        if e.get("pagina") and e["pagina"] not in doc["paginas"]:
            doc["paginas"].append(e["pagina"])
    for d in docs:
        d.pop("_chave")
        d["paginas"].sort()
    ordem_fase = {"credenciamento": 0, "com a proposta": 1, "habilitação": 2}
    docs.sort(key=lambda d: (ordem_fase[d["fase"]], not d["especifico"], d["nome"].lower()))
    return docs


# Cardinais como palavra exata; ordinais e "um/uma" só junto de unidade de tempo
# ("terceiro dia útil", "um ano") - "segundo o edital" e "uma empresa" não são números.
_CARDINAIS = re.compile(
    r"\b(dois|duas|tres|quatro|cinco|seis|sete|oito|nove|dez|onze|doze|treze|quatorze|catorze|"
    r"quinze|dezesseis|dezessete|dezoito|dezenove|vinte|trinta|quarenta|cinquenta|sessenta|"
    r"setenta|oitenta|noventa|cem|cento|duzentos|trezentos|mil)\b"
)
_ORDINAIS_TEMPO = re.compile(
    r"\b(um|uma|primeir[oa]|segund[oa]|terceir[oa]|quart[oa]|quint[oa]|sext[oa]|setim[oa]|"
    r"oitav[oa]|non[oa]|decim[oa]) (dia|dias|mes|meses|ano|anos|hora|horas|exercicio|"
    r"semestre|quadrimestre|bimestre)\b"
)


def _tem_numero(texto: str) -> bool:
    """Número em algarismo ou por extenso ("terceiro dia útil", "cinco dias")."""
    if re.search(r"\d", texto):
        return True
    t = _normalizar(texto)
    return bool(_CARDINAIS.search(t) or _ORDINAIS_TEMPO.search(t))


def _agrupar_destaques(destaques: list[dict]) -> list[dict]:
    """Uma linha por documento (o balanço tem 4-5 exigências; viram uma linha com detalhes).

    Sem documento, a mesma regra repetida por item ("máximo unitário R$ 2.366,82",
    "máximo unitário R$ 3.415,55"...) vira uma linha só, com os valores dentro.
    """
    linhas: list[dict] = []
    for e in destaques:
        documento = e.get("documento")
        if documento and _DOC_GENERICO.match(_normalizar(documento)):
            documento = None  # "Documentos de habilitação" não diz qual documento
        if documento:
            chave, titulo = "doc " + _chave_comparacao(documento), documento
        else:
            # molde da regra: o rótulo curto sem números e valores
            molde = re.sub(r"\s+", " ", re.sub(r"(r\$\s*)?\d[\d.,/%]*", " ", (e.get("curto") or "").lower())).strip()
            chave = ("regra " + _chave_comparacao(molde)) if len(molde) >= 6 else None
            titulo = e.get("curto") or e["exigencia"]
        linha = next((l for l in linhas if chave and l["_chave"] and (
            l["_chave"] == chave or (chave.startswith("doc ") and l["_chave"].startswith("doc ")
                                     and SequenceMatcher(None, l["_chave"], chave).ratio() >= 0.8))), None)
        if linha is None:
            linha = {"_chave": chave, "titulo": titulo, "molde": molde if not documento else None,
                     "categoria": e["categoria"], "exigencias": [], "clausulas": [], "paginas": []}
            linhas.append(linha)
        linha["exigencias"].append(e)
        for c in e.get("clausulas") or []:
            if c not in linha["clausulas"]:
                linha["clausulas"].append(c)
        if e.get("pagina") and e["pagina"] not in linha["paginas"]:
            linha["paginas"].append(e["pagina"])
    for l in linhas:
        l.pop("_chave")
        l["paginas"].sort()
        molde = l.pop("molde")
        if molde and len(l["exigencias"]) > 1:  # regra repetida: título sem o valor de cada um
            itens = sum(1 for x in l["exigencias"] if x.get("item"))
            sufixo = f" ({itens} itens)" if itens > 1 else f" ({len(l['exigencias'])}x)"
            l["titulo"] = molde[:1].upper() + molde[1:].rstrip(" -:") + sufixo
    return linhas


_UNIDADES = {"dia", "dias", "util", "uteis", "corridos", "hora", "horas", "mes", "meses", "ano", "anos",
              "prazo", "ate", "minimo", "minima", "maximo", "maxima", "apos", "contados", "partir"}


def _agrupar_execucao(exigencias: list[dict]) -> list[dict]:
    """Só o que tem número (prazo, percentual, valor), uma linha por regra.

    Obrigações genéricas de contrato ("manter sigilo", "cumprir a legislação")
    ficam na lista completa. A mesma regra repetida - no edital, no TR e na
    minuta, ou uma vez por item ("Item 1: garantia de 12 meses"...) - vira uma
    linha, com os itens e as cláusulas juntos.
    """
    linhas: list[dict] = []
    for e in exigencias:
        if not _numero_da_regra(e["exigencia"]):
            continue
        tokens = set(_chave_comparacao(e.get("curto") or e["exigencia"]).split()) - _PALAVRAS_VAZIAS
        numeros = _numeros(tokens)
        termos = tokens - numeros - _UNIDADES
        linha = next((l for l in linhas if l["categoria"] == e["categoria"] and l["_numeros"] == numeros
                      and termos and l["_termos"] and (termos <= l["_termos"] or l["_termos"] <= termos)), None)
        if linha is None:
            texto = re.sub(r"^\s*item \d+[.\d]*\s*[:-]\s*", "", e["exigencia"], flags=re.IGNORECASE)
            linha = {"_numeros": numeros, "_termos": termos, "categoria": e["categoria"],
                     "exigencia": texto[:1].upper() + texto[1:], "itens": [], "clausulas": [], "paginas": []}
            linhas.append(linha)
        if e.get("item") and e["item"] not in linha["itens"]:
            linha["itens"].append(e["item"])
        for c in e.get("clausulas") or []:
            if c not in linha["clausulas"]:
                linha["clausulas"].append(c)
        if e.get("pagina") and e["pagina"] not in linha["paginas"]:
            linha["paginas"].append(e["pagina"])
    for l in linhas:
        del l["_numeros"], l["_termos"]
        l["paginas"].sort()
    return linhas


def resumir_exigencias(exigencias: list[dict] | None, itens_validos: set | None = None) -> dict | None:
    """Organiza a matriz em visões de uso, sem descartar nada.

    - destaques: o que este edital pede de diferente E elimina se descumprido;
    - documentos: uma linha por documento a apresentar;
    - produto: especificações técnicas agrupadas por item;
    - execucao: prazos, pagamento, garantia e multas próprios deste edital;
    - grupos: a lista completa por categoria (registro auditável).
    Análises anteriores à marcação padrão/específica não têm destaques.
    """
    if not exigencias:
        return None
    por_cat: dict[str, list] = {}
    for e in exigencias:
        por_cat.setdefault(e["categoria"], []).append(e)

    classificado = any("padrao" in e for e in exigencias)
    especificas = [e for e in exigencias if classificado and not e.get("padrao")]
    # Destaque = candidato pelo filtro do código e, quando a revisão no modelo
    # principal rodou (campo "destaque"), confirmado por ela.
    revisado = any("destaque" in e for e in exigencias)
    destaques = _agrupar_destaques([
        e for e in especificas
        if _candidato_destaque(e) and (e.get("destaque") or not revisado)
    ])
    documentos = _agrupar_documentos(exigencias)

    produto: dict[str, list] = {}
    vistos: dict[str, set] = {}
    for e in por_cat.get("especificacao_produto", []):
        # item que não existe na tabela (a IA leu "32 aparelhos" como item 32) vale para todos
        item = e["item"] or ""
        if itens_validos and item and item not in itens_validos:
            item = ""
        # etiqueta repetida no mesmo item ("tecnologia inverter" 2x, "selo Procel A" x
        # "classificação energética A Procel") não aparece de novo
        rotulo = _chave_comparacao(e.get("curto") or e["exigencia"])
        ja_vistos = vistos.setdefault(item, set())
        if rotulo in ja_vistos or any(_mesmo_rotulo(rotulo, v) for v in ja_vistos):
            continue
        ja_vistos.add(rotulo)
        produto.setdefault(item, []).append(e)
    produto_lista = [
        {"item": item or None, "exigencias": produto[item]}
        for item in sorted(produto, key=lambda i: (i != "", i.zfill(4)))
    ]

    return {
        "total": len(exigencias),
        "graves": sum(1 for e in exigencias if e["grave"]),
        "nao_localizadas": sum(1 for e in exigencias if e["verificacao"] == "nao_localizado"),
        "classificado": classificado,
        "especificas": len(especificas),
        "padrao": len(exigencias) - len(especificas) if classificado else None,
        "destaques": destaques,
        "documentos": [d for d in documentos if not d["condicional"]],
        "documentos_condicionais": [d for d in documentos if d["condicional"]],
        "produto": produto_lista,
        "execucao": _agrupar_execucao([e for e in especificas if e["categoria"] in CATEGORIAS_EXECUCAO]),
        "grupos": [
            {"chave": c, "rotulo": CATEGORIAS[c], "exigencias": por_cat[c]}
            for c in CATEGORIAS if c in por_cat
        ],
    }
