"""OCR OPCIONAL do Seller SKU — pre-preenche o catalogo p/ confirmacao em lote.

O Seller SKU so existe rasterizado (~6px) no bitmap da Shopee. Nesse tamanho o
OCR NAO e confiavel (ver ADR 2026-06-12: troca B<->6, E<->L, V<->Y e le o mesmo
SKU diferente em cada etiqueta). Por isso o resultado aqui NUNCA vai direto para
a impressao: e apenas uma SUGESTAO que o usuario confere/corrige no dialogo de
lote. A impressao usa exclusivamente o texto confirmado no catalogo manual.

Dependencia opcional: ``pytesseract`` + binario ``tesseract`` no sistema. Sem
eles, ``is_available()`` devolve False e a UI desabilita o recurso — o
instalador continua enxuto e o OCR nao volta a ser obrigatorio (era ~100MB).
"""
from __future__ import annotations

from functools import lru_cache

from PIL import Image

from ..utils.logger import get_logger

log = get_logger("ocr_seller")

# Seller SKU e ASCII (letras maiusculas, digitos e separadores). Restringir o
# alfabeto reduz o lixo que o Tesseract inventa na fonte minuscula.
_WHITELIST = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_/."

# Upscale antes do OCR: a fonte de 6px fica abaixo do piso do Tesseract; ampliar
# com LANCZOS ajuda um pouco (ainda erra, mas o palpite fica mais util).
_ESCALA = 10


@lru_cache(maxsize=1)
def _pytesseract():
    """Importa pytesseract e confirma o binario; None se qualquer um faltar."""
    try:
        import pytesseract  # type: ignore

        pytesseract.get_tesseract_version()  # erro cedo se o binario sumir
        return pytesseract
    except Exception as exc:  # noqa: BLE001 (ImportError, TesseractNotFound, etc.)
        log.info("OCR indisponivel: %s", exc)
        return None


def is_available() -> bool:
    """True se da para rodar OCR (pytesseract + binario tesseract presentes)."""
    return _pytesseract() is not None


def unavailable_reason() -> str | None:
    """Mensagem (PT) explicando por que o OCR nao esta disponivel, ou None."""
    if _pytesseract() is None:
        return (
            "OCR indisponivel: instale o Tesseract (binario do sistema) e o "
            "pacote Python 'pytesseract' para pre-preencher o Seller SKU."
        )
    return None


def _limpar(txt: str) -> str:
    """Normaliza o palpite: maiusculas, sem o prefixo 'SKU' que vaza do rotulo."""
    t = " ".join((txt or "").strip().upper().split())
    for prefixo in ("SKU:", "SKU"):
        if t.startswith(prefixo):
            t = t[len(prefixo):].strip(" :")
            break
    return t


def ocr_seller(crop: Image.Image, *, escala: int = _ESCALA) -> str:
    """Melhor palpite do Seller SKU a partir do recorte do bitmap.

    Devolve string vazia se o OCR estiver indisponivel ou nada for reconhecido.
    O resultado e SUGESTAO, nunca verdade — quem confirma e o usuario.
    """
    pt = _pytesseract()
    if pt is None or crop is None:
        return ""
    try:
        cinza = crop.convert("L")
        grande = cinza.resize(
            (max(1, cinza.width * escala), max(1, cinza.height * escala)),
            Image.LANCZOS,
        )
        cfg = f"--psm 7 -c tessedit_char_whitelist={_WHITELIST}"
        bruto = pt.image_to_string(grande, config=cfg)
    except Exception as exc:  # noqa: BLE001
        log.warning("Falha no OCR do Seller SKU: %s", exc)
        return ""
    return _limpar(bruto)
