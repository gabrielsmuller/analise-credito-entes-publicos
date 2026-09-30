"""Persistência das análises em DynamoDB (uso serverless / Lambda).

Tabela única (single-table), chave composta pk/sk:

    pk                     sk           conteúdo
    ------------------     ----------   ------------------------------------------
    CONTADOR               analises     valor: último id de análise emitido
    ANALISE#{id}           META         metadados + payloads gzip (dados/scorecard)
    ANALISE#{id}           MSG_SEQ      valor: último nº de mensagem daquela análise
    ANALISE#{id}           MSG#{seq}    uma mensagem do chat
    CONTADOR               editais      valor: último id de edital emitido
    EDITAL#{id}            META         texto do edital + análise da IA (gzip)
    EDITAL#{id}            MSG_SEQ/MSG# chat do edital, no mesmo formato

Os JSONs grandes (dados coletados e scorecard) são gravados comprimidos com
gzip como atributo binário: cabe folgado no limite de 400 KB por item do
DynamoDB e evita depender de um segundo serviço (S3) só para o payload.
Os ids continuam inteiros e sequenciais (contador atômico), preservando as
URLs /dossie/{id} e a mesma interface do backend SQLite.
"""
import gzip
import json
import time
from datetime import datetime

import boto3
from boto3.dynamodb.conditions import Attr, Key

from .config import AWS_REGION, DYNAMO_TABELA

# Jobs (análises em processamento) expiram sozinhos via TTL do DynamoDB: são
# efêmeros, só servem para o navegador acompanhar o progresso.
JOB_TTL = 24 * 3600

_tabela = None


def _t():
    global _tabela
    if _tabela is None:
        _tabela = boto3.resource("dynamodb", region_name=AWS_REGION).Table(DYNAMO_TABELA)
    return _tabela


def _gz(obj) -> bytes:
    return gzip.compress(json.dumps(obj, ensure_ascii=False).encode("utf-8"))


def _ungz(dado) -> object:
    # boto3 devolve binário como boto3.dynamodb.types.Binary; bytes() extrai os bytes.
    return json.loads(gzip.decompress(bytes(dado)).decode("utf-8"))


def _num(valor):
    """DynamoDB devolve números como Decimal; normaliza para int ou None."""
    return int(valor) if valor is not None else None


def _proximo_id(contador: str = "analises") -> int:
    resp = _t().update_item(
        Key={"pk": "CONTADOR", "sk": contador},
        UpdateExpression="ADD valor :um",
        ExpressionAttributeValues={":um": 1},
        ReturnValues="UPDATED_NEW",
    )
    return int(resp["Attributes"]["valor"])


def _proximo_seq(analise_id: int, prefixo: str = "ANALISE") -> int:
    resp = _t().update_item(
        Key={"pk": f"{prefixo}#{analise_id}", "sk": "MSG_SEQ"},
        UpdateExpression="ADD valor :um",
        ExpressionAttributeValues={":um": 1},
        ReturnValues="UPDATED_NEW",
    )
    return int(resp["Attributes"]["valor"])


def salvar_analise(municipio, uf, cod_ibge, cnpj, dados, scorecard, dossie_md) -> int:
    analise_id = _proximo_id()
    _t().put_item(
        Item={
            "pk": f"ANALISE#{analise_id}",
            "sk": "META",
            "id": analise_id,
            "criado_em": datetime.now().isoformat(timespec="seconds"),
            "municipio": municipio,
            "uf": uf,
            "cod_ibge": cod_ibge,
            "cnpj": cnpj,
            "score": scorecard.get("score"),
            "semaforo": scorecard.get("semaforo"),
            "dados_gz": _gz(dados),
            "scorecard_gz": _gz(scorecard),
            "dossie_md": dossie_md,
        }
    )
    return analise_id


