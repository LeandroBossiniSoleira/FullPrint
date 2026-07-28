"""Ferramenta de calibracao: renderiza folhas GRF e os crops dos stickers.

Uso:
    python tests/render_grf.py [arquivo.txt]

Sem argumento, usa data/exemplo_shopee.txt. Salva em data/dev_output/:
  - folha_NN.png            (ate 3 folhas inteiras)
  - sticker_NN_MM_<sku>.png (crops via grf_decoder.crop_sticker)

Use com o TXT real da Shopee para conferir visualmente se a grade 2x5 do
crop_sticker esta recortando a celula certa de cada QR.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.core import grf_decoder  # noqa: E402

OUT_DIR = ROOT / "data" / "dev_output"
MAX_FOLHAS = 3


def render(path: Path) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    conteudo = path.read_bytes().decode("utf-8", errors="replace")
    folhas = grf_decoder.extrair_etiquetas(conteudo)
    print(f"[scan] {len(folhas)} folhas GRF em {path.name}")

    for folha in folhas[:MAX_FOLHAS]:
        out = OUT_DIR / f"folha_{folha.indice:02d}.png"
        folha.imagem.save(out)
        print(f"  folha #{folha.indice}: {folha.largura}x{folha.altura}, "
              f"{len(folha.stickers)} QRs -> {out}")
        for j, st in enumerate(folha.stickers, start=1):
            crop = grf_decoder.crop_sticker(folha.imagem, st)
            sku_slug = "".join(c if c.isalnum() else "_" for c in st.sku)[:24]
            out_st = OUT_DIR / f"sticker_{folha.indice:02d}_{j:02d}_{sku_slug}.png"
            crop.save(out_st)
            print(f"    sticker {j}: sku={st.sku} qr=({st.qr_left},{st.qr_top}) "
                  f"crop={crop.size} -> {out_st.name}")


def main() -> int:
    if len(sys.argv) > 1:
        alvo = Path(sys.argv[1]).expanduser()
    else:
        alvo = ROOT / "data" / "exemplo_shopee.txt"
    if not alvo.exists():
        print(f"FALHA: arquivo nao encontrado: {alvo}")
        return 1
    render(alvo)
    return 0


if __name__ == "__main__":
    sys.exit(main())
