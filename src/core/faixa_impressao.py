"""Faixa (recorte) da fila de impressao: comecar/retomar de um SKU e/ou limitar
a quantidade de etiquetas.

Motivacao: lotes longos (milhares de etiquetas) travam, acaba a bobina ou o
operador precisa parar no meio. Sem isso, retomar significa reimprimir o lote
inteiro e descartar a parte ja impressa. Com a faixa, o operador aponta o grupo
de SKU onde a impressao parou e manda "daqui ate o fim" -- ou "as proximas N
etiquetas", quando sabe quanto ainda cabe na bobina.

A unidade da faixa e' o **grupo consecutivo de SKU**, o mesmo criado por
``label_renderer.grupos_consecutivos`` e anunciado pela etiqueta separadora.
E' o que o operador ve fisicamente saindo da impressora, entao e' a referencia
natural para dizer "parei aqui". A ordem do arquivo nunca e' alterada -- so
recortada.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ItemFila:
    """Um grupo de SKU na fila, na ordem exata em que sera impresso.

    ``inicio``/``fim`` sao indices na lista plana de etiquetas (fim exclusivo),
    o que permite recortar sem reconstruir os grupos.
    """

    ordem: int          # 1-based: e' o numero que o operador ve no preview
    sku: str
    seller_sku: str
    inicio: int
    fim: int

    @property
    def qtd(self) -> int:
        return self.fim - self.inicio

    def rotulo(self) -> str:
        alvo = self.seller_sku or self.sku
        return f"{self.ordem:03d} · {alvo}"


@dataclass
class Faixa:
    """Recorte pedido pelo operador.

    - ``inicio`` / ``fim``: ``ordem`` (1-based) do primeiro/ultimo grupo,
      inclusive. ``None`` = inicio do lote / fim da fila.
    - ``limite``: maximo de etiquetas de produto a imprimir, contadas a partir
      do inicio da faixa. ``None`` = sem limite. Aplicado DEPOIS do recorte por
      grupo, e pode cortar um grupo no meio (o proposito e' justamente "cabe
      mais 200 etiquetas nesta bobina").

    Separadoras nao entram no ``limite``: sao geradas pelo renderer a partir do
    recorte final, e o grupo cortado no meio continua recebendo a sua.
    """

    inicio: int | None = None
    fim: int | None = None
    limite: int | None = None

    @property
    def ativa(self) -> bool:
        return self.inicio is not None or self.fim is not None or self.limite is not None

    def limpar(self) -> None:
        self.inicio = None
        self.fim = None
        self.limite = None


def montar_fila(etiquetas: list) -> list[ItemFila]:
    """Quebra a lista em grupos de SKU igual e CONSECUTIVO, sem reordenar.

    Mesma regra de ``label_renderer.grupos_consecutivos`` (um SKU que reaparece
    depois de outro vira um grupo novo, com sua propria separadora), mas devolve
    indices em vez de copiar as etiquetas -- e' o que a UI lista e o que
    ``aplicar`` usa para recortar.
    """
    fila: list[ItemFila] = []
    inicio = 0
    for i, et in enumerate(etiquetas):
        sku = getattr(et, "sku", "") or ""
        if i > 0 and sku == (getattr(etiquetas[i - 1], "sku", "") or ""):
            continue
        if i > 0:
            fila.append(_item(len(fila) + 1, etiquetas, inicio, i))
        inicio = i
    if etiquetas:
        fila.append(_item(len(fila) + 1, etiquetas, inicio, len(etiquetas)))
    return fila


def _item(ordem: int, etiquetas: list, inicio: int, fim: int) -> ItemFila:
    trecho = etiquetas[inicio:fim]
    seller = next((getattr(et, "seller_sku", "") for et in trecho if getattr(et, "seller_sku", "")), "")
    return ItemFila(
        ordem=ordem,
        sku=getattr(trecho[0], "sku", "") or "",
        seller_sku=seller,
        inicio=inicio,
        fim=fim,
    )


def grupos_na_faixa(fila: list[ItemFila], faixa: Faixa) -> list[ItemFila]:
    """Grupos da fila que caem dentro da faixa (ignorando ``limite``).

    Usado pela UI para marcar no preview o que entra e o que fica de fora.
    Limites invertidos ou fora do range sao tolerados (clamp), nunca erro: a
    UI nao deve travar por um valor digitado torto.
    """
    if not fila:
        return []
    inicio = max(1, faixa.inicio or 1)
    fim = min(len(fila), faixa.fim if faixa.fim is not None else len(fila))
    if fim < inicio:
        inicio, fim = fim, inicio
    return [g for g in fila if inicio <= g.ordem <= fim]


def intervalo(etiquetas: list, faixa: Faixa, fila: list[ItemFila] | None = None) -> range:
    """Indices (na lista plana) que a faixa seleciona.

    Devolver o intervalo -- e nao so' as etiquetas -- deixa a UI marcar linha por
    linha o que entra/fica de fora sem procurar objetos na lista (``list.index``
    compara por valor e acharia a etiqueta errada quando duas sao iguais).
    """
    if not etiquetas:
        return range(0)
    if not faixa.ativa:
        return range(len(etiquetas))
    fila = fila if fila is not None else montar_fila(etiquetas)
    grupos = grupos_na_faixa(fila, faixa)
    if not grupos:
        return range(0)
    inicio, fim = grupos[0].inicio, grupos[-1].fim
    if faixa.limite is not None:
        fim = min(fim, inicio + max(0, faixa.limite))
    return range(inicio, fim)


def aplicar(etiquetas: list, faixa: Faixa, fila: list[ItemFila] | None = None) -> list:
    """Recorta a lista de etiquetas conforme a faixa. Devolve as MESMAS
    instancias (sem copia), na ordem original."""
    trecho = intervalo(etiquetas, faixa, fila)
    return etiquetas[trecho.start : trecho.stop]


def resumo(fila: list[ItemFila], faixa: Faixa, total_etiquetas: int) -> str:
    """Frase curta para a UI/log: o que sera impresso nesta faixa."""
    if not fila:
        return "Nenhum lote processado."
    grupos = grupos_na_faixa(fila, faixa)
    if not grupos:
        return "Faixa vazia (nenhum grupo selecionado)."
    primeiro = grupos[0]
    fim_plano = grupos[-1].fim
    if faixa.limite is not None:
        fim_plano = min(fim_plano, primeiro.inicio + max(0, faixa.limite))
    qtd = fim_plano - primeiro.inicio
    if not faixa.ativa:
        return f"Lote completo: {len(fila)} grupos · {qtd} etiquetas"
    # Grupos que REALMENTE recebem etiqueta (o limite pode parar antes do fim
    # da faixa, e cortar o ultimo grupo no meio).
    alcancados = [g for g in grupos if g.inicio < fim_plano]
    if not alcancados:
        return "Faixa vazia (limite de etiquetas zerou o recorte)."
    ultimo = alcancados[-1]
    if ultimo.ordem == len(fila) and ultimo.fim == fim_plano:
        ate = "fim da fila"
    elif ultimo.fim > fim_plano:
        ate = f"{ultimo.rotulo()} (parcial)"
    else:
        ate = ultimo.rotulo()
    partes = [f"De {primeiro.rotulo()} ate {ate}"]
    if faixa.limite is not None:
        partes.append(f"max {faixa.limite} etiquetas")
    partes.append(
        f"{len(alcancados)} de {len(fila)} grupos · {qtd} de {total_etiquetas} etiquetas"
    )
    return "  |  ".join(partes)


def blocos_pass_through(etiquetas_todas: list, selecionadas: list) -> tuple[str, int]:
    """Monta o ZPL do recorte no modo FIEL, a partir dos blocos originais.

    No modo fiel o app envia os bytes do arquivo sem tocar. Com faixa nao da:
    precisa remontar so os blocos ``^XA..^XZ`` do recorte. Detalhe critico do
    GRF da Shopee: uma folha 10x15 carrega VARIOS stickers e todos compartilham
    o mesmo ``zpl_raw``. Nao existe recortar meia folha -- se o recorte pega um
    sticker da folha, a folha inteira sai.

    Devolve ``(zpl, extras)``, onde ``extras`` e' quantas etiquetas fora da
    faixa vao sair por causa dessas folhas compartilhadas (0 = recorte exato).
    O chamador deve avisar o operador quando ``extras > 0``.
    """
    if not selecionadas:
        return "", 0
    escolhidos = {_chave_bloco(et) for et in selecionadas}
    dentro = {id(et) for et in selecionadas}
    blocos: list[str] = []
    vistos: set = set()
    extras = 0
    for et in etiquetas_todas:
        chave = _chave_bloco(et)
        if chave not in escolhidos:
            continue
        if id(et) not in dentro:
            extras += 1  # carona: sticker da mesma folha, fora da faixa
        if chave in vistos:
            continue
        vistos.add(chave)
        bruto = (getattr(et, "zpl_raw", "") or "").strip()
        if bruto:
            blocos.append(bruto)
    return "\n".join(blocos) + ("\n" if blocos else ""), extras


def _chave_bloco(et) -> tuple:
    """Identifica o bloco ``^XA..^XZ`` de origem da etiqueta.

    GRF: a folha (``grf_indice``) -- varios stickers por bloco. Texto puro:
    o proprio indice, 1 bloco por etiqueta.
    """
    grf = getattr(et, "metadados", {}).get("grf_indice")
    if grf is not None:
        return ("grf", grf)
    return ("idx", getattr(et, "indice", 0))