def buscar_analise(analise_id: int):
    item = _t().get_item(Key={"pk": f"ANALISE#{analise_id}", "sk": "META"}).get("Item")
    if not item:
        return None
    return {
        "id": _num(item.get("id")),
        "criado_em": item.get("criado_em"),
        "municipio": item.get("municipio"),
        "uf": item.get("uf"),
        "cod_ibge": item.get("cod_ibge"),
        "cnpj": item.get("cnpj"),
        "score": _num(item.get("score")),
        "semaforo": item.get("semaforo"),
        "dossie_md": item.get("dossie_md"),
        "dados": _ungz(item["dados_gz"]),
        "scorecard": _ungz(item["scorecard_gz"]),
    }


def listar_mensagens(analise_id: int) -> list[dict]:
    """Histórico do chat de uma análise, em ordem cronológica (sk crescente)."""
    resp = _t().query(
        KeyConditionExpression=Key("pk").eq(f"ANALISE#{analise_id}")
        & Key("sk").begins_with("MSG#")
    )
    return [
        {
            "papel": i["papel"],
            "conteudo": i["conteudo"],
            "criado_em": i.get("criado_em"),
            "autor": i.get("autor"),
        }
        for i in resp.get("Items", [])
    ]


def salvar_mensagens(analise_id: int, mensagens: list[dict], autor: str | None = None) -> None:
    for m in mensagens:
        seq = _proximo_seq(analise_id)
        _t().put_item(
            Item={
                "pk": f"ANALISE#{analise_id}",
                "sk": f"MSG#{seq:08d}",
                "papel": m["papel"],
                "conteudo": m["conteudo"],
                "criado_em": datetime.now().isoformat(timespec="seconds"),
                "autor": autor,
            }
        )


def contar_mensagens() -> dict[int, int]:
    """Quantas mensagens cada análise tem. Volume baixo: um scan resolve."""
    contagem: dict[int, int] = {}
    # só mensagens de análises: o chat dos editais usa o mesmo formato de sk
    kwargs = {"FilterExpression": Attr("pk").begins_with("ANALISE#") & Attr("sk").begins_with("MSG#")}
    while True:
        resp = _t().scan(**kwargs)
        for i in resp.get("Items", []):
            aid = int(str(i["pk"]).split("#", 1)[1])
            contagem[aid] = contagem.get(aid, 0) + 1
        if "LastEvaluatedKey" not in resp:
            return contagem
        kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]


# --- Jobs de processamento assíncrono -------------------------------------

def criar_job(job_id: str) -> None:
    _t().put_item(
        Item={
            "pk": f"JOB#{job_id}",
            "sk": "META",
            "status": "processando",
            "criado_em": datetime.now().isoformat(timespec="seconds"),
            "expira_em": int(time.time()) + JOB_TTL,
        }
    )


def buscar_job(job_id: str):
    item = _t().get_item(Key={"pk": f"JOB#{job_id}", "sk": "META"}).get("Item")
    if not item:
        return None
    return {
        "status": item.get("status"),
        "analise_id": _num(item.get("analise_id")),
        "erro": item.get("erro"),
        "erro_dossie": item.get("erro_dossie"),
        "criado_em": item.get("criado_em"),
    }


def atualizar_job(job_id, status, analise_id=None, erro=None, erro_dossie=None) -> None:
    # 'status' é palavra reservada no DynamoDB - vai por ExpressionAttributeNames.
    partes = ["#s = :s"]
    nomes = {"#s": "status"}
    valores = {":s": status}
    if analise_id is not None:
        partes.append("analise_id = :a")
        valores[":a"] = analise_id
    if erro is not None:
        partes.append("erro = :e")
        valores[":e"] = erro
    if erro_dossie is not None:
        partes.append("erro_dossie = :ed")
        valores[":ed"] = erro_dossie
    _t().update_item(
        Key={"pk": f"JOB#{job_id}", "sk": "META"},
        UpdateExpression="SET " + ", ".join(partes),
        ExpressionAttributeNames=nomes,
        ExpressionAttributeValues=valores,
    )


