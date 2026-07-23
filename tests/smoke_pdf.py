"""Smoke test: leitura da camada textual do PDF de etiquetas Shopee Full.

Uso:
    python tests/smoke_pdf.py <arquivo.pdf>

Sem argumento, tenta ~/Downloads/Print_Barcode_20260716033014_TESTE.pdf.
Valida que `pdf_reader` extrai SKU / Seller SKU / descricao de cada pagina SEM
OCR, e imprime os campos + eventuais problemas de validacao por etiqueta.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.core import pdf_reader  # noqa: E402

_PADRAO = Path.home() / "Downloads" / "Print_Barcode_20260716033014_TESTE.pdf"


def main() -> int:
    pdf = Path(sys.argv[1]).expanduser() if len(sys.argv) > 1 else _PADRAO
    if not pdf.exists():
        print(f"FALHA: PDF nao encontrado: {pdf}")
        print("Passe o caminho de um PDF real da Shopee Full como argumento.")
        return 1

    print(f"Camada textual presente: {pdf_reader.tem_camada_textual(pdf)}")
    etiquetas = pdf_reader.ler_etiquetas(pdf)
    print(f"Paginas lidas: {len(etiquetas)}\n")

    com_problema = 0
    sellers = set()
    for et in etiquetas:
        problemas = et.validar()
        if problemas:
            com_problema += 1
        sellers.add(et.seller_sku)
        marca = "  " if not problemas else "!!"
        print(f"{marca} p{et.pagina:02d} | sku={et.sku} | seller={et.seller_sku}")
        print(f"       desc={et.descricao!r}")
        if problemas:
            print(f"       PROBLEMAS: {', '.join(problemas)}")

    print(f"\nResumo: {len(etiquetas)} etiquetas, {len(sellers)} seller SKUs distintos, "
          f"{com_problema} com problema de validacao.")
    return 0 if com_problema == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
