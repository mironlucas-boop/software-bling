# Bling ERP · Pedidos

Sistema FastAPI para consulta e monitoramento de pedidos via API Bling v3.

## Estrutura

```
bling-erp/
├── backend/
│   ├── main.py          # FastAPI app (rotas, lógica de negócio)
│   ├── auth.py          # Gerenciamento de OAuth2 (access/refresh token)
│   ├── config.py        # Configurações via env ou .env
│   ├── models.py        # Modelos Pydantic
│   └── requirements.txt
└── frontend/
    └── static/
        └── index.html   # Dashboard SPA
```

## Configuração

### Variáveis de ambiente (`.env` na pasta `backend/`)

```env
CLIENT_ID=86f6eb0eb0df9c8bb9dc6751b3518cc6a7486f2d
CLIENT_SECRET=83f4a138225543bb8cdfb49dca7b628da707f91179a718aca78e57e415da
INITIAL_ACCESS_TOKEN=eyJ0eXAi...  # token atual
INITIAL_REFRESH_TOKEN=4b3b28b4...  # refresh token atual
```

> Os valores padrão já estão preenchidos no `config.py` com os dados fornecidos.

## Instalação

```bash
cd backend
pip install -r requirements.txt
```

## Execução

```bash
cd backend
uvicorn main:app --reload --host 0.0.0.0 --port 8000
```

Acesse: **http://localhost:8000**

## Endpoints da API

| Método | Rota | Descrição |
|--------|------|-----------|
| GET | `/api/pedidos` | Lista pedidos (paginado, filtros) |
| GET | `/api/pedidos/{id}` | Detalhe de um pedido |
| POST | `/api/pedidos/atualizar` | Força re-busca no Bling (background) |
| GET | `/api/stats` | Estatísticas resumidas |
| POST | `/api/webhook/pedidos` | Recebe eventos de webhook do Bling |

### Parâmetros do GET `/api/pedidos`

| Param | Tipo | Descrição |
|-------|------|-----------|
| `pagina` | int | Página (padrão: 1) |
| `limite` | int | Itens por página (padrão: 20) |
| `situacao` | str | ID da situação para filtrar |
| `busca` | str | Busca por número ou nome do cliente |

## Webhook

Configure no Bling (Configurações → Webhooks) a URL:

```
https://SEU_DOMINIO/api/webhook/pedidos
```

O backend atualiza automaticamente o pedido no cache ao receber o evento.

## Fluxo de autenticação

1. Na inicialização, usa o `INITIAL_ACCESS_TOKEN` configurado.
2. Antes de cada requisição verifica se o token expirará em < 5 minutos.
3. Se necessário, faz refresh automático usando o `INITIAL_REFRESH_TOKEN`.
4. Tokens são renovados em memória (para produção, considere persistir em Redis/DB).
