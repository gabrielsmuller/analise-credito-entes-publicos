"""App web interno de análise de crédito de entes públicos."""
import asyncio
from pathlib import Path

import markdown as md
from fastapi import Body, FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import db
from .auth import AutenticacaoBasica
from .collectors.base import novo_client
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
    async with novo_client() as client:
        # o CNPJ vem do registro oficial do Tesouro, não digitado: um CNPJ
        # errado não falharia, apenas traria dados de outra entidade
        cnpj = await resolver_cnpj(client, cod_ibge)
        resultados = await asyncio.gather(
            coletar_ente(client, cod_ibge),
            coletar_contexto(client, cod_ibge),
            coletar_capag(client, cod_ibge),
            coletar_siconfi(client, cod_ibge),
            coletar_pagamentos(client, cod_ibge),
            coletar_pncp(client, cnpj),
            coletar_cnpj(client, cnpj),
            coletar_transparencia(client, cnpj),
            coletar_transferencias(client, cnpj),
            coletar_convenios(client, cod_ibge, cnpj),
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

    url = f"/dossie/{analise_id}"
    if erro_dossie:
        url += f"?erro_dossie={erro_dossie[:200]}"
    # htmx segue este cabeçalho; navegação normal usa o redirect
    if request.headers.get("HX-Request"):
        return HTMLResponse("", headers={"HX-Redirect": url})
    return RedirectResponse(url, status_code=303)


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
