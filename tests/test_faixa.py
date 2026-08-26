"""Testes da faixa de impressao (retomar de um SKU / limitar a N etiquetas)."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.core import faixa_impressao as fx  # noqa: E402
from src.core.label_renderer import grupos_consecutivos  # noqa: E402
from src.core.parser import EtiquetaZPL  # noqa: E402


def _lote(*pares: tuple[str, int]) -> list[EtiquetaZPL]:
    """Lote sintetico: ("SKU", qtd) na ordem de chegada."""
    etiquetas: list[EtiquetaZPL] = []
    i = 0
    for sku, qtd in pares:
        for _ in range(qtd):
            i += 1
            etiquetas.append(
                EtiquetaZPL(
                    sku=sku,
                    zpl_raw=f"^XA^FD{sku}-{i}^FS^XZ",
                    indice=i,
                    seller_sku=f"SELLER-{sku}",
                    metadados={"fonte": "pdf"},
                )
            )
    return etiquetas


class TestMontarFila(unittest.TestCase):
    def test_grupos_consecutivos_com_indices(self):
        fila = fx.montar_fila(_lote(("A", 3), ("B", 2), ("C", 1)))
        self.assertEqual([(g.ordem, g.sku, g.qtd, g.inicio, g.fim) for g in fila],
                         [(1, "A", 3, 0, 3), (2, "B", 2, 3, 5), (3, "C", 1, 5, 6)])

    def test_sku_reaparecendo_vira_grupo_novo(self):
        # Mesma regra do renderer: nada e' reordenado, o SKU repetido ganha
        # grupo (e separadora) proprio.
        fila = fx.montar_fila(_lote(("A", 2), ("B", 1), ("A", 2)))
        self.assertEqual([g.ordem for g in fila], [1, 2, 3])
        self.assertEqual([g.sku for g in fila], ["A", "B", "A"])

    def test_mesma_quebra_do_renderer(self):
        etiquetas = _lote(("A", 2), ("B", 3), ("A", 1), ("C", 4))
        fila = fx.montar_fila(etiquetas)
        self.assertEqual([g.qtd for g in fila], [len(g) for g in grupos_consecutivos(etiquetas)])

    def test_lote_vazio(self):
        self.assertEqual(fx.montar_fila([]), [])


class TestAplicarFaixa(unittest.TestCase):
    def setUp(self):
        self.etiquetas = _lote(("A", 3), ("B", 2), ("C", 4))
        self.fila = fx.montar_fila(self.etiquetas)

    def test_sem_faixa_imprime_tudo(self):
        faixa = fx.Faixa()
        self.assertFalse(faixa.ativa)
        self.assertEqual(fx.aplicar(self.etiquetas, faixa, self.fila), self.etiquetas)

    def test_retomar_do_grupo_ate_o_fim(self):
        # Caso central: a bobina acabou no B, retoma de B.
        recorte = fx.aplicar(self.etiquetas, fx.Faixa(inicio=2), self.fila)
        self.assertEqual([et.sku for et in recorte], ["B", "B", "C", "C", "C", "C"])

    def test_parar_num_grupo(self):
        recorte = fx.aplicar(self.etiquetas, fx.Faixa(fim=2), self.fila)
        self.assertEqual([et.sku for et in recorte], ["A", "A", "A", "B", "B"])

    def test_intervalo_fechado(self):
        recorte = fx.aplicar(self.etiquetas, fx.Faixa(inicio=2, fim=2), self.fila)
        self.assertEqual([et.sku for et in recorte], ["B", "B"])

    def test_limite_corta_grupo_no_meio(self):
        # "cabe mais 3 etiquetas nesta bobina" a partir do grupo B.
        recorte = fx.aplicar(self.etiquetas, fx.Faixa(inicio=2, limite=3), self.fila)
        self.assertEqual([et.sku for et in recorte], ["B", "B", "C"])

    def test_limite_sozinho_conta_do_inicio(self):
        recorte = fx.aplicar(self.etiquetas, fx.Faixa(limite=4), self.fila)
        self.assertEqual([et.sku for et in recorte], ["A", "A", "A", "B"])

    def test_limite_maior_que_o_lote_nao_estoura(self):
        recorte = fx.aplicar(self.etiquetas, fx.Faixa(limite=999), self.fila)
        self.assertEqual(len(recorte), len(self.etiquetas))

    def test_limites_invertidos_ou_fora_do_range_sao_tolerados(self):
        # A UI nao deve travar por um valor torto: clamp, nunca excecao.
        self.assertEqual(
            [et.sku for et in fx.aplicar(self.etiquetas, fx.Faixa(inicio=3, fim=1), self.fila)],
            [et.sku for et in self.etiquetas],
        )
        self.assertEqual(
            len(fx.aplicar(self.etiquetas, fx.Faixa(inicio=99), self.fila)),
            4,  # clamp no ultimo grupo (C)
        )

    def test_preserva_as_instancias(self):
        recorte = fx.aplicar(self.etiquetas, fx.Faixa(inicio=2), self.fila)
        self.assertIs(recorte[0], self.etiquetas[3])

    def test_intervalo_bate_com_o_recorte(self):
        faixa = fx.Faixa(inicio=2, limite=3)
        alvo = fx.intervalo(self.etiquetas, faixa, self.fila)
        self.assertEqual(list(alvo), [3, 4, 5])
        self.assertEqual(len(alvo), len(fx.aplicar(self.etiquetas, faixa, self.fila)))


class TestResumo(unittest.TestCase):
    def setUp(self):
        self.etiquetas = _lote(("A", 3), ("B", 2), ("C", 4))
        self.fila = fx.montar_fila(self.etiquetas)

    def test_lote_completo(self):
        texto = fx.resumo(self.fila, fx.Faixa(), len(self.etiquetas))
        self.assertIn("Lote completo", texto)
        self.assertIn("9 etiquetas", texto)

    def test_faixa_ate_o_fim(self):
        texto = fx.resumo(self.fila, fx.Faixa(inicio=2), len(self.etiquetas))
        self.assertIn("fim da fila", texto)
        self.assertIn("6 de 9 etiquetas", texto)

    def test_faixa_com_limite(self):
        texto = fx.resumo(self.fila, fx.Faixa(inicio=2, limite=3), len(self.etiquetas))
        self.assertIn("max 3 etiquetas", texto)
        self.assertIn("3 de 9 etiquetas", texto)

    def test_sem_lote(self):
        self.assertIn("Nenhum lote", fx.resumo([], fx.Faixa(), 0))


class TestPassThrough(unittest.TestCase):
    def test_texto_puro_recorta_bloco_a_bloco(self):
        # 1 bloco ^XA..^XZ por etiqueta -> recorte exato, sem carona.
        etiquetas = _lote(("A", 2), ("B", 2))
        for et in etiquetas:
            et.metadados = {}  # sem grf_indice: identifica pelo indice do bloco
        fila = fx.montar_fila(etiquetas)
        recorte = fx.aplicar(etiquetas, fx.Faixa(inicio=2), fila)
        zpl, extras = fx.blocos_pass_through(etiquetas, recorte)
        self.assertEqual(extras, 0)
        self.assertEqual(zpl.count("^XA"), 2)
        self.assertIn("B-3", zpl)
        self.assertNotIn("A-1", zpl)

    def test_folha_grf_e_indivisivel_e_avisa_as_caronas(self):
        # Uma folha 10x15 (grf_indice=1) com 3 stickers, 2 na faixa: a folha
        # inteira sai e o 3o sticker vai de carona -> extras=1.
        folha = "^XA~DGR:F1^XZ"
        etiquetas = [
            EtiquetaZPL(sku="A", zpl_raw=folha, indice=1, metadados={"grf_indice": 1}),
            EtiquetaZPL(sku="A", zpl_raw=folha, indice=2, metadados={"grf_indice": 1}),
            EtiquetaZPL(sku="B", zpl_raw=folha, indice=3, metadados={"grf_indice": 1}),
        ]
        zpl, extras = fx.blocos_pass_through(etiquetas, etiquetas[:2])
        self.assertEqual(zpl.count("^XA"), 1)  # folha emitida uma unica vez
        self.assertEqual(extras, 1)

    def test_recorte_vazio(self):
        self.assertEqual(fx.blocos_pass_through(_lote(("A", 1)), []), ("", 0))


if __name__ == "__main__":
    unittest.main()
