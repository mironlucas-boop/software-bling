from fastapi import FastAPI, HTTPException, Request, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from contextlib import asynccontextmanager
from shopify_webhook import router as shopify_router
from shopify_service import router as shopify_router
import httpx
import asyncio
from datetime import datetime
from typing import Optional
import json
import os
import base64

from config import settings
from auth import BlingAuth
from models import WebhookPayload

# ── Lifespan ──────────────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.auth = BlingAuth(
        client_id=settings.CLIENT_ID,
        client_secret=settings.CLIENT_SECRET,
        access_token=settings.INITIAL_ACCESS_TOKEN,
        refresh_token=settings.INITIAL_REFRESH_TOKEN,
    )
    app.state.pedidos_cache = []
    app.state.last_update = None
    yield

app = FastAPI(
    title="Bling ERP - Pedidos",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(shopify_router)


# ── OAuth Helper ───────────────────────────────────────────────────────────────

BLING_AUTH_URL = "https://www.bling.com.br/Api/v3/oauth/authorize"
BLING_TOKEN_URL = "https://www.bling.com.br/Api/v3/oauth/token"
REDIRECT_URI = "http://localhost:8001/auth/callback"

@app.get("/auth/login", response_class=HTMLResponse)
async def auth_login():
    """Página de re-autorização OAuth — use quando o token expirar."""
    import secrets
    state = secrets.token_hex(16)
    url = (
        f"{BLING_AUTH_URL}"
        f"?response_type=code"
        f"&client_id={settings.CLIENT_ID}"
        f"&redirect_uri={REDIRECT_URI}"
        f"&state={state}"
    )
    return HTMLResponse(f"""
<!DOCTYPE html>
<html lang="pt-BR">
<head>
  <meta charset="UTF-8">
  <title>Autorizar Bling</title>
  <style>
    body {{ font-family: sans-serif; display: flex; flex-direction: column;
            align-items: center; justify-content: center; height: 100vh;
            margin: 0; background: #f0f2f5; }}
    .card {{ background: white; padding: 2rem 3rem; border-radius: 12px;
             box-shadow: 0 4px 20px rgba(0,0,0,.1); text-align: center; max-width: 420px; }}
    h2 {{ color: #1a1a2e; margin-bottom: .5rem; }}
    p  {{ color: #555; margin-bottom: 1.5rem; font-size: .95rem; }}
    a  {{ background: #0066cc; color: white; padding: .75rem 2rem;
          border-radius: 8px; text-decoration: none; font-weight: 600;
          display: inline-block; transition: background .2s; }}
    a:hover {{ background: #0052a3; }}
    .warn {{ background: #fff3cd; border: 1px solid #ffc107; border-radius: 8px;
             padding: .75rem 1rem; margin-bottom: 1.5rem; color: #856404;
             font-size: .85rem; }}
  </style>
</head>
<body>
  <div class="card">
    <h2>🔑 Autorizar Bling</h2>
    <div class="warn">⚠️ Seu refresh token expirou.<br>Clique abaixo para re-autorizar.</div>
    <p>Você será redirecionado ao Bling para conceder acesso novamente.</p>
    <a href="{url}">Autorizar no Bling</a>
  </div>
</body>
</html>
""")

@app.get("/auth/callback", response_class=HTMLResponse)
async def auth_callback(code: str = None, error: str = None):
    """Recebe o code do Bling, troca por access+refresh token e atualiza em memória."""
    if error or not code:
        return HTMLResponse(f"<h2>Erro na autorização: {error}</h2>", status_code=400)

    credentials = base64.b64encode(
        f"{settings.CLIENT_ID}:{settings.CLIENT_SECRET}".encode()
    ).decode()

    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(
            BLING_TOKEN_URL,
            headers={
                "Authorization": f"Basic {credentials}",
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/json",
            },
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": REDIRECT_URI,
            },
        )

    if resp.status_code != 200:
        return HTMLResponse(
            f"<h2>Erro ao trocar token: {resp.status_code}</h2><pre>{resp.text}</pre>",
            status_code=500,
        )

    data = resp.json()
    access_token  = data["access_token"]
    refresh_token = data["refresh_token"]

    # Atualiza o objeto BlingAuth em memória
    auth: BlingAuth = app.state.auth
    auth._access_token  = access_token
    auth._refresh_token = refresh_token
    auth._expires_at    = None  # força revalidação na próxima chamada

    return HTMLResponse(f"""
<!DOCTYPE html>
<html lang="pt-BR">
<head>
  <meta charset="UTF-8">
  <title>Autorizado!</title>
  <style>
    body {{ font-family: sans-serif; display: flex; align-items: center;
            justify-content: center; height: 100vh; margin: 0; background: #f0f2f5; }}
    .card {{ background: white; padding: 2rem 3rem; border-radius: 12px;
             box-shadow: 0 4px 20px rgba(0,0,0,.1); text-align: center; max-width: 420px; }}
    h2 {{ color: #155724; }}
    p  {{ color: #555; margin-bottom: 1.5rem; }}
    code {{ background: #f8f9fa; padding: .2rem .5rem; border-radius: 4px;
            font-size: .75rem; word-break: break-all; display: block;
            margin: .5rem 0; text-align: left; }}
    a  {{ background: #0066cc; color: white; padding: .75rem 2rem;
          border-radius: 8px; text-decoration: none; font-weight: 600; }}
  </style>
</head>
<body>
  <div class="card">
    <h2>✅ Autorizado com sucesso!</h2>
    <p>Tokens atualizados em memória. Copie os valores abaixo para o seu <code>.env</code> ou <code>config.py</code> para não precisar re-autorizar após reiniciar.</p>
    <strong>INITIAL_ACCESS_TOKEN</strong>
    <code>{access_token}</code>
    <strong>INITIAL_REFRESH_TOKEN</strong>
    <code>{refresh_token}</code>
    <br><br>
    <a href="/">Voltar ao painel</a>
  </div>
</body>
</html>
""")


