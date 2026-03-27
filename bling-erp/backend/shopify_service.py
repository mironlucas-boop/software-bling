"""
shopify_service.py
──────────────────
Serviço de integração Shopify → Bling
"""

import logging
from typing import AsyncGenerator, Optional
import httpx
import json
import asyncio
from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse
from config import settings

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/shopify", tags=["Shopify Integration"])

# ── Configurações Shopify ─────────────────────────────────────────────────────
SHOPIFY_STORE   = "barbara-porto.myshopify.com"
SHOPIFY_TOKEN   = "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"
SHOPIFY_GRAPHQL = f"https://{SHOPIFY_STORE}/admin/api/2023-04/graphql.json"
SHOPIFY_BASE    = f"https://{SHOPIFY_STORE}/admin/api/2024-01"
SHOPIFY_HEADERS = {
    "X-Shopify-Access-Token": SHOPIFY_TOKEN,
    "Content-Type": "application/json",
}

BLING_BASE         = "https://api.bling.com.br/Api/v3"
SITUACAO_EM_ABERTO = 6
VENDEDOR_ID        = 15596876569  # fixo por enquanto


# ── Helpers de formatação ─────────────────────────────────────────────────────
def limpar_doc(doc: str) -> str:
    return "".join(c for c in (doc or "") if c.isdigit())


def extrair_cpf(pedido: dict, fallback_addr: dict = None) -> str:
    """Tenta extrair CPF de note_attributes e campo company (sem I/O)."""
    chaves = {"cpf", "documento", "cpf_cnpj", "cpf/cnpj", "tax_id"}
    for attr in pedido.get("note_attributes", []):
        if attr.get("name", "").strip().lower() in chaves:
            return limpar_doc(attr.get("value", ""))

    company = (
        (fallback_addr or {}).get("company", "")
        or (pedido.get("customer") or {}).get("default_address", {}).get("company", "")
        or (pedido.get("billing_address") or {}).get("company", "")
        or (pedido.get("shipping_address") or {}).get("company", "")
    )
    doc = limpar_doc(company)
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
                return limpar_doc(node.get("value", ""))
    except Exception as e:
        logger.warning(f"[CPF GraphQL] Erro: {e}")
    return ""


async def extrair_cpf_completo(
    client: httpx.AsyncClient, pedido: dict, fallback_addr: dict = None
) -> str:
    """Ordem: note_attributes → company (todas as fontes) → GraphQL."""
    cpf = extrair_cpf(pedido, fallback_addr)
    if cpf:
        return cpf
    return await extrair_cpf_graphql(client, str(pedido.get("id", "")))


def extrair_telefone(pedido: dict, fallback_addr: dict = None) -> str:
    phone = pedido.get("customer", {}).get("phone") or ""
    if not phone:
        phone = (pedido.get("shipping_address") or {}).get("phone") or ""
    if not phone and fallback_addr:
        phone = fallback_addr.get("phone") or ""
    return phone.replace("+55", "").strip()


def extrair_nome(pedido: dict, fallback_addr: dict = None) -> str:
    customer = pedido.get("customer", {})
    nome = f"{customer.get('first_name', '')} {customer.get('last_name', '')}".strip()
    if not nome and fallback_addr:
        nome = fallback_addr.get("name", "").strip()
    return nome or "Cliente Shopify"


def montar_endereco(pedido: dict, fallback_addr: dict = None) -> Optional[dict]:
    addr = pedido.get("shipping_address") or pedido.get("billing_address") or fallback_addr
    if not addr:
        return None

    uf = addr.get("province_code") or addr.get("province") or ""

    # 🔥 LOGRADOURO + NÚMERO
    endereco_raw = addr.get("address1", "")
    endereco = endereco_raw
    numero = "0"

    if "," in endereco_raw:
        partes = endereco_raw.split(",", 1)
        endereco = partes[0].strip()
        numero = partes[1].strip()

    # 🔥 COMPLEMENTO + BAIRRO
    address2 = addr.get("address2", "") or ""
    complemento = ""
    bairro = ""

    if "," in address2:
        partes = address2.split(",", 1)
        complemento = partes[0].strip()
        bairro = partes[1].strip()
    else:
        bairro = address2.strip()

    return {
        "endereco": endereco,
        "numero": numero,  # 👈 NOVO CAMPO CORRETO
        "complemento": complemento,
        "bairro": bairro,
        "cep": limpar_doc(addr.get("zip", "")),
        "municipio": addr.get("city", ""),
        "uf": uf[:2].upper() if uf else "",
        "pais": "BR",
    }


