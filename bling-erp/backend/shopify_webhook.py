"""
shopify_webhook.py
──────────────────
Integração Shopify → Bling ERP

Fluxo:
  1. Shopify envia POST /webhook/shopify/pedidos quando um pedido é criado
  2. Extrai CPF do campo customizado (note_attributes ou custom attributes)
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

# ── Constantes Bling ──────────────────────────────────────────────────────────
BLING_BASE = "https://api.bling.com.br/Api/v3"
SITUACAO_EM_ABERTO = 6  # "Em aberto" no Bling


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
    import base64
    computed = base64.b64encode(digest).decode()
    return hmac.compare_digest(computed, hmac_header or "")





# ── Extração de dados do payload Shopify ──────────────────────────────────────
def extrair_cpf(pedido_shopify: dict) -> str:
    """
    Tenta extrair CPF/CNPJ do pedido Shopify.
    A Shopify guarda campos customizados em 'note_attributes'.
    Chaves comuns: 'cpf', 'CPF', 'documento', 'cpf_cnpj'
    """
    chaves_cpf = {"cpf", "CPF", "Cpf", "documento", "cpf_cnpj", "cpf/cnpj", "tax_id"}

    # note_attributes: [{"name": "cpf", "value": "123.456.789-00"}, ...]
    for attr in pedido_shopify.get("note_attributes", []):
        if attr.get("name", "").strip().lower() in {c.lower() for c in chaves_cpf}:
            return limpar_documento(attr.get("value", ""))

    # customer tax_exemptions ou metafields (fallback)
    customer = pedido_shopify.get("customer", {})
    for attr in customer.get("metafields", []):
        if attr.get("key", "").lower() in {c.lower() for c in chaves_cpf}:
            return limpar_documento(attr.get("value", ""))

    return ""


def limpar_documento(doc: str) -> str:
    """Remove pontuação do CPF/CNPJ."""
    return "".join(c for c in (doc or "") if c.isdigit())


def extrair_telefone(pedido_shopify: dict) -> str:
    """Prioriza telefone do cliente, depois do endereço de entrega."""
    customer = pedido_shopify.get("customer", {})
    phone = customer.get("phone") or ""
    if not phone:
        shipping = pedido_shopify.get("shipping_address", {})
        phone = shipping.get("phone") or ""
    # Normaliza: remove +55 e não-dígitos exceto espaço e hífen
    phone = phone.replace("+55", "").strip()
    return phone


def extrair_nome_completo(pedido_shopify: dict) -> str:
    customer = pedido_shopify.get("customer", {})
    first = customer.get("first_name", "")
    last = customer.get("last_name", "")
    nome = f"{first} {last}".strip()
    if not nome:
        # fallback para endereço de cobrança
        billing = pedido_shopify.get("billing_address", {})
        nome = billing.get("name", "Cliente Shopify")
    return nome


def montar_endereco_bling(pedido_shopify: dict) -> Optional[dict]:
    """Monta o objeto de endereço no formato esperado pelo Bling."""
    addr = pedido_shopify.get("shipping_address") or pedido_shopify.get("billing_address")
    if not addr:
        return None

    # Tenta separar endereço e número
    address1 = addr.get("address1", "")
    address2 = addr.get("address2", "")

    return {
        "endereco": address1,
        "complemento": address2,
        "cep": limpar_documento(addr.get("zip", "")),
        "bairro": addr.get("address2", ""),  # Shopify não tem campo bairro — usa address2
        "municipio": addr.get("city", ""),
        "uf": addr.get("province_code", addr.get("province", ""))[:2].upper() if addr.get("province_code") or addr.get("province") else "",
        "pais": "BR",
    }


def montar_itens_bling(pedido_shopify: dict) -> list:
    """Converte line_items da Shopify para o formato de itens do Bling."""
    itens = []
    for item in pedido_shopify.get("line_items", []):
        bling_item = {
            "descricao": item.get("title", "Produto"),
            "quantidade": item.get("quantity", 1),
            "valor": float(item.get("price", 0)),
        }

        # SKU → código do produto no Bling (se existir)
        sku = item.get("sku", "")
        if sku:
            bling_item["codigo"] = sku

        itens.append(bling_item)

    return itens


# ── Operações no Bling ────────────────────────────────────────────────────────
async def buscar_contato_por_email(client: httpx.AsyncClient, token: str, email: str) -> Optional[int]:
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


async def criar_contato_bling(client: httpx.AsyncClient, token: str, pedido_shopify: dict) -> int:
    """Cria um novo contato no Bling e retorna o ID gerado."""
    nome = extrair_nome_completo(pedido_shopify)
    cpf = extrair_cpf(pedido_shopify)
    telefone = extrair_telefone(pedido_shopify)
    customer = pedido_shopify.get("customer", {})
    email = customer.get("email", "")
    endereco = montar_endereco_bling(pedido_shopify)

    payload = {
        "nome": nome,
        "email": email,
        "telefone": telefone,
        "celular": telefone,
        "tipo": "F",  # Pessoa Física — ajuste para "J" se CNPJ
        "situacao": "A",
    }

    if cpf:
        payload["numeroDocumento"] = cpf
        # Se CNPJ (14 dígitos), muda tipo para Jurídica
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


async def criar_pedido_bling(client: httpx.AsyncClient, token: str, pedido_shopify: dict, contato_id: int) -> dict:
    """Cria o pedido de venda no Bling e retorna os dados criados."""
    itens = montar_itens_bling(pedido_shopify)

    if not itens:
        raise ValueError("Pedido da Shopify não contém itens válidos.")

    # Frete
    frete = 0.0
    for shipping in pedido_shopify.get("shipping_lines", []):
        frete += float(shipping.get("price", 0))

    # Desconto
    desconto = float(pedido_shopify.get("total_discounts", 0))

    # Número do pedido na Shopify — salvo nas observações para rastreabilidade
    numero_shopify = pedido_shopify.get("order_number") or pedido_shopify.get("name", "")
    obs = f"Pedido Shopify #{numero_shopify}"
    if pedido_shopify.get("note"):
        obs += f" | Obs: {pedido_shopify['note']}"

    payload = {
        "contato": {"id": contato_id},
        "situacao": {"id": SITUACAO_EM_ABERTO},
        "itens": itens,
        "observacoes": obs,
        "observacoesInternas": f"Importado automaticamente via webhook Shopify. ID Shopify: {pedido_shopify.get('id')}",
    }

    # Frete
    if frete > 0:
        payload["transporte"] = {
            "fretePorConta": 1,  # 1 = destinatário
            "frete": frete,
        }

    # Desconto total
    if desconto > 0:
        payload["desconto"] = {
            "tipo": "V",  # V = valor fixo, P = percentual
            "valor": desconto,
        }

    # Endereço de entrega
    endereco = montar_endereco_bling(pedido_shopify)
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
    numero = pedido_shopify.get("order_number") or pedido_shopify.get("name")
    customer = pedido_shopify.get("customer", {})
    email = customer.get("email", "")

    logger.info(f"[SHOPIFY] Pedido recebido: #{numero} (id={shopify_id}) | cliente: {email}")

    # 3. Obtém token Bling
    auth = request.app.state.auth
    async with httpx.AsyncClient(timeout=30) as client:
        token = await auth.get_valid_token(client)

        # 4. Busca ou cria contato no Bling
        contato_id = await buscar_contato_por_email(client, token, email)

        if contato_id is None:
            logger.info(f"[BLING] Contato não encontrado para '{email}' — criando novo...")
            contato_id = await criar_contato_bling(client, token, pedido_shopify)
        else:
            logger.info(f"[BLING] Reutilizando contato existente id={contato_id}")

        # 5. Cria o pedido no Bling
        pedido_bling = await criar_pedido_bling(client, token, pedido_shopify, contato_id)

    return {
        "status": "ok",
        "shopify_order": numero,
        "bling_pedido_id": pedido_bling.get("id"),
        "contato_id": contato_id,
        "message": f"Pedido #{numero} criado no Bling com sucesso.",
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

    auth = request.app.state.auth
    async with httpx.AsyncClient(timeout=30) as client:
        token = await auth.get_valid_token(client)
        email = pedido_shopify.get("customer", {}).get("email", "")

        contato_id = await buscar_contato_por_email(client, token, email)
        if contato_id is None:
            contato_id = await criar_contato_bling(client, token, pedido_shopify)

        pedido_bling = await criar_pedido_bling(client, token, pedido_shopify, contato_id)

    return {
        "status": "ok (TESTE)",
        "shopify_order": pedido_shopify.get("order_number"),
        "bling_pedido_id": pedido_bling.get("id"),
        "contato_id": contato_id,
    }