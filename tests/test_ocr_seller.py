"""Testes do OCR opcional do Seller SKU (parte pura, sem exigir o binario)."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.core import ocr_seller as O  # noqa: E402


class TestLimpar(unittest.TestCase):
    def test_normaliza_e_remove_prefixo_sku(self):
        self.assertEqual(O._limpar("  k-saore-tf  "), "K-SAORE-TF")
        self.assertEqual(O._limpar("SKU: XYZ-9"), "XYZ-9")
        self.assertEqual(O._limpar("SKU ABC-1"), "ABC-1")
        self.assertEqual(O._limpar("ABC-123"), "ABC-123")  # sem prefixo: intacto
        self.assertEqual(O._limpar(""), "")
        self.assertEqual(O._limpar("  a  b  "), "A B")  # colapsa espacos


class TestGuards(unittest.TestCase):
    def test_ocr_com_crop_none_devolve_vazio(self):
        # Independe do Tesseract: crop None curto-circuita antes do OCR.
        self.assertEqual(O.ocr_seller(None), "")

    def test_unavailable_reason_coerente_com_is_available(self):
        # Os dois devem concordar: disponivel <-> sem motivo de indisponibilidade.
        self.assertEqual(O.is_available(), O.unavailable_reason() is None)


if __name__ == "__main__":
    unittest.main()
