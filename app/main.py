"""App web interno de análise de crédito de entes públicos."""
import asyncio
import json
import os
import uuid
from pathlib import Path

import markdown as md
from fastapi import Body, FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import db
from .auth import AutenticacaoBasica
from .collectors.base import com_teto, novo_client
from .collectors.capag import coletar_capag
from .collectors.cnpj import coletar_cnpj
from .collectors.entes import buscar as buscar_entes, coletar_ente, resolver_cnpj
from .collectors.ibge import buscar_municipios, coletar_contexto, listar_municipios
from .collectors.pagamentos import coletar_pagamentos
from .collectors.pncp import coletar_pncp
from .collectors.siconfi import coletar_siconfi
from .collectors.transparencia import coletar_convenios, coletar_transferencias, coletar_transparencia
from .dossier import gerar_dossie, responder_pergunta
from .scoring import calcular_scorecard

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


async def executar_analise(
    cod_ibge: str,
    municipio: str,
    uf: str,
    serasa: str,
    cauc: str,
    cauc_situacao: str,
) -> tuple[int, str | None]:
    """Coleta, pontua e redige o dossiê. Devolve (id_da_análise, erro_do_dossiê).

    É o miolo pesado da análise (2-3 min). Roda em segundo plano - ver
    disparar_analise -, então nunca é chamado diretamente por uma requisição
    HTTP que o navegador precise segurar.
    """
    async with novo_client() as client:
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

    scorecard = calcular_scorecard(dados)

    observacoes = {
        "serasa": serasa.strip() or None,
        "cauc": cauc.strip() or None,
        "cauc_situacao": cauc_situacao,
    }
    # guardadas junto dos dados para aparecerem no dossiê e no histórico
    dados["observacoes_manuais"] = {"fonte": "observacoes_manuais", "ok": True, "dados": observacoes}

    try:
        dossie_md = await asyncio.to_thread(
            gerar_dossie, municipio, uf, dados, scorecard, observacoes
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


async def disparar_analise(params: dict) -> str:
    """Cria o job e inicia o processamento em segundo plano; devolve o job_id.

    - No Lambda: invoca a própria função de forma assíncrona (InvocationType
      Event). A requisição HTTP retorna na hora, sem segurar o navegador por
      minutos (o que a Function URL/API Gateway não permitiria).
    - Local: dispara uma task em background, para a experiência de polling ser
      idêntica à de produção.
    """
    job_id = uuid.uuid4().hex[:12]
    db.criar_job(job_id)
    nome_funcao = os.getenv("AWS_LAMBDA_FUNCTION_NAME")
    if nome_funcao:
        import boto3

        boto3.client("lambda").invoke(
            FunctionName=nome_funcao,
            InvocationType="Event",
            Payload=json.dumps({"tipo": "analise", "job_id": job_id, "params": params}).encode(),
        )
    else:
        asyncio.create_task(_processar_job(job_id, params))
    return job_id


@app.post("/analisar")
async def analisar(
    request: Request,
    cod_ibge: str = Form(...),
    municipio: str = Form(""),
    uf: str = Form(""),
    serasa: str = Form(""),
    cauc: str = Form(""),
    cauc_situacao: str = Form("nao_consultado"),
):
    params = {
        "cod_ibge": cod_ibge,
        "municipio": municipio,
        "uf": uf,
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
async def processando(request: Request, job_id: str):
    job = db.buscar_job(job_id)
    if job is None:
        return HTMLResponse("Processo não encontrado", status_code=404)
    return templates.TemplateResponse(request, "processando.html", {"job_id": job_id})


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
        if isinstance(event, dict) and event.get("tipo") == "analise":
            # asyncio.run() FECHA o loop ao terminar; como o Lambda reaproveita o
            # container, o Mangum depois chamaria get_event_loop() num loop morto
            # e toda requisição HTTP daria 500. Por isso rodamos o worker num loop
            # próprio e deixamos um loop novo e aberto para o Mangum reaproveitar.
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                loop.run_until_complete(_processar_job(event["job_id"], event["params"]))
            finally:
                loop.close()
                asyncio.set_event_loop(asyncio.new_event_loop())
            return {"ok": True}
        return _mangum(event, context)
except ImportError:  # pragma: no cover - mangum não é dependência do modo local
    handler = None