def montar_etiqueta(pedido: dict, fallback_addr: dict = None) -> Optional[dict]:
    addr = pedido.get("shipping_address") or pedido.get("billing_address") or fallback_addr
    if not addr:
        return None

    nome = (
        addr.get("name")
        or f"{addr.get('first_name', '')} {addr.get('last_name', '')}".strip()
        or extrair_nome(pedido, fallback_addr)
    )

    uf = addr.get("province_code") or addr.get("province") or ""

    # 🔥 LOGRADOURO + NÚMERO
    endereco_raw = addr.get("address1", "")
    endereco = endereco_raw
    numero = "0"

    if "," in endereco_raw:
        partes = endereco_raw.split(",", 1)
        endereco = partes[0].strip()
        numero = partes[1].strip() or "0"

    # 🔥 COMPLEMENTO + BAIRRO
    address2 = addr.get("address2", "") or ""
    complemento = ""
    bairro = ""

    if "," in address2:
        partes = address2.split(",", 1)
        complemento = partes[0].strip()
        bairro = partes[1].strip()
    else:
        bairro = address2.strip()

    return {
        "nome": nome,
        "endereco": endereco,
        "numero": numero,
        "complemento": complemento,  # 👈 AGORA VAI
        "bairro": bairro,            # 👈 AGORA VAI
        "municipio": addr.get("city", ""),
        "uf": uf[:2].upper() if uf else "",
        "cep": limpar_doc(addr.get("zip", "")),
        "nomePais": "Brasil",
    }

# ── Busca produto no Bling pelo SKU ──────────────────────────────────────────
async def buscar_id_produto_bling(
    client: httpx.AsyncClient, token: str, sku: str
) -> int:
    """Busca o ID do produto no Bling pelo código (SKU)."""
    resp = await client.get(
        f"{BLING_BASE}/produtos",
        headers={"Authorization": f"Bearer {token}"},
        params={"codigo": sku},
    )
    await asyncio.sleep(0.4)
    if resp.status_code != 200:
        raise RuntimeError(f"Erro ao buscar produto '{sku}': {resp.status_code} — {resp.text[:200]}")
    data = resp.json().get("data", [])
    if not data:
        raise RuntimeError(f"Produto com SKU '{sku}' não encontrado no Bling.")
    return data[0]["id"]


async def montar_itens(
    client: httpx.AsyncClient, token: str, pedido: dict
) -> list:
    """Busca o ID de cada produto no Bling pelo SKU e monta a lista de itens."""
    itens = []
    for item in pedido.get("line_items", []):
        sku = item.get("sku", "")
        if not sku:
            raise RuntimeError(
                f"Item '{item.get('title')}' não possui SKU — não é possível localizar no Bling."
            )
        produto_id = await buscar_id_produto_bling(client, token, sku)
        itens.append({
            "produto":    {"id": produto_id},
            "descricao":  item.get("title", ""),
            "quantidade": item.get("quantity", 1),
            "valor":      float(item.get("price", 0)),
            "unidade":    "UN",
        })
    return itens


# ── Busca pedido e cliente na Shopify ─────────────────────────────────────────
async def buscar_pedido_shopify(order_id: str) -> dict:
    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.get(
            f"{SHOPIFY_BASE}/orders/{order_id}.json",
            headers=SHOPIFY_HEADERS,
        )
        if resp.status_code == 404:
            raise ValueError(f"Pedido #{order_id} não encontrado na Shopify.")
        if resp.status_code != 200:
            raise RuntimeError(f"Erro Shopify {resp.status_code}: {resp.text[:200]}")
        return resp.json().get("order", {})


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


