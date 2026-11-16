import logging
import re


class RedactTokens(logging.Filter):
    """Oculta el token del bot de Telegram en los logs.

    Las excepciones de `requests` imprimen la URL completa
    (https://api.telegram.org/bot<ID>:<TOKEN>/getMe), lo que deja el token
    escrito en consola y en los archivos de log.
    """
    _RX = re.compile(r"(?:bot)?\d{6,}:[A-Za-z0-9_-]{30,}")

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
            red = self._RX.sub("<token-oculto>", msg)
            if red != msg:
                record.msg, record.args = red, ()
        except Exception:
            pass
        return True