# ── Helper ─────────────────────────────────────────────────────────────────────
async def fetch_all_pedidos(auth: BlingAuth) -> list:
    """Busca todos os pedidos paginados da API Bling."""
    all_pedidos = []
    page = 1
    limit = 100

    async with httpx.AsyncClient(timeout=30) as client:
        while True:
            token = await auth.get_valid_token(client)
            print(f"[BLING] Buscando página {page} ...")
            resp = await client.get(
                "https://api.bling.com.br/Api/v3/pedidos/vendas",
                headers={"Authorization": f"Bearer {token}"},
                params={"pagina": page, "limite": limit},
            )
            print(f"[BLING] Status: {resp.status_code}")

            if resp.status_code == 401:
                print("[BLING] Token expirado, tentando refresh...")
                try:
                    token = await auth.refresh(client)
                    print("[BLING] Refresh OK, repetindo requisição...")
                except Exception as e:
                    print(f"[BLING] ERRO no refresh: {e}")
                    break
                resp = await client.get(
                    "https://api.bling.com.br/Api/v3/pedidos/vendas",
                    headers={"Authorization": f"Bearer {token}"},
                    params={"pagina": page, "limite": limit},
                )
                print(f"[BLING] Status após refresh: {resp.status_code}")

            if resp.status_code != 200:
                print(f"[BLING] ERRO HTTP {resp.status_code}: {resp.text[:500]}")
                break

            data = resp.json()
            items = data.get("data", [])
            print(f"[BLING] Página {page}: {len(items)} pedidos recebidos")
            if not items:
                break

            all_pedidos.extend(items)

            if len(items) < limit:
                break

            page += 1

    print(f"[BLING] Total carregado: {len(all_pedidos)} pedidos")
    return all_pedidos


# ── Routes ─────────────────────────────────────────────────────────────────────

@app.get("/api/pedidos")
async def get_pedidos(
    pagina: int = 1,
    limite: int = 20,
    situacao: Optional[str] = None,
    busca: Optional[str] = None,
):
    pedidos = app.state.pedidos_cache

    if situacao and situacao != "todos":
        pedidos = [p for p in pedidos if str(p.get("situacao", {}).get("id", "")) == situacao]

    if busca:
        b = busca.lower()
        pedidos = [
            p for p in pedidos
            if b in str(p.get("numero", "")).lower()
            or b in (p.get("contato", {}).get("nome", "") or "").lower()
        ]

    total = len(pedidos)
    start = (pagina - 1) * limite
    end = start + limite
    page_items = pedidos[start:end]

    return {
        "data": page_items,
        "total": total,
        "pagina": pagina,
        "limite": limite,
        "ultima_atualizacao": app.state.last_update,
    }


