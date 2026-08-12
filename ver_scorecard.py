"""Mostra o scorecard de uma análise.

Uso: python ver_scorecard.py [id] [--recalcular]

--recalcular refaz o cálculo com o código atual sobre os dados já coletados,
útil para ver o efeito de mudanças de peso sem refazer a coleta.
"""
import json
import sqlite3
import sys

from app.scoring import calcular_scorecard

recalcular = "--recalcular" in sys.argv
ids = [a for a in sys.argv[1:] if not a.startswith("--")]

con = sqlite3.connect("data/analises.db")
campos = "id, score, semaforo, scorecard_json, dados_json"
if ids:
    row = con.execute(f"SELECT {campos} FROM analises WHERE id = ?", (ids[0],)).fetchone()
else:
    row = con.execute(f"SELECT {campos} FROM analises ORDER BY id DESC LIMIT 1").fetchone()

if recalcular:
    sc = calcular_scorecard(json.loads(row[4]))
    print(f"análise {row[0]} | RECALCULADO: score {sc['score']} | {sc['semaforo']}")
else:
    sc = json.loads(row[3])
    print(f"análise {row[0]} | score {row[1]} | {row[2]}")
print(f"cobertura de dados: {sc['cobertura_dados_pct']}%\n")
for d in sc["dimensoes"]:
    pontos = f"{d['pontos']}/{d['maximo']}" if d["pontos"] is not None else "sem dado"
    print(f"  {d['dimensao'][:42]:44} {pontos:10} {d['detalhe'][:70]}")
print("\nrestrições:", sc["restricoes_aplicadas"] or "nenhuma")
