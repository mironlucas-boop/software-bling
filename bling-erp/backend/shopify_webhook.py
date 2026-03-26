"""
shopify_webhook.py
──────────────────
Integração Shopify → Bling ERP

Fluxo:
  1. Shopify envia POST /webhook/shopify/pedidos quando um pedido é criado
  2. Extrai CPF do campo customizado (note_attributes, company ou GraphQL)
  3. Busca contato no Bling pelo email — reutiliza se existir, cria se não
  4. Cria o pedido no Bling com situação "Em aberto" (id=6)

Como usar:
  - Adicione este arquivo na pasta backend/
  - Em main.py, importe e registre o router:
      from shopify_webhook import router as shopify_router
      app.include_router(shopify_router)
  - Configure o webhook na Shopify para:
      URL: https://SEU-NGROK.ngrok.io/webhook/shopify/pedidos
      Formato: JSON
      Evento: orders/create
"""

import base64
import hashlib
import hmac
import json
import logging
from typing import Optional

import httpx
from fastapi import APIRouter, HTTPException, Request, Header
from config import settings

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/webhook", tags=["Shopify Webhook"])

# ── Configurações Shopify ─────────────────────────────────────────────────────
SHOPIFY_STORE   = "barbara-porto.myshopify.com"
SHOPIFY_TOKEN   = "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"
SHOPIFY_GRAPHQL = f"https://{SHOPIFY_STORE}/admin/api/2023-04/graphql.json"
SHOPIFY_BASE    = f"https://{SHOPIFY_STORE}/admin/api/2024-01"
SHOPIFY_HEADERS = {
    "X-Shopify-Access-Token": SHOPIFY_TOKEN,
    "Content-Type": "application/json",
}

# ── Constantes Bling ──────────────────────────────────────────────────────────
BLING_BASE         = "https://api.bling.com.br/Api/v3"
SITUACAO_EM_ABERTO = 6


# ── Verificação de assinatura Shopify (segurança) ─────────────────────────────
def verificar_assinatura_shopify(body: bytes, hmac_header: str) -> bool:
    """
    Verifica se o webhook realmente veio da Shopify.
    Requer SHOPIFY_WEBHOOK_SECRET nas configurações.
    Se não houver secret configurado, pula a verificação (modo dev).
    """
    secret = getattr(settings, "SHOPIFY_WEBHOOK_SECRET", None)
    if not secret:
        logger.warning("[SHOPIFY] SHOPIFY_WEBHOOK_SECRET não configurado — pulando verificação de assinatura.")
        return True

    digest = hmac.new(secret.encode(), body, hashlib.sha256).digest()
    computed = base64.b64encode(digest).decode()
    return hmac.compare_digest(computed, hmac_header or "")


# ── Busca cliente na Shopify ──────────────────────────────────────────────────
async def buscar_cliente_shopify(customer_id: int) -> dict:
    """Busca dados completos do cliente incluindo default_address."""
    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.get(
            f"{SHOPIFY_BASE}/customers/{customer_id}.json",
            headers=SHOPIFY_HEADERS,
        )
        if resp.status_code != 200:
            return {}
        return resp.json().get("customer", {})


async def resolver_fallback_addr(pedido_shopify: dict) -> dict:
    """
    Retorna o endereço de fallback do cliente quando o pedido não tem
    shipping_address nem billing_address (ex: vendas POS).
    """
    if pedido_shopify.get("shipping_address") or pedido_shopify.get("billing_address"):
        return {}
    customer_id = pedido_shopify.get("customer", {}).get("id")
    if not customer_id:
        return {}
    cliente = await buscar_cliente_shopify(customer_id)
    return cliente.get("default_address", {})


# ── Extração de dados do payload Shopify ──────────────────────────────────────
def limpar_documento(doc: str) -> str:
    """Remove pontuação do CPF/CNPJ."""
    return "".join(c for c in (doc or "") if c.isdigit())


