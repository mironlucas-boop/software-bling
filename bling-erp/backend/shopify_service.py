"""
shopify_service.py
──────────────────
Serviço de integração Shopify → Bling

Responsável por:
  - Buscar pedido na API Admin da Shopify
  - Executar a sequência de integração com logs detalhados
  - Criar/reutilizar contato no Bling
  - Criar pedido no Bling
"""

import logging
from typing import AsyncGenerator, Optional
import httpx
import json
from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse
from config import settings

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/shopify", tags=["Shopify Integration"])

# ── Configurações Shopify ─────────────────────────────────────────────────────
SHOPIFY_STORE    = "barbara-porto.myshopify.com"
SHOPIFY_TOKEN    = "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"
SHOPIFY_BASE     = f"https://{SHOPIFY_STORE}/admin/api/2024-01"
SHOPIFY_HEADERS  = {
    "X-Shopify-Access-Token": SHOPIFY_TOKEN,
    "Content-Type": "application/json",
}

BLING_BASE           = "https://api.bling.com.br/Api/v3"
SITUACAO_EM_ABERTO   = 6


# ── Helpers de formatação ─────────────────────────────────────────────────────
def limpar_doc(doc: str) -> str:
    return "".join(c for c in (doc or "") if c.isdigit())


def extrair_cpf(pedido: dict) -> str:
    chaves = {"cpf", "documento", "cpf_cnpj", "cpf/cnpj", "tax_id"}
    for attr in pedido.get("note_attributes", []):
        if attr.get("name", "").strip().lower() in chaves:
            return limpar_doc(attr.get("value", ""))
    return ""


def extrair_telefone(pedido: dict) -> str:
    phone = pedido.get("customer", {}).get("phone") or ""
    if not phone:
        phone = pedido.get("shipping_address", {}).get("phone") or ""
    return phone.replace("+55", "").strip()


def montar_endereco(pedido: dict) -> Optional[dict]:
    addr = pedido.get("shipping_address") or pedido.get("billing_address")
    if not addr:
        return None
    uf = addr.get("province_code") or addr.get("province") or ""
    return {
        "endereco":    addr.get("address1", ""),
        "complemento": addr.get("address2", ""),
        "cep":         limpar_doc(addr.get("zip", "")),
        "bairro":      addr.get("address2", ""),
        "municipio":   addr.get("city", ""),
        "uf":          uf[:2].upper() if uf else "",
        "pais":        "BR",
    }


def montar_itens(pedido: dict) -> list:
    itens = []
    for item in pedido.get("line_items", []):
        i = {
            "descricao":  item.get("title", "Produto"),
            "quantidade": item.get("quantity", 1),
            "valor":      float(item.get("price", 0)),
        }
        if item.get("sku"):
            i["codigo"] = item["sku"]
        itens.append(i)
    return itens


# ── Busca pedido na Shopify ───────────────────────────────────────────────────
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


# ── Operações Bling ───────────────────────────────────────────────────────────
async def buscar_contato_email(client: httpx.AsyncClient, token: str, email: str) -> Optional[int]:
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
            return data[0].get("id")
    return None


async def criar_contato(client: httpx.AsyncClient, token: str, pedido: dict) -> int:
    customer = pedido.get("customer", {})
    nome = f"{customer.get('first_name','')} {customer.get('last_name','')}".strip() or "Cliente Shopify"
    cpf  = extrair_cpf(pedido)
    tel  = extrair_telefone(pedido)
    end  = montar_endereco(pedido)

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

    resp = await client.post(
        f"{BLING_BASE}/contatos",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json=payload,
    )
    if resp.status_code not in (200, 201):
        raise RuntimeError(f"Erro ao criar contato: {resp.status_code} — {resp.text[:300]}")
    return resp.json()["data"]["id"]


