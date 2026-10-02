"""App web interno de análise de crédito de entes públicos."""
import asyncio
import json
import os
import uuid
from pathlib import Path

import markdown as md
from fastapi import Body, FastAPI, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import db
from .auth import AutenticacaoBasica
from .collectors.base import com_teto, nao_aplicavel, novo_client
from .collectors.capag import coletar_capag
from .collectors.cnpj import coletar_cnpj
from .collectors.entes import buscar as buscar_entes, coletar_ente, resolver_cnpj
from .collectors.ibge import buscar_municipios, coletar_contexto, listar_municipios
from .collectors.pagamentos import coletar_pagamentos
from .collectors.pncp import coletar_pncp
from .collectors.siconfi import coletar_siconfi
from .collectors.transparencia import coletar_convenios, coletar_transferencias, coletar_transparencia
from .dossier import extrair_serasa, gerar_dossie, responder_pergunta
from .edital_exigencias import (
    CATEGORIAS, LIMITE_MATRIZ, extrair_exigencias, normalizar_item, resumir_exigencias,
)
from .edital_ia import (
    MAX_CARACTERES, analisar_edital, prazos_edital, responder_pergunta_edital, situacao_prazo,
)
from .scoring import calcular_scorecard
from .serasa import extrair_texto_pdf, normalizar_serasa, parece_relatorio_serasa

app = FastAPI(title="Análise de Crédito - Entes Públicos")
app.add_middleware(AutenticacaoBasica)
app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")
templates = Jinja2Templates(directory=Path(__file__).parent / "templates")


def _br(valor: float, casas: int = 2) -> str:
    """Formata número no padrão brasileiro (1.234.567,89)."""
    return f"{valor:,.{casas}f}".replace(",", "§").replace(".", ",").replace("§", ".")


def _num(valor) -> float | None:
    """Converte para float ou devolve None.

    Tolera Undefined do Jinja e strings: análises antigas no histórico não têm
    os campos adicionados depois, e a página precisa continuar renderizando.
    """
    try:
        return float(valor)
    except Exception:  # noqa: BLE001 - Undefined levanta UndefinedError, não TypeError
        return None


def filtro_moeda(valor) -> str:
    v = _num(valor)
    return f"R$ {_br(v)}" if v is not None else "-"


def filtro_moeda_curta(valor) -> str:
    """Valores grandes em escala legível: R$ 2,65 bi."""
    v = _num(valor)
    if v is None:
        return "-"
    for limite, sufixo in ((1e9, "bi"), (1e6, "mi"), (1e3, "mil")):
        if abs(v) >= limite:
            return f"R$ {_br(v / limite, 2)} {sufixo}"
    return f"R$ {_br(v)}"


def filtro_numero(valor, casas: int = 0) -> str:
    v = _num(valor)
    return _br(v, casas) if v is not None else "-"


def filtro_pct(valor, casas: int = 1) -> str:
    v = _num(valor)
    return f"{_br(v, casas)}%" if v is not None else "-"


templates.env.filters["moeda"] = filtro_moeda
templates.env.filters["moeda_curta"] = filtro_moeda_curta
templates.env.filters["numero"] = filtro_numero
templates.env.filters["pct"] = filtro_pct


def filtro_data_br(valor) -> str:
    """AAAA-MM-DD (ou ISO com hora) -> DD/MM/AAAA; devolve o original se não reconhecer."""
    texto = str(valor or "")
    if len(texto) >= 10 and texto[4] == "-" and texto[7] == "-":
        return f"{texto[8:10]}/{texto[5:7]}/{texto[:4]}"
    return texto or "-"


templates.env.filters["data_br"] = filtro_data_br


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse(request, "index.html", {})


@app.get("/api/municipios")
async def api_municipios(q: str = ""):
    """Autocomplete a partir do registro do Tesouro, que já traz o CNPJ."""
    if len(q.strip()) < 2:
        return []
    async with novo_client() as client:
        try:
            return await buscar_entes(client, q.strip())
        except Exception:  # noqa: BLE001 - registro indisponível: cai para o IBGE, sem CNPJ
            return await buscar_municipios(client, q.strip())


