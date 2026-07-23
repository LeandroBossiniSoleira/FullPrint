"""Testes essenciais: parser, agrupador, pass-through e decoder GRF."""
from __future__ import annotations

import base64
import sys
import tempfile
import unittest
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.core import grf_decoder  # noqa: E402
from src.core.agrupador import EtiquetaAgrupador  # noqa: E402
from src.core.grf_decoder import StickerInfo  # noqa: E402
from src.core.parser import ShopeeZPLParser  # noqa: E402
from src.services.printer import ZebraPrinterService  # noqa: E402


CONTEUDO_EXEMPLO = """
^XA
^CI28
^FO50,50^A0N,40,40^FDSKU: ABC-123^FS
^FO50,120^A0N,30,30^FDCamiseta Algodao Azul M^FS
^BCN,80,Y,N,N^FDABC-123^FS
^XZ
^XA
^FO50,50^A0N,40,40^FDSKU: ABC-123^FS
^FO50,120^A0N,30,30^FDCamiseta Algodao Azul M^FS
^XZ
^XA
^FO50,50^A0N,40,40^FDSKU: XYZ-999^FS
^FO50,120^A0N,30,30^FDCalca Jeans Preta 42^FS
^XZ
""".strip()


def _trio_grf_sintetico(row_bytes: int = 8, altura: int = 16) -> str:
    """Monta um trio ~DG + print + delete valido com bitmap em branco.

    O CRC do Z64 nao e validado pelo decoder, entao '0000' basta.
    """
    grf = bytes(row_bytes * altura)
    b64 = base64.b64encode(zlib.compress(grf)).decode("ascii")
    total = len(grf)
    return (
        f"~DGR:TST.GRF,{total},{row_bytes},:Z64:{b64}:0000\n"
        "^XA^MMT,Y^PON^MNY^FO0,0^XGR:TST.GRF,1,1^FS^PQ1,0,0,N^XZ\n"
        "^XA^IDR:TST.GRF^FS^XZ"
    )


class TestShopeeParserTexto(unittest.TestCase):
    def setUp(self) -> None:
        self.parser = ShopeeZPLParser()

    def test_extrai_tres_blocos(self):
        etiquetas = self.parser.parse_content(CONTEUDO_EXEMPLO)
        self.assertEqual(len(etiquetas), 3)

    def test_sku_extraido(self):
        etiquetas = self.parser.parse_content(CONTEUDO_EXEMPLO)
        self.assertEqual(etiquetas[0].sku, "ABC-123")
        self.assertEqual(etiquetas[2].sku, "XYZ-999")

    def test_zpl_raw_preservado(self):
        etiquetas = self.parser.parse_content(CONTEUDO_EXEMPLO)
        for et in etiquetas:
            self.assertTrue(et.zpl_raw.startswith("^XA"))
            self.assertTrue(et.zpl_raw.endswith("^XZ"))

    def test_parse_bytes_equivale_a_parse_content(self):
        por_bytes = self.parser.parse_bytes(CONTEUDO_EXEMPLO.encode("utf-8"))
        por_str = self.parser.parse_content(CONTEUDO_EXEMPLO)
        self.assertEqual(
            [e.sku for e in por_bytes],
            [e.sku for e in por_str],
        )


class TestAgrupador(unittest.TestCase):
    def test_agrupa_por_sku(self):
        parser = ShopeeZPLParser()
        agrupador = EtiquetaAgrupador()
        etiquetas = parser.parse_content(CONTEUDO_EXEMPLO)
        grupos = agrupador.agrupar_por_sku(etiquetas)
        self.assertEqual(len(grupos), 2)
        self.assertEqual(grupos["ABC-123"].qtd, 2)
        self.assertEqual(grupos["XYZ-999"].qtd, 1)


class TestPassThroughPrinter(unittest.TestCase):
    """O teste do bug raiz: o que sai da 'impressora' deve ser identico,
    byte a byte, ao que entrou — sem decode, re-encode ou re-render."""

    def test_bytes_identicos_na_saida_dev(self):
        # \xff nao e UTF-8 valido: se houver decode/re-encode no caminho,
        # o byte muda e o teste falha.
        dados = _trio_grf_sintetico().encode("ascii") + b"\n\xff\x00binario"
        with tempfile.TemporaryDirectory() as tmp:
            printer = ZebraPrinterService(dev_mode=True, dev_output_dir=Path(tmp))
            printer.print_zpl(dados, printer_name="[DEV]", job_name="PASSTHROUGH")
            saidas = list(Path(tmp).glob("*.zpl"))
            self.assertEqual(len(saidas), 1)
            self.assertEqual(saidas[0].read_bytes(), dados)

    def test_caminho_str_legado_continua_funcionando(self):
        conteudo = "^XA^FDteste^FS^XZ"
        with tempfile.TemporaryDirectory() as tmp:
            printer = ZebraPrinterService(dev_mode=True, dev_output_dir=Path(tmp))
            printer.print_zpl(conteudo, printer_name="[DEV]", job_name="LEGADO")
            saidas = list(Path(tmp).glob("*.zpl"))
            self.assertEqual(saidas[0].read_text(encoding="utf-8"), conteudo)


