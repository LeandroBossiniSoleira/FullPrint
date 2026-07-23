"""Decodifica etiquetas GRF Z64 (Shopee Full) e extrai SKU/posicao via QR code.

Estrutura tipica de UMA "folha" no TXT da Shopee Full:

    ~DGR:DEMO.GRF,124236,102,:Z64:<base64-zlib>:<crc16>
    ^XA...^XGR:DEMO.GRF,1,1^FS^PQ1,0,0,N^XZ
    ^XA^IDR:DEMO.GRF^FS^XZ

Cada GRF contem ate ~10 stickers numa grade 2 colunas x 5 linhas. ATENCAO:
GRFs misturam stickers de SKUs DIFERENTES (verificado empiricamente em ~23%
do arquivo de teste). Por isso retornamos uma lista de stickers por GRF,
nao 1 SKU "principal".
"""
from __future__ import annotations

import base64
import re
import zlib
from dataclasses import dataclass, field

from PIL import Image
from pyzbar import pyzbar

from ..utils.logger import get_logger

log = get_logger("grf_decoder")

RE_TRIO_GRF = re.compile(
    r"(~DG(?P<nome>[^,]+),(?P<total>\d+),(?P<row>\d+),:Z64:(?P<b64>[A-Za-z0-9+/=]+):(?P<crc>[0-9A-Fa-f]+))"
    r"(?P<sep1>\s*)"
    r"(?P<print>\^XA[^~]*?\^XGR:[^,]+,\d+,\d+\^FS[^~]*?\^XZ)"
    r"(?P<sep2>\s*)"
    r"(?P<delete>\^XA\^IDR:[^^]+\^FS\^XZ)",
    re.DOTALL,
)


@dataclass
class StickerInfo:
    """Um QR individual dentro do GRF — corresponde a UM sticker imprimivel."""
    sku: str
    qr_left: int
    qr_top: int
    qr_width: int
    qr_height: int


@dataclass
class EtiquetaGRF:
    """Uma "folha" GRF: o ZPL bruto + bitmap + lista de stickers detectados."""
    indice: int
    nome_grf: str
    largura: int
    altura: int
    zpl_raw: str
    imagem: Image.Image
    stickers: list[StickerInfo] = field(default_factory=list)


def _decodificar_z64(b64: str) -> bytes:
    return zlib.decompress(base64.b64decode(b64))


def _grf_para_imagem(grf: bytes, row_bytes: int) -> Image.Image:
    width = row_bytes * 8
    height = len(grf) // row_bytes
    img = Image.frombytes("1", (width, height), grf)
    # ZPL GRF: bit 1 = tinta (preto). PIL "1" frombytes: bit 1 = branco.
    return img.point(lambda p: 0 if p > 0 else 255).convert("1")


def _detectar_stickers(img: Image.Image) -> list[StickerInfo]:
    stickers: list[StickerInfo] = []
    for c in pyzbar.decode(img):
        if c.type != "QRCODE":
            continue
        sku = c.data.decode("ascii", errors="replace")
        stickers.append(
            StickerInfo(
                sku=sku,
                qr_left=c.rect.left,
                qr_top=c.rect.top,
                qr_width=c.rect.width,
                qr_height=c.rect.height,
            )
        )
    # Ordena top-to-bottom, left-to-right (ordem de leitura)
    stickers.sort(key=lambda s: (s.qr_top, s.qr_left))
    return stickers


# Geometria do sticker em torno do QR. Calibrada na folha real 816x1218 (medida
# em 35+ produtos, estavel): QR ~164x172, colunas em x~180/359 (pitch ~179px).
# O bloco de texto abaixo do QR tem 4 linhas, em y RELATIVO a base do QR:
#   Seller SKU  ~[+7:+13]   |  SKU numerico ~[+14:+20]  |  descricao ~[+21:+37]
# As colunas distam so ~179px, dai a margem lateral pequena (texto cabe em ~168px).
CROP_MARGEM_X = 8
CROP_MARGEM_TOPO = 4
CROP_ALTURA_TEXTO = 58


def _crop_clampado(imagem: Image.Image, x: int, y: int, w: int, h: int) -> Image.Image:
    img_w, img_h = imagem.size
    x = max(0, min(x, img_w - 1))
    y = max(0, min(y, img_h - 1))
    w = max(1, min(w, img_w - x))
    h = max(1, min(h, img_h - y))
    return imagem.crop((x, y, x + w, y + h))


def crop_sticker(folha: Image.Image, st: StickerInfo) -> Image.Image:
    """Recorta um sticker da folha, ancorado na posicao do QR.

    Nao assume grade fixa: o recorte e o retangulo do QR expandido para os
    lados (texto e pouco mais largo que o QR) e para baixo (4 linhas de
    texto: Seller SKU, SKU e descricao em 2 linhas).
    """
    return _crop_clampado(
        folha,
        st.qr_left - CROP_MARGEM_X,
        st.qr_top - CROP_MARGEM_TOPO,
        st.qr_width + 2 * CROP_MARGEM_X,
        st.qr_height + CROP_MARGEM_TOPO + CROP_ALTURA_TEXTO,
    )


# Quiet zone (margem branca) ao redor do QR para manter legibilidade.
CROP_QR_QUIET = 6


