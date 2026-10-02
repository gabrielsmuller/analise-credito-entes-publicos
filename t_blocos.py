import time
from concurrent.futures import ThreadPoolExecutor

import app.edital_ia as ia
from app.edital_exigencias import SYSTEM_EXIGENCIAS, _blocos, paginas_do_texto

ia.IA_TIMEOUT = 600  # só para medir quanto cada bloco realmente leva
SP = r"C:/Users/SUPORT~1/AppData/Local/Temp/claude/D--Claude-Projects-analise-financeira-2/6163a30a-947f-4d33-a7c6-672888c8456a/scratchpad/"
texto = open(SP + "iffar.txt", encoding="utf-8").read()
blocos = _blocos(paginas_do_texto(texto))


def medir(i_b):
    i, b = i_b
    t0 = time.time()
    try:
        r = ia._chamar_json(SYSTEM_EXIGENCIAS, "Trecho do edital:\n\n" + b)
        n = len(r.get("exigencias") or [])
        erro = ""
    except Exception as exc:  # noqa: BLE001
        n, erro = 0, f"{type(exc).__name__}: {exc}"[:80]
    pag = b.split("\n", 1)[0]
    return i, pag, len(b), round(time.time() - t0), n, erro


with ThreadPoolExecutor(max_workers=6) as pool:
    for i, pag, tam, seg, n, erro in pool.map(medir, enumerate(blocos)):
        print(f"bloco {i:2} ({pag}) {tam:6} chars -> {seg:4}s, {n:3} exigencias {erro}")
