"""Compara os scorecards de várias análises lado a lado, para validação.

Uso: python comparar.py [id1 id2 ...]   (sem argumentos: uma por município)
"""
import json
import sqlite3
import sys

con = sqlite3.connect("data/analises.db")

if len(sys.argv) > 1:
    ids = [int(a) for a in sys.argv[1:]]
else:  # última análise de cada município
    ids = [
        r[0]
        for r in con.execute(
            "SELECT MAX(id) FROM analises GROUP BY municipio, uf ORDER BY MAX(id)"
        ).fetchall()
    ]

analises = []
for i in ids:
    r = con.execute(
        "SELECT id, municipio, uf, score, semaforo, dados_json, scorecard_json FROM analises WHERE id=?",
        (i,),
    ).fetchone()
    analises.append(
        {
            "id": r[0], "municipio": r[1], "uf": r[2], "score": r[3], "semaforo": r[4],
            "dados": json.loads(r[5]), "sc": json.loads(r[6]),
        }
    )


def cel(v, larg=16):
    return str(v)[:larg].ljust(larg)


print("=" * 100)
print(cel("DIMENSÃO", 40) + "".join(cel(f"{a['municipio']}/{a['uf']}", 15) for a in analises))
print("=" * 100)

dimensoes = [d["dimensao"] for d in analises[0]["sc"]["dimensoes"]]
for nome in dimensoes:
    linha = cel(nome, 40)
    for a in analises:
        d = next((x for x in a["sc"]["dimensoes"] if x["dimensao"] == nome), None)
        if d is None:
            linha += cel("—", 15)
        elif d["pontos"] is None:
            linha += cel("sem dado", 15)
        else:
            linha += cel(f"{d['pontos']}/{d['maximo']}", 15)
    print(linha)

print("-" * 100)
print(cel("SCORE", 40) + "".join(cel(f"{a['score']} {a['semaforo']}", 15) for a in analises))
print(cel("COBERTURA", 40) + "".join(cel(f"{a['sc']['cobertura_dados_pct']}%", 15) for a in analises))

print("\n\n=== DADOS-CHAVE ===")
for a in analises:
    d = a["dados"]
    print(f"\n--- {a['municipio']}/{a['uf']} (análise {a['id']}) — score {a['score']} {a['semaforo']}")

    capag = d.get("capag", {})
    print(f"  CAPAG: {capag['dados']['nota_capag'] if capag.get('ok') else 'ERRO: ' + str(capag.get('erro'))[:60]}")

    pag = d.get("pagamentos", {})
    if pag.get("ok") and pag["dados"].get("caixa_por_ano"):
        for ano in pag["dados"]["caixa_por_ano"]:
            nv = ano["nao_vinculados"]
            print(f"    caixa {ano['ano']}: cobertura {nv['indice_cobertura']} | "
                  f"líquido após RP {nv['disponibilidade_liquida_apos_rp']}")
        rp = (pag["dados"].get("restos_a_pagar_por_ano") or [{}])[0]
        if rp:
            print(f"    RP {rp.get('referencia')}: processados pagos {rp['processados']['pct_pago']}% | "
                  f"não proc. cancelados {rp['nao_processados']['pct_cancelado']}%")
        ex = pag["dados"].get("execucao_pagamento") or {}
        print(f"    liquidado x pago: {ex.get('pct_pago')}%")
    else:
        print(f"    pagamentos: ERRO {str(pag.get('erro'))[:70]}")

    sic = d.get("siconfi", {})
    if sic.get("ok"):
        rgf = sic["dados"].get("rgf") or {}
        print(f"  RGF {rgf.get('referencia')}: pessoal {rgf.get('pessoal_pct_rcl')}% | "
              f"DCL {rgf.get('dcl_pct_rcl')}% | RCL {rgf.get('rcl_ajustada')}")
    else:
        print(f"  SICONFI: ERRO {str(sic.get('erro'))[:60]}")

    for fonte in ("transparencia", "convenios", "transferencias", "pncp", "ibge"):
        f = d.get(fonte, {})
        if not f.get("ok"):
            print(f"  {fonte}: ERRO {str(f.get('erro'))[:70]}")

    print(f"  restrições: {a['sc']['restricoes_aplicadas'] or 'nenhuma'}")
