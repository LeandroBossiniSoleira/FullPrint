"""Re-monta etiquetas no layout da bobina do usuario (modo ``composto``).

Estrategia: compor cada LINHA da bobina (todas as colunas) como UM bitmap, do
tamanho exato da midia, e enviar 1 bloco ZPL (``^GFA``) por linha. Assim cada
``^XA`` tem a altura de uma etiqueta -> a impressora sincroniza no gap a cada
linha (imprime todas) e o conteudo cai alinhado a etiqueta.

Legibilidade (v0.3.2): o SKU Shopee — que temos como TEXTO confiavel (lido do
QR) — e re-escrito como **texto nativo** com fonte TrueType na resolucao da
impressora (203 dpi), nitido em qualquer tamanho. O **Seller SKU** e a
**descricao** so existem rasterizados no bitmap da Shopee (sem OCR confiavel),
entao sao **recortados do bitmap** (``grf_decoder.crop_seller_sku`` /
``crop_descricao``) com downscale limpo (sem dithering). Assim o Seller SKU
aparece SEMPRE (mesmo sem mapeamento no catalogo).
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

# Espacamentos internos da etiqueta (dots). Pequenos e fixos: o que importa
# para o alinhamento na bobina sao as margens/vaos do modelo (configuraveis).
PAD = 4
GAP_QR_TEXTO = 8
GAP_LINHA = 2  # espaco vertical entre as linhas de texto

# Fracao da altura util da etiqueta reservada a cada bloco. Seller SKU e o codigo
# de coleta mais importante -> maior fatia; SKU Shopee (texto nativo) menor; o
# restante vai para a descricao (bitmap).
FRAC_SELLER = 0.38
FRAC_SKU = 0.22

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


def _render_texto(texto: str, box_w: int, box_h: int, bold: bool = False) -> Image.Image | None:
    """Desenha ``texto`` (preto/branco, 1-bit) ajustando a fonte para caber em
    (``box_w`` x ``box_h``). Retorna a imagem aparada ou ``None`` se vazio."""
    texto = (texto or "").strip()
    if not texto or box_w <= 0 or box_h <= 0:
        return None

    # Maior tamanho de fonte que cabe na caixa (busca binaria, fontes cacheadas).
    lo, hi, melhor = 6, max(6, box_h), 6
    while lo <= hi:
        mid = (lo + hi) // 2
        l, t, r, b = _fonte(mid, bold).getbbox(texto)
        if (r - l) <= box_w and (b - t) <= box_h:
            melhor, lo = mid, mid + 1
        else:
            hi = mid - 1
    fonte = _fonte(melhor, bold)

    l, t, r, b = fonte.getbbox(texto)
    w, h = max(1, r - l), max(1, b - t)
    img = Image.new("L", (w, h), 255)
    ImageDraw.Draw(img).text((-l, -t), texto, font=fonte, fill=0)
    # Threshold sem dithering: preserva os tracos finos da fonte.
    return img.point(lambda p: 0 if p < 128 else 255).convert("1")


def _resize_bitmap(img: Image.Image, box_w: int, box_h: int) -> Image.Image | None:
    """Encaixa um recorte do bitmap (Seller SKU ou descricao) na caixa, com
    downscale limpo: aproveita toda a caixa (sem teto de escala), autocontraste
    e threshold SEM dithering (Floyd-Steinberg picotava o texto). LANCZOS
    suaviza as bordas antes do threshold.
    """
    src = _trim_tinta(img).convert("L")
    w, h = src.size
    if w == 0 or h == 0 or box_w <= 0 or box_h <= 0:
        return None
    escala = min(box_w / w, box_h / h)
    novo = (max(1, round(w * escala)), max(1, round(h * escala)))
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


def _colocar_etiqueta(canvas: Image.Image, x0: int, item: _Item, model: LabelModel) -> None:
    """Compoe UMA etiqueta sobre o canvas: QR a esquerda; a direita (de cima
    para baixo) Seller SKU (texto nativo do catalogo, ou bitmap se nao mapeado)
    + SKU Shopee (texto nativo) + descricao (bitmap)."""
    altura = model.altura_dots
    topo = model.dots(model.margem_topo_mm)

    # --- QR (esquerda, centralizado verticalmente) ---
    qr_dots = min(model.dots(model.qr_mm), altura - topo - 2 * PAD)
    # Preferencia: regenerar o QR do dado decodificado (modulos em dots inteiros
    # -> nitido). Fallback: recorte do bitmap original reescalado (NEAREST).
    qr = _qr_nitido(item.sku, qr_dots)
    if qr is None:
        qr = item.qr.convert("L").resize((qr_dots, qr_dots), Image.NEAREST).convert("1")
    qr_w, qr_h = qr.size
    # Centraliza o QR (que pode ser <= qr_dots) na area reservada.
    qr_x = x0 + PAD + max(0, (qr_dots - qr_w) // 2)
    qr_y = topo + max(0, (altura - topo - qr_h) // 2)
    canvas.paste(qr, (qr_x, qr_y))

    # --- Coluna de conteudo a direita do QR ---
    box_x = x0 + PAD + qr_dots + GAP_QR_TEXTO
    box_w = model.largura_dots - PAD - qr_dots - GAP_QR_TEXTO - PAD
    box_y = topo + PAD
    box_h = altura - topo - 2 * PAD
    if box_w <= 0 or box_h <= 0:
        return

    y = box_y
    # Seller SKU (codigo de coleta mais importante), em destaque.
    # Preferencia: texto do catalogo manual -> fonte nativa, nitido e preenchendo
    # a caixa. Fallback: recorte do bitmap (texto ~6px da folha, so legivel ampliado).
    if item.seller_sku.strip() or item.seller_img is not None:
        h_seller = round(box_h * FRAC_SELLER)
        if item.seller_sku.strip():
            img = _render_texto(item.seller_sku, box_w, h_seller, bold=True)
        else:
            img = _resize_bitmap(item.seller_img, box_w, h_seller)
        if img is not None:
            canvas.paste(img, (box_x, y))
        y += h_seller + GAP_LINHA

    # SKU Shopee (texto nativo, nitido).
    if item.sku.strip():
        h_sku = round(box_h * FRAC_SKU)
        img = _render_texto(f"SKU {item.sku}", box_w, h_sku, bold=False)
        if img is not None:
            canvas.paste(img, (box_x, y))
        y += h_sku + GAP_LINHA

    # Descricao (bitmap) ocupa todo o espaco vertical restante.
    desc_h = box_y + box_h - y
    if item.descricao is not None and desc_h > 4:
        desc = _resize_bitmap(item.descricao, box_w, desc_h)
        if desc is not None:
            canvas.paste(desc, (box_x, y))


def compor_etiqueta(item: _Item, model: LabelModel) -> Image.Image:
    """Imagem de UMA etiqueta isolada (para o preview)."""
    canvas = Image.new("1", (model.largura_dots, model.altura_dots), 1)
    _colocar_etiqueta(canvas, 0, item, model)
    return canvas


def compor_linha(itens: list[_Item], model: LabelModel) -> Image.Image:
    """Imagem de UMA linha da bobina (ate ``model.colunas`` etiquetas)."""
    canvas = Image.new("1", (model.linha_largura_dots, model.altura_dots), 1)
    for col, item in enumerate(itens[: model.colunas]):
        _colocar_etiqueta(canvas, model.x0_coluna(col), item, model)
    return canvas


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


def gerar_zpl(linhas: list[Image.Image], model: LabelModel, lote_id: str = "LOTE") -> str:
    """Monta o ZPL final: 1 bloco ^XA por linha da bobina."""
    largura = model.linha_largura_dots
    altura = model.altura_dots
    blocos: list[str] = []
    for img in linhas:
        blocos.append(
            "^XA\n"
            "^CI28\n"
            "^LH0,0\n"
            f"^PW{largura}\n"
            f"^LL{altura}\n"
            f"^FO0,0{imagem_para_gfa(img)}^FS\n"
            "^PQ1,0,0,N\n"
            "^XZ"
        )
    log.info("Lote %s composto: %d linhas (%dx%d dots cada)", lote_id, len(linhas), largura, altura)
    return "\n".join(blocos)


def _item_etiqueta(etiqueta) -> _Item | None:
    """Monta o ``_Item`` (QR + Seller SKU + descricao recortados + SKU) de uma
    EtiquetaZPL.

    Retorna None se a etiqueta nao tem sticker (ex.: placeholder SEM-QR),
    pois sem o QR nao da para compor a etiqueta nova.
    """
    folha = etiqueta.metadados.get("imagem_folha")
    st = etiqueta.metadados.get("sticker")
    if folha is None or st is None:
        return None
    return _Item(
        qr=grf_decoder.crop_qr(folha, st),
        seller_img=grf_decoder.crop_seller_sku(folha, st),
        descricao=grf_decoder.crop_descricao(folha, st),
        sku=(getattr(etiqueta, "sku", "") or ""),
        seller_sku=(getattr(etiqueta, "seller_sku", "") or ""),
    )


def preview_etiqueta(etiqueta, model: LabelModel) -> Image.Image | None:
    """Imagem da etiqueta composta para UMA EtiquetaZPL (preview na UI)."""
    item = _item_etiqueta(etiqueta)
    if item is None:
        return None
    return compor_etiqueta(item, model)


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

    linhas: list[Image.Image] = []
    for i in range(0, len(itens), model.colunas):
        linhas.append(compor_linha(itens[i : i + model.colunas], model))
    return gerar_zpl(linhas, model, lote_id=lote_id), len(itens), ignoradas
