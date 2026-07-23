"""Re-monta etiquetas no layout da bobina do usuario (modo ``composto``).

Estrategia: compor cada LINHA da bobina (todas as colunas) como UM bitmap, do
tamanho exato da midia, e enviar 1 bloco ZPL (``^GFA``) por linha. Assim cada
``^XA`` tem a altura de uma etiqueta -> a impressora sincroniza no gap a cada
linha (imprime todas) e o conteudo cai alinhado a etiqueta.

Legibilidade (v0.4): o SKU Shopee (sempre confiavel, vem do QR) e o Seller SKU
(quando confirmado no catalogo manual/OCR) sao impressos como **texto ZPL
NATIVO** (``^A0`` + ``^FD``, fonte escalavel da propria impressora) -- nitidez
real, sem limite de raster (ver ClickUp 86ajk2mc2: nenhum ajuste de bitmap
chegava perto da nitidez de um campo de texto de verdade). A **descricao** e o
Seller SKU **sem mapeamento no catalogo** so existem rasterizados no bitmap da
Shopee (sem OCR confiavel), entao continuam **recortados do bitmap**
(``grf_decoder.crop_seller_sku`` / ``crop_descricao``) com downscale limpo.

``_colocar_etiqueta`` sempre pinta os campos nativos no canvas TAMBEM (preview
visual rapido, aproximacao em fonte TrueType via PIL) e devolve a lista de
``_CampoNativo`` com o texto/posicao/tamanho reais em unidades ZPL; quem gera o
ZPL de impressao de fato (``gerar_zpl``) usa essa lista para emitir campos
``^FO...^A0N,h,w^FD...^FS`` de verdade, e usa uma versao do canvas SEM esses
campos pintados (senao o texto sairia duplicado: bitmap + campo nativo).
"""
from __future__ import annotations

from dataclasses import dataclass

import qrcode
from qrcode.constants import ERROR_CORRECT_M
from PIL import Image, ImageDraw, ImageFont, ImageOps

from ..utils.logger import get_logger
from . import grf_decoder
from .label_models import LabelModel

log = get_logger("label_renderer")

# Quiet zone (modulos brancos) ao redor do QR regenerado. 4 e o minimo da norma
# ISO/IEC 18004 -> leitura confiavel mesmo em etiqueta pequena (50x25mm).
QR_BORDER_MODULOS = 4

# Espacamentos internos da etiqueta (dots). A zona segura da borda vem do
# modelo (``pad_interno_mm``, configuravel); os demais sao pequenos e fixos.
GAP_QR_TEXTO = 8
GAP_LINHA = 2  # espaco vertical entre as linhas de texto
GAP_TOPO_CODIGOS = 6  # desce o 1o bloco (Seller SKU) — ver ClickUp 86ajk2mc2

# Layout fiel ao sticker original da Shopee: descricao no TOPO (largura total),
# QR abaixo a esquerda e os codigos (seller sku -> SKU) a direita do QR, na
# mesma ordem de leitura do original. Fracoes = TETO de altura de cada bloco;
# a posicao final usa a altura real renderizada.
FRAC_DESC = 0.32     # descricao (bitmap), sobre a altura util da etiqueta
FRAC_SELLER = 0.45   # seller sku, sobre a altura da zona abaixo da descricao
FRAC_SKU = 0.30      # SKU Shopee (texto nativo), idem

# Fontes candidatas (nome resolve no SO; caminhos absolutos cobrem o Linux/CI).
# No Windows do ARTHUR, "arial.ttf" resolve pelo nome. Fallback: fonte embutida
# do Pillow. O texto nativo aqui e so o SKU Shopee (ASCII), sem acentos.
_FONTES_TTF = {
    False: [
        "DejaVuSans.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "arial.ttf",
        "Arial.ttf",
        "LiberationSans-Regular.ttf",
    ],
    True: [
        "DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "arialbd.ttf",
        "Arial Bold.ttf",
        "LiberationSans-Bold.ttf",
    ],
}
_FONT_CACHE: dict[tuple[int, bool], ImageFont.ImageFont] = {}


@dataclass
class _Item:
    """Conteudo de UMA etiqueta a compor: QR + Seller SKU e descricao (bitmaps)
    + SKU Shopee (texto, do QR). ``seller_sku`` e o texto do catalogo manual
    (quando mapeado) -> renderizado nativo e nitido em vez do recorte do bitmap."""
    qr: Image.Image
    seller_img: Image.Image | None
    descricao: Image.Image | None
    sku: str
    seller_sku: str = ""
    # Texto da descricao (fonte PDF). Quando preenchido, a descricao e' renderizada
    # como texto ZPL NATIVO (nitido) em vez do recorte de bitmap ``descricao``.
    descricao_texto: str = ""


@dataclass
class _CampoNativo:
    """1 linha de texto ZPL nativo (``^A0`` + ``^FD``) a emitir na impressao,
    em coordenadas ABSOLUTAS (dots) da etiqueta/linha da bobina."""
    x: int
    y: int
    altura: int    # ^A altura (dots)
    largura: int   # ^A largura (dots)
    texto: str


# Calibrado com zpl-renderer-js local (script de calibracao, 2026-07-22, ver
# ClickUp 86ajk2mc2): pra fonte ZPL escalavel (``^A0``), com largura = altura *
# RAZAO_LARGURA_ALTURA, a largura TOTAL do texto renderizado fica em torno de
# ``n_caracteres * FATOR_LARGURA_CHAR * altura`` (medido ~0.354 pra strings
# com letras/tracos, ~0.286 pra strings so com digitos -- usamos o maior, com
# folga, pra nunca estourar a caixa mesmo em strings so de letras).
FATOR_LARGURA_CHAR = 0.36
RAZAO_LARGURA_ALTURA = 0.6
GAP_LINHA_NATIVA = 4  # espaco (dots) entre linhas de texto ZPL nativo quebrado


def _fonte(size: int, bold: bool = False) -> ImageFont.ImageFont:
    """Carrega (com cache) a melhor fonte disponivel no tamanho pedido."""
    size = max(6, int(size))
    chave = (size, bold)
    cached = _FONT_CACHE.get(chave)
    if cached is not None:
        return cached
    fonte: ImageFont.ImageFont | None = None
    for nome in _FONTES_TTF[bold]:
        try:
            fonte = ImageFont.truetype(nome, size)
            break
        except OSError:
            continue
    if fonte is None:
        try:
            fonte = ImageFont.load_default(size=size)  # Pillow >= 10.1
        except TypeError:
            fonte = ImageFont.load_default()
    _FONT_CACHE[chave] = fonte
    return fonte