# Fontes indexadas por município que não existem para um ente avaliado só por
# CNPJ (autarquia, universidade, consórcio, fundação, empresa pública): esses
# entes não entregam demonstrativos fiscais próprios ao SICONFI nem têm CAPAG.
_FONTES_MUNICIPAIS = [
    ("ente", "registro de municípios do SICONFI"),
    ("ibge", "população e PIB municipal do IBGE"),
    ("capag", "CAPAG do Tesouro Nacional (só entes federativos)"),
    ("siconfi", "RGF/RREO do SICONFI (só entes federativos)"),
    ("pagamentos", "caixa e restos a pagar do SICONFI"),
    ("convenios", "convênios federais por código de município"),
]


async def _extrair_serasa_seguro(texto: str) -> dict | None:
    """Extrai o relatório Serasa por IA; devolve um resultado no padrão dos coletores.

    None quando não há PDF. Falha de extração não derruba a análise: vira uma fonte
    "indisponível", e o scorecard trata como se o relatório não tivesse sido lido.
    """
    if not (texto or "").strip():
        return None
    try:
        dados = normalizar_serasa(await asyncio.to_thread(extrair_serasa, texto))
        return {"fonte": "serasa", "ok": True, "dados": dados, "texto": texto}
    except Exception as exc:  # noqa: BLE001 - extração falhou: registra e segue
        return {"fonte": "serasa", "ok": False, "dados": None, "texto": texto,
                "erro": f"falha ao extrair o relatório Serasa: {type(exc).__name__}: {exc}"}


async def executar_analise(
    tipo_ente: str,
    cod_ibge: str,
    municipio: str,
    uf: str,
    tipo_orgao: str,
    serasa_texto: str,
    serasa: str,
    cauc: str,
    cauc_situacao: str,
) -> tuple[int, str | None]:
    """Coleta, pontua e redige o dossiê. Devolve (id_da_análise, erro_do_dossiê).

    Dois modos, conforme tipo_ente:
      - "municipio": CNPJ resolvido pelo registro do Tesouro e todo o pipeline
        fiscal do SICONFI (comportamento igual ao histórico). O relatório Serasa
        é opcional aqui.
      - "cnpj": ente identificado pelo próprio relatório Serasa (autarquia,
        universidade, consórcio...). A IA extrai nome e CNPJ do PDF; só rodam as
        fontes indexadas por CNPJ, e o relatório Serasa é o eixo do score.

    É o miolo pesado da análise (2-3 min). Roda em segundo plano - ver
    disparar_analise -, então nunca é chamado diretamente por uma requisição
    HTTP que o navegador precise segurar.
    """
    serasa_res = await _extrair_serasa_seguro(serasa_texto)
    serasa_dados = (serasa_res or {}).get("dados") or {}
    async with novo_client() as client:
        if tipo_ente == "cnpj":
            cod_ibge = None
            # nome e CNPJ vêm do próprio relatório Serasa, extraídos pela IA
            cnpj = serasa_dados.get("cnpj") or None
            municipio = (serasa_dados.get("razao_social") or municipio or "").strip() or "(ente sem nome)"
            # só as fontes que se resolvem por CNPJ
            resultados = await asyncio.gather(
                com_teto("pncp", coletar_pncp(client, cnpj)),
                com_teto("cnpj", coletar_cnpj(client, cnpj)),
                com_teto("transparencia", coletar_transparencia(client, cnpj)),
                com_teto("transferencias", coletar_transferencias(client, cnpj)),
            )
            dados = {r["fonte"]: r for r in resultados}
            for fonte, motivo in _FONTES_MUNICIPAIS:
                dados[fonte] = nao_aplicavel(fonte, f"não se aplica a este ente ({motivo})")
            # o cadastro da Receita completa a UF
            cad = dados.get("cnpj", {})
            if cad.get("ok") and not (uf or "").strip():
                uf = cad["dados"].get("uf") or ""
        else:
            # o CNPJ vem do registro oficial do Tesouro, não digitado: um CNPJ
            # errado não falharia, apenas traria dados de outra entidade
            try:
                cnpj = await asyncio.wait_for(resolver_cnpj(client, cod_ibge), timeout=60)
            except asyncio.TimeoutError:
                cnpj = None

            # cada coletor tem teto próprio: uma fonte pendurada vira "indisponível"
            # em vez de travar a análise inteira (ver com_teto em collectors/base.py)
            resultados = await asyncio.gather(
                com_teto("ente", coletar_ente(client, cod_ibge)),
                com_teto("ibge", coletar_contexto(client, cod_ibge)),
                com_teto("capag", coletar_capag(client, cod_ibge)),
                com_teto("siconfi", coletar_siconfi(client, cod_ibge)),
                com_teto("pagamentos", coletar_pagamentos(client, cod_ibge)),
                com_teto("pncp", coletar_pncp(client, cnpj)),
                com_teto("cnpj", coletar_cnpj(client, cnpj)),
                com_teto("transparencia", coletar_transparencia(client, cnpj)),
                com_teto("transferencias", coletar_transferencias(client, cnpj)),
                com_teto("convenios", coletar_convenios(client, cod_ibge, cnpj)),
            )
            dados = {r["fonte"]: r for r in resultados}

            # nome e UF vêm do registro do Tesouro, não do formulário: o servidor já
            # tem o dado canônico, e aceitar o do cliente abre espaço para divergência
            # de acentuação e para um rótulo que não corresponde ao ente analisado
            ente = dados.get("ente", {})
            if ente.get("ok"):
                municipio = ente["dados"]["nome"]
                uf = ente["dados"]["uf"]

    # marcadores lidos pelo scorecard, pelo dossiê e pelo template
    dados["tipo_ente"] = tipo_ente
    if serasa_res is not None:
        dados["serasa"] = serasa_res
    if tipo_ente == "cnpj" and (tipo_orgao or "").strip():
        dados["tipo_orgao"] = {
            "fonte": "tipo_orgao", "ok": True, "dados": {"rotulo": tipo_orgao.strip()}
        }

    observacoes = {
        "serasa": serasa.strip() or None,
        "cauc": cauc.strip() or None,
        "cauc_situacao": cauc_situacao,
    }
    # guardadas junto dos dados para aparecerem no dossiê e no histórico, e para
    # o scorecard aplicar a restrição do CAUC. Precisa entrar ANTES de
    # calcular_scorecard, que lê essas observações.
    dados["observacoes_manuais"] = {"fonte": "observacoes_manuais", "ok": True, "dados": observacoes}

    scorecard = calcular_scorecard(dados, tipo_ente=tipo_ente)

    try:
        dossie_md = await asyncio.to_thread(
            gerar_dossie, municipio, uf, dados, scorecard, observacoes, tipo_ente
        )
        erro_dossie = None
    except Exception as exc:  # noqa: BLE001 - dossiê indisponível não descarta a coleta
        dossie_md = None
        erro_dossie = f"{type(exc).__name__}: {exc}"

    analise_id = db.salvar_analise(municipio, uf, cod_ibge, cnpj, dados, scorecard, dossie_md)
    return analise_id, erro_dossie


