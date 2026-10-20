import re
import unicodedata

from pydantic import EmailStr, TypeAdapter, ValidationError

ADDRESS = re.compile(r"(?<![\w@])[\w.!#$%&'*+/=?^`{|}~-]+@[\w.-]+")


def normalized(message):
    return "".join(
        char
        for char in unicodedata.normalize("NFD", message.lower())
        if not unicodedata.combining(char)
    ).strip()


def unquoted(message):
    message = re.sub(r"```[\s\S]*?```|`[^`]*`|(?m:^\s*>.*$)", " ", message)
    return re.sub(r""""[^"]*"|“[^”]*”|‘[^’]*’|(?<!\w)'[^']*'(?!\w)""", " ", message)


def recipient_from(message):
    candidates = ADDRESS.findall(unquoted(message))
    if len(candidates) == 1:
        try:
            return TypeAdapter(EmailStr).validate_python(candidates[0].rstrip("."))
        except ValidationError:
            pass
    return None


def agenda_intent(message):
    text = normalized(unquoted(message))
    return not re.search(r"\b(?:no|nunca|sin)\b.{0,30}\b(?:agend|calendar)", text) and bool(
        re.search(r"\b(?:agenda(?:r|lo|me)?|calendariza(?:r|lo)?)\b", text)
    )