def _trim_tinta(img: Image.Image) -> Image.Image:
    """Apara o espaco branco em volta da tinta (bbox dos pixels pretos)."""
    cinza = img.convert("L")
    bbox = ImageOps.invert(cinza).getbbox()  # bbox dos pixels nao-brancos
    return img.crop(bbox) if bbox else img


GAP_LINHA_TEXTO = 2  # espaco (dots) entre linhas quando o texto nativo quebra em 2

# Quando o texto NAO quebra em linhas, a altura disponivel costuma sobrar (o
# gargalo real e a largura da etiqueta). Deixamos a altura esticar alem da
# escala da largura pra usar esse espaco -- prioriza legibilidade pratica
# sobre fidelidade de proporcao (ver ClickUp 86ajk2mc2). Testado visualmente
# (recorte real do Seller SKU, texto bem mais largo que alto): esticar a
# altura ATE A CAIXA TODA (sem teto) distorce demais em textos com esse
# formato (~14x mais alto que a escala da largura) -- vira tiras verticais
# ilegiveis. 3.0 deixa o texto notavelmente maior/mais legivel sem chegar
# nesse ponto de distorcao.
ESTICA_ALTURA_MAX = 3.0


def _dividir_texto_meio(texto: str) -> tuple[str, str] | None:
    """Acha o melhor ponto pra quebrar ``texto`` em 2 linhas, perto do meio,
    preferindo um separador natural (espaco, ``_``, ``-``). ``None`` se o
    texto for curto demais pra valer a pena quebrar."""
    if len(texto) < 8:
        return None
    meio = len(texto) / 2
    melhor = None
    for i, c in enumerate(texto):
        if c in " _-" and (melhor is None or abs(i - meio) < abs(melhor - meio)):
            melhor = i
    if melhor is not None:
        # Quebra DEPOIS do separador (ex.: "SKU 123_" / "456"), mantendo-o na 1a linha.
        linha1, linha2 = texto[: melhor + 1].rstrip(), texto[melhor + 1 :].lstrip()
    else:
        meio_i = round(meio)
        linha1, linha2 = texto[:meio_i], texto[meio_i:]
    if not linha1 or not linha2:
        return None
    return linha1, linha2


def _maior_fonte_para_linhas(linhas: list[str], box_w: int, box_h: int, bold: bool) -> int:
    """Maior tamanho de fonte (busca binaria) que cabe as ``linhas`` empilhadas
    em (``box_w`` x ``box_h``), com ``GAP_LINHA_TEXTO`` entre elas."""

    def cabe(size: int) -> bool:
        fonte = _fonte(size, bold)
        boxes = [fonte.getbbox(l) for l in linhas]
        largura = max(r - l for l, t, r, b in boxes)
        altura = sum(b - t for l, t, r, b in boxes) + GAP_LINHA_TEXTO * (len(linhas) - 1)
        return largura <= box_w and altura <= box_h

    lo, hi, melhor = 6, max(6, box_h), 6
    while lo <= hi:
        mid = (lo + hi) // 2
        if cabe(mid):
            melhor, lo = mid, mid + 1
        else:
            hi = mid - 1
    return melhor