async def _processar_job(job_id: str, params: dict) -> None:
    """Roda a análise e grava o resultado no job (para o polling ler)."""
    try:
        analise_id, erro_dossie = await executar_analise(**params)
        db.atualizar_job(job_id, "pronto", analise_id=analise_id, erro_dossie=erro_dossie)
    except Exception as exc:  # noqa: BLE001 - falha vira status de erro, não derruba nada
        db.atualizar_job(job_id, "erro", erro=f"{type(exc).__name__}: {exc}")


def _disparar(evento: dict) -> str:
    """Cria o job e inicia o processamento em segundo plano; devolve o job_id.

    - No Lambda: invoca a própria função de forma assíncrona (InvocationType
      Event). A requisição HTTP retorna na hora, sem segurar o navegador por
      minutos (o que a Function URL/API Gateway não permitiria). O payload de
      uma invocação assíncrona tem teto de 256 KB - por isso textos grandes
      (editais) vão para o banco antes, e o evento leva só o id.
    - Local: dispara uma task em background, para a experiência de polling ser
      idêntica à de produção.
    """
    job_id = uuid.uuid4().hex[:12]
    db.criar_job(job_id)
    evento = {**evento, "job_id": job_id}
    nome_funcao = os.getenv("AWS_LAMBDA_FUNCTION_NAME")
    if nome_funcao:
        import boto3

        boto3.client("lambda").invoke(
            FunctionName=nome_funcao,
            InvocationType="Event",
            Payload=json.dumps(evento).encode(),
        )
    else:
        asyncio.create_task(_executar_evento(evento))
    return job_id


async def disparar_analise(params: dict) -> str:
    return _disparar({"tipo": "analise", "params": params})


