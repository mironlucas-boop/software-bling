import asyncio
import base64
import json
import os
from datetime import datetime, timedelta
from typing import Optional

import httpx

TOKEN_URL = "https://www.bling.com.br/Api/v3/oauth/token"
TOKENS_FILE = os.path.join(os.path.dirname(__file__), "tokens.json")


def _load_tokens_from_disk() -> dict:
    """Lê tokens salvos em disco. Retorna dict vazio se não existir."""
    try:
        with open(TOKENS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_tokens_to_disk(access_token: str, refresh_token: str) -> None:
    """Persiste os tokens em disco para sobreviver a reinicializações."""
    with open(TOKENS_FILE, "w", encoding="utf-8") as f:
        json.dump(
            {
                "access_token": access_token,
                "refresh_token": refresh_token,
                "saved_at": datetime.now().isoformat(),
            },
            f,
            indent=2,
        )


class BlingAuth:
    """Gerencia access_token e refresh_token do Bling OAuth2.
    
    Os tokens são automaticamente persistidos em 'tokens.json' após cada
    renovação, de modo que reinicializações do servidor não exijam
    re-autorização manual.
    """

    def __init__(
        self,
        client_id: str,
        client_secret: str,
        access_token: str,
        refresh_token: str,
    ):
        self.client_id = client_id
        self.client_secret = client_secret

        # Tenta carregar tokens salvos em disco; usa os passados como fallback
        saved = _load_tokens_from_disk()
        if saved.get("access_token") and saved.get("refresh_token"):
            print(f"[AUTH] Tokens carregados do disco (salvos em {saved.get('saved_at', '?')})")
            self._access_token = saved["access_token"]
            self._refresh_token = saved["refresh_token"]
        else:
            print("[AUTH] Nenhum tokens.json encontrado — usando tokens do config.")
            self._access_token = access_token
            self._refresh_token = refresh_token

        self._expires_at: Optional[datetime] = None  # força refresh na 1ª chamada
        self._lock = asyncio.Lock()

    # ── Public ──────────────────────────────────────────────────────────────

    async def get_valid_token(self, client: httpx.AsyncClient) -> str:
        """Retorna um token válido, renovando e persistindo se necessário."""
        if self._is_expired():
            await self.refresh(client)
        return self._access_token

    async def refresh(self, client: httpx.AsyncClient) -> str:
        """Usa o refresh_token para obter novo access_token e salva em disco."""
        async with self._lock:
            # Double-check após adquirir o lock
            if not self._is_expired():
                return self._access_token

            credentials = base64.b64encode(
                f"{self.client_id}:{self.client_secret}".encode()
            ).decode()

            resp = await client.post(
                TOKEN_URL,
                headers={
                    "Authorization": f"Basic {credentials}",
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Accept": "application/json",
                },
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": self._refresh_token,
                },
            )

            if resp.status_code != 200:
                raise RuntimeError(
                    f"Falha ao renovar token Bling: {resp.status_code} – {resp.text}"
                )

            data = resp.json()
            self._access_token = data["access_token"]
            self._refresh_token = data.get("refresh_token", self._refresh_token)
            expires_in = int(data.get("expires_in", 21600))
            self._expires_at = datetime.now() + timedelta(seconds=expires_in - 300)

            # ✅ Persiste automaticamente em disco
            _save_tokens_to_disk(self._access_token, self._refresh_token)
            print("[AUTH] Tokens renovados e salvos em tokens.json")

            return self._access_token

    def update_tokens(self, access_token: str, refresh_token: str) -> None:
        """Atualiza tokens em memória e em disco (usado pelo callback OAuth)."""
        self._access_token = access_token
        self._refresh_token = refresh_token
        self._expires_at = None  # força revalidação na próxima chamada
        _save_tokens_to_disk(access_token, refresh_token)
        print("[AUTH] Tokens atualizados via OAuth callback e salvos em tokens.json")

    # ── Private ─────────────────────────────────────────────────────────────

    def _is_expired(self) -> bool:
        if self._expires_at is None:
            return True  # força refresh na primeira chamada
        return datetime.now() >= self._expires_at