def listar_analises():
    """Todas as análises, mais recentes primeiro. Volume baixo (~10/mês): scan."""
    itens = []
    # análises e jobs compartilham sk="META"; filtramos pelo prefixo do pk para
    # não trazer os jobs (que não têm município/score) para a lista do histórico.
    kwargs = {
        "FilterExpression": Attr("pk").begins_with("ANALISE#") & Attr("sk").eq("META"),
        "ProjectionExpression": "id, criado_em, municipio, uf, score, semaforo",
    }
    while True:
        resp = _t().scan(**kwargs)
        itens.extend(resp.get("Items", []))
        if "LastEvaluatedKey" not in resp:
            break
        kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]
    itens.sort(key=lambda i: _num(i.get("id")) or 0, reverse=True)
    return [
        {
            "id": _num(i.get("id")),
            "criado_em": i.get("criado_em"),
            "municipio": i.get("municipio"),
            "uf": i.get("uf"),
            "score": _num(i.get("score")),
            "semaforo": i.get("semaforo"),
        }
        for i in itens
    ]


# --- Editais (piloto de análise de editais) --------------------------------

def criar_edital(arquivo: str, texto: str) -> int:
    edital_id = _proximo_id("editais")
    _t().put_item(
        Item={
            "pk": f"EDITAL#{edital_id}",
            "sk": "META",
            "id": edital_id,
            "criado_em": datetime.now().isoformat(timespec="seconds"),
            "arquivo": arquivo,
            "texto_gz": _gz(texto),
        }
    )
    return edital_id


def concluir_edital(edital_id: int, dados: dict, resumo_md: str | None) -> None:
    _t().update_item(
        Key={"pk": f"EDITAL#{edital_id}", "sk": "META"},
        UpdateExpression="SET dados_gz = :d, resumo_md = :r",
        ExpressionAttributeValues={":d": _gz(dados), ":r": resumo_md},
    )


def buscar_edital(edital_id: int):
    item = _t().get_item(Key={"pk": f"EDITAL#{edital_id}", "sk": "META"}).get("Item")
    if not item:
        return None
    return {
        "id": _num(item.get("id")),
        "criado_em": item.get("criado_em"),
        "arquivo": item.get("arquivo"),
        "texto": _ungz(item["texto_gz"]),
        "dados": _ungz(item["dados_gz"]) if item.get("dados_gz") else None,
        "resumo_md": item.get("resumo_md"),
    }


def listar_editais() -> list[dict]:
    """Metadados para a lista (o texto integral não é trazido). Volume baixo: scan."""
    itens = []
    kwargs = {
        "FilterExpression": Attr("pk").begins_with("EDITAL#") & Attr("sk").eq("META"),
        "ProjectionExpression": "id, criado_em, arquivo, dados_gz",
    }
    while True:
        resp = _t().scan(**kwargs)
        itens.extend(resp.get("Items", []))
        if "LastEvaluatedKey" not in resp:
            break
        kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]
    itens.sort(key=lambda i: _num(i.get("id")) or 0, reverse=True)
    return [
        {
            "id": _num(i.get("id")),
            "criado_em": i.get("criado_em"),
            "arquivo": i.get("arquivo"),
            "dados": _ungz(i["dados_gz"]) if i.get("dados_gz") else None,
        }
        for i in itens
    ]


def listar_mensagens_edital(edital_id: int) -> list[dict]:
    resp = _t().query(
        KeyConditionExpression=Key("pk").eq(f"EDITAL#{edital_id}") & Key("sk").begins_with("MSG#")
    )
    return [
        {"papel": i["papel"], "conteudo": i["conteudo"], "criado_em": i.get("criado_em"), "autor": i.get("autor")}
        for i in resp.get("Items", [])
    ]


def salvar_mensagens_edital(edital_id: int, mensagens: list[dict], autor: str | None = None) -> None:
    for m in mensagens:
        seq = _proximo_seq(edital_id, prefixo="EDITAL")
        _t().put_item(
            Item={
                "pk": f"EDITAL#{edital_id}",
                "sk": f"MSG#{seq:08d}",
                "papel": m["papel"],
                "conteudo": m["conteudo"],
                "criado_em": datetime.now().isoformat(timespec="seconds"),
                "autor": autor,
            }
        )