async def _processar_edital(job_id: str, edital_id: int) -> None:
    """Roda a leitura do edital pela IA e grava o resultado (para o polling ler)."""
    try:
        edital = db.buscar_edital(edital_id)
        if edital is None:
            raise ValueError(f"edital {edital_id} não encontrado")
        # leitura geral e matriz de exigências rodam em paralelo (chamadas independentes)
        geral, matriz = await asyncio.gather(
            asyncio.to_thread(analisar_edital, edital["texto"]),
            asyncio.to_thread(extrair_exigencias, edital["texto"]),
            return_exceptions=True,
        )
        if isinstance(geral, Exception):
            raise geral
        dados = geral
        if isinstance(matriz, Exception):
            # a matriz falhar não descarta a leitura geral: fica registrado na tela
            dados["exigencias_erro"] = f"{type(matriz).__name__}: {matriz}"
        else:
            dados["exigencias"] = matriz
        # edital longo demais é cortado - e isso precisa aparecer na tela, nunca em silêncio
        total = len(edital["texto"])
        if total > MAX_CARACTERES:
            dados["texto_cortado"] = {
                "total": total,
                "visao_geral": MAX_CARACTERES,
                "matriz": min(total, LIMITE_MATRIZ),
                "matriz_cortada": total > LIMITE_MATRIZ,
            }
        resumo_md = dados.pop("visao_geral_md", None)
        db.concluir_edital(edital_id, dados, resumo_md)
        db.atualizar_job(job_id, "pronto", analise_id=edital_id)
    except Exception as exc:  # noqa: BLE001 - falha vira status de erro, não derruba nada
        db.atualizar_job(job_id, "erro", erro=f"{type(exc).__name__}: {exc}")


async def _executar_evento(evento: dict) -> None:
    """Despacha o evento do worker para o processamento certo."""
    if evento.get("tipo") == "edital":
        await _processar_edital(evento["job_id"], int(evento["edital_id"]))
    else:
        await _processar_job(evento["job_id"], evento["params"])


def _erro_form(request: Request, mensagem: str) -> HTMLResponse:
    """Rejeita o formulário com uma mensagem legível, antes de gastar a análise.

    O cabeçalho X-Erro carrega o texto para o htmx exibir em #erro-analise (o
    corpo cobre a navegação normal, sem htmx).
    """
    return HTMLResponse(mensagem, status_code=400, headers={"X-Erro": mensagem})


# PDFs de relatório Serasa são pequenos (algumas centenas de KB). O teto evita
# que um upload grande demais entre no fluxo (e no payload da invocação async).
_MAX_PDF_BYTES = 8 * 1024 * 1024


async def _texto_do_pdf(request: Request, arquivo: UploadFile | None, obrigatorio: bool):
    """Lê o PDF enviado e devolve (texto, resposta_de_erro). Só um dos dois é não-nulo."""
    tem_arquivo = arquivo is not None and (arquivo.filename or "").strip()
    if not tem_arquivo:
        if obrigatorio:
            return None, _erro_form(request, "Anexe o PDF do relatório Serasa para analisar este ente.")
        return "", None
    conteudo = await arquivo.read()
    if len(conteudo) > _MAX_PDF_BYTES:
        return None, _erro_form(request, "O PDF é grande demais (máx. 8 MB).")
    try:
        texto = await asyncio.to_thread(extrair_texto_pdf, conteudo)
    except ValueError as exc:
        return None, _erro_form(request, str(exc))
    if not parece_relatorio_serasa(texto):
        return None, _erro_form(
            request, "O PDF não parece um relatório Serasa/SCC Check. Confira o arquivo."
        )
    return texto, None


@app.post("/analisar")
async def analisar(
    request: Request,
    tipo_ente: str = Form("municipio"),
    cod_ibge: str = Form(""),
    municipio: str = Form(""),
    uf: str = Form(""),
    tipo_orgao: str = Form(""),
    serasa: str = Form(""),
    cauc: str = Form(""),
    cauc_situacao: str = Form("nao_consultado"),
    relatorio_serasa: UploadFile | None = File(None),
):
    tipo_ente = "cnpj" if tipo_ente == "cnpj" else "municipio"
    # valida o mínimo de cada modo antes de gastar uma análise (2-3 min)
    if tipo_ente == "municipio" and not (cod_ibge or "").strip():
        return _erro_form(request, "Selecione um município na lista de sugestões.")

    # relatório Serasa: obrigatório no modo CNPJ, opcional no município
    serasa_texto, erro = await _texto_do_pdf(
        request, relatorio_serasa, obrigatorio=(tipo_ente == "cnpj")
    )
    if erro is not None:
        return erro

    params = {
        "tipo_ente": tipo_ente,
        "cod_ibge": cod_ibge,
        "municipio": municipio,
        "uf": uf,
        "tipo_orgao": tipo_orgao,
        "serasa_texto": serasa_texto,
        "serasa": serasa,
        "cauc": cauc,
        "cauc_situacao": cauc_situacao,
    }
    job_id = await disparar_analise(params)
    url = f"/processando/{job_id}"
    # htmx segue este cabeçalho; navegação normal usa o redirect
    if request.headers.get("HX-Request"):
        return HTMLResponse("", headers={"HX-Redirect": url})
    return RedirectResponse(url, status_code=303)


