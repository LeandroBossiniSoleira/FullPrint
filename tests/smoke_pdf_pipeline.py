"""Smoke test Etapa 2: PDF -> EtiquetaZPL (ShopeePDFParser) -> ZPL composto.

Prova o caminho de ponta a ponta pela API real do parser, headless:
- ShopeePDFParser.parse_file(pdf) -> lista de EtiquetaZPL
- gerar_zpl_de_etiquetas -> ZPL final (^GFA do QR + ^A0/^FD nativos)
- confere: nenhuma etiqueta ignorada, QR presente, descricao/seller/sku nos ^FD.

Uso: python tests/smoke_pdf_pipeline.py [arquivo.pdf]
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.core import label_renderer as lr  # noqa: E402
from src.core.label_models import LabelModel  # noqa: E402
from src.core.parser import ShopeePDFParser  # noqa: E402

_PADRAO = Path.home() / "Downloads" / "Print_Barcode_20260716033014_TESTE.pdf"


def main() -> int:
    pdf = Path(sys.argv[1]).expanduser() if len(sys.argv) > 1 else _PADRAO
    if not pdf.exists():
        print(f"FALHA: PDF nao encontrado: {pdf}")
        return 1

    etiquetas = ShopeePDFParser().parse_file(pdf)
    model = LabelModel(id="t", nome="t")  # 50x25, 2 colunas
    zpl, qtd, ignoradas = lr.gerar_zpl_de_etiquetas(etiquetas, model, lote_id="SMOKE")

    n_xa = zpl.count("^XA")
    n_gfa = zpl.count("^GFA")   # >=1 por linha (QR + descricao/qr no bitmap)
    n_fd = zpl.count("^FD")     # campos nativos (seller/sku/descricao)

    # Todas as descricoes/sellers/skus distintos devem aparecer nos ^FD.
    esperados = set()
    for et in etiquetas:
        esperados.update(et.descricao.split())
        esperados.add(et.seller_sku)
        esperados.add(et.sku)
    faltando = sorted(p for p in esperados if p and p not in zpl)

    ok = (
        qtd == len(etiquetas)
        and ignoradas == 0
        and n_xa == (len(etiquetas) + model.colunas - 1) // model.colunas
        and n_gfa >= n_xa
        and n_fd > 0
        and not faltando
    )

    print(f"Etiquetas: {qtd} compostas, {ignoradas} ignoradas")
    print(f"Blocos ^XA (linhas da bobina): {n_xa}  |  ^GFA: {n_gfa}  |  ^FD nativos: {n_fd}")
    if faltando:
        print(f"!! Conteudo ausente no ZPL: {faltando}")
    print("RESULTADO:", "OK" if ok else "FALHA")
    return 0 if ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