def extrair_cpf(pedido_shopify: dict, fallback_addr: dict = None) -> str:
    """
    Tenta extrair CPF/CNPJ do pedido Shopify.
    Ordem: note_attributes → metafields → company (fallback_addr,
    customer.default_address, billing_address, shipping_address).
    """
    chaves_cpf = {"cpf", "cpf", "cpf", "documento", "cpf_cnpj", "cpf/cnpj", "tax_id"}

    # note_attributes: [{"name": "cpf", "value": "123.456.789-00"}, ...]
    for attr in pedido_shopify.get("note_attributes", []):
        if attr.get("name", "").strip().lower() in chaves_cpf:
            return limpar_documento(attr.get("value", ""))

    # customer metafields (fallback)
    customer = pedido_shopify.get("customer", {})
    for attr in customer.get("metafields", []):
        if attr.get("key", "").lower() in chaves_cpf:
            return limpar_documento(attr.get("value", ""))

    # Campo company em todas as fontes de endereço
    company = (
        (fallback_addr or {}).get("company", "")
        or customer.get("default_address", {}).get("company", "")
        or (pedido_shopify.get("billing_address") or {}).get("company", "")
        or (pedido_shopify.get("shipping_address") or {}).get("company", "")
    )
    doc = limpar_documento(company)
    if len(doc) in (11, 14):
        return doc

    return ""


async def extrair_cpf_graphql(client: httpx.AsyncClient, order_id: str) -> str:
    """Busca CPF/CNPJ via localizationExtensions no GraphQL da Shopify."""
    gid = f"gid://shopify/Order/{order_id}"
    query = """
    query($id: ID!) {
      order(id: $id) {
        localizationExtensions(first: 5) {
          edges {
            node {
              purpose
              title
              value
            }
          }
        }
      }
    }
    """
    try:
        resp = await client.post(
            SHOPIFY_GRAPHQL,
            headers=SHOPIFY_HEADERS,
            json={"query": query, "variables": {"id": gid}},
        )
        if resp.status_code != 200:
            return ""
        edges = (
            resp.json()
            .get("data", {})
            .get("order", {})
            .get("localizationExtensions", {})
            .get("edges", [])
        )
        for edge in edges:
            node = edge.get("node", {})
            if node.get("purpose") == "TAX":
                return limpar_documento(node.get("value", ""))
    except Exception as e:
        logger.warning(f"[CPF GraphQL] Erro: {e}")
    return ""


async def extrair_cpf_completo(
    client: httpx.AsyncClient, pedido_shopify: dict, fallback_addr: dict = None
) -> str:
    """Ordem: note_attributes → metafields → company → GraphQL."""
    cpf = extrair_cpf(pedido_shopify, fallback_addr)
    if cpf:
        return cpf
    return await extrair_cpf_graphql(client, str(pedido_shopify.get("id", "")))


def extrair_telefone(pedido_shopify: dict, fallback_addr: dict = None) -> str:
    """Prioriza telefone do cliente, depois do endereço de entrega, depois do fallback."""
    customer = pedido_shopify.get("customer", {})
    phone = customer.get("phone") or ""
    if not phone:
        phone = (pedido_shopify.get("shipping_address") or {}).get("phone") or ""
    if not phone and fallback_addr:
        phone = fallback_addr.get("phone") or ""
    return phone.replace("+55", "").strip()


def extrair_nome_completo(pedido_shopify: dict, fallback_addr: dict = None) -> str:
    """Extrai nome completo do cliente, com fallback para default_address."""
    customer = pedido_shopify.get("customer", {})
    first = customer.get("first_name", "")
    last  = customer.get("last_name", "")
    nome  = f"{first} {last}".strip()
    if not nome:
        billing = pedido_shopify.get("billing_address", {})
        nome = billing.get("name", "").strip()
    if not nome and fallback_addr:
        nome = fallback_addr.get("name", "").strip()
    return nome or "Cliente Shopify"


def montar_endereco_bling(pedido_shopify: dict, fallback_addr: dict = None) -> Optional[dict]:
    """Monta o objeto de endereço no formato esperado pelo Bling.
    Usa fallback_addr quando o pedido não tem shipping_address nem billing_address.
    """
    addr = (
        pedido_shopify.get("shipping_address")
        or pedido_shopify.get("billing_address")
        or fallback_addr
    )
    if not addr:
        return None

    uf = addr.get("province_code") or addr.get("province") or ""
    return {
        "endereco":    addr.get("address1", ""),
        "complemento": addr.get("address2", "") or "",
        "cep":         limpar_documento(addr.get("zip", "")),
        "bairro":      addr.get("address2", "") or "",
        "municipio":   addr.get("city", ""),
        "uf":          uf[:2].upper() if uf else "",
        "pais":        "BR",
    }


