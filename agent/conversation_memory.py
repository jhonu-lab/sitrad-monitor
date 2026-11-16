"""
agent/conversation_memory.py — memoria de conversación POR USUARIO.

Guarda cada par (pregunta, respuesta) bajo una clave de usuario
(p. ej. "<chat_id>:<user_id>") en un JSONL que sobrevive a reinicios.

Dos usos:
    1. context_block(user_key): últimos turnos, compactos, para anteponer al
       prompt del sistema y resolver referencias ("y la del evaporador?").
    2. search(user_key, text): búsqueda por palabras clave en TODO el historial
       del usuario ("¿qué me dijiste sobre el compresor?").

Las cifras de respuestas viejas pueden haber cambiado: el bloque de contexto
lo dice explícitamente para que el modelo vuelva a consultar herramientas
en lugar de repetir valores caducados.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import unicodedata
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Turn:
    user_key: str
    user_name: str
    ts: datetime
    question: str
    answer: str


def _strip_accents(s: str) -> str:
    return "".join(
        c for c in unicodedata.normalize("NFD", s) if unicodedata.category(c) != "Mn"
    ).lower()


_STOP = {
    "que", "de", "la", "el", "los", "las", "un", "una", "en", "y", "o", "me", "te",
    "se", "del", "al", "por", "para", "con", "es", "fue", "dijiste", "pregunte",
    "ayer", "antes", "hoy", "cual", "como", "cuanto", "sobre",
}


def _tokens(text: str) -> set[str]:
    out = set()
    for raw in _strip_accents(text).replace("?", " ").replace("¿", " ").split():
        w = "".join(ch for ch in raw if ch.isalnum())
        if len(w) >= 3 and w not in _STOP:
            out.add(w)
    return out


class ConversationMemory:
    def __init__(
        self,
        path: str | None = "logs/conversations.jsonl",
        max_turns_per_user: int = 200,
        context_turns: int = 4,
        max_chars_question: int = 300,
        max_chars_answer: int = 600,
    ) -> None:
        self.path = path
        self.context_turns = context_turns
        self.max_chars_question = max_chars_question
        self.max_chars_answer = max_chars_answer
        self._lock = threading.Lock()
        self._turns: dict[str, deque[Turn]] = defaultdict(
            lambda: deque(maxlen=max_turns_per_user)
        )
        self._load()

    # ------------------------------------------------------------ persistencia

    def _load(self) -> None:
        if not self.path or not os.path.exists(self.path):
            return
        n = 0
        with open(self.path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                    t = Turn(
                        user_key=d["user_key"],
                        user_name=d.get("user_name", ""),
                        ts=datetime.fromisoformat(d["ts"]),
                        question=d["question"],
                        answer=d["answer"],
                    )
                except (ValueError, KeyError, TypeError):
                    continue            # línea corrupta: no bloquea el resto
                self._turns[t.user_key].append(t)
                n += 1
        logger.info("ConversationMemory: %d turnos cargados.", n)

    def _append(self, t: Turn) -> None:
        if not self.path:                 # modo solo memoria
            return
        d = os.path.dirname(self.path)
        if d:
            os.makedirs(d, exist_ok=True)
        with open(self.path, "a", encoding="utf-8", newline="\n") as fh:
            json.dump(
                {"user_key": t.user_key, "user_name": t.user_name,
                 "ts": t.ts.isoformat(), "question": t.question, "answer": t.answer},
                fh, ensure_ascii=False,
            )
            fh.write("\n")

    # ------------------------------------------------------------------- API

    def record(
        self,
        user_key: str,
        question: str,
        answer: str,
        user_name: str = "",
        ts: datetime | None = None,
    ) -> None:
        if not user_key or not question or not answer:
            return
        t = Turn(user_key, user_name, ts or datetime.now(timezone.utc),
                 question.strip(), answer.strip())
        with self._lock:
            self._turns[user_key].append(t)
            try:
                self._append(t)
            except OSError:
                logger.exception("No se pudo persistir la conversación.")

    def recent(self, user_key: str, n: int | None = None) -> list[Turn]:
        with self._lock:
            turns = list(self._turns.get(user_key, ()))
        return turns[-(n or self.context_turns):]

    def forget(self, user_key: str) -> int:
        """Borra TODO lo guardado de ese usuario (memoria y archivo)."""
        with self._lock:
            n = len(self._turns.pop(user_key, ()))
            if self.path and os.path.exists(self.path):
                tmp = self.path + ".tmp"
                with open(self.path, "r", encoding="utf-8") as src, \
                        open(tmp, "w", encoding="utf-8", newline="\n") as dst:
                    for line in src:
                        try:
                            if json.loads(line).get("user_key") == user_key:
                                continue
                        except ValueError:
                            pass
                        dst.write(line)
                os.replace(tmp, self.path)
        return n

    def search(self, user_key: str, text: str, limit: int = 3,
               now: datetime | None = None) -> list[dict]:
        """Turnos del usuario con más palabras en común con `text`."""
        q = _tokens(text)
        if not q:
            return []
        now = now or datetime.now(timezone.utc)
        scored = []
        with self._lock:
            turns = list(self._turns.get(user_key, ()))
        for t in turns:
            score = len(q & _tokens(t.question + " " + t.answer))
            if score:
                scored.append((score, t.ts, t))
        scored.sort(key=lambda x: (x[0], x[1]), reverse=True)
        return [
            {
                "hace_min": round((now - t.ts).total_seconds() / 60.0, 1),
                "pregunta": self._cut(t.question, self.max_chars_question),
                "respuesta": self._cut(t.answer, self.max_chars_answer),
            }
            for _, _, t in scored[:limit]
        ]

    def context_block(self, user_key: str, now: datetime | None = None) -> str:
        turns = self.recent(user_key)
        if not turns:
            return ""
        now = now or datetime.now(timezone.utc)
        lines = [
            "CONVERSACIÓN PREVIA CON ESTE USUARIO (de la más antigua a la más "
            "reciente). Úsala solo para entender referencias como \"y la del "
            "evaporador?\" o \"compárala con la anterior\". Los valores de "
            "respuestas anteriores pueden haber cambiado: vuelve a consultar "
            "las herramientas, no repitas cifras viejas.",
        ]
        for t in turns:
            mins = (now - t.ts).total_seconds() / 60.0
            age = f"{mins:.0f} min" if mins < 120 else f"{mins / 60:.1f} h"
            lines.append(f"[hace {age}] Usuario: {self._cut(t.question, self.max_chars_question)}")
            lines.append(f"            Respuesta: {self._cut(t.answer, self.max_chars_answer)}")
        return "\n".join(lines)

    @staticmethod
    def _cut(s: str, n: int) -> str:
        s = " ".join(s.split())
        return s if len(s) <= n else s[: n - 1].rstrip() + "…"
