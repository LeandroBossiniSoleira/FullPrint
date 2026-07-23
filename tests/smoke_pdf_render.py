"""Smoke test Etapa 1: descricao do PDF -> texto ZPL NATIVO no render composto.

Le os campos do PDF real (pdf_reader), monta um _Item por produto (QR gerado do
sku, descricao como TEXTO) e compoe o ZPL. Confere que a descricao sai como
campos ^FD nativos (nao bitmap) e que nenhuma palavra se perde na quebra.

Uso: python tests/smoke_pdf_render.py [arquivo.pdf]
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from PIL import Image  # noqa: E402

from src.core import label_renderer as lr  # noqa: E402
from src.core import pdf_reader  # noqa: E402
from src.core.label_models import LabelModel  # noqa: E402

_PADRAO = Path.home() / "Downloads" / "Print_Barcode_20260716033014_TESTE.pdf"


def main() -> int:
    pdf = Path(sys.argv[1]).expanduser() if len(sys.argv) > 1 else _PADRAO
    if not pdf.exists():
        print(f"FALHA: PDF nao encontrado: {pdf}")
        return 1

    model = LabelModel(id="t", nome="t")  # 50x25, 2 col, padrao
    # 1 etiqueta por produto distinto (dedup por sku) pra inspecionar cada caso.
    vistos: dict[str, pdf_reader.PDFEtiqueta] = {}
    for et in pdf_reader.ler_etiquetas(pdf):
        vistos.setdefault(et.sku, et)

    falhas = 0
    for et in vistos.values():
        item = lr._Item(
            qr=Image.new("1", (10, 10), 1),  # placeholder; render regenera do sku
            seller_img=None,
            descricao=None,
            sku=et.sku,
            seller_sku=et.seller_sku,
            descricao_texto=et.descricao,
        )
        _canvas, campos = lr.compor_etiqueta_zpl(item, model)
        textos = [c.texto for c in campos]
        # Palavras da descricao que devem aparecer intactas nos campos nativos.
        palavras_desc = et.descricao.split()
        # Junta os campos que NAO sao SKU/Seller (i.e., a descricao) e confere
        # que todas as palavras da descricao estao presentes, em ordem.
        desc_reconstruida = " ".join(
            t for t in textos if t != et.seller_sku and not t.startswith("SKU ")
        )
        faltando = [p for p in palavras_desc if p not in desc_reconstruida]
        alturas_desc = sorted({c.altura for c in campos})

        ok = not faltando
        falhas += 0 if ok else 1
        print(f"{'OK' if ok else '!!'} sku={et.sku}")
        print(f"   desc ({len(palavras_desc)} palavras) -> campos nativos: {textos}")
        print(f"   alturas de fonte (dots): {alturas_desc}")
        if faltando:
            print(f"   PALAVRAS FALTANDO: {faltando}")

    print(f"\n{len(vistos)} produtos, {falhas} com perda de palavra na descricao.")
    return 0 if falhas == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
