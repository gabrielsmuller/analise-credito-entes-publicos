"""Autenticação HTTP Basic para uso interno.

Proporcional ao cenário: duas pessoas na rede local. O objetivo não é resistir
a um atacante determinado, é impedir que qualquer máquina da rede abra o
sistema e dispare análises (que consomem crédito de IA) ou leia os dossiês.

Se APP_SENHA não estiver definida, não há autenticação - modo de uso local em
127.0.0.1. O script de rede se recusa a subir sem senha.
"""
import secrets

from fastapi import Request
from fastapi.responses import Response
from starlette.middleware.base import BaseHTTPMiddleware

from .config import APP_SENHA, APP_USUARIOS

LIVRES = ("/static/",)


def _credenciais_validas(cabecalho: str | None) -> str | None:
    """Devolve o usuário autenticado, ou None."""
    if not cabecalho or not cabecalho.lower().startswith("basic "):
        return None
    import base64

    try:
        bruto = base64.b64decode(cabecalho.split(" ", 1)[1]).decode("utf-8")
        usuario, _, senha = bruto.partition(":")
    except Exception:  # noqa: BLE001 - cabeçalho malformado é só falha de auth
        return None

    # compare_digest evita vazar informação pelo tempo de comparação
    senha_ok = secrets.compare_digest(senha, APP_SENHA)
    usuario_ok = any(secrets.compare_digest(usuario, u) for u in APP_USUARIOS)
    return usuario if senha_ok and usuario_ok else None


class AutenticacaoBasica(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if not APP_SENHA or request.url.path.startswith(LIVRES):
            request.state.usuario = None
            return await call_next(request)

        usuario = _credenciais_validas(request.headers.get("authorization"))
        if usuario is None:
            return Response(
                status_code=401,
                headers={"WWW-Authenticate": 'Basic realm="Análise de Crédito"'},
                content="Acesso restrito.",
            )
        request.state.usuario = usuario
        return await call_next(request)