async def criar_pedido_bling(client: httpx.AsyncClient, token: str, pedido: dict, contato_id: int) -> dict:
    itens = montar_itens(pedido)
    if not itens:
        raise ValueError("Nenhum item encontrado no pedido.")

    frete    = sum(float(s.get("price", 0)) for s in pedido.get("shipping_lines", []))
    desconto = float(pedido.get("total_discounts", 0))
    numero   = pedido.get("order_number") or pedido.get("name", "")
    obs      = f"Pedido Shopify #{numero}"
    if pedido.get("note"):
        obs += f" | {pedido['note']}"

    payload = {
        "contato":             {"id": contato_id},
        "situacao":            {"id": SITUACAO_EM_ABERTO},
        "itens":               itens,
        "observacoes":         obs,
        "observacoesInternas": f"Importado via integração Shopify. ID: {pedido.get('id')}",
    }
    if frete > 0:
        payload["transporte"] = {"fretePorConta": 1, "frete": frete}
    if desconto > 0:
        payload["desconto"] = {"tipo": "V", "valor": desconto}

    end = montar_endereco(pedido)
    if end:
        payload["enderecoEntrega"] = end

    resp = await client.post(
        f"{BLING_BASE}/pedidos/vendas",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json=payload,
    )
    if resp.status_code not in (200, 201):
        raise RuntimeError(f"Erro ao criar pedido: {resp.status_code} — {resp.text[:500]}")
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

    customer = pedido.get("customer", {})
    addr     = pedido.get("shipping_address") or pedido.get("billing_address") or {}

    return {
        "id":               pedido.get("id"),
        "numero":           pedido.get("order_number") or pedido.get("name"),
        "data":             pedido.get("created_at", "")[:10],
        "status_pagamento": pedido.get("financial_status"),
        "status_entrega":   pedido.get("fulfillment_status") or "pending",
        "cliente": {
            "nome":     f"{customer.get('first_name','')} {customer.get('last_name','')}".strip(),
            "email":    customer.get("email", ""),
            "telefone": extrair_telefone(pedido),
            "cpf":      extrair_cpf(pedido),
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
                "titulo":    i.get("title"),
                "sku":       i.get("sku", ""),
                "quantidade": i.get("quantity"),
                "preco":     float(i.get("price", 0)),
            }
            for i in pedido.get("line_items", [])
        ],
        "frete":    sum(float(s.get("price", 0)) for s in pedido.get("shipping_lines", [])),
        "desconto": float(pedido.get("total_discounts", 0)),
        "total":    float(pedido.get("total_price", 0)),
        "obs":      pedido.get("note", ""),
    }


@router.get("/integrar/{order_id}")
async def integrar_pedido(order_id: str, request: Request):
    """
    Executa a integração Shopify → Bling com logs em tempo real via SSE.
    O frontend consome este endpoint como EventSource.
    """

    async def executar() -> AsyncGenerator[str, None]:
        def log(tipo: str, msg: str):
            return f"data: {json.dumps({'tipo': tipo, 'msg': msg})}\n\n"

        try:
            yield log("info", f"🔍 Buscando pedido #{order_id} na Shopify...")
            pedido = await buscar_pedido_shopify(order_id)
            numero = pedido.get("order_number") or pedido.get("name")
            yield log("ok", f"✅ Pedido #{numero} encontrado — {len(pedido.get('line_items', []))} item(s)")

            email = pedido.get("customer", {}).get("email", "")
            cpf   = extrair_cpf(pedido)
            nome  = f"{pedido.get('customer',{}).get('first_name','')} {pedido.get('customer',{}).get('last_name','')}".strip()

            yield log("info", f"👤 Cliente: {nome} | {email} | CPF: {cpf or 'não informado'}")

            auth = request.app.state.auth
            async with httpx.AsyncClient(timeout=30) as client:
                token = await auth.get_valid_token(client)
                yield log("info", "🔑 Token Bling obtido com sucesso")

                # Contato
                yield log("info", f"🔎 Verificando se contato já existe no Bling (email: {email})...")
                contato_id = await buscar_contato_email(client, token, email)

                if contato_id:
                    yield log("ok", f"✅ Contato existente encontrado — ID: {contato_id} (reutilizando)")
                else:
                    yield log("info", "➕ Contato não encontrado — criando novo contato no Bling...")
                    contato_id = await criar_contato(client, token, pedido)
                    yield log("ok", f"✅ Contato criado com sucesso — ID: {contato_id}")

                # Pedido
                yield log("info", "📦 Criando pedido de venda no Bling...")
                for item in pedido.get("line_items", []):
                    yield log("info", f"   → {item.get('quantity')}x {item.get('title')} — R$ {item.get('price')}")

                pedido_bling = await criar_pedido_bling(client, token, pedido, contato_id)
                bling_id     = pedido_bling.get("id")

                yield log("ok",      f"✅ Pedido criado no Bling — ID: {bling_id}")
                yield log("sucesso", f"🎉 Integração concluída! Shopify #{numero} → Bling ID {bling_id}")

        except Exception as e:
            yield log("erro", f"❌ Erro: {str(e)}")

        yield "data: {\"tipo\": \"fim\"}\n\n"

    return StreamingResponse(
        executar(),
        media_type="text/event-stream",
        headers={
            "Cache-Control":               "no-cache",
            "X-Accel-Buffering":           "no",
            "Access-Control-Allow-Origin": "*",
        },
    )