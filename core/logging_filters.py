import logging
import re


class RedactTokens(logging.Filter):
    """Oculta el token del bot de Telegram en los logs (mensaje Y traceback).

    Las excepciones de `requests` imprimen la URL completa
    (https://api.telegram.org/bot<ID>:<TOKEN>/getMe), lo que deja el token
    escrito en consola y en los archivos de log; con logger.exception el
    token aparece además dentro del traceback.
    """
    _RX = re.compile(r"(?:bot)?\d{6,}:[A-Za-z0-9_-]{30,}")
    _formatter = logging.Formatter()

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
            red = self._RX.sub("<token-oculto>", msg)
            if red != msg:
                record.msg, record.args = red, ()
            if record.exc_info and not record.exc_text:
                record.exc_text = self._formatter.formatException(record.exc_info)
            if record.exc_text:
                record.exc_text = self._RX.sub("<token-oculto>", record.exc_text)
        except Exception:
            pass
        return True
