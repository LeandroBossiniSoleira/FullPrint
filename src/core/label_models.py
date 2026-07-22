"""Modelos de etiqueta configuraveis pelo usuario + persistencia em JSON.

Dois modos de impressao por modelo:

- ``pass_through``: envia o arquivo da Shopee byte a byte (fiel ao 10x15
  original). Para quem usa a bobina padrao da Shopee.
- ``composto``: re-monta as etiquetas no tamanho/layout da bobina do usuario.
  Cada sticker (QR + texto) e recortado do bitmap original (sem OCR, pixels
  identicos) e reposicionado pelo ``label_renderer``. Resolve impressao em
  bobinas menores / multiplas colunas (ex.: 50x25mm, 2 colunas).

As dimensoes sao em milimetros; a conversao para dots usa o ``dpi`` do modelo
(ZD220 = 203 dpi = 8 dots/mm).
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from threading import Lock

from ..utils.logger import get_logger

log = get_logger("label_models")

MODO_PASS_THROUGH = "pass_through"
MODO_COMPOSTO = "composto"


def mm_para_dots(mm: float, dpi: int = 203) -> int:
    return round(mm * dpi / 25.4)


@dataclass
class LabelModel:
    id: str
    nome: str
    modo: str = MODO_COMPOSTO
    dpi: int = 203
    # Geometria da bobina (uma etiqueta fisica + disposicao das colunas).
    largura_mm: float = 50.0
    altura_mm: float = 25.0
    colunas: int = 2
    margem_esq_mm: float = 1.0
    margem_dir_mm: float = 1.0
    margem_topo_mm: float = 0.0     # deslocamento do conteudo dentro da etiqueta
    gap_colunas_mm: float = 3.0     # vao (die-cut) entre colunas
    gap_linhas_mm: float = 3.0      # vao entre linhas (informativo; impressora avanca)
    # Layout do conteudo dentro de cada etiqueta. ``qr_mm`` e area MAXIMA: o QR
    # regenerado arredonda p/ modulos inteiros e o texto ganha a folga. 18mm da
    # modulo de 0,5mm nos SKUs reais (v2, 33 modulos) — escaneavel — e libera
    # ~4mm de largura para o texto em relacao aos 21mm anteriores.
    qr_mm: float = 18.0
    # Zona segura: conteudo fica a esta distancia da borda do die-cut, p/ nao
    # perder tinta quando a bobina desliza. 1mm segue o referencial de etiqueta
    # 50x25 do PCP 2.0 (mesma midia, tarefa "Impressao de OP - Emitir Etiqueta").
    pad_interno_mm: float = 1.0

    # ---- conversoes para dots ------------------------------------------
    def dots(self, mm: float) -> int:
        return mm_para_dots(mm, self.dpi)

    @property
    def largura_dots(self) -> int:
        return self.dots(self.largura_mm)

    @property
    def altura_dots(self) -> int:
        return self.dots(self.altura_mm)

    @property
    def linha_largura_dots(self) -> int:
        """Largura total da linha impressa (todas as colunas + margens/vaos).

        Arredondada para multiplo de 8 (byte cheio no ^GFA, evita sliver preto
        nos bits de padding da ultima coluna do bitmap)."""
        total = (
            self.dots(self.margem_esq_mm)
            + self.colunas * self.largura_dots
            + (self.colunas - 1) * self.dots(self.gap_colunas_mm)
            + self.dots(self.margem_dir_mm)
        )
        return (total + 7) // 8 * 8

    def x0_coluna(self, col: int) -> int:
        """X inicial (dots) da etiqueta na coluna ``col`` dentro da linha."""
        return self.dots(self.margem_esq_mm) + col * (
            self.largura_dots + self.dots(self.gap_colunas_mm)
        )

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "LabelModel":
        campos = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in d.items() if k in campos})


def _modelos_padrao() -> list[LabelModel]:
    return [
        LabelModel(
            id="shopee_10x15",
            nome="Shopee 10x15 (original, fiel)",
            modo=MODO_PASS_THROUGH,
        ),
        LabelModel(
            id="etiqueta_50x25_2col",
            nome="Etiqueta 50x25mm (2 colunas)",
            modo=MODO_COMPOSTO,
            largura_mm=50.0,
            altura_mm=25.0,
            colunas=2,
            margem_esq_mm=1.0,
            margem_dir_mm=1.0,
            margem_topo_mm=0.0,
            gap_colunas_mm=3.0,
            gap_linhas_mm=3.0,
            qr_mm=18.0,
            pad_interno_mm=1.0,
        ),
    ]


@dataclass
class _Estado:
    ativo: str
    modelos: list[LabelModel] = field(default_factory=list)


class LabelModelStore:
    """Lista persistente de modelos + qual esta ativo (data/label_models.json)."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._lock = Lock()
        self._modelos: dict[str, LabelModel] = {}
        self._ativo: str = ""
        self._carregar()

    def _carregar(self) -> None:
        if self.path.exists():
            try:
                with self.path.open("r", encoding="utf-8") as fh:
                    dados = json.load(fh)
                modelos = [LabelModel.from_dict(m) for m in dados.get("modelos", [])]
                self._modelos = {m.id: m for m in modelos}
                self._ativo = dados.get("ativo", "")
                log.info("Modelos carregados: %d (ativo=%s)", len(self._modelos), self._ativo)
            except Exception as exc:  # noqa: BLE001
                log.warning("Falha ao ler modelos %s: %s", self.path, exc)
        if not self._modelos:
            self._modelos = {m.id: m for m in _modelos_padrao()}
            self._ativo = "etiqueta_50x25_2col"
            self._salvar()
        if self._ativo not in self._modelos:
            self._ativo = next(iter(self._modelos))

    def _salvar(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "ativo": self._ativo,
                "modelos": [m.to_dict() for m in self._modelos.values()],
            }
            tmp = self.path.with_suffix(self.path.suffix + ".tmp")
            with tmp.open("w", encoding="utf-8") as fh:
                json.dump(payload, fh, ensure_ascii=False, indent=2)
            tmp.replace(self.path)
        except Exception as exc:  # noqa: BLE001
            log.warning("Falha ao salvar modelos: %s", exc)

    # ---- API -----------------------------------------------------------
    def listar(self) -> list[LabelModel]:
        with self._lock:
            return list(self._modelos.values())

    def get(self, model_id: str) -> LabelModel | None:
        with self._lock:
            return self._modelos.get(model_id)

    def ativo(self) -> LabelModel:
        with self._lock:
            return self._modelos[self._ativo]

    def set_ativo(self, model_id: str) -> None:
        with self._lock:
            if model_id in self._modelos:
                self._ativo = model_id
                self._salvar()

    def salvar_modelo(self, modelo: LabelModel) -> None:
        with self._lock:
            self._modelos[modelo.id] = modelo
            self._salvar()

    def remover(self, model_id: str) -> bool:
        with self._lock:
            # Nunca deixa a lista vazia nem remove o pass-through padrao.
            if model_id == "shopee_10x15" or len(self._modelos) <= 1:
                return False
            self._modelos.pop(model_id, None)
            if self._ativo == model_id:
                self._ativo = next(iter(self._modelos))
            self._salvar()
            return True