async def resolver_fallback_addr(pedido: dict) -> dict:
    """
    Retorna o endereço de fallback do cliente quando o pedido não tem
    shipping_address nem billing_address (ex: vendas POS).
    """
    if pedido.get("shipping_address") or pedido.get("billing_address"):
        return {}
    customer_id = pedido.get("customer", {}).get("id")
    if not customer_id:
        return {}
    cliente = await buscar_cliente_shopify(customer_id)
    return cliente.get("default_address", {})


# ── Operações Bling ───────────────────────────────────────────────────────────
async def buscar_contato_documento(
    client: httpx.AsyncClient,
    token: str,
    documento: str,
    log_func=None
) -> Optional[int]:

    if not documento:
        return None

    url = f"{BLING_BASE}/contatos"
    params = {
        "numeroDocumento": documento,
        "limite": 1
    }

    # 🧪 DEBUG REQUEST
    if log_func:
        log_func("🔎 Buscar contato por CPF/CNPJ no Bling:")
        log_func(json.dumps({
            "url": url,
            "params": params
        }, indent=2, ensure_ascii=False))

    resp = await client.get(
        url,
        headers={"Authorization": f"Bearer {token}"},
        params=params,
    )

    # 🧪 DEBUG RESPONSE
    if log_func:
        log_func(f"📨 Response contato: {resp.status_code}")
        log_func(resp.text)

    if resp.status_code == 200:
        data = resp.json().get("data", [])
        if data:
            return data[0].get("id")

    return None


async def criar_contato(
    client: httpx.AsyncClient,
    token: str,
    pedido: dict,
    fallback_addr: dict = None,
    log_func=None
) -> int:

    fallback_addr = fallback_addr or {}

    nome = extrair_nome(pedido, fallback_addr)
    cpf  = await extrair_cpf_completo(client, pedido, fallback_addr)
    tel  = extrair_telefone(pedido, fallback_addr)
    end  = montar_endereco(pedido, fallback_addr)

    customer = pedido.get("customer", {})

    payload = {
        "nome":     nome,
        "email":    customer.get("email", ""),
        "telefone": tel,
        "celular":  tel,
        "tipo":     "J" if len(cpf) == 14 else "F",
        "situacao": "A",
    }
    if cpf:
        payload["numeroDocumento"] = cpf
    if end:
        payload["endereco"] = {"geral": end}

    # 🧪 DEBUG DO JSON
    if log_func:
        log_func("📦 JSON CRIAR CONTATO:")
        log_func(json.dumps(payload, indent=2, ensure_ascii=False))

    resp = await client.post(
        f"{BLING_BASE}/contatos",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json=payload,
    )

    # 🧪 DEBUG DO JSON
    if log_func:
        log_func("📦 JSON CRIAR CONTATO:")
        log_func(json.dumps(payload, indent=2, ensure_ascii=False))

    await asyncio.sleep(0.4)

    if resp.status_code not in (200, 201):
        raise RuntimeError(f"Erro ao criar contato: {resp.status_code} — {resp.text[:300]}")
    return resp.json()["data"]["id"]


# _______ Função ______________________________ #

