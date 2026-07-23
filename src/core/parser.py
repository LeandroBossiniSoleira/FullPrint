"""Parser ZPL para arquivos Shopee.

Define a interface abstrata `MarketplaceParser` (Strategy Pattern) e a
implementação concreta `ShopeeZPLParser`. Novos marketplaces (Mercado Livre,
etc.) implementam a mesma interface na Fase 2.

O parse alimenta APENAS o preview (tabela + imagem). A impressão é
pass-through dos bytes originais do arquivo — nunca depende do parse.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from ..utils import zpl_utils
from ..utils.logger import get_logger
from . import grf_decoder, pdf_reader
from .sku_catalog import SKUCatalog

log = get_logger("parser")

ProgressCallback = Callable[[str], None]


@dataclass
class EtiquetaZPL:
    """Uma "etiqueta logica" — para GRF, equivale a 1 sticker (1 QR).

    Multiplos stickers do mesmo GRF compartilham `zpl_raw` e `grf_indice`
    via metadados.
    """
    sku: str                       # SKU numerico do Shopee (sempre vem do QR)
    zpl_raw: str
    indice: int = 0                # indice global do sticker
    seller_sku: str = ""           # opcional, vem do cache manual
    descricao: str = ""            # texto do produto (fonte PDF); vazio = usa bitmap
    metadados: dict = field(default_factory=dict)


class MarketplaceParser(ABC):
    """Interface base para parsers de marketplaces."""

    nome: str = "abstrato"

    @abstractmethod
    def parse_file(self, filepath: str | Path) -> list[EtiquetaZPL]:
        ...

    @abstractmethod
    def parse_content(self, conteudo: str) -> list[EtiquetaZPL]:
        ...


class ShopeeZPLParser(MarketplaceParser):
    nome = "Shopee"

    def __init__(
        self,
        encoding_primario: str = "utf-8",
        progress_callback: ProgressCallback | None = None,
        catalog: SKUCatalog | None = None,
    ) -> None:
        self.encoding_primario = encoding_primario
        self.progress_callback = progress_callback or (lambda _msg: None)
        self.catalog = catalog

    def parse_file(self, filepath: str | Path) -> list[EtiquetaZPL]:
        path = Path(filepath)
        if not path.exists():
            raise FileNotFoundError(f"Arquivo nao encontrado: {path}")

        etiquetas = self.parse_bytes(path.read_bytes())
        log.info("Parse concluido: %s -> %d etiquetas", path.name, len(etiquetas))
        return etiquetas

    def parse_bytes(self, dados: bytes) -> list[EtiquetaZPL]:
        """Parseia os bytes do arquivo (mesmo buffer usado na impressao)."""
        return self.parse_content(self._decodificar_com_fallback(dados))

    def parse_content(self, conteudo: str) -> list[EtiquetaZPL]:
        # Auto-detect: arquivos da Shopee Full em GRF (imagem rasterizada)
        # nao tem ^FD legivel; o SKU vem do QR decodificado do bitmap.
        if grf_decoder.has_grf_z64(conteudo):
            return self._parse_grf(conteudo)
        return self._parse_texto(conteudo)

    def _parse_texto(self, conteudo: str) -> list[EtiquetaZPL]:
        blocos = zpl_utils.extrair_blocos(conteudo)
        etiquetas: list[EtiquetaZPL] = []
        for idx, bloco in enumerate(blocos, start=1):
            sku = zpl_utils.extrair_sku(bloco) or f"SEM-SKU-{idx:04d}"
            etiquetas.append(
                EtiquetaZPL(
                    sku=sku,
                    zpl_raw=bloco,
                    indice=idx,
                )
            )
        return etiquetas

    def _parse_grf(self, conteudo: str) -> list[EtiquetaZPL]:
        self.progress_callback("Decodificando etiquetas GRF...")
        grfs = grf_decoder.extrair_etiquetas(conteudo)
        if not grfs:
            return []

        # 1 EtiquetaZPL por STICKER. Stickers do mesmo GRF compartilham
        # zpl_raw e a imagem da folha (referencia, sem copia).
        etiquetas: list[EtiquetaZPL] = []
        indice_global = 0
        for g in grfs:
            if not g.stickers:
                # Folha sem QR legivel: placeholder para o preview/contagem
                # nao zerarem — a impressao (pass-through) nao depende disso.
                indice_global += 1
                etiquetas.append(
                    EtiquetaZPL(
                        sku=f"SEM-QR-{g.indice:03d}",
                        zpl_raw=g.zpl_raw,
                        indice=indice_global,
                        metadados={
                            "grf_indice": g.indice,
                            "imagem_folha": g.imagem,
                        },
                    )
                )
                continue
            for st in g.stickers:
                indice_global += 1
                cache_manual = self.catalog.get(st.sku) if self.catalog else None
                etiquetas.append(
                    EtiquetaZPL(
                        sku=st.sku,
                        zpl_raw=g.zpl_raw,
                        indice=indice_global,
                        seller_sku=cache_manual or "",
                        metadados={
                            "grf_indice": g.indice,
                            "sticker": st,
                            "imagem_folha": g.imagem,
                        },
                    )
                )
        return etiquetas

    def _decodificar_com_fallback(self, dados: bytes) -> str:
        encodings = [self.encoding_primario, "utf-8", "cp1252", "latin-1"]
        ultimo_erro: Exception | None = None
        for enc in encodings:
            try:
                return dados.decode(enc)
            except UnicodeDecodeError as exc:
                ultimo_erro = exc
                log.debug("Falha ao decodificar com %s: %s", enc, exc)
        raise UnicodeDecodeError(
            self.encoding_primario,
            b"",
            0,
            1,
            f"Nao foi possivel decodificar o arquivo ({ultimo_erro})",
        )


class ShopeePDFParser(MarketplaceParser):
    """Le o PDF de etiquetas da Shopee pela CAMADA TEXTUAL (sem OCR).

    Cada pagina do PDF = 1 etiqueta. SKU / Seller SKU / descricao vem como
    TEXTO real (via ``pdf_reader``) -> render nativo (^A0) nitido, sem os
    recortes rasterizados do fluxo GRF. Nao ha ``zpl_raw`` (o PDF nao e ZPL):
    a impressao e SEMPRE composta (nunca pass-through).
    """

    nome = "Shopee PDF"

    def __init__(self, progress_callback: ProgressCallback | None = None) -> None:
        self.progress_callback = progress_callback or (lambda _msg: None)

    def parse_file(self, filepath: str | Path) -> list[EtiquetaZPL]:
        path = Path(filepath)
        if not path.exists():
            raise FileNotFoundError(f"Arquivo nao encontrado: {path}")
        etiquetas = self._converter(pdf_reader.ler_etiquetas(path))
        log.info("Parse PDF concluido: %s -> %d etiquetas", path.name, len(etiquetas))
        return etiquetas

    def parse_bytes(self, dados: bytes) -> list[EtiquetaZPL]:
        return self._converter(pdf_reader.ler_etiquetas_bytes(dados))

    def parse_content(self, conteudo: str) -> list[EtiquetaZPL]:
        raise NotImplementedError("PDF nao e texto puro; use parse_file/parse_bytes.")

    def _converter(self, paginas: list[pdf_reader.PDFEtiqueta]) -> list[EtiquetaZPL]:
        etiquetas: list[EtiquetaZPL] = []
        for i, pe in enumerate(paginas, start=1):
            etiquetas.append(
                EtiquetaZPL(
                    sku=pe.sku or f"SEM-SKU-{i:04d}",
                    zpl_raw="",  # sem ZPL bruto -> forca modo composto na UI
                    indice=i,
                    seller_sku=pe.seller_sku,
                    descricao=pe.descricao,
                    metadados={
                        "fonte": "pdf",
                        "pagina": pe.pagina,
                        "problemas": pe.validar(),
                    },
                )
            )
        return etiquetas