def montar_itens_bling(pedido_shopify: dict) -> list:
    """Converte line_items da Shopify para o formato de itens do Bling."""
    itens = []
    for item in pedido_shopify.get("line_items", []):
        bling_item = {
            "descricao":  item.get("title", "Produto"),
            "quantidade": item.get("quantity", 1),
            "valor":      float(item.get("price", 0)),
        }
        sku = item.get("sku", "")
        if sku:
            bling_item["codigo"] = sku
        itens.append(bling_item)
    return itens


# ── Operações no Bling ────────────────────────────────────────────────────────
async def buscar_contato_por_email(
    client: httpx.AsyncClient, token: str, email: str
) -> Optional[int]:
    """Busca contato no Bling pelo email. Retorna o ID se encontrar, None se não."""
    if not email:
        return None

    resp = await client.get(
        f"{BLING_BASE}/contatos",
        headers={"Authorization": f"Bearer {token}"},
        params={"email": email, "limite": 1},
    )
    if resp.status_code == 200:
        data = resp.json().get("data", [])
        if data:
            contato_id = data[0].get("id")
            logger.info(f"[BLING] Contato encontrado pelo email '{email}': id={contato_id}")
            return contato_id
    return None


async def criar_contato_bling(
    client: httpx.AsyncClient,
    token: str,
    pedido_shopify: dict,
    fallback_addr: dict = None,
) -> int:
    """Cria um novo contato no Bling e retorna o ID gerado."""
    fallback_addr = fallback_addr or {}
    nome     = extrair_nome_completo(pedido_shopify, fallback_addr)
    cpf      = await extrair_cpf_completo(client, pedido_shopify, fallback_addr)
    telefone = extrair_telefone(pedido_shopify, fallback_addr)
    customer = pedido_shopify.get("customer", {})
    email    = customer.get("email", "")
    endereco = montar_endereco_bling(pedido_shopify, fallback_addr)

    payload = {
        "nome":     nome,
        "email":    email,
        "telefone": telefone,
        "celular":  telefone,
        "tipo":     "F",
        "situacao": "A",
    }
    if cpf:
        payload["numeroDocumento"] = cpf
        if len(cpf) == 14:
            payload["tipo"] = "J"
    if endereco:
        payload["endereco"] = {"geral": endereco}

    resp = await client.post(
        f"{BLING_BASE}/contatos",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        json=payload,
    )
    if resp.status_code not in (200, 201):
        raise RuntimeError(f"Erro ao criar contato no Bling: {resp.status_code} — {resp.text[:300]}")

    contato_id = resp.json().get("data", {}).get("id")
    logger.info(f"[BLING] Contato criado: id={contato_id} nome='{nome}'")
    return contato_id


async def criar_pedido_bling(
    client: httpx.AsyncClient,
    token: str,
    pedido_shopify: dict,
    contato_id: int,
    fallback_addr: dict = None,
) -> dict:
    """Cria o pedido de venda no Bling e retorna os dados criados."""
    fallback_addr  = fallback_addr or {}
    itens = montar_itens_bling(pedido_shopify)
    if not itens:
        raise ValueError("Pedido da Shopify não contém itens válidos.")

    frete   = sum(float(s.get("price", 0)) for s in pedido_shopify.get("shipping_lines", []))
    desconto = float(pedido_shopify.get("total_discounts", 0))

    numero_shopify = pedido_shopify.get("order_number") or pedido_shopify.get("name", "")
    obs = f"Pedido Shopify #{numero_shopify}"
    if pedido_shopify.get("note"):
        obs += f" | Obs: {pedido_shopify['note']}"

    payload = {
        "contato":             {"id": contato_id},
        "situacao":            {"id": SITUACAO_EM_ABERTO},
        "itens":               itens,
        "observacoes":         obs,
        "observacoesInternas": f"Importado automaticamente via webhook Shopify. ID Shopify: {pedido_shopify.get('id')}",
    }
    if frete > 0:
        payload["transporte"] = {"fretePorConta": 1, "frete": frete}
    if desconto > 0:
        payload["desconto"] = {"tipo": "V", "valor": desconto}

    endereco = montar_endereco_bling(pedido_shopify, fallback_addr)
    if endereco:
        payload["enderecoEntrega"] = endereco

    resp = await client.post(
        f"{BLING_BASE}/pedidos/vendas",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        json=payload,
    )
    if resp.status_code not in (200, 201):
        raise RuntimeError(f"Erro ao criar pedido no Bling: {resp.status_code} — {resp.text[:500]}")

    dados = resp.json().get("data", {})
    logger.info(f"[BLING] Pedido criado: id={dados.get('id')} | Shopify #{numero_shopify}")
    return dados


