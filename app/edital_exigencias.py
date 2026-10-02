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
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from difflib import SequenceMatcher

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

TAMANHO_BLOCO = 24_000  # caracteres por chamada; blocos menores = leitura mais atenta
MAX_PARALELO = 6
# Lida em blocos, a matriz aceita bem mais texto que a leitura geral (uma chamada só,
# limitada pelo contexto do modelo). 800 mil caracteres ~ 250 páginas ~ 34 blocos,
# ~7 min no pior caso - dentro do teto de 15 min do Lambda. Acima disso, corta e AVISA.
LIMITE_MATRIZ = 800_000

SYSTEM_EXIGENCIAS = """Você recebe UM TRECHO de um edital de licitação (as páginas vêm marcadas \
com "=== página N ==="). Liste TODAS as exigências, obrigações, condições, prazos, especificações \
técnicas e penalidades que o trecho impõe ao licitante ou ao contratado.

Regras:
- Seja exaustivo: cada obrigação vira UMA linha própria. Não resuma, não junte várias exigências \
numa linha, não omita por parecer óbvio ou repetido. Na dúvida, inclua.
- Especificações técnicas de produto: uma linha por requisito, indicando o item do objeto \
(ex.: item 2, "serpentina de cobre"; item 2, "gás refrigerante R32"; item 2, "vazão de ar de 860 m3/h").
- Inclua também regras de julgamento que afetam o licitante: preço máximo, critério de \
inexequibilidade (ex.: "abaixo de 50% do orçado"), motivos de desclassificação e de inabilitação.
- Ignore o que não impõe nada ao fornecedor: justificativas, fundamentos legais genéricos, \
definições, obrigações exclusivas da Administração.
- "exigencia": frase completa e autossuficiente, com os números, prazos e valores do texto.
- "trecho": cópia LITERAL de 40 a 250 caracteres do edital que sustenta a exigência, exatamente \
como está (mesmas palavras, sem corrigir nem reescrever).
- "curto": a exigência em até 8 palavras (ex.: "gás refrigerante R32", "balanço dos 2 últimos \
exercícios", "entrega em 10 dias úteis").
- "padrao": true se for exigência de rotina, presente em praticamente qualquer pregão da Lei \
14.133 (certidões fiscais e trabalhistas usuais, contrato social, declarações-padrão, regras gerais \
do sistema eletrônico, sanções transcritas da lei sem números próprios). false se for particular \
deste edital ou trouxer número, prazo, valor ou condição definidos por ele (especificações técnicas, \
prazos de entrega, índices contábeis, multas com percentuais próprios, catálogo, amostra...). \
Na dúvida, false.
- "documento": só quando a exigência pede que o licitante APRESENTE um documento para participar \
- no credenciamento, junto com a proposta ou na habilitação. Use o nome curto e padronizado \
(ex.: "Balanço patrimonial", "Certidão negativa de falência", "Atestado de capacidade técnica", \
"Catálogo ou ficha técnica do produto", "Prova de regularidade com o FGTS"), sempre o mesmo nome \
para o mesmo documento. NÃO é documento: o que só se usa depois (nota fiscal, recurso, pedido de \
esclarecimento ou impugnação, ordem de fornecimento, manual ou certificado entregue com o produto) \
- nesses casos, null.
- "condicional": true SOMENTE se a exigência vale apenas para estes casos: cooperativa, MEI, \
empresário individual, sociedade simples, empresa estrangeira, consórcio, empresa em recuperação \
judicial, filial ou agência, ME/EPP que queira o benefício. O licitante típico é uma sociedade \
empresária limitada: o que se pede dela (contrato social, alterações, documentos dos \
administradores) e o que vale para todos (regularidade fiscal, trabalhista, FGTS, inscrições) é \
false.
- Use o hífen simples "-"; nunca o travessão longo.

Devolva APENAS JSON:
{"exigencias": [{"categoria": "__CATEGORIAS__", "item": "número do item do objeto ou null", \
"exigencia": "...", "curto": "...", "padrao": true/false, "documento": "... ou null", \
"condicional": true/false, \
"clausula": "numeração da cláusula ou null", \
"consequencia": "o que acontece se descumprir (desclassificação, inabilitação, multa...) ou null", \
"trecho": "..."}]}""".replace("__CATEGORIAS__", "|".join(CATEGORIAS))


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