@app.get("/processando/{job_id}", response_class=HTMLResponse)
async def processando(request: Request, job_id: str, tipo: str = "analise"):
    job = db.buscar_job(job_id)
    if job is None:
        return HTMLResponse("Processo não encontrado", status_code=404)
    return templates.TemplateResponse(
        request, "processando.html", {"job_id": job_id, "tipo": "edital" if tipo == "edital" else "analise"}
    )


@app.get("/status/{job_id}")
async def status_job(job_id: str):
    job = db.buscar_job(job_id)
    if job is None:
        return JSONResponse({"erro": "não encontrado"}, status_code=404)
    return {
        "status": job.get("status"),
        "analise_id": job.get("analise_id"),
        "erro": job.get("erro"),
        "erro_dossie": job.get("erro_dossie"),
    }


@app.get("/dossie/{analise_id}", response_class=HTMLResponse)
async def dossie(request: Request, analise_id: int, erro_dossie: str = ""):
    analise = db.buscar_analise(analise_id)
    if analise is None:
        return HTMLResponse("Análise não encontrada", status_code=404)
    dossie_html = None
    if analise.get("dossie_md"):
        dossie_html = md.markdown(analise["dossie_md"], extensions=["tables"])
    return templates.TemplateResponse(
        request,
        "dossie.html",
        {
            "analise": analise,
            "dossie_html": dossie_html,
            "erro_dossie": erro_dossie,
            "mensagens": db.listar_mensagens(analise_id),
        },
    )


@app.post("/api/chat/{analise_id}")
async def api_chat(request: Request, analise_id: int, corpo: dict = Body(...)):
    """Conversa com a IA sobre uma análise já realizada.

    O histórico vem do banco, não do navegador: assim ele sobrevive a um
    refresh, fica visível para as duas pessoas que usam o sistema e não pode
    ser reescrito pelo cliente.
    """
    analise = db.buscar_analise(analise_id)
    if analise is None:
        return JSONResponse({"erro": "análise não encontrada"}, status_code=404)

    pergunta = (corpo.get("pergunta") or "").strip()
    if not pergunta:
        return JSONResponse({"erro": "pergunta vazia"}, status_code=400)

    autor = getattr(request.state, "usuario", None)
    historico = [
        {"role": m["papel"], "content": m["conteudo"]} for m in db.listar_mensagens(analise_id)
    ]
    historico.append({"role": "user", "content": pergunta})

    try:
        resposta = await asyncio.to_thread(
            responder_pergunta,
            analise["municipio"],
            analise["uf"],
            analise["dados"],
            analise["scorecard"],
            analise.get("dossie_md"),
            historico,
        )
    except Exception as exc:  # noqa: BLE001 - erro da IA não deve derrubar a página
        return JSONResponse({"erro": f"{type(exc).__name__}: {exc}"}, status_code=502)

    # só grava depois de a resposta chegar, para não deixar pergunta órfã
    db.salvar_mensagens(
        analise_id,
        [{"papel": "user", "conteudo": pergunta}, {"papel": "assistant", "conteudo": resposta}],
        autor=autor,
    )
    return {"resposta": resposta, "autor": autor}


# --- Editais (piloto) --------------------------------------------------------

_MARCADORES_EDITAL = ("edital", "licita", "objeto", "proposta", "habilita")


def _sem_acento(texto: str) -> str:
    import unicodedata

    return unicodedata.normalize("NFKD", texto or "").encode("ascii", "ignore").decode().lower().strip()


