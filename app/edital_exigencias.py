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

from .edital_ia import _chamar_json, _texto_limitado

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
MAX_PARALELO = 4

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
- Use o hífen simples "-"; nunca o travessão longo.

Devolva APENAS JSON:
{"exigencias": [{"categoria": "__CATEGORIAS__", "item": "número do item do objeto ou null", \
"exigencia": "...", "clausula": "numeração da cláusula ou null", \
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
    """'01', '1', 'item 1' -> '1'. Texto que não é número de item (ex.: '32 aparelhos...') -> None."""
    texto = _limpar(valor)
    if not texto or len(texto) > 12:
        return None
    m = re.search(r"\d+", texto)
    return str(int(m.group())) if m else None


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
    for u in unicas:
        u.pop("_chave")
        u.pop("clausula", None)
    return unicas


def _limpar(valor) -> str | None:
    texto = str(valor).strip() if valor is not None else ""
    return texto if texto and texto.lower() not in ("null", "none", "-") else None


def extrair_exigencias(texto: str) -> list[dict]:
    """Matriz exaustiva de exigências: leitura em blocos, dedupe e verificação."""
    paginas = paginas_do_texto(_texto_limitado(texto))
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


def resumir_exigencias(exigencias: list[dict] | None) -> dict | None:
    """Contagens e agrupamento por categoria, para a tela."""
    if not exigencias:
        return None
    por_cat: dict[str, list] = {}
    for e in exigencias:
        por_cat.setdefault(e["categoria"], []).append(e)
    return {
        "total": len(exigencias),
        "graves": sum(1 for e in exigencias if e["grave"]),
        "nao_localizadas": sum(1 for e in exigencias if e["verificacao"] == "nao_localizado"),
        "grupos": [
            {"chave": c, "rotulo": CATEGORIAS[c], "exigencias": por_cat[c]}
            for c in CATEGORIAS if c in por_cat
        ],
    }
