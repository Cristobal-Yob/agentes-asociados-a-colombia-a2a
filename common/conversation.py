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


def confirms_preview(message):
    text = re.sub(r"[,!.;¿?¡]+", " ", normalized(message))
    text = " ".join(text.split())
    return text in {
        "si",
        "yes",
        "confirmo",
        "si envialo",
        "si enviar",
        "confirmo el envio",
        "confirmo enviar",
        "envialo",
        "send",
        "yes send",
    }


def cancels_delivery(message):
    if normalized(unquoted(message)) in {
        "no",
        "cancelar",
        "cancela",
        "cancelo",
        "salir",
        "cancel",
        "stop",
    }:
        return True
    return bool(
        re.search(
            r"\b(?:no|nunca)\b.{0,40}\b(?:envies|enviar|enviarlo|mandes|mandar|send)\b|"
            r"\bno\s+quiero\s+(?:un\s+)?(?:correo|email)\b|"
            r"\b(?:do not|dont|don't)\s+send\b",
            normalized(unquoted(message)),
        )
    )


def has_message_content(message):
    text = normalized(ADDRESS.sub(" ", message))
    if text in {"hola", "buenas", "buenos dias", "hello", "hi"}:
        return False
    remainder = re.sub(
        r"\b(?:envia|enviame|envialo|enviamelo|enviar|manda|mandame|mandamelo|mandar|correo|email|e-mail|un|el|a|para|por|"
        r"favor|si|quiero|necesito|prepara|preparalo|preparame|preparar|redacta|redactalo|"
        r"redactame|redactar|mensaje|y|este|ese|lo|me|mi)\b",
        " ",
        text,
    )
    return bool(re.search(r"\w", remainder))


SEND_COMMAND = r"(?:envia(?:me|melo|lo|la)?|enviar|manda(?:me|melo|lo|la)?|mandar|send)"
DRAFT_COMMAND = r"(?:redacta(?:me|lo|la)?|redactar|prepara(?:me|lo|la)?|preparar|draft)"


def email_intent(message):
    """La autorización sale solo de órdenes humanas directas, nunca del modelo o citas."""
    text = normalized(unquoted(message))
    if cancels_delivery(message) or re.search(
        r"\b(?:sin\s+enviar(?:lo)?|solo\s+(?:un\s+)?borrador)\b", text
    ):
        return "draft" if re.search(DRAFT_COMMAND, text) else "none"
    # Una orden debe iniciar la frase o una cláusula; descripciones y nombres no autorizan.
    prefix = (
        r"(?:^|[;.!?\n]|\by\s+)(?:\s*(?:hola[, ]+|por favor[, ]+|quiero que |"
        r"necesito que |quiero |necesito |puedes |podrias |puede |me puedes |"
        r"me podrias |please ))*\s*"
    )
    if re.search(prefix + SEND_COMMAND + r"\b", text):
        return "send"
    if re.search(prefix + DRAFT_COMMAND + r"\b", text):
        return "draft"
    return "none"