async def criar_pedido_bling(
    client: httpx.AsyncClient,
    token: str,
    pedido: dict,
    contato_id: int,
    fallback_addr: dict = None,
    log_func=None,  # 👈 NOVO
) -> dict:
    import asyncio
    import json

    fallback_addr = fallback_addr or {}

    def to_float(valor):
        if valor is None:
            return 0.0
        if isinstance(valor, (int, float)):
            return float(valor)
        return float(str(valor).replace(",", "."))

    itens = await montar_itens(client, token, pedido)
    if not itens:
        raise ValueError("Nenhum item encontrado no pedido.")

    frete    = sum(to_float(s.get("price", 0)) for s in pedido.get("shipping_lines", []))
    desconto = to_float(pedido.get("total_discounts", 0))
    total    = to_float(pedido.get("total_price", 0))

    numero      = pedido.get("order_number") or pedido.get("name", "")
    data_pedido = pedido.get("created_at", "")[:10]

    obs = f"Pedido Shopify #{numero}"
    if pedido.get("note"):
        obs += f" | {pedido['note']}"

    payload = {
        "numeroLoja":         str(pedido.get("id", "")),
        "numeroPedidoCompra": str(numero),
        "data":               data_pedido,
        "dataSaida":          data_pedido,
        "contato":            {"id": contato_id},

        # NÃO enviar situacao
        "vendedor": {"id": VENDEDOR_ID},
        "itens":    itens,

        "parcelas": [
            {
                "dataVencimento": data_pedido,
                "valor":          round(total, 2),
                "observacoes":    "PIX",
            }
        ],

        "observacoes":         obs,
        "observacoesInternas": f"Importado via integração Shopify. ID: {pedido.get('id')}",
    }

    if desconto > 0:
        payload["desconto"] = {
            "tipo": "V",
            "valor": round(desconto, 2)
        }

    etiqueta = montar_etiqueta(pedido, fallback_addr)
    payload["transporte"] = {
        "fretePorConta":     0,
        "frete":             round(frete, 2),
        "quantidadeVolumes": 1,
    }

    if etiqueta:
        payload["transporte"]["etiqueta"] = etiqueta

    # 🔥 ENVIA PRO HTML (não usa print!)
    if log_func:
        log_func("📦 Payload enviado ao Bling:")
        log_func(json.dumps(payload, indent=2, ensure_ascii=False))

    await asyncio.sleep(0.4)

    resp = await client.post(
        f"{BLING_BASE}/pedidos/vendas",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json"
        },
        json=payload,
    )

    if log_func:
        log_func(f"📨 Resposta Bling: {resp.status_code}")
        log_func(resp.text)

    if resp.status_code not in (200, 201):
        raise RuntimeError(
            f"Erro ao criar pedido: {resp.status_code} — {resp.text[:500]}"
        )

    return resp.json().get("data", {})


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.get("/pedido/{order_id}")
async def get_pedido_shopify(order_id: str):
    """Busca e retorna os dados de um pedido da Shopify para preview."""
    try:
        pedido = await buscar_pedido_shopify(order_id)
    except ValueError as e:
        return {"erro": str(e)}
    except Exception as e:
        return {"erro": f"Erro ao buscar pedido: {str(e)}"}

    fallback_addr = await resolver_fallback_addr(pedido)

    customer = pedido.get("customer", {})
    addr = (
        pedido.get("shipping_address")
        or pedido.get("billing_address")
        or fallback_addr
        or {}
    )

    async with httpx.AsyncClient(timeout=20) as client:
        cpf = await extrair_cpf_completo(client, pedido, fallback_addr)

    codigo_cobranca = next(
        (a.get("value", "") for a in pedido.get("note_attributes", [])
         if a.get("name") == "payment_additional_cc_nsu"),
        ""
    )

    return {
        "id":               pedido.get("id"),
        "numero":           pedido.get("order_number") or pedido.get("name"),
        "canal":            pedido.get("source_name", ""),
        "data":             pedido.get("created_at", "")[:10],
        "status_pagamento": pedido.get("financial_status"),
        "status_entrega":   pedido.get("fulfillment_status") or "pending",
        "cliente": {
            "nome":     extrair_nome(pedido, fallback_addr),
            "email":    customer.get("email", ""),
            "telefone": extrair_telefone(pedido, fallback_addr),
            "cpf":      cpf,
        },
        "endereco": {
            "logradouro": addr.get("address1", ""),
            "bairro":     addr.get("address2", ""),
            "cidade":     addr.get("city", ""),
            "uf":         (addr.get("province_code") or "")[:2].upper(),
            "cep":        addr.get("zip", ""),
        },
        "itens": [
            {
                "titulo":     i.get("title"),
                "sku":        i.get("sku", ""),
                "quantidade": i.get("quantity"),
                "preco":      float(i.get("price", 0)),
            }
            for i in pedido.get("line_items", [])
        ],
        "frete":           sum(float(s.get("price", 0)) for s in pedido.get("shipping_lines", [])),
        "desconto":        float(pedido.get("total_discounts", 0)),
        "total":           float(pedido.get("total_price", 0)),
        "obs":             pedido.get("note", ""),
        "codigo_cobranca": codigo_cobranca,
    }


