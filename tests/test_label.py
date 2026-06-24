"""Testes do modelo de etiqueta configuravel e do renderizador composto."""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from PIL import Image  # noqa: E402

from src.core import label_renderer as R  # noqa: E402
from src.core.grf_decoder import StickerInfo  # noqa: E402
from src.core.label_models import (  # noqa: E402
    LabelModel,
    LabelModelStore,
    mm_para_dots,
)
from src.core.parser import EtiquetaZPL  # noqa: E402


class TestLabelModel(unittest.TestCase):
    def test_geometria_em_dots(self):
        m = LabelModel(
            id="x", nome="x", largura_mm=50, altura_mm=25, colunas=2,
            margem_esq_mm=1, margem_dir_mm=1, gap_colunas_mm=3, dpi=203,
        )
        self.assertEqual(m.altura_dots, mm_para_dots(25))          # 200
        self.assertEqual(m.largura_dots, mm_para_dots(50))         # 400
        self.assertEqual(m.x0_coluna(0), mm_para_dots(1))          # 8
        self.assertEqual(m.x0_coluna(1), 8 + 400 + mm_para_dots(3))  # 432
        self.assertEqual(m.linha_largura_dots % 8, 0)              # multiplo de 8 p/ ^GFA


class TestLabelModelStore(unittest.TestCase):
    def test_seed_e_persistencia(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "label_models.json"
            store = LabelModelStore(path)
            self.assertTrue(path.exists())  # seed gravado
            self.assertIsNotNone(store.get("shopee_10x15"))
            self.assertEqual(store.ativo().modo, "composto")  # 50x25 ativo por padrao

            # Recarrega de disco e mantem estado
            store.set_ativo("shopee_10x15")
            store2 = LabelModelStore(path)
            self.assertEqual(store2.ativo().id, "shopee_10x15")

    def test_pass_through_nao_removivel(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = LabelModelStore(Path(tmp) / "m.json")
            self.assertFalse(store.remover("shopee_10x15"))


def _decodificar_gfa(gfa: str) -> tuple[Image.Image, int]:
    """Inverte imagem_para_gfa: ^GFA,total,total,rowbytes,HEX -> imagem 1-bit."""
    _total, _total2, rowbytes, hexstr = gfa[len("^GFA,"):].split(",", 3)
    rowbytes = int(rowbytes)
    invertido = bytes.fromhex(hexstr)
    raw = bytes(b ^ 0xFF for b in invertido)  # desfaz a inversao
    w = rowbytes * 8
    h = len(raw) // rowbytes
    return Image.frombytes("1", (w, h), raw), rowbytes


class TestGFA(unittest.TestCase):
    def test_round_trip(self):
        # Imagem com largura multipla de 8 (round-trip exato).
        img = Image.new("1", (24, 10), 1)
        for x in range(0, 24, 2):
            img.putpixel((x, 5), 0)  # alguns pixels pretos
        gfa = R.imagem_para_gfa(img)
        self.assertTrue(gfa.startswith("^GFA,"))
        recuperada, rowbytes = _decodificar_gfa(gfa)
        self.assertEqual(rowbytes, 3)  # 24/8
        self.assertEqual(recuperada.size, img.size)
        # tobytes() normaliza a representacao (1 vs 255) -> compara os bits.
        self.assertEqual(recuperada.tobytes(), img.tobytes())


def _etiqueta_sintetica(sku: str, com_qr: bool = True, seller_sku: str = "") -> EtiquetaZPL:
    folha = Image.new("1", (816, 1218), 1)
    md: dict = {"imagem_folha": folha, "grf_indice": 1}
    if com_qr:
        md["sticker"] = StickerInfo(sku=sku, qr_left=180, qr_top=24, qr_width=172, qr_height=172)
    return EtiquetaZPL(sku=sku, zpl_raw="", indice=1, seller_sku=seller_sku, metadados=md)


class TestGerarZpl(unittest.TestCase):
    def setUp(self):
        self.model = LabelModel(id="x", nome="x", colunas=2)

    def test_agrupa_em_linhas_de_2(self):
        etiquetas = [_etiqueta_sintetica(f"SKU{i}") for i in range(5)]  # 5 stickers
        zpl, n, ign = R.gerar_zpl_de_etiquetas(etiquetas, self.model, "T")
        self.assertEqual(n, 5)
        self.assertEqual(ign, 0)
        self.assertEqual(zpl.count("^XA"), 3)   # ceil(5/2) linhas
        self.assertIn("^GFA,", zpl)
        self.assertIn(f"^PW{self.model.linha_largura_dots}", zpl)

    def test_ignora_sem_qr(self):
        etiquetas = [_etiqueta_sintetica("A"), _etiqueta_sintetica("SEM-QR", com_qr=False)]
        zpl, n, ign = R.gerar_zpl_de_etiquetas(etiquetas, self.model, "T")
        self.assertEqual(n, 1)
        self.assertEqual(ign, 1)
        self.assertEqual(zpl.count("^XA"), 1)

    def test_preview_etiqueta_dimensao(self):
        et = _etiqueta_sintetica("A")
        img = R.preview_etiqueta(et, self.model)
        self.assertEqual(img.size, (self.model.largura_dots, self.model.altura_dots))

    def test_seller_sku_do_catalogo_vira_texto_nativo(self):
        # Folha sintetica e toda branca -> o crop do bitmap nao tem tinta. Com o
        # Seller SKU do catalogo, o renderer desenha texto nativo -> ha tinta.
        item = R._item_etiqueta(_etiqueta_sintetica("123", seller_sku="ABC-XYZ-9"))
        self.assertEqual(item.seller_sku, "ABC-XYZ-9")  # texto propagado ao renderer
        com = R.preview_etiqueta(_etiqueta_sintetica("123", seller_sku="ABC-XYZ-9"), self.model)
        sem = R.preview_etiqueta(_etiqueta_sintetica("123"), self.model)
        # 0 = preto. Mais pixels pretos com o texto nativo desenhado.
        tinta = lambda im: sum(im.convert("L").histogram()[:128])
        self.assertGreater(tinta(com), tinta(sem))


class TestQrNitido(unittest.TestCase):
    def test_modulos_inteiros_e_cabe_no_espaco(self):
        lado = 168  # 21mm @ 203dpi
        qr = R._qr_nitido("BR2406230012345", lado)
        self.assertIsNotNone(qr)
        w, h = qr.size
        self.assertTrue(w <= lado and h <= lado)   # nunca estoura o espaco
        self.assertEqual(w, h)                      # quadrado
        # Bilevel (modo "1"): sem cinza de reescala -> modulos nitidos.
        self.assertEqual(qr.mode, "1")

    def test_dado_vazio_ou_denso_demais_retorna_none(self):
        self.assertIsNone(R._qr_nitido("", 168))
        self.assertIsNone(R._qr_nitido("X" * 50, 8))  # QR nao cabe nem com 1 dot/modulo

    def test_qr_regenerado_decodifica_no_mesmo_dado(self):
        try:
            from pyzbar import pyzbar
        except Exception:  # noqa: BLE001 (libzbar pode faltar no CI)
            self.skipTest("pyzbar/libzbar indisponivel")
        dado = "BR2406230012345"
        qr = R._qr_nitido(dado, 200)
        ampliado = qr.convert("L").resize((qr.width * 3, qr.height * 3), Image.NEAREST)
        lido = [d.data.decode() for d in pyzbar.decode(ampliado)]
        self.assertIn(dado, lido)


if __name__ == "__main__":
    unittest.main()
