"""Teste manual dos coletores com um município real (uso: python teste_coletores.py [cod_ibge] [cnpj])."""
import asyncio
import json
import sys

from app.collectors.base import novo_client
from app.collectors.capag import coletar_capag
from app.collectors.cnpj import coletar_cnpj
from app.collectors.ibge import coletar_contexto
from app.collectors.pncp import coletar_pncp
from app.collectors.siconfi import coletar_siconfi
from app.collectors.transparencia import (
    coletar_convenios,
    coletar_transferencias,
    coletar_transparencia,
)
from app.scoring import calcular_scorecard

COD_IBGE = sys.argv[1] if len(sys.argv) > 1 else "4202404"  # Blumenau/SC
CNPJ = sys.argv[2] if len(sys.argv) > 2 else "83108357000115"  # Prefeitura de Blumenau


async def main():
    async with novo_client() as client:
        resultados = await asyncio.gather(
            coletar_contexto(client, COD_IBGE),
            coletar_capag(client, COD_IBGE),
            coletar_siconfi(client, COD_IBGE),
            coletar_pncp(client, CNPJ),
            coletar_cnpj(client, CNPJ),
            coletar_transparencia(client, CNPJ),
            coletar_transferencias(client, CNPJ),
            coletar_convenios(client, COD_IBGE),
        )
    dados = {r["fonte"]: r for r in resultados}
    for fonte, r in dados.items():
        print(f"\n=== {fonte} — {'OK' if r['ok'] else 'ERRO: ' + str(r['erro'])} ===")
        if r["ok"]:
            saida = json.dumps(r["dados"], ensure_ascii=False, indent=2)
            print(saida[:1500])
    print("\n=== SCORECARD ===")
    print(json.dumps(calcular_scorecard(dados), ensure_ascii=False, indent=2))


asyncio.run(main())