def _credito_do_orgao(dados: dict) -> dict | None:
    """Liga o edital à análise de crédito: acha a última análise do mesmo município.

    Só casa município (nome + UF). Para outros órgãos o CNPJ do edital nem sempre
    é o do ente pagador, então a tela apenas oferece a análise por CNPJ.
    """
    if (dados or {}).get("tipo_orgao") != "municipio" or not dados.get("municipio"):
        return None
    nome, uf = _sem_acento(dados["municipio"]), _sem_acento(dados.get("uf") or "")
    for a in db.listar_analises():  # já vem da mais recente para a mais antiga
        if _sem_acento(a.get("municipio")) == nome and (not uf or _sem_acento(a.get("uf")) == uf):
            return a
    return None


def _conferir_itens(dados: dict) -> dict | None:
    """Confere se a soma dos itens bate com o valor estimado total do edital."""
    itens = (dados or {}).get("itens") or []
    total = (dados or {}).get("valor_estimado_total")
    valores = [i.get("valor_total_ref") for i in itens]
    if not itens or total is None or any(v is None for v in valores):
        return None
    soma = round(sum(valores), 2)
    return {"soma": soma, "total": total, "confere": abs(soma - total) <= max(1.0, total * 0.001)}


@app.get("/editais", response_class=HTMLResponse)
async def editais(request: Request):
    lista = db.listar_editais()
    for e in lista:
        e["prazo"] = situacao_prazo((e.get("dados") or {}).get("data_abertura"))
    return templates.TemplateResponse(request, "editais.html", {"editais": lista})


# O navegador extrai o texto do PDF (pdf.js) e envia só o texto: o API Gateway ->
# Lambda aceita no máximo 6 MB por requisição (em base64), e editais com imagens
# passam fácil disso (o de Brotas de Macaúbas tem 7,3 MB e ~130 KB de texto).
# O upload do arquivo continua aceito para uso local e testes.
_MAX_TEXTO_EDITAL = 3_000_000


@app.post("/editais/analisar")
async def analisar_edital_upload(
    request: Request,
    texto: str = Form(""),
    arquivo_nome: str = Form(""),
    arquivo: UploadFile | None = File(None),
):
    if texto.strip():
        if len(texto) > _MAX_TEXTO_EDITAL:
            return _erro_form(request, "O edital é longo demais para análise (texto acima de 3 milhões de caracteres).")
        nome = (arquivo_nome or "edital.pdf").strip()[:200]
    elif arquivo is not None and (arquivo.filename or "").strip():
        conteudo = await arquivo.read()
        try:
            texto = await asyncio.to_thread(extrair_texto_pdf, conteudo)
        except ValueError as exc:
            return _erro_form(request, str(exc))
        nome = arquivo.filename
    else:
        return _erro_form(request, "Anexe o PDF do edital.")
    baixo = texto.lower()
    if sum(m in baixo for m in _MARCADORES_EDITAL) < 3:
        return _erro_form(request, "O PDF não parece um edital de licitação. Confira o arquivo.")

    # o texto vai para o banco já aqui; o worker recebe só o id (ver _disparar)
    edital_id = db.criar_edital(nome, texto)
    job_id = _disparar({"tipo": "edital", "edital_id": edital_id})
    url = f"/processando/{job_id}?tipo=edital"
    if request.headers.get("HX-Request"):
        return HTMLResponse("", headers={"HX-Redirect": url})
    return RedirectResponse(url, status_code=303)


@app.get("/edital/{edital_id}", response_class=HTMLResponse)
async def ver_edital(request: Request, edital_id: int):
    edital = db.buscar_edital(edital_id)
    if edital is None:
        return HTMLResponse("Edital não encontrado", status_code=404)
    dados = edital.get("dados") or {}
    resumo_html = md.markdown(edital["resumo_md"], extensions=["tables"]) if edital.get("resumo_md") else None

    # especificações do produto ficam junto da tabela de itens: casa o número do
    # item da matriz ("1") com o da tabela ("01"); número fora da tabela vale para todos
    chaves_tabela = set()
    for item in dados.get("itens") or []:
        item["chave_item"] = normalizar_item(item.get("numero"))
        chaves_tabela.add(item["chave_item"])
    matriz = resumir_exigencias(dados.get("exigencias"), itens_validos=chaves_tabela - {None})
    specs_gerais, specs_por_item = [], {}
    for grupo in (matriz or {}).get("produto", []):
        if grupo["item"] is None:
            specs_gerais = grupo["exigencias"]
        else:
            specs_por_item[grupo["item"]] = grupo["exigencias"]
    return templates.TemplateResponse(
        request,
        "edital.html",
        {
            "edital": edital,
            "e": dados,
            "resumo_html": resumo_html,
            "prazo": situacao_prazo(dados.get("data_abertura")),
            "prazos": prazos_edital(dados),
            # análises anteriores ao checklist fixo não o têm: a tela oferece reanalisar
            "checklist": dados.get("checklist") if isinstance(dados.get("checklist"), list) else None,
            "conferencia": _conferir_itens(dados),
            "matriz": matriz,
            "categorias": CATEGORIAS,
            "specs_gerais": specs_gerais,
            "specs_por_item": specs_por_item,
            "credito": _credito_do_orgao(dados),
            "mensagens": db.listar_mensagens_edital(edital_id),
        },
    )