def _desenhar_linhas(linhas: list[str], size: int, bold: bool) -> Image.Image:
    """Desenha cada linha (fonte ``size``), empilha centralizado e binariza."""
    fonte = _fonte(size, bold)
    imgs = []
    for txt in linhas:
        l, t, r, b = fonte.getbbox(txt)
        w, h = max(1, r - l), max(1, b - t)
        im = Image.new("L", (w, h), 255)
        ImageDraw.Draw(im).text((-l, -t), txt, font=fonte, fill=0)
        imgs.append(im)
    largura = max(im.width for im in imgs)
    altura = sum(im.height for im in imgs) + GAP_LINHA_TEXTO * (len(imgs) - 1)
    canvas = Image.new("L", (largura, altura), 255)
    y = 0
    for im in imgs:
        canvas.paste(im, ((largura - im.width) // 2, y))
        y += im.height + GAP_LINHA_TEXTO
    # Threshold sem dithering: preserva os tracos finos da fonte.
    return canvas.point(lambda p: 0 if p < 128 else 255).convert("1")


def _render_texto(texto: str, box_w: int, box_h: int, bold: bool = False, max_linhas: int = 1) -> Image.Image | None:
    """Desenha ``texto`` (preto/branco, 1-bit) ajustando a fonte para caber em
    (``box_w`` x ``box_h``). Retorna a imagem aparada ou ``None`` se vazio.

    Com ``max_linhas=2``, tambem tenta quebrar o texto em 2 linhas e usa
    essa versao se ela permitir uma fonte MAIOR que em 1 linha so -- util
    pra codigos longos (Seller SKU/SKU), onde 1 linha forcaria fonte minuscula
    so pra caber na largura da etiqueta.
    """
    texto = (texto or "").strip()
    if not texto or box_w <= 0 or box_h <= 0:
        return None

    candidatos = [[texto]]
    if max_linhas >= 2:
        partes = _dividir_texto_meio(texto)
        if partes is not None:
            candidatos.append(list(partes))

    melhor_linhas, melhor_fonte = candidatos[0], _maior_fonte_para_linhas(candidatos[0], box_w, box_h, bold)
    for linhas in candidatos[1:]:
        fonte = _maior_fonte_para_linhas(linhas, box_w, box_h, bold)
        if fonte > melhor_fonte:
            melhor_linhas, melhor_fonte = linhas, fonte

    return _desenhar_linhas(melhor_linhas, melhor_fonte, bold)


def _altura_zpl_para_linhas(linhas: list[str], box_w: int, box_h: int) -> int:
    """Maior ``altura`` (dots) do campo ZPL nativo (``^A0``, largura =
    ``altura * RAZAO_LARGURA_ALTURA``) que cabe as ``linhas`` empilhadas em
    (``box_w`` x ``box_h``), com ``GAP_LINHA_NATIVA`` entre elas. Estimativa de
    largura calibrada empiricamente (ver ``FATOR_LARGURA_CHAR``), sem metricas
    reais de fonte da impressora."""
    n_linhas = len(linhas)
    max_chars = max((len(l) for l in linhas), default=0)
    if max_chars == 0:
        return 0
    disponivel_h = (box_h - GAP_LINHA_NATIVA * (n_linhas - 1)) / n_linhas
    altura_por_largura = box_w / (max_chars * FATOR_LARGURA_CHAR)
    return int(min(disponivel_h, altura_por_largura))


def _campo_zpl_nativo(texto: str, box_w: int, box_h: int, max_linhas: int = 1) -> tuple[list[str], int, int] | None:
    """Escolhe (linhas, altura, largura) pro texto ser impresso como campo ZPL
    NATIVO (``^A0``) cabendo em (``box_w`` x ``box_h``) -- irmao de
    ``_render_texto``, mesma logica de quebra em ``max_linhas``, mas devolvendo
    o TAMANHO em unidades ZPL (dots) em vez de uma imagem PIL. ``None`` se o
    texto for vazio ou nao coube nem no minimo legivel.
    """
    texto = (texto or "").strip()
    if not texto or box_w <= 0 or box_h <= 0:
        return None

    candidatos = [[texto]]
    if max_linhas >= 2:
        partes = _dividir_texto_meio(texto)
        if partes is not None:
            candidatos.append(list(partes))

    melhor_linhas, melhor_altura = candidatos[0], _altura_zpl_para_linhas(candidatos[0], box_w, box_h)
    for linhas in candidatos[1:]:
        altura = _altura_zpl_para_linhas(linhas, box_w, box_h)
        if altura > melhor_altura:
            melhor_linhas, melhor_altura = linhas, altura

    if melhor_altura < 8:  # fonte minuscula demais pra ser legivel/valida no ZPL
        return None
    largura = max(1, round(melhor_altura * RAZAO_LARGURA_ALTURA))
    return melhor_linhas, melhor_altura, largura


def _campo_zpl_nativo_altura_fixa(
    texto: str, altura: int, box_w: int, max_linhas: int = 1
) -> tuple[list[str], int, int] | None:
    """Como ``_campo_zpl_nativo``, mas com a ALTURA JA DECIDIDA (padrao do
    lote inteiro, ver ``_calibrar_padrao_lote``) -- so escolhe se o texto
    cabe em 1 linha nessa altura ou precisa quebrar em 2, nunca muda o
    tamanho da fonte. Garante que TODAS as etiquetas do lote saiam com a
    mesma "fonte" (mesma altura/largura de campo ZPL), em vez de cada uma
    otimizar seu proprio tamanho (ver ClickUp 86ajk2mc2: usuario queria
    aparencia padronizada entre etiquetas, nao cada uma no seu melhor
    tamanho individual)."""
    texto = (texto or "").strip()
    if not texto or altura < 8 or box_w <= 0:
        return None
    largura = max(1, round(altura * RAZAO_LARGURA_ALTURA))

    def cabe(linhas: list[str]) -> bool:
        return max(len(l) for l in linhas) * FATOR_LARGURA_CHAR * largura <= box_w

    linha_unica = [texto]
    if max_linhas < 2 or cabe(linha_unica):
        return linha_unica, altura, largura
    partes = _dividir_texto_meio(texto)
    if partes is not None:
        return list(partes), altura, largura
    return linha_unica, altura, largura  # sem onde quebrar -- tenta mesmo assim


# Altura minima (dots) legivel pra descricao nativa. ~16 dots @203dpi ~= 2mm.
# Abaixo disso o texto termico borra/some -- o dimensionador para aqui e, se nem
# assim couber na faixa reservada, renderiza no minimo mesmo e sinaliza (a
# descricao empurra o resto pra baixo; a validacao pre-impressao acusa).
MIN_DESC_ALTURA = 16


def _quebrar_palavras(texto: str, max_chars: int) -> list[str]:
    """Quebra ``texto`` em linhas de ate ``max_chars`` caracteres, SEM cortar
    palavra no meio (word-wrap guloso). Preserva a ordem e todas as palavras
    (requisito: descricao nunca perde/altera conteudo). Uma palavra maior que
    ``max_chars`` fica sozinha na linha (vai estourar a largura -- caso raro em
    descricao; o dimensionador reduz a fonte ate caber ou sinaliza)."""
    palavras = texto.split()
    linhas: list[str] = []
    atual = ""
    for p in palavras:
        if not atual:
            atual = p
        elif len(atual) + 1 + len(p) <= max_chars:
            atual = f"{atual} {p}"
        else:
            linhas.append(atual)
            atual = p
    if atual:
        linhas.append(atual)
    return linhas


def _campo_desc_nativo(
    texto: str, box_w: int, box_h: int
) -> tuple[list[str], int, int, bool]:
    """Dimensiona a descricao como texto ZPL NATIVO (``^A0``) na faixa
    (``box_w`` x ``box_h``), no TOPO da etiqueta em largura total.

    Diferente do SKU/Seller SKU (token unico, quebra em ate 2), a descricao e'
    uma frase -> word-wrap em quantas linhas forem necessarias. Escolhe a MAIOR
    altura de fonte (do teto da faixa pra baixo) cujas linhas quebradas caibam
    em largura E altura. Se nem no minimo legivel (``MIN_DESC_ALTURA``) couber
    na altura da faixa, devolve a versao no minimo mesmo assim (empurra o resto
    pra baixo) com ``estourou=True`` -- nunca corta o texto.

    Retorna (linhas, altura, largura, estourou) ou ``([], 0, 0, False)`` se
    vazio. ``largura`` = altura * RAZAO_LARGURA_ALTURA (mesma proporcao dos
    demais campos nativos).
    """
    texto = " ".join((texto or "").split())  # normaliza espacos
    if not texto or box_w <= 0 or box_h <= 0:
        return [], 0, 0, False

    def _wrap(altura: int) -> tuple[list[str], int]:
        # Largura TOTAL do texto ~= n_chars * FATOR_LARGURA_CHAR * ALTURA (calibracao
        # do projeto, ver FATOR_LARGURA_CHAR) -- a largura do char escala com a
        # ALTURA da fonte, nao com o parametro ``largura`` do ^A0. Usar ``largura``
        # aqui subestimava a largura em ~1.67x e estourava a borda.
        largura = max(1, round(altura * RAZAO_LARGURA_ALTURA))
        max_chars = max(1, int(box_w / (FATOR_LARGURA_CHAR * altura)))
        return _quebrar_palavras(texto, max_chars), largura

    teto = max(MIN_DESC_ALTURA, box_h)
    for altura in range(teto, MIN_DESC_ALTURA - 1, -1):
        linhas, largura = _wrap(altura)
        alt_total = len(linhas) * altura + GAP_LINHA_NATIVA * (len(linhas) - 1)
        if alt_total <= box_h:
            return linhas, altura, largura, False

    # Nao coube nem no minimo: usa o minimo e sinaliza estouro (o chamador
    # deixa a descricao empurrar o resto; a validacao pre-impressao alerta).
    linhas, largura = _wrap(MIN_DESC_ALTURA)
    return linhas, MIN_DESC_ALTURA, largura, True


def _colunas_com_tinta(img_cinza: Image.Image, limiar: int = 200) -> list[bool]:
    """Pra cada coluna de ``img_cinza`` (modo L), diz se ha algum pixel de tinta."""
    w, h = img_cinza.size
    px = img_cinza.load()
    return [any(px[x, y] < limiar for y in range(h)) for x in range(w)]


def _dividir_coluna_bitmap(img_cinza: Image.Image) -> list[Image.Image] | None:
    """Divide um recorte de UMA linha de texto (ex.: Seller SKU) em 2 metades
    lado a lado, cortando no espaco em branco entre caracteres mais perto do
    meio. Texto comprido numa coluna estreita fica minusculo pra caber inteiro
    numa linha so -- quebrando em 2, cada metade escala bem mais (metade da
    largura de texto na mesma caixa). ``None`` se o texto for curto demais ou
    nao tiver um gap de coluna claro pra cortar sem partir uma letra ao meio.
    """
    w, h = img_cinza.size
    if w < 40:
        return None
    tinta = _colunas_com_tinta(img_cinza)
    meio = w / 2
    melhor: tuple[float, int, int] | None = None
    x = 0
    while x < w:
        if not tinta[x]:
            ini = x
            while x < w and not tinta[x]:
                x += 1
            centro = (ini + x) / 2
            if melhor is None or abs(centro - meio) < abs(melhor[0] - meio):
                melhor = (centro, ini, x)
        else:
            x += 1
    if melhor is None:
        return None
    _, ini, fim = melhor
    corte = (ini + fim) // 2
    esquerda = _trim_tinta(img_cinza.crop((0, 0, corte, h)))
    direita = _trim_tinta(img_cinza.crop((corte, 0, w, h)))
    if esquerda.size[0] == 0 or direita.size[0] == 0:
        return None
    return [esquerda, direita]


def _dividir_linhas_bitmap(img_cinza: Image.Image, gap_min: int = 1) -> list[Image.Image] | None:
    """Detecta linhas de texto ja empilhadas no recorte (ex.: descricao em 2
    linhas) via faixas de linhas em branco, e devolve uma sub-imagem por linha.
    ``None`` se nao achar uma quebra clara (conteudo de 1 linha so)."""
    w, h = img_cinza.size
    px = img_cinza.load()
    linhas_com_tinta = [any(px[x, y] < 200 for x in range(w)) for y in range(h)]
    segmentos: list[tuple[int, int]] = []
    inicio = None
    brancos = 0
    for y, tem in enumerate(linhas_com_tinta):
        if tem:
            if inicio is None:
                inicio = y
            brancos = 0
        elif inicio is not None:
            brancos += 1
            if brancos >= gap_min:
                segmentos.append((inicio, y - brancos))
                inicio = None
    if inicio is not None:
        segmentos.append((inicio, h))
    if len(segmentos) < 2:
        return None
    partes = [_trim_tinta(img_cinza.crop((0, max(0, a - 1), w, min(h, b + 2)))) for a, b in segmentos]
    return [p for p in partes if p.size[0] > 0]


def _montar_partes_bitmap(partes: list[Image.Image], box_w: int, box_h: int, gap: int = 2) -> tuple[Image.Image, float] | None:
    """Escala ``partes`` (uma ou mais sub-imagens ja aparadas) por um fator
    UNICO e comum entre elas, empilha centralizado com ``gap`` entre linhas.
    Largura usa a maior parte; altura estica ate ``ESTICA_ALTURA_MAX`` alem da
    escala da largura (ver ``_resize_bitmap``). Devolve (imagem, escala_largura)
    -- a escala serve pra comparar candidatos (1 linha vs. quebrado) e escolher
    o que renderiza MAIOR.
    """
    larguras = [p.size[0] for p in partes]
    alturas = [p.size[1] for p in partes]
    if not larguras or max(larguras) == 0 or box_w <= 0 or box_h <= 0:
        return None
    disponivel_h = box_h - gap * (len(partes) - 1)
    if disponivel_h <= 0:
        return None
    escala_w = box_w / max(larguras)
    escala_h = min(disponivel_h / sum(alturas), escala_w * ESTICA_ALTURA_MAX)
    if escala_w <= 0 or escala_h <= 0:
        return None
    redimensionadas = []
    for p in partes:
        w, h = p.size
        novo = (max(1, round(w * escala_w)), max(1, round(h * escala_h)))
        red = ImageOps.autocontrast(p.resize(novo, Image.LANCZOS))
        redimensionadas.append(red.point(lambda v: 0 if v < 145 else 255).convert("1"))
    largura_final = max(r.width for r in redimensionadas)
    altura_final = sum(r.height for r in redimensionadas) + gap * (len(redimensionadas) - 1)
    canvas = Image.new("1", (largura_final, altura_final), 1)
    y = 0
    for r in redimensionadas:
        canvas.paste(r, ((largura_final - r.width) // 2, y))
        y += r.height + gap
    return canvas, escala_w


def _resize_bitmap(img: Image.Image, box_w: int, box_h: int, max_linhas: int = 1) -> Image.Image | None:
    """Encaixa um recorte do bitmap (Seller SKU ou descricao) na caixa, com
    downscale limpo: autocontraste e threshold SEM dithering (Floyd-Steinberg
    picotava o texto). LANCZOS suaviza as bordas antes do threshold.

    A largura costuma ser o gargalo (a coluna de codigos ao lado do QR e
    estreita); a altura sobra. Em vez de travar a proporcao pela menor escala
    (deixando a altura ociosa), a altura estica ate ``ESTICA_ALTURA_MAX`` alem
    da escala da largura -- texto maior e mais legivel, ao custo de leve
    distorcao (ver ClickUp 86ajk2mc2: prioridade e legibilidade pratica, nao
    fidelidade visual).

    Com ``max_linhas=2``, tambem tenta quebrar o conteudo em 2 linhas -- ou
    porque ja SAO 2 linhas empilhadas no recorte (``_dividir_linhas_bitmap``,
    caso da descricao), ou cortando 1 linha comprida ao meio num espaco entre
    caracteres (``_dividir_coluna_bitmap``, caso do Seller SKU). A quebra por
    LINHA (quando existe) sempre vence -- corte por coluna so entra quando o
    conteudo e' 1 linha so (senao ele cortaria cada linha ao meio e empilharia
    "metade esquerda das 2 linhas" sobre "metade direita das 2 linhas",
    embaralhando a ordem de leitura). Mantem o conteudo como BITMAP (fiel ao
    original impresso pela Shopee), so reorganiza o layout.

    NAO aplica reforco de traco (MinFilter/dilate): testado visualmente e
    piora -- gruda letras adjacentes e fecha contra-formas (ex.: "a", "o"),
    ja que os tracos originais tem so 1-2px. So o LANCZOS + esticar a altura
    ja melhora a legibilidade sem esse efeito colateral.
    """
    src = _trim_tinta(img).convert("L")
    w, h = src.size
    if w == 0 or h == 0 or box_w <= 0 or box_h <= 0:
        return None

    candidatos = [[src]]
    if max_linhas >= 2:
        por_linha = _dividir_linhas_bitmap(src)
        if por_linha is not None:
            # Ja tem quebra de linha natural (ex.: descricao): NAO tenta corte
            # por coluna tambem -- cortaria cada linha ao meio e embaralharia
            # a ordem de leitura (ver docstring acima).
            candidatos.append(por_linha[:max_linhas])
        else:
            por_coluna = _dividir_coluna_bitmap(src)
            if por_coluna is not None:
                candidatos.append(por_coluna)

    melhor: tuple[Image.Image, float] | None = None
    for partes in candidatos:
        resultado = _montar_partes_bitmap(partes, box_w, box_h)
        if resultado is not None and (melhor is None or resultado[1] > melhor[1]):
            melhor = resultado
    return melhor[0] if melhor is not None else None


def _resize_bitmap_escala_fixa(img: Image.Image, escala_w: float, escala_h: float) -> Image.Image | None:
    """Como ``_resize_bitmap``, mas com a ESCALA JA DECIDIDA (padrao do lote
    inteiro, ver ``_calibrar_padrao_lote``) -- aplica a MESMA escala em
    qualquer etiqueta, em vez de cada uma buscar seu proprio tamanho maximo
    (o que fazia etiquetas com texto curto ficarem enormes e as com texto
    longo minusculas -- lote visualmente inconsistente)."""
    src = _trim_tinta(img).convert("L")
    w, h = src.size
    if w == 0 or h == 0 or escala_w <= 0 or escala_h <= 0:
        return None
    novo = (max(1, round(w * escala_w)), max(1, round(h * escala_h)))
    red = ImageOps.autocontrast(src.resize(novo, Image.LANCZOS))
    return red.point(lambda p: 0 if p < 145 else 255).convert("1")


def _qr_nitido(data: str, lado_dots: int) -> Image.Image | None:
    """Regenera o QR a partir do dado decodificado (``item.sku``), com cada
    modulo medindo um numero INTEIRO de dots -> sem reescala fracionaria.

    O QR original da Shopee chega ja rasterizado; redimensiona-lo (NEAREST) para
    o tamanho do destino por fator nao-inteiro deixa modulos com larguras
    diferentes (uns 1px, outros 2px) -> QR irregular, as vezes ilegivel. Aqui
    re-codificamos o MESMO conteudo e escolhemos ``box_size`` (dots por modulo)
    inteiro que caiba em ``lado_dots`` -> QR perfeitamente uniforme e nitido.

    Devolve uma imagem "1" (lado <= ``lado_dots``) ou ``None`` se nao der para
    gerar (dado vazio ou modulos nao cabem nem com 1 dot por modulo).
    """
    if not data or lado_dots <= 0:
        return None
    try:
        qr = qrcode.QRCode(error_correction=ERROR_CORRECT_M, border=QR_BORDER_MODULOS)
        qr.add_data(data)
        qr.make(fit=True)
        # Modulos totais = grade do QR + quiet zone dos dois lados.
        total_mod = qr.modules_count + 2 * qr.border
        box = lado_dots // total_mod  # dots por modulo (inteiro, sem fracao)
        if box < 1:
            return None  # QR denso demais para o espaco: cai no fallback
        qr.box_size = box
        return qr.make_image(fill_color="black", back_color="white").get_image().convert("1")
    except Exception as exc:  # noqa: BLE001
        log.warning("Falha ao regenerar QR (%s); usando recorte do bitmap.", exc)
        return None


@dataclass
class _PadraoLote:
    """Tamanho PADRAO (mesmo pra todas as etiquetas do lote) pro SKU/Seller
    SKU, calibrado uma vez pelo PIOR CASO do lote inteiro (ver
    ``_calibrar_padrao_lote``). Campos ``None`` quando o lote nao tem nenhum
    item daquele tipo (ex.: nenhum Seller SKU mapeado no catalogo)."""
    sku_altura: int
    seller_altura_nativa: int | None
    seller_escala_w: float | None
    seller_escala_h: float | None


def _calibrar_padrao_lote(itens: list["_Item"], model: LabelModel) -> _PadraoLote:
    """Acha o tamanho PADRAO do SKU/Seller SKU pra todo o lote, calibrado
    pelo PIOR CASO (texto mais longo / imagem mais larga entre TODOS os
    itens) -- garante caber em qualquer etiqueta e faz todas saírem com a
    mesma "fonte"/escala, em vez de cada uma otimizar seu proprio tamanho
    (ClickUp 86ajk2mc2: usuario queria etiquetas padronizadas, nao cada uma
    do seu jeito).

    Geometria de referencia (box_w/box_h nominais) assume o PIOR CASO
    tambem: descricao usando toda a fatia reservada (``FRAC_DESC``) e QR no
    tamanho MAXIMO reservado (``qr_mm``) -- a geometria REAL de cada item so
    pode ser igual ou mais generosa que essa (QR regenerado quase sempre sai
    menor que a reserva, descricao raramente usa 100% da fatia), entao o
    padrao calibrado aqui sempre caiba de verdade no render final.
    """
    pad = model.dots(model.pad_interno_mm)
    topo = model.dots(model.margem_topo_mm)
    util_w = model.largura_dots - 2 * pad
    util_h = model.altura_dots - topo - 2 * pad
    zona_h_nominal = max(0, util_h - round(util_h * FRAC_DESC) - GAP_QR_TEXTO // 2)
    qr_dots_nominal = min(model.dots(model.qr_mm), zona_h_nominal)
    box_w_nominal = max(0, util_w - qr_dots_nominal - GAP_QR_TEXTO)
    sku_box_h_nominal = round(zona_h_nominal * FRAC_SKU)
    seller_box_h_nominal = round(zona_h_nominal * FRAC_SELLER)

    sku_alturas: list[int] = []
    seller_alturas_nativas: list[int] = []
    seller_crops: list[tuple[int, int]] = []
    for it in itens:
        if it.sku.strip():
            campo = _campo_zpl_nativo(f"SKU {it.sku}", box_w_nominal, sku_box_h_nominal, max_linhas=2)
            if campo is not None:
                sku_alturas.append(campo[1])
        if it.seller_sku.strip():
            campo = _campo_zpl_nativo(it.seller_sku, box_w_nominal, seller_box_h_nominal, max_linhas=2)
            if campo is not None:
                seller_alturas_nativas.append(campo[1])
        elif it.seller_img is not None:
            w, h = _trim_tinta(it.seller_img).convert("L").size
            if w > 0 and h > 0:
                seller_crops.append((w, h))

    sku_altura = min(sku_alturas) if sku_alturas else max(8, sku_box_h_nominal)
    seller_altura_nativa = min(seller_alturas_nativas) if seller_alturas_nativas else None

    seller_escala_w = seller_escala_h = None
    if seller_crops and box_w_nominal > 0 and seller_box_h_nominal > 0:
        seller_escala_w = min(box_w_nominal / w for w, h in seller_crops)
        seller_escala_h = min(min(seller_box_h_nominal / h, seller_escala_w * ESTICA_ALTURA_MAX) for w, h in seller_crops)

    return _PadraoLote(sku_altura, seller_altura_nativa, seller_escala_w, seller_escala_h)


def _colocar_etiqueta(
    canvas: Image.Image,
    x0: int,
    item: _Item,
    model: LabelModel,
    bake_texto_nativo: bool = True,
    padrao: _PadraoLote | None = None,
) -> list[_CampoNativo]:
    """Compoe UMA etiqueta sobre o canvas, fiel ao sticker original da Shopee:
    descricao no TOPO (largura total, centralizada); abaixo, QR a esquerda e os
    codigos a direita, na ordem do original (seller sku -> SKU Shopee).

    Devolve os campos de texto ZPL NATIVO (SKU sempre; Seller SKU quando
    mapeado no catalogo) com posicao/tamanho ja resolvidos -- quem gera o ZPL
    de impressao usa essa lista pra emitir ``^A0``/``^FD`` de verdade.

    ``bake_texto_nativo`` controla se esses mesmos campos TAMBEM sao pintados
    no ``canvas`` como aproximacao em fonte TrueType (PIL) -- True pro preview
    visual rapido da UI (o canvas fica completo e correto sozinho); False pro
    canvas usado na impressao real (senao o texto sairia duplicado: bitmap +
    campo ZPL nativo por cima).

    ``padrao`` (``_PadraoLote``, opcional): quando fornecido, usa o tamanho
    PADRAO do lote (calibrado uma vez por ``_calibrar_padrao_lote``) em vez
    de cada etiqueta buscar seu proprio tamanho maximo -- garante que todas
    as etiquetas do lote saiam com a mesma "fonte"/escala. Sem ``padrao``
    (``None``, o default), cada etiqueta e' otimizada individualmente --
    comportamento usado pelo preview de UMA etiqueta isolada.
    """
    campos: list[_CampoNativo] = []
    altura = model.altura_dots
    topo = model.dots(model.margem_topo_mm)
    pad = model.dots(model.pad_interno_mm)
    util_x = x0 + pad
    util_w = model.largura_dots - 2 * pad
    util_y = topo + pad
    util_h = altura - topo - 2 * pad
    if util_w <= 0 or util_h <= 0:
        return campos

    # --- Descricao no topo, largura total (como no sticker original). Quando ha
    # TEXTO (fonte PDF), renderiza NATIVO (^A0, nitido, word-wrap); senao cai no
    # recorte de bitmap (comportamento legado). So desce o quanto ela realmente
    # usou.
    zona_y = util_y
    desc_box_h = round(util_h * FRAC_DESC)
    if item.descricao_texto.strip():
        linhas, alt, larg, _estourou = _campo_desc_nativo(item.descricao_texto, util_w, desc_box_h)
        y = util_y
        for linha in linhas:
            # Centraliza a linha na largura util. Largura do texto ~= n_chars *
            # FATOR * ALTURA (escala com a altura da fonte, ver _campo_desc_nativo).
            larg_txt = min(util_w, round(len(linha) * FATOR_LARGURA_CHAR * alt))
            x = util_x + max(0, (util_w - larg_txt) // 2)
            campos.append(_CampoNativo(x=x, y=y, altura=alt, largura=larg, texto=linha))
            if bake_texto_nativo:
                aprox = _render_texto(linha, util_w, alt)
                if aprox is not None:
                    canvas.paste(aprox, (util_x + (util_w - aprox.width) // 2, y))
            y += alt + GAP_LINHA_NATIVA
        if linhas:
            zona_y = (y - GAP_LINHA_NATIVA) + GAP_QR_TEXTO // 2
    elif item.descricao is not None:
        desc = _resize_bitmap(item.descricao, util_w, desc_box_h, max_linhas=2)
        if desc is not None:
            canvas.paste(desc, (util_x + (util_w - desc.width) // 2, util_y))
            zona_y = util_y + desc.height + GAP_QR_TEXTO // 2

    # --- Zona inferior: QR a esquerda ---
    zona_h = util_y + util_h - zona_y
    qr_dots = min(model.dots(model.qr_mm), zona_h)
    # Preferencia: regenerar o QR do dado decodificado (modulos em dots inteiros
    # -> nitido). Fallback: recorte do bitmap original reescalado (NEAREST).
    qr = _qr_nitido(item.sku, qr_dots)
    if qr is None:
        qr = item.qr.convert("L").resize((qr_dots, qr_dots), Image.NEAREST).convert("1")
    qr_w, qr_h = qr.size
    canvas.paste(qr, (util_x, zona_y + max(0, (zona_h - qr_h) // 2)))

    # --- Codigos a direita do QR REAL (nao da reserva ``qr_mm``): o QR
    # regenerado arredonda p/ modulos inteiros e a folga vira largura de texto.
    box_x = util_x + qr_w + GAP_QR_TEXTO
    box_w = util_x + util_w - box_x
    if box_w <= 0 or zona_h <= 0:
        return campos

    # Cada bloco e' (altura_total, imagem_bitmap_ou_None, linhas_nativas_ou_None).
    # linhas_nativas: list[(texto, altura_zpl, largura_zpl, bold)].
    blocos: list[tuple[int, Image.Image | None, list[tuple[str, int, int, bool]] | None]] = []

    # Seller SKU (codigo de coleta mais importante), em destaque.
    # Preferencia: texto do catalogo manual -> campo ZPL NATIVO (^A0), MESMA
    # fonte/peso do SKU (^A0 nao tem variante negrito -- padronizado, ver
    # ClickUp 86ajk2mc2). Fallback: recorte do bitmap (texto ~6px da folha).
    # NAO manda a imagem esticada pra preencher a caixa toda (testado: o texto
    # e' bem mais largo que alto -- ~27x mais largo -- e esticar a altura pra
    # preencher uma caixa 3:1 vira tiras verticais ilegiveis). Usa
    # ``_resize_bitmap`` (escala pela largura, altura estica so ate
    # ESTICA_ALTURA_MAX, tenta quebrar em 2 linhas se ajudar) pra chegar o
    # mais perto possivel do tamanho/nitidez do campo nativo do SKU.
    seller_box_h = round(zona_h * FRAC_SELLER)
    if item.seller_sku.strip():
        if padrao is not None and padrao.seller_altura_nativa is not None:
            campo = _campo_zpl_nativo_altura_fixa(item.seller_sku, padrao.seller_altura_nativa, box_w, max_linhas=2)
        else:
            campo = _campo_zpl_nativo(item.seller_sku, box_w, seller_box_h, max_linhas=2)
        if campo is not None:
            linhas, alt, larg = campo
            linhas_nativas = [(l, alt, larg, False) for l in linhas]
            altura_total = alt * len(linhas) + GAP_LINHA_NATIVA * (len(linhas) - 1)
            blocos.append((altura_total, None, linhas_nativas))
    elif item.seller_img is not None:
        if padrao is not None and padrao.seller_escala_w is not None:
            img = _resize_bitmap_escala_fixa(item.seller_img, padrao.seller_escala_w, padrao.seller_escala_h)
        else:
            img = _resize_bitmap(item.seller_img, box_w, seller_box_h, max_linhas=2)
        if img is not None:
            blocos.append((img.height, img, None))

    # SKU Shopee: sempre confiavel (vem do QR) -> sempre campo ZPL nativo.
    sku_box_h = round(zona_h * FRAC_SKU)
    if item.sku.strip():
        if padrao is not None:
            campo = _campo_zpl_nativo_altura_fixa(f"SKU {item.sku}", padrao.sku_altura, box_w, max_linhas=2)
        else:
            campo = _campo_zpl_nativo(f"SKU {item.sku}", box_w, sku_box_h, max_linhas=2)
        if campo is not None:
            linhas, alt, larg = campo
            linhas_nativas = [(l, alt, larg, False) for l in linhas]
            altura_total = alt * len(linhas) + GAP_LINHA_NATIVA * (len(linhas) - 1)
            blocos.append((altura_total, None, linhas_nativas))
    if not blocos:
        return campos

    # Com 2+ blocos, o 1o (Seller SKU) nascia colado no topo da zona (mesma
    # altura do topo do QR) -> "muito acima" no teste fisico (ClickUp
    # 86ajk2mc2). Desce um pouco antes de distribuir a folga entre os blocos.
    topo_extra = GAP_TOPO_CODIGOS if len(blocos) > 1 else 0
    sobra = zona_h - topo_extra - sum(b[0] for b in blocos)
    if len(blocos) == 1:
        ys = [zona_y + max(0, sobra // 2)]
    else:
        vao = max(GAP_LINHA, sobra / (len(blocos) - 1))
        ys = []
        y = float(zona_y + topo_extra)
        for altura_b, _, _ in blocos:
            ys.append(y)
            y += altura_b + vao

    for (_, imagem, linhas_nativas), y0 in zip(blocos, ys):
        y0 = round(y0)
        if imagem is not None:
            canvas.paste(imagem, (box_x, y0))
            continue
        y_linha = y0
        for texto_l, alt_l, larg_l, bold_l in linhas_nativas:
            campos.append(_CampoNativo(x=box_x, y=y_linha, altura=alt_l, largura=larg_l, texto=texto_l))
            if bake_texto_nativo:
                aprox = _render_texto(texto_l, box_w, alt_l, bold=bold_l)
                if aprox is not None:
                    canvas.paste(aprox, (box_x, y_linha))
            y_linha += alt_l + GAP_LINHA_NATIVA

    return campos


def compor_etiqueta(item: _Item, model: LabelModel) -> Image.Image:
    """Imagem de UMA etiqueta isolada, com o texto nativo (SKU/Seller SKU)
    JA aproximado em fonte TrueType (PIL) -- pro preview visual rapido da UI
    (painel/miniatura), que nao passa pelo interpretador ZPL. Pra impressao
    de verdade use ``compor_etiqueta_zpl``."""
    canvas = Image.new("1", (model.largura_dots, model.altura_dots), 1)
    _colocar_etiqueta(canvas, 0, item, model, bake_texto_nativo=True)
    return canvas


def compor_etiqueta_zpl(
    item: _Item, model: LabelModel, padrao: _PadraoLote | None = None
) -> tuple[Image.Image, list[_CampoNativo]]:
    """Igual a ``compor_etiqueta``, mas SEM o texto nativo pintado no bitmap
    (``bake_texto_nativo=False``) -- devolve o canvas (QR/descricao/Seller SKU
    cru) + a lista de campos ZPL nativos a emitir separadamente. Usar pra
    gerar o ZPL de impressao/interpretacao de verdade (senao o texto sairia
    duplicado: bitmap + campo nativo). ``padrao``: ver ``_colocar_etiqueta``."""
    canvas = Image.new("1", (model.largura_dots, model.altura_dots), 1)
    campos = _colocar_etiqueta(canvas, 0, item, model, bake_texto_nativo=False, padrao=padrao)
    return canvas, campos


def compor_linha(
    itens: list[_Item], model: LabelModel, padrao: _PadraoLote | None = None
) -> tuple[Image.Image, list[_CampoNativo]]:
    """Imagem de UMA linha da bobina (ate ``model.colunas`` etiquetas) pronta
    pra impressao: canvas SEM texto nativo pintado + campos ZPL nativos de
    todas as colunas, em coordenadas absolutas da linha. ``padrao``: ver
    ``_colocar_etiqueta``."""
    canvas = Image.new("1", (model.linha_largura_dots, model.altura_dots), 1)
    campos: list[_CampoNativo] = []
    for col, item in enumerate(itens[: model.colunas]):
        campos.extend(
            _colocar_etiqueta(canvas, model.x0_coluna(col), item, model, bake_texto_nativo=False, padrao=padrao)
        )
    return canvas, campos


def imagem_para_gfa(img: Image.Image) -> str:
    """Serializa uma imagem 1-bit como campo grafico ZPL ``^GFA`` (hex ASCII).

    ZPL: bit 1 = tinta (preto). PIL "1".tobytes(): bit 1 = branco (255).
    Por isso invertemos os bytes. A largura deve ser multipla de 8 para os
    bits de padding nao virarem uma faixa preta na borda."""
    bw = img.convert("1")
    w, h = bw.size
    row_bytes = (w + 7) // 8
    raw = bw.tobytes()
    invertido = bytes(b ^ 0xFF for b in raw)
    total = len(invertido)
    return f"^GFA,{total},{total},{row_bytes},{invertido.hex().upper()}"


def _escapar_texto_zpl(texto: str) -> str:
    """``^`` e ``~`` sao prefixos de comando no ZPL -- se aparecerem dentro de
    um ``^FD`` cortam o campo no meio. SKU/Seller SKU sao alfanumericos (nunca
    vistos com esses caracteres), mas isso e defensivo contra dado inesperado."""
    return texto.replace("^", "").replace("~", "")


def gerar_zpl(
    linhas: list[tuple[Image.Image, list[_CampoNativo]]], model: LabelModel, lote_id: str = "LOTE"
) -> str:
    """Monta o ZPL final: 1 bloco ^XA por (imagem, campos nativos).

    ``^PW``/``^LL`` saem do tamanho REAL de cada imagem (nao de constantes do
    modelo), para servir tanto a uma linha cheia da bobina quanto a uma unica
    etiqueta (preview/interpretacao). Para linhas cheias o tamanho coincide com
    ``linha_largura_dots`` x ``altura_dots`` — comportamento inalterado.

    Cada campo nativo (SKU sempre; Seller SKU quando mapeado no catalogo) vira
    um bloco ``^FO x,y ^A0N,altura,largura ^FD texto ^FS`` -- texto impresso
    pela fonte escalavel da propria impressora, nitidez real (ver ClickUp
    86ajk2mc2). O resto (QR, descricao, Seller SKU sem catalogo) continua
    como bitmap ``^GFA``.
    """
    blocos: list[str] = []
    for img, campos in linhas:
        bw = img.convert("1")
        campos_zpl = "".join(
            f"^FO{c.x},{c.y}^A0N,{c.altura},{c.largura}^FD{_escapar_texto_zpl(c.texto)}^FS\n" for c in campos
        )
        blocos.append(
            "^XA\n"
            "^CI28\n"
            "^LH0,0\n"
            f"^PW{bw.width}\n"
            f"^LL{bw.height}\n"
            f"^FO0,0{imagem_para_gfa(bw)}^FS\n"
            f"{campos_zpl}"
            "^PQ1,0,0,N\n"
            "^XZ"
        )
    log.info("Lote %s composto: %d blocos", lote_id, len(linhas))
    return "\n".join(blocos)


def gerar_zpl_preview_etiqueta(etiqueta, model: LabelModel, padrao: _PadraoLote | None = None) -> str | None:
    """ZPL composto de UMA etiqueta — o MESMO conteudo do preview e da impressao.

    Serve para interpretar via Node (botao "Interpretar ZPL") conferindo o que
    sera de fato impresso, em vez do ``zpl_raw`` bruto (a folha 10x15 inteira da
    Shopee, que renderizada na etiqueta pequena sairia cortada/duplicada).
    Retorna None se a etiqueta nao tem sticker (sem QR para compor).

    ``padrao``: passe o resultado de ``calibrar_padrao_lote`` (calibrado com
    TODAS as etiquetas do lote atual) pra esse preview mostrar o MESMO
    tamanho padronizado que vai sair na impressao real, em vez do tamanho
    otimizado so pra essa etiqueta isolada."""
    item = _item_etiqueta(etiqueta)
    if item is None:
        return None
    return gerar_zpl([compor_etiqueta_zpl(item, model, padrao=padrao)], model)


def _item_etiqueta(etiqueta) -> _Item | None:
    """Monta o ``_Item`` (QR + Seller SKU + descricao recortados + SKU) de uma
    EtiquetaZPL.

    Retorna None se a etiqueta nao tem sticker (ex.: placeholder SEM-QR),
    pois sem o QR nao da para compor a etiqueta nova.
    """
    folha = etiqueta.metadados.get("imagem_folha")
    st = etiqueta.metadados.get("sticker")
    descricao_texto = (getattr(etiqueta, "descricao", "") or "").strip()
    if folha is None or st is None:
        # Fonte sem bitmap (ex.: PDF): tudo vem de TEXTO + QR regenerado do sku
        # (``_qr_nitido`` recodifica o dado; o ``qr`` placeholder so seria usado
        # se a regeneracao falhasse). Sem sku nao da pra compor (nem QR).
        sku = (getattr(etiqueta, "sku", "") or "").strip()
        if not sku or sku.startswith("SEM-"):
            return None
        return _Item(
            qr=Image.new("1", (1, 1), 1),
            seller_img=None,
            descricao=None,
            sku=sku,
            seller_sku=(getattr(etiqueta, "seller_sku", "") or ""),
            descricao_texto=descricao_texto,
        )
    return _Item(
        qr=grf_decoder.crop_qr(folha, st),
        seller_img=grf_decoder.crop_seller_sku(folha, st),
        # Com texto de descricao (fonte PDF) o render usa o nativo -> nao precisa
        # do recorte de bitmap. Sem texto, mantem o recorte (comportamento legado).
        descricao=None if descricao_texto else grf_decoder.crop_descricao(folha, st),
        sku=(getattr(etiqueta, "sku", "") or ""),
        seller_sku=(getattr(etiqueta, "seller_sku", "") or ""),
        descricao_texto=descricao_texto,
    )


def preview_etiqueta(etiqueta, model: LabelModel) -> Image.Image | None:
    """Imagem da etiqueta composta para UMA EtiquetaZPL (preview na UI)."""
    item = _item_etiqueta(etiqueta)
    if item is None:
        return None
    return compor_etiqueta(item, model)


def calibrar_padrao_lote(etiquetas: list, model: LabelModel) -> _PadraoLote:
    """Calibra o tamanho PADRAO do SKU/Seller SKU a partir de uma lista de
    ``EtiquetaZPL`` brutas (todo o lote carregado) -- usar ANTES de mostrar o
    preview de uma etiqueta especifica (``gerar_zpl_preview_etiqueta``,
    ``compor_etiqueta_zpl``), garantindo que o preview mostre o MESMO
    tamanho que vai sair impresso de verdade (``gerar_zpl_de_etiquetas`` usa
    o mesmo calculo internamente)."""
    itens = [i for i in (_item_etiqueta(et) for et in etiquetas) if i is not None]
    return _calibrar_padrao_lote(itens, model)


def gerar_zpl_de_etiquetas(etiquetas: list, model: LabelModel, lote_id: str = "LOTE") -> tuple[str, int, int]:
    """Compoe o lote inteiro a partir das EtiquetaZPL parseadas.

    Agrupa em linhas de ``model.colunas`` na ordem do arquivo. Retorna
    (zpl, qtd_compostas, qtd_ignoradas) — ignoradas = stickers sem QR.
    """
    itens: list[_Item] = []
    ignoradas = 0
    for et in etiquetas:
        item = _item_etiqueta(et)
        if item is None:
            ignoradas += 1
            continue
        itens.append(item)

    # Calibra o tamanho PADRAO uma vez pra todo o lote (pior caso entre todos
    # os itens) -> todas as etiquetas saem com a mesma "fonte"/escala, em vez
    # de cada uma otimizar seu proprio tamanho (ClickUp 86ajk2mc2).
    padrao = _calibrar_padrao_lote(itens, model)
    linhas: list[tuple[Image.Image, list[_CampoNativo]]] = []
    for i in range(0, len(itens), model.colunas):
        linhas.append(compor_linha(itens[i : i + model.colunas], model, padrao=padrao))
    return gerar_zpl(linhas, model, lote_id=lote_id), len(itens), ignoradas