def _blocos(paginas: list[str]) -> list[str]:
    blocos, atual, tamanho = [], [], 0
    for n, pagina in enumerate(paginas, 1):
        trecho = f"=== página {n} ===\n{pagina}"
        if atual and tamanho + len(trecho) > TAMANHO_BLOCO:
            blocos.append("\n".join(atual))
            atual, tamanho = [], 0
        atual.append(trecho)
        tamanho += len(trecho)
    if atual:
        blocos.append("\n".join(atual))
    return blocos


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


def _mesma_exigencia(a: str, b: str) -> bool:
    """Quase idênticas, ou uma contida na outra (mesmos números e termos)."""
    if SequenceMatcher(None, a, b).ratio() >= 0.85:
        return True
    ta = {w for w in a.split() if w not in _PALAVRAS_VAZIAS}
    tb = {w for w in b.split() if w not in _PALAVRAS_VAZIAS}
    menor, maior = (ta, tb) if len(ta) <= len(tb) else (tb, ta)
    if len(menor) < 3:
        return False
    # números diferentes nunca se fundem (10 dias x 30 dias, 12.000 x 18.000 BTU)
    if {w for w in menor if any(c.isdigit() for c in w)} - maior:
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


def extrair_exigencias(texto: str) -> list[dict]:
    """Matriz exaustiva de exigências: leitura em blocos, dedupe e verificação."""
    paginas = paginas_do_texto(texto[:LIMITE_MATRIZ])
    blocos = _blocos(paginas)
    with ThreadPoolExecutor(max_workers=MAX_PARALELO) as pool:
        respostas = list(pool.map(
            lambda b: _chamar_json(SYSTEM_EXIGENCIAS, "Trecho do edital:\n\n" + b), blocos
        ))

    paginas_norm = [_normalizar(p) for p in paginas]
    shingles_pag = [_shingles(p) for p in paginas_norm]

    brutas = []
    for resp in respostas:
        for e in (resp or {}).get("exigencias") or []:
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
    return unicas


CATEGORIAS_EXECUCAO = ("entrega_execucao", "recebimento_pagamento", "garantia_assistencia", "sancoes")
FASE_DOCUMENTO = {"proposta": "com a proposta", "participacao": "credenciamento"}


# "Documentos de habilitação", "Documentos originais ou cópias autenticadas": nomes
# genéricos que não dizem QUAL documento - os específicos já aparecem cada um na sua linha.
_DOC_GENERICO = re.compile(
    r"^(os )?documentos?( de habilitacao| originais( ou copias autenticadas)?| exigidos"
    r"| previstos( no edital| no termo de referencia)?| complementares)?$"
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

    Exigências sem documento ficam uma por linha.
    """
    linhas: list[dict] = []
    for e in destaques:
        chave = _chave_comparacao(e["documento"]) if e.get("documento") else None
        linha = next((l for l in linhas if chave and l["_chave"] and
                      SequenceMatcher(None, l["_chave"], chave).ratio() >= 0.8), None)
        if linha is None:
            linha = {"_chave": chave, "titulo": e["documento"] or e.get("curto") or e["exigencia"],
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
    # Destaque = específica, eliminatória e CONCRETA: pede um documento ou traz um
    # número (prazo, percentual, índice). Regras genéricas que a IA às vezes marca
    # como específicas ("obedecer às especificações", "preço dentro do máximo")
    # ficam na lista completa - o critério é aplicado aqui, não deixado à IA.
    destaques = _agrupar_destaques([
        e for e in especificas
        if e["grave"] and not e.get("condicional")
        # produto tem quadro próprio; sanções são consequência prevista em lei, não exigência
        and e["categoria"] not in ("especificacao_produto", "sancoes")
        and (e.get("documento") or _tem_numero(e["exigencia"]))
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
        "execucao": [e for e in especificas if e["categoria"] in CATEGORIAS_EXECUCAO],
        "grupos": [
            {"chave": c, "rotulo": CATEGORIAS[c], "exigencias": por_cat[c]}
            for c in CATEGORIAS if c in por_cat
        ],
    }
