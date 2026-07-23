"""Leitor da camada textual do PDF de etiquetas da Shopee Full (PyMuPDF).

O PDF gerado pela Shopee traz o texto REAL pesquisavel (Ctrl+F acha SKU/Seller
SKU) -- ao contrario do TXT/GRF, onde SKU/Seller SKU/descricao so existem
rasterizados no bitmap. Aqui extraimos esse texto de forma DETERMINISTICA (sem
OCR, sem IA), pra alimentar o render nativo (`^A0`) com nitidez real.

Estrutura observada no PDF real (Print_Barcode_*.pdf, validado 2026-07-23):

- 1 etiqueta por PAGINA (pagina ~169.9 x 113.0 pt = 60 x 40 mm).
- Campos ROTULADOS explicitamente no texto, uma linha cada:
    seller sku:KTFFLCST-6UNTOOVER-MASC   <- Seller SKU (letras + tracos)
    barcode:26092652572_276870363349     <- SKU numerico Shopee (= conteudo do QR)
    whs skuid:26092652572_276870363349   <- idem barcode (redundante)
- A descricao do produto vem nas linhas do TOPO, acima dos rotulos.

A identificacao e' por PREFIXO DE ROTULO (conteudo), nao por coordenada fixa --
robusto a variacoes de posicao. As coordenadas (y) sao usadas so pra reconstruir
as linhas e ordenar; a divisao por etiqueta e' trivial (1 por pagina).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from itertools import groupby
from pathlib import Path

import fitz  # PyMuPDF

from ..utils.logger import get_logger

log = get_logger("pdf_reader")

# Rotulo -> campo. Casado por PREFIXO (case-insensitive) na parte antes do ":".
# "barcode" e "whs skuid" carregam o MESMO valor (SKU numerico / conteudo do QR);
# preferimos "barcode" e usamos "whs skuid" so como fallback.
_RE_SELLER = re.compile(r"^\s*seller\s+sku\s*$", re.IGNORECASE)
_RE_BARCODE = re.compile(r"^\s*barcode\s*$", re.IGNORECASE)
_RE_WHS = re.compile(r"^\s*whs\s+skuid\s*$", re.IGNORECASE)

# Formato esperado do SKU numerico (sanidade, nao adivinhacao): dois blocos de
# digitos separados por "_" (ex.: 26092652572_276870363349).
RE_SKU_NUMERICO = re.compile(r"^\d+_\d+$")


@dataclass
class PDFEtiqueta:
    """Campos textuais de UMA etiqueta (1 pagina do PDF), extraidos da camada
    de texto. ``sku`` = SKU numerico Shopee (conteudo do QR); ``seller_sku`` =
    codigo do vendedor; ``descricao`` = nome do produto (pode ter varias linhas,
    aqui juntadas por espaco)."""
    pagina: int              # 1-based
    sku: str = ""
    seller_sku: str = ""
    descricao: str = ""
    # Diagnostico: linhas cruas da pagina (ordem de leitura) -- util pra depurar
    # PDFs com layout inesperado sem reabrir o arquivo.
    linhas_raw: list[str] = field(default_factory=list)

    def validar(self) -> list[str]:
        """Lista de problemas (vazia = OK). Nao adivinha nada: so aponta o que
        falta ou nao bate o formato esperado, pra a UI alertar antes de imprimir."""
        problemas: list[str] = []
        if not self.descricao.strip():
            problemas.append("descricao ausente")
        if not self.seller_sku.strip():
            problemas.append("seller_sku ausente")
        if not self.sku.strip():
            problemas.append("sku (barcode) ausente")
        elif not RE_SKU_NUMERICO.match(self.sku):
            problemas.append(f"sku fora do formato numerico esperado: {self.sku!r}")
        return problemas


def _linhas_da_pagina(page: "fitz.Page") -> list[tuple[float, str]]:
    """Reconstroi as linhas de texto da pagina como (y, texto), na ordem de
    leitura. Agrupa as palavras por (bloco, linha) do proprio PyMuPDF e junta
    por espaco -- assim "seller" + "sku:VALOR" viram uma linha so."""
    # (x0, y0, x1, y1, palavra, bloco, linha, n_palavra)
    palavras = page.get_text("words")
    palavras.sort(key=lambda w: (w[5], w[6], w[0]))
    linhas: list[tuple[float, str]] = []
    for _, grupo in groupby(palavras, key=lambda w: (w[5], w[6])):
        grupo = list(grupo)
        texto = " ".join(w[4] for w in grupo).strip()
        if not texto:
            continue
        y = min(w[1] for w in grupo)
        linhas.append((y, texto))
    linhas.sort(key=lambda t: t[0])
    return linhas


def _parsear_pagina(indice: int, page: "fitz.Page") -> PDFEtiqueta:
    linhas = _linhas_da_pagina(page)
    et = PDFEtiqueta(pagina=indice, linhas_raw=[t for _, t in linhas])

    barcode = ""
    whs = ""
    descricao_linhas: list[str] = []
    primeiro_rotulo_visto = False

    for _, texto in linhas:
        rotulo, sep, valor = texto.partition(":")
        campo = None
        if sep:  # tem ":" -> pode ser um campo rotulado
            if _RE_SELLER.match(rotulo):
                campo = "seller"
            elif _RE_BARCODE.match(rotulo):
                campo = "barcode"
            elif _RE_WHS.match(rotulo):
                campo = "whs"

        if campo is None:
            # Linha sem rotulo reconhecido: e' descricao SE ainda nao vimos
            # nenhum campo (a descricao fica no topo). Depois dos rotulos,
            # linhas soltas sao ignoradas (ruido).
            if not primeiro_rotulo_visto:
                descricao_linhas.append(texto)
            continue

        primeiro_rotulo_visto = True
        valor = valor.strip()
        if campo == "seller":
            et.seller_sku = valor
        elif campo == "barcode":
            barcode = valor
        elif campo == "whs":
            whs = valor

    et.sku = barcode or whs  # barcode preferido; whs skuid e' o mesmo valor
    et.descricao = " ".join(descricao_linhas).strip()
    return et


def _ler_doc(doc: "fitz.Document") -> list[PDFEtiqueta]:
    return [_parsear_pagina(i, page) for i, page in enumerate(doc, start=1)]


def ler_etiquetas(caminho: str | Path) -> list[PDFEtiqueta]:
    """Le TODAS as paginas do PDF e devolve uma ``PDFEtiqueta`` por pagina.

    Nao faz OCR nem rasteriza nada: usa so a camada textual. Se uma pagina nao
    tiver texto (PDF digitalizado), os campos vem vazios e ``validar()`` acusa
    -- cabe ao chamador decidir (ex.: fallback OCR opcional).
    """
    caminho = Path(caminho)
    if not caminho.exists():
        raise FileNotFoundError(f"PDF nao encontrado: {caminho}")
    with fitz.open(caminho) as doc:
        etiquetas = _ler_doc(doc)
    log.info("PDF %s: %d paginas lidas", caminho.name, len(etiquetas))
    return etiquetas


def ler_etiquetas_bytes(dados: bytes) -> list[PDFEtiqueta]:
    """Igual a ``ler_etiquetas``, mas a partir dos BYTES do PDF (a UI le o
    arquivo uma vez e reaproveita o buffer)."""
    with fitz.open(stream=dados, filetype="pdf") as doc:
        etiquetas = _ler_doc(doc)
    log.info("PDF (bytes): %d paginas lidas", len(etiquetas))
    return etiquetas


def tem_camada_textual(caminho: str | Path) -> bool:
    """True se o PDF tem texto extraivel (heuristica: alguma pagina retorna
    texto). Serve pra decidir cedo se o caminho textual e' viavel ou se e' um
    PDF digitalizado (que exigiria o fallback OCR opcional)."""
    with fitz.open(caminho) as doc:
        for page in doc:
            if page.get_text("text").strip():
                return True
    return False