@app.post("/api/pedidos/atualizar")
async def atualizar_pedidos(background_tasks: BackgroundTasks):
    async def _fetch():
        try:
            print("[TASK] Iniciando busca de pedidos no Bling...")
            pedidos = await fetch_all_pedidos(app.state.auth)
            app.state.pedidos_cache = pedidos
            app.state.last_update = datetime.now().isoformat()
            print(f"[TASK] Cache atualizado com {len(pedidos)} pedidos.")
        except Exception as e:
            import traceback
            print(f"[TASK] ERRO ao buscar pedidos: {e}")
            traceback.print_exc()

    background_tasks.add_task(_fetch)
    return {"message": "Atualização iniciada em background"}


@app.get("/api/pedidos/{pedido_id}")
async def get_pedido(pedido_id: int):
    auth = app.state.auth
    async with httpx.AsyncClient(timeout=30) as client:
        token = await auth.get_valid_token(client)

        # 1️⃣ Busca o pedido
        resp = await client.get(
            f"https://api.bling.com.br/Api/v3/pedidos/vendas/{pedido_id}",
            headers={"Authorization": f"Bearer {token}"},
        )
        if resp.status_code == 404:
            raise HTTPException(status_code=404, detail="Pedido não encontrado")
        if resp.status_code != 200:
            raise HTTPException(status_code=resp.status_code, detail=resp.text)

        pedido_data = resp.json()
        pedido = pedido_data.get("data", pedido_data)

        # 2️⃣ Busca os detalhes do contato se tiver ID
        contato_id = pedido.get("contato", {}).get("id")
        if contato_id:
            resp_contato = await client.get(
                f"https://api.bling.com.br/Api/v3/contatos/{contato_id}",
                headers={"Authorization": f"Bearer {token}"},
            )
            if resp_contato.status_code == 200:
                contato_completo = resp_contato.json().get("data", {})
                # Mescla os dados no objeto contato do pedido
                pedido["contato"].update({
                    "email":      contato_completo.get("email", ""),
                    "telefone":   contato_completo.get("telefone", ""),
                    "celular":    contato_completo.get("celular", ""),
                    "cpfCnpj":    contato_completo.get("numeroDocumento", ""),
                    "endereco":   contato_completo.get("endereco", {}),
                })

        return pedido_data  # retorna estrutura original com contato enriquecido


@app.post("/api/webhook/pedidos")
async def webhook_pedidos(request: Request):
    try:
        payload = await request.json()
    except Exception:
        payload = {}

    print(f"[WEBHOOK] Recebido: {json.dumps(payload, ensure_ascii=False)}")

    pedido_id = payload.get("data", {}).get("id")
    if pedido_id:
        auth = app.state.auth
        async with httpx.AsyncClient(timeout=30) as client:
            token = await auth.get_valid_token(client)
            resp = await client.get(
                f"https://api.bling.com.br/Api/v3/pedidos/vendas/{pedido_id}",
                headers={"Authorization": f"Bearer {token}"},
            )
            if resp.status_code == 200:
                updated = resp.json().get("data", {})
                cache = app.state.pedidos_cache
                for i, p in enumerate(cache):
                    if p.get("id") == pedido_id:
                        cache[i] = updated
                        break
                else:
                    cache.insert(0, updated)
                app.state.last_update = datetime.now().isoformat()

    return {"status": "ok"}


SITUACAO_NOMES = {
    0: "Sem situação",
    1: "Em aberto",
    2: "Atendido",
    3: "Cancelado",
    4: "Em andamento",
    5: "Vencido",
    6: "Em aberto",
    9: "Inativo",
    15: "Em digitação",
}

@app.get("/api/stats")
async def get_stats():
    pedidos = app.state.pedidos_cache
    total = len(pedidos)

    situacoes: dict[str, int] = {}
    valor_total = 0.0
    for p in pedidos:
        sit = p.get("situacao", {})
        nome = sit.get("nome") or SITUACAO_NOMES.get(sit.get("id"), "Desconhecida")
        situacoes[nome] = situacoes.get(nome, 0) + 1
        valor_total += float(p.get("totalProdutos") or p.get("total") or 0)

    return {
        "total_pedidos": total,
        "valor_total": round(valor_total, 2),
        "por_situacao": situacoes,
        "ultima_atualizacao": app.state.last_update,
    }


# Serve o frontend (deve ser o último mount)
app.mount("/", StaticFiles(directory="../frontend/static", html=True), name="static")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8001, reload=True)