# ── Endpoint principal ────────────────────────────────────────────────────────
@router.post("/shopify/pedidos")
async def receber_pedido_shopify(
    request: Request,
    x_shopify_hmac_sha256: Optional[str] = Header(None),
    x_shopify_topic: Optional[str] = Header(None),
):
    """
    Recebe o webhook de criação de pedido da Shopify e cria no Bling.

    Configurar na Shopify:
      Admin → Settings → Notifications → Webhooks
      Evento: Order creation
      URL: https://SEU-NGROK.ngrok.io/webhook/shopify/pedidos
      Formato: JSON
    """
    body = await request.body()

    # 1. Verifica assinatura
    if not verificar_assinatura_shopify(body, x_shopify_hmac_sha256 or ""):
        logger.warning("[SHOPIFY] Assinatura inválida — requisição rejeitada.")
        raise HTTPException(status_code=401, detail="Assinatura Shopify inválida.")

    # 2. Parse do payload
    try:
        pedido_shopify = json.loads(body)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Payload JSON inválido.")

    shopify_id = pedido_shopify.get("id")
    numero     = pedido_shopify.get("order_number") or pedido_shopify.get("name")
    customer   = pedido_shopify.get("customer", {})
    email      = customer.get("email", "")

    logger.info(f"[SHOPIFY] Pedido recebido: #{numero} (id={shopify_id}) | cliente: {email}")

    # 3. Resolve fallback de endereço para pedidos POS
    fallback_addr = await resolver_fallback_addr(pedido_shopify)
    if fallback_addr:
        logger.info(f"[SHOPIFY] Endereço obtido do cliente: {fallback_addr.get('address1', '')} — {fallback_addr.get('city', '')}")
    elif not pedido_shopify.get("shipping_address") and not pedido_shopify.get("billing_address"):
        logger.warning(f"[SHOPIFY] Pedido #{numero} sem endereço e sem default_address no cliente.")

    # 4. Obtém token Bling
    auth = request.app.state.auth
    async with httpx.AsyncClient(timeout=30) as client:
        token = await auth.get_valid_token(client)

        # 5. Busca ou cria contato no Bling
        contato_id = await buscar_contato_por_email(client, token, email)
        if contato_id is None:
            logger.info(f"[BLING] Contato não encontrado para '{email}' — criando novo...")
            contato_id = await criar_contato_bling(client, token, pedido_shopify, fallback_addr)
        else:
            logger.info(f"[BLING] Reutilizando contato existente id={contato_id}")

        # 6. Cria o pedido no Bling
        pedido_bling = await criar_pedido_bling(client, token, pedido_shopify, contato_id, fallback_addr)

    return {
        "status":         "ok",
        "shopify_order":  numero,
        "bling_pedido_id": pedido_bling.get("id"),
        "contato_id":     contato_id,
        "message":        f"Pedido #{numero} criado no Bling com sucesso.",
    }


# ── Endpoint de teste (simula um pedido sem precisar da Shopify) ──────────────
@router.post("/shopify/pedidos/teste")
async def testar_webhook(request: Request):
    """
    Endpoint de teste: envia um payload simulado de pedido Shopify
    para validar a integração sem precisar configurar a Shopify.

    Uso:
      POST http://localhost:8001/webhook/shopify/pedidos/teste
      Body: payload real copiado do Postman ou do painel Shopify
    """
    body = await request.body()
    try:
        pedido_shopify = json.loads(body)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Payload JSON inválido.")

    # Resolve fallback de endereço para pedidos POS
    fallback_addr = await resolver_fallback_addr(pedido_shopify)

    auth = request.app.state.auth
    async with httpx.AsyncClient(timeout=30) as client:
        token  = await auth.get_valid_token(client)
        email  = pedido_shopify.get("customer", {}).get("email", "")

        contato_id = await buscar_contato_por_email(client, token, email)
        if contato_id is None:
            contato_id = await criar_contato_bling(client, token, pedido_shopify, fallback_addr)

        pedido_bling = await criar_pedido_bling(client, token, pedido_shopify, contato_id, fallback_addr)

    return {
        "status":          "ok (TESTE)",
        "shopify_order":   pedido_shopify.get("order_number"),
        "bling_pedido_id": pedido_bling.get("id"),
        "contato_id":      contato_id,
    }