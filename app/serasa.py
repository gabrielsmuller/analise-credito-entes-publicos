"""Relatório Serasa (formato SCC Check): extração de texto do PDF.

A leitura dos números é feita pela IA (ver dossier.extrair_serasa), que tolera as
variações de layout melhor que um parser de regex. Aqui só transformamos o PDF em
texto e fazemos uma checagem leve de que o arquivo parece mesmo um relatório de
crédito - o cálculo do score continua determinístico, sobre os campos extraídos.
"""
from io import BytesIO

# Marcadores esperados no relatório SCC Check / Serasa. Usados só para recusar,
# cedo, um PDF que claramente não é um relatório de crédito.
_MARCADORES = ("serasa", "scc check", "score", "cnpj", "restri", "pefin")


def extrair_texto_pdf(conteudo: bytes) -> str:
    """Extrai o texto do PDF. Levanta ValueError se o arquivo não for um PDF válido."""
    from pdfminer.high_level import extract_text  # import tardio: dependência pesada

    try:
        texto = extract_text(BytesIO(conteudo)) or ""
    except Exception as exc:  # noqa: BLE001 - PDF corrompido/criptografado
        raise ValueError(f"não foi possível ler o PDF: {type(exc).__name__}") from exc
    if not texto.strip():
        raise ValueError(
            "o PDF não tem texto selecionável (pode ser digitalizado como imagem) - "
            "exporte o relatório do Serasa/SCC Check em PDF de texto, não escaneado"
        )
    return texto


def parece_relatorio_serasa(texto: str) -> bool:
    """Heurística leve: o texto tem cara de relatório de crédito?"""
    baixo = texto.lower()
    return sum(m in baixo for m in _MARCADORES) >= 3


def _digitos(valor) -> str | None:
    d = "".join(ch for ch in str(valor or "") if ch.isdigit())
    return d or None


def normalizar_serasa(d: dict) -> dict:
    """Ajustes determinísticos sobre o que a IA extraiu, sem inventar dados.

    A IA transcreve; aqui só normalizamos o CNPJ (só dígitos) e calculamos o total
    das restrições quando o modelo não o somou - conta trivial que não convém
    delegar ao modelo.
    """
    d = dict(d or {})
    if d.get("cnpj"):
        d["cnpj"] = _digitos(d["cnpj"])
    r = d.get("restricoes") or {}
    if not d.get("restricoes_valor_total"):
        total = sum((v.get("valor") or 0) for v in r.values() if isinstance(v, dict) and v.get("consta"))
        d["restricoes_valor_total"] = round(total, 2) if total else None
    return d