class TestGRFSintetico(unittest.TestCase):
    def test_decoder_extrai_trio_verbatim(self):
        trio = _trio_grf_sintetico()
        folhas = grf_decoder.extrair_etiquetas(trio)
        self.assertEqual(len(folhas), 1)
        folha = folhas[0]
        self.assertEqual(folha.zpl_raw, trio)
        self.assertEqual(folha.largura, 64)
        self.assertEqual(folha.altura, 16)

    def test_parser_emite_placeholder_para_folha_sem_qr(self):
        # Bitmap em branco nao tem QR: o parser nao pode zerar a contagem,
        # senao o botao Imprimir trava com um arquivo perfeitamente valido.
        etiquetas = ShopeeZPLParser().parse_content(_trio_grf_sintetico())
        self.assertEqual(len(etiquetas), 1)
        self.assertEqual(etiquetas[0].sku, "SEM-QR-001")
        self.assertIsNotNone(etiquetas[0].metadados.get("imagem_folha"))


class TestCropSticker(unittest.TestCase):
    def _folha(self, largura: int = 816, altura: int = 1218):
        from PIL import Image

        return Image.new("1", (largura, altura), 1)

    def test_recorte_ancorado_no_qr(self):
        folha = self._folha()
        st = StickerInfo(sku="X", qr_left=180, qr_top=24, qr_width=172, qr_height=172)
        crop = grf_decoder.crop_sticker(folha, st)
        self.assertEqual(
            crop.size,
            (
                172 + 2 * grf_decoder.CROP_MARGEM_X,
                172 + grf_decoder.CROP_MARGEM_TOPO + grf_decoder.CROP_ALTURA_TEXTO,
            ),
        )

    def test_recorte_clampa_nas_bordas_da_folha(self):
        folha = self._folha()
        # QR colado no canto superior esquerdo: as margens nao podem
        # estourar os limites da imagem.
        st = StickerInfo(sku="X", qr_left=0, qr_top=0, qr_width=172, qr_height=172)
        crop = grf_decoder.crop_sticker(folha, st)
        self.assertLessEqual(crop.size[0], 172 + 2 * grf_decoder.CROP_MARGEM_X)
        self.assertGreater(crop.size[0], 172)
        # QR colado no fim da folha: recorte nao passa da borda inferior.
        st2 = StickerInfo(sku="X", qr_left=180, qr_top=1218 - 100, qr_width=172, qr_height=100)
        crop2 = grf_decoder.crop_sticker(folha, st2)
        self.assertLessEqual(crop2.size[1], 100 + grf_decoder.CROP_MARGEM_TOPO)

    def test_crop_seller_e_descricao_separam_as_linhas(self):
        # Geometria calibrada: abaixo do QR vem Seller SKU (~[+6:+11]), SKU
        # numerico (~[+14:+20]) e descricao (~[+21:+37]). crop_seller_sku pega
        # a linha inteira (label + valor -- a Shopee centraliza esse texto, um
        # offset fixo pra pular so o label corta o valor em SKUs mais curtos/
        # longos, ver ClickUp 86ajk2mc2); crop_descricao pula Seller+SKU e
        # pega a descricao.
        from PIL import ImageOps

        st = StickerInfo(sku="X", qr_left=180, qr_top=24, qr_width=172, qr_height=172)
        qb = st.qr_top + st.qr_height  # base do QR

        def tem_tinta(img):
            return ImageOps.invert(img.convert("L")).getbbox() is not None

        def folha_com_pixel(dy):
            f = self._folha()
            f.putpixel((200, qb + dy), 0)  # 1 pixel preto na linha dy abaixo do QR
            return f

        # Largura igual aos demais recortes; altura = faixa do Seller SKU.
        seller_dim = grf_decoder.crop_seller_sku(self._folha(), st)
        self.assertEqual(
            seller_dim.size,
            (172 + 2 * grf_decoder.CROP_MARGEM_X, grf_decoder.SELLER_ALTURA),
        )

        # +8 = linha Seller SKU -> entra no seller, fora da descricao.
        f = folha_com_pixel(8)
        self.assertTrue(tem_tinta(grf_decoder.crop_seller_sku(f, st)))
        self.assertFalse(tem_tinta(grf_decoder.crop_descricao(f, st)))

        # +16 = linha do SKU numerico (texto nativo) -> pulada por AMBOS.
        f = folha_com_pixel(16)
        self.assertFalse(tem_tinta(grf_decoder.crop_seller_sku(f, st)))
        self.assertFalse(tem_tinta(grf_decoder.crop_descricao(f, st)))

        # +30 = linha de descricao -> entra na descricao, fora do seller.
        f = folha_com_pixel(30)
        self.assertFalse(tem_tinta(grf_decoder.crop_seller_sku(f, st)))
        self.assertTrue(tem_tinta(grf_decoder.crop_descricao(f, st)))


if __name__ == "__main__":
    unittest.main()
