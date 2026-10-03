"""Cliente HTTP para la API REST de Sitrad PRO (o del simulador)."""
import logging
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

logger = logging.getLogger(__name__)


class SitradClient:
    """
    Cliente agnóstico: funciona igual contra el simulador o Sitrad real.
    Solo cambia base_url.
    """

    def __init__(self, base_url: str, verify_ssl: bool = False,
                 timeout: int = 10):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.session = self._build_session(verify_ssl)

    def _build_session(self, verify_ssl: bool) -> requests.Session:
        session = requests.Session()
        session.verify = verify_ssl
        retry = Retry(
            total=3,
            backoff_factor=1,
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=["GET"],
        )
        adapter = HTTPAdapter(max_retries=retry)
        session.mount("http://", adapter)
        session.mount("https://", adapter)
        return session

    def get_instruments(self):
        try:
            r = self.session.get(f"{self.base_url}/instruments",
                                 timeout=self.timeout)
            r.raise_for_status()
            return r.json().get("instruments", [])
        except Exception as e:
            logger.error(f"Error listando instrumentos: {e}")
            return []

    def get_values(self, instrument_id: int):
        try:
            r = self.session.get(
                f"{self.base_url}/instruments/{instrument_id}/values",
                timeout=self.timeout,
            )
            r.raise_for_status()
            return r.json()
        except Exception as e:
            logger.error(f"Error obteniendo valores de {instrument_id}: {e}")
            return None