@router.get("/integrar/{order_id}")
async def integrar_pedido(order_id: str, request: Request):
    """
    Executa a integração Shopify → Bling com logs em tempo real via SSE.
    """

    async def executar() -> AsyncGenerator[str, None]:
        def log(tipo: str, msg: str):
            return f"data: {json.dumps({'tipo': tipo, 'msg': msg})}\n\n"

        try:
            yield log("info", f"🔍 Buscando pedido #{order_id} na Shopify...")
            pedido = await buscar_pedido_shopify(order_id)
            numero = pedido.get("order_number") or pedido.get("name")
            yield log("ok", f"✅ Pedido #{numero} encontrado — {len(pedido.get('line_items', []))} item(s)")

            # Resolve fallback de endereço
            fallback_addr = {}
            customer_id = pedido.get("customer", {}).get("id")

            if not pedido.get("shipping_address") and not pedido.get("billing_address"):
                if customer_id:
                    yield log("info", "📍 Pedido sem endereço — buscando dados do cliente...")
                    cliente_completo = await buscar_cliente_shopify(customer_id)
                    fallback_addr = cliente_completo.get("default_address", {})
                    if fallback_addr:
                        yield log("ok", f"✅ Endereço obtido: {fallback_addr.get('address1', '')}")
                    else:
                        yield log("info", "⚠️ Cliente sem endereço padrão")
                else:
                    yield log("info", "⚠️ Pedido sem endereço e sem cliente")

            email = pedido.get("customer", {}).get("email", "")
            nome  = extrair_nome(pedido, fallback_addr)

            auth = request.app.state.auth

            async with httpx.AsyncClient(timeout=30) as client:
                token = await auth.get_valid_token(client)
                yield log("info", "🔑 Token Bling obtido com sucesso")

                # 🔥 CPF
                cpf = await extrair_cpf_completo(client, pedido, fallback_addr)
                yield log("info", f"👤 Cliente: {nome} | {email} | CPF: {cpf or 'não informado'}")

                # 🚨 TRAVA DE CPF
                if not cpf or len(cpf) not in (11, 14):
                    yield log("erro", "❌ Pedido sem CPF válido — não é possível integrar")
                    return

                # ─────────────────────────────
                # 🔎 BUSCAR CONTATO POR CPF
                # ─────────────────────────────
                debug_logs = []

                def log_func(msg):
                    debug_logs.append(msg)

                contato_id = await buscar_contato_documento(
                    client,
                    token,
                    cpf,
                    log_func=log_func
                )

                for msg in debug_logs:
                    yield log("debug", msg)

                if contato_id:
                    yield log("ok", f"✅ Contato encontrado — ID: {contato_id}")
                else:
                    yield log("info", "➕ Contato não encontrado — criando...")
                    contato_id = await criar_contato(client, token, pedido, fallback_addr, log_func=log_func)
                    yield log("ok", f"✅ Contato criado — ID: {contato_id}")

                # ─────────────────────────────
                # 📦 ITENS
                # ─────────────────────────────
                yield log("info", "🔎 Localizando produtos no Bling...")
                for item in pedido.get("line_items", []):
                    yield log("info", f"→ {item.get('quantity')}x {item.get('title')} (SKU: {item.get('sku')})")

                # ─────────────────────────────
                # 📋 CRIAR PEDIDO
                # ─────────────────────────────
                yield log("info", "📋 Criando pedido no Bling...")

                debug_logs = []

                def log_func(msg):
                    debug_logs.append(msg)

                pedido_bling = await criar_pedido_bling(
                    client,
                    token,
                    pedido,
                    contato_id,
                    fallback_addr,
                    log_func=log_func
                )

                bling_id = pedido_bling.get("id")

                for msg in debug_logs:
                    yield log("debug", msg)

                yield log("ok", f"✅ Pedido criado — ID: {bling_id}")
                yield log("sucesso", f"🎉 Integração concluída! Shopify #{numero} → Bling {bling_id}")

        except Exception as e:
            yield log("erro", f"❌ Erro: {str(e)}")

        yield "data: {\"tipo\": \"fim\"}\n\n"

    return StreamingResponse(
        executar(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Access-Control-Allow-Origin": "*",
        },
    )