@app.post("/edital/{edital_id}/reanalisar")
async def reanalisar_edital(request: Request, edital_id: int):
    """Refaz a leitura da IA sobre o texto já guardado (sem novo upload). O chat é mantido."""
    if db.buscar_edital(edital_id) is None:
        return HTMLResponse("Edital não encontrado", status_code=404)
    job_id = _disparar({"tipo": "edital", "edital_id": edital_id})
    return RedirectResponse(f"/processando/{job_id}?tipo=edital", status_code=303)


@app.post("/api/edital-chat/{edital_id}")
async def api_chat_edital(request: Request, edital_id: int, corpo: dict = Body(...)):
    """Conversa com a IA sobre o edital; histórico no banco, como no dossiê."""
    edital = db.buscar_edital(edital_id)
    if edital is None:
        return JSONResponse({"erro": "edital não encontrado"}, status_code=404)
    pergunta = (corpo.get("pergunta") or "").strip()
    if not pergunta:
        return JSONResponse({"erro": "pergunta vazia"}, status_code=400)

    autor = getattr(request.state, "usuario", None)
    historico = [
        {"role": m["papel"], "content": m["conteudo"]} for m in db.listar_mensagens_edital(edital_id)
    ]
    historico.append({"role": "user", "content": pergunta})
    try:
        resposta = await asyncio.to_thread(
            responder_pergunta_edital, edital["texto"], edital.get("dados"), historico
        )
    except Exception as exc:  # noqa: BLE001 - erro da IA não deve derrubar a página
        return JSONResponse({"erro": f"{type(exc).__name__}: {exc}"}, status_code=502)

    db.salvar_mensagens_edital(
        edital_id,
        [{"papel": "user", "conteudo": pergunta}, {"papel": "assistant", "conteudo": resposta}],
        autor=autor,
    )
    return {"resposta": resposta, "autor": autor}


@app.get("/historico", response_class=HTMLResponse)
async def historico(request: Request):
    return templates.TemplateResponse(request, "historico.html", {"analises": db.listar_analises()})


@app.on_event("startup")
async def aquecer_cache_municipios():
    try:
        async with novo_client() as client:
            await listar_municipios(client)
    except Exception:  # noqa: BLE001 - sem rede no boot, autocomplete tenta de novo depois
        pass


# Adaptador para AWS Lambda. O mesmo Lambda tem dois papéis:
#  - front HTTP (via API Gateway -> Mangum): páginas, /analisar, polling, chat;
#  - worker: quando é invocado de forma assíncrona com {"tipo":"analise",...},
#    roda a análise pesada em segundo plano (ver disparar_analise).
# Presente só quando mangum está instalado; no notebook local o app roda por
# uvicorn e este handler é ignorado. lifespan="off" evita rodar o startup acima
# em todo cold start (o cache de municípios é reconstruído sob demanda).
try:
    from mangum import Mangum

    _mangum = Mangum(app, lifespan="off")

    def handler(event, context):
        if isinstance(event, dict) and event.get("tipo") in ("analise", "edital"):
            # asyncio.run() FECHA o loop ao terminar; como o Lambda reaproveita o
            # container, o Mangum depois chamaria get_event_loop() num loop morto
            # e toda requisição HTTP daria 500. Por isso rodamos o worker num loop
            # próprio e deixamos um loop novo e aberto para o Mangum reaproveitar.
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                loop.run_until_complete(_executar_evento(event))
            finally:
                loop.close()
                asyncio.set_event_loop(asyncio.new_event_loop())
            return {"ok": True}
        return _mangum(event, context)
except ImportError:  # pragma: no cover - mangum não é dependência do modo local
    handler = None