def crop_qr(folha: Image.Image, st: StickerInfo) -> Image.Image:
    """Recorta apenas o QR (com quiet zone) — para reposicionar no layout novo."""
    return _crop_clampado(
        folha,
        st.qr_left - CROP_QR_QUIET,
        st.qr_top - CROP_QR_QUIET,
        st.qr_width + 2 * CROP_QR_QUIET,
        st.qr_height + 2 * CROP_QR_QUIET,
    )


# Faixa do Seller SKU (1a linha do bloco, abaixo do QR). Recortamos do bitmap
# porque o Seller SKU so existe rasterizado / no catalogo manual; aqui
# garantimos que ele apareca SEMPRE. Calibrado com dado real (scan pixel a
# pixel, 2026-07-22, ClickUp 86ajk2mc2): tinta em [+6:+11], proxima linha (SKU
# numerico) comeca em +14 -> folga de 2px em ambos os lados (era so 1px no
# topo antes, cortava em stickers com fonte 1-2px mais alta).
SELLER_TOPO = 4
SELLER_ALTURA = 9

# ATENCAO: a Shopee CENTRALIZA o texto "Seller SKU:VALOR" nessa linha (nao
# alinha a esquerda) -- testado com >30 SKUs reais, ClickUp 86ajk2mc2. Por
# isso NAO da pra usar um offset X fixo pra pular so o label "Seller SKU:" e
# pegar so o valor: pra textos mais curtos/longos o bloco inteiro desloca
# (fica mais ou menos centralizado), entao um corte fixo calibrado numa
# amostra corta o INICIO do valor em muitos outros SKUs. Por isso recortamos a
# linha INTEIRA (label + valor) sempre -- garante que nunca falta nada, ao
# custo de exibir o label redundante junto.


def crop_seller_sku(folha: Image.Image, st: StickerInfo) -> Image.Image:
    """Recorta a linha do Seller SKU (1a linha de texto, logo abaixo do QR) —
    label ("Seller SKU:") + valor juntos, como a Shopee imprime.
    """
    return _crop_clampado(
        folha,
        st.qr_left - CROP_MARGEM_X,
        st.qr_top + st.qr_height + SELLER_TOPO,
        st.qr_width + 2 * CROP_MARGEM_X,
        SELLER_ALTURA,
    )


# Distancia (px, abaixo do fim do QR) onde COMECA a descricao do produto. As 2
# primeiras linhas do bloco sao Seller SKU + SKU numerico; a descricao vem em
# ~[+21:+37]. Comecar em 20 pula o SKU (texto nativo) e captura AS DUAS linhas
# da descricao (antes 28 pegava so a ultima + residuo). Calibrado em arquivos
# reais (Print_Barcode_*.txt / teste FullPrint.txt).
CROP_DESCRICAO_TOPO = 20


def crop_descricao(folha: Image.Image, st: StickerInfo) -> Image.Image:
    """Recorta as linhas de descricao do produto (abaixo do Seller SKU/SKU).

    Pula a faixa superior (Seller SKU + SKU numerico) e vai ate o fim do bloco
    de texto; o branco excedente e aparado no render. A descricao so existe
    rasterizada no bitmap da Shopee (sem OCR).
    """
    return _crop_clampado(
        folha,
        st.qr_left - CROP_MARGEM_X,
        st.qr_top + st.qr_height + CROP_DESCRICAO_TOPO,
        st.qr_width + 2 * CROP_MARGEM_X,
        CROP_ALTURA_TEXTO - CROP_DESCRICAO_TOPO,
    )


def extrair_etiquetas(conteudo: str) -> list[EtiquetaGRF]:
    """Decodifica cada GRF do TXT e detecta seus QRs (stickers internos)."""
    etiquetas: list[EtiquetaGRF] = []
    for idx, m in enumerate(RE_TRIO_GRF.finditer(conteudo), start=1):
        total = int(m["total"])
        row = int(m["row"])
        try:
            grf = _decodificar_z64(m["b64"])
        except Exception as exc:  # noqa: BLE001
            log.warning("GRF #%d: falha Z64 (%s)", idx, exc)
            continue
        if len(grf) != total:
            log.debug("GRF #%d: tamanho descomprimido %d != %d", idx, len(grf), total)
        try:
            img = _grf_para_imagem(grf, row)
        except Exception as exc:  # noqa: BLE001
            log.warning("GRF #%d: falha render (%s)", idx, exc)
            continue

        try:
            stickers = _detectar_stickers(img)
        except Exception as exc:  # noqa: BLE001
            log.warning("GRF #%d: falha decodificar QRs (%s)", idx, exc)
            stickers = []

        etiquetas.append(
            EtiquetaGRF(
                indice=idx,
                nome_grf=m["nome"],
                largura=row * 8,
                altura=len(grf) // row,
                zpl_raw=m.group(0),
                imagem=img,
                stickers=stickers,
            )
        )
    log.info("GRF: %d folhas, %d stickers totais", len(etiquetas), sum(len(e.stickers) for e in etiquetas))
    return etiquetas


def has_grf_z64(conteudo: str) -> bool:
    return "~DG" in conteudo and ":Z64:" in conteudo
