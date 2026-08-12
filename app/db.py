"""Persistência das análises em SQLite."""
import json
import sqlite3
from datetime import datetime

from .config import DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS analises (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    criado_em TEXT NOT NULL,
    municipio TEXT NOT NULL,
    uf TEXT,
    cod_ibge TEXT,
    cnpj TEXT,
    score INTEGER,
    semaforo TEXT,
    dados_json TEXT NOT NULL,
    scorecard_json TEXT NOT NULL,
    dossie_md TEXT
);

CREATE TABLE IF NOT EXISTS mensagens_chat (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    analise_id INTEGER NOT NULL REFERENCES analises(id) ON DELETE CASCADE,
    criado_em TEXT NOT NULL,
    papel TEXT NOT NULL,
    conteudo TEXT NOT NULL,
    autor TEXT
);

CREATE INDEX IF NOT EXISTS idx_mensagens_analise ON mensagens_chat(analise_id, id);
"""


def conectar() -> sqlite3.Connection:
    con = sqlite3.connect(DB_PATH, timeout=15)
    con.row_factory = sqlite3.Row
    # WAL permite leitura durante escrita: com duas pessoas usando ao mesmo
    # tempo, uma consulta não fica bloqueada por uma análise em gravação.
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA foreign_keys=ON")
    con.executescript(SCHEMA)
    return con


def salvar_analise(municipio, uf, cod_ibge, cnpj, dados, scorecard, dossie_md) -> int:
    con = conectar()
    with con:
        cur = con.execute(
            "INSERT INTO analises (criado_em, municipio, uf, cod_ibge, cnpj, score, semaforo,"
            " dados_json, scorecard_json, dossie_md) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                datetime.now().isoformat(timespec="seconds"),
                municipio,
                uf,
                cod_ibge,
                cnpj,
                scorecard.get("score"),
                scorecard.get("semaforo"),
                json.dumps(dados, ensure_ascii=False),
                json.dumps(scorecard, ensure_ascii=False),
                dossie_md,
            ),
        )
    analise_id = cur.lastrowid
    con.close()
    return analise_id


def buscar_analise(analise_id: int):
    con = conectar()
    row = con.execute("SELECT * FROM analises WHERE id = ?", (analise_id,)).fetchone()
    con.close()
    if row is None:
        return None
    analise = dict(row)
    analise["dados"] = json.loads(analise.pop("dados_json"))
    analise["scorecard"] = json.loads(analise.pop("scorecard_json"))
    return analise


def listar_mensagens(analise_id: int) -> list[dict]:
    """Histórico do chat de uma análise, em ordem cronológica."""
    con = conectar()
    rows = con.execute(
        "SELECT papel, conteudo, criado_em, autor FROM mensagens_chat "
        "WHERE analise_id = ? ORDER BY id",
        (analise_id,),
    ).fetchall()
    con.close()
    return [dict(r) for r in rows]


def salvar_mensagens(analise_id: int, mensagens: list[dict], autor: str | None = None) -> None:
    con = conectar()
    with con:
        con.executemany(
            "INSERT INTO mensagens_chat (analise_id, criado_em, papel, conteudo, autor) "
            "VALUES (?,?,?,?,?)",
            [
                (
                    analise_id,
                    datetime.now().isoformat(timespec="seconds"),
                    m["papel"],
                    m["conteudo"],
                    autor,
                )
                for m in mensagens
            ],
        )
    con.close()


def contar_mensagens() -> dict[int, int]:
    """Quantas mensagens cada análise tem, para exibir no histórico."""
    con = conectar()
    rows = con.execute(
        "SELECT analise_id, COUNT(*) AS n FROM mensagens_chat GROUP BY analise_id"
    ).fetchall()
    con.close()
    return {r["analise_id"]: r["n"] for r in rows}


def listar_analises():
    con = conectar()
    rows = con.execute(
        "SELECT id, criado_em, municipio, uf, score, semaforo FROM analises ORDER BY id DESC"
    ).fetchall()
    con.close()
    return [dict(r) for r in rows]
