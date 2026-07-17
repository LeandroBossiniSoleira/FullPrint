"""Dialogo de confirmacao EM LOTE do Seller SKU pre-preenchido por OCR.

Mostra uma linha por SKU: a imagem ampliada do recorte (para o usuario LER o
texto real) ao lado de um campo editavel ja preenchido com o palpite do OCR.
O usuario corrige o que estiver errado e salva tudo de uma vez no catalogo.

O OCR e so sugestao (ver ``core/ocr_seller``): a impressao usa exclusivamente
o texto confirmado aqui.
"""
from __future__ import annotations

import threading
from typing import Callable

import customtkinter as ctk
from PIL import Image, ImageTk

# Geometria do thumbnail do recorte (o texto-fonte tem ~6px; ampliamos p/ leitura).
_THUMB_H = 30
_THUMB_W_MAX = 380


class OcrBatchDialog(ctk.CTkToplevel):
    def __init__(
        self,
        master,
        *,
        itens: list[tuple[str, Image.Image, str]],
        ocr_func: Callable[[Image.Image], str],
        on_save: Callable[[dict[str, str]], None],
    ) -> None:
        """``itens``: lista de (sku_numerico, recorte_seller, valor_atual_catalogo).
        ``ocr_func``: funcao que recebe o recorte e devolve o palpite (string).
        ``on_save``: recebe {sku: seller_sku} confirmado (so os nao-vazios)."""
        super().__init__(master)
        self._itens = itens
        self._ocr_func = ocr_func
        self._on_save = on_save
        self._entries: dict[str, ctk.CTkEntry] = {}
        self._thumbs: list[ImageTk.PhotoImage] = []  # refs vivas (evita GC)

        self.title("Pre-preencher Seller SKU (OCR) — confira e edite")
        self.geometry("760x560")
        self.transient(master)
        self.after(50, self._centralizar_e_focar)

        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(1, weight=1)

        ctk.CTkLabel(
            self,
            text=(
                "O OCR le a fonte minuscula da Shopee com ERROS (sugestao, nao verdade). "
                "Confira cada linha pela imagem e corrija antes de salvar."
            ),
            wraplength=720,
            justify="left",
            anchor="w",
        ).grid(row=0, column=0, sticky="ew", padx=16, pady=(14, 6))

        self._scroll = ctk.CTkScrollableFrame(self)
        self._scroll.grid(row=1, column=0, sticky="nsew", padx=16, pady=6)
        self._scroll.grid_columnconfigure(1, weight=1)

        self._lbl_status = ctk.CTkLabel(self._scroll, text="Reconhecendo via OCR...")
        self._lbl_status.grid(row=0, column=0, columnspan=3, padx=8, pady=20, sticky="w")

        rodape = ctk.CTkFrame(self, fg_color="transparent")
        rodape.grid(row=2, column=0, sticky="ew", padx=16, pady=(6, 14))
        rodape.grid_columnconfigure(0, weight=1)
        self._lbl_rodape = ctk.CTkLabel(rodape, text="", anchor="w")
        self._lbl_rodape.grid(row=0, column=0, sticky="w")
        self._btn_cancelar = ctk.CTkButton(
            rodape, text="Cancelar", width=120, fg_color="gray30", command=self.destroy
        )
        self._btn_cancelar.grid(row=0, column=1, padx=(6, 6))
        self._btn_salvar = ctk.CTkButton(
            rodape, text="Salvar todos", width=160, state="disabled", command=self._salvar
        )
        self._btn_salvar.grid(row=0, column=2)

        # OCR fora da UI (35 SKUs ~ alguns segundos); popula as linhas ao terminar.
        threading.Thread(target=self._rodar_ocr, name="OcrBatchWorker", daemon=True).start()

    def _centralizar_e_focar(self) -> None:
        try:
            self.grab_set()  # modal
            self.lift()
            self.focus_force()
        except Exception:  # noqa: BLE001 (janela ja fechada)
            pass

    def _rodar_ocr(self) -> None:
        palpites: list[tuple[str, Image.Image, str, str]] = []
        for i, (sku, crop, atual) in enumerate(self._itens, start=1):
            # Ja mapeado: mantem o valor do catalogo; senao usa o palpite do OCR.
            sugestao = atual or self._ocr_func(crop)
            palpites.append((sku, crop, atual, sugestao))
            self.after(0, self._progresso, i, len(self._itens))
        self.after(0, self._montar_linhas, palpites)

    def _progresso(self, i: int, total: int) -> None:
        if self._lbl_status.winfo_exists():
            self._lbl_status.configure(text=f"Reconhecendo via OCR... {i}/{total}")

    def _thumb(self, crop: Image.Image) -> ImageTk.PhotoImage:
        g = crop.convert("L")
        fator = _THUMB_H / max(1, g.height)
        w = min(_THUMB_W_MAX, max(1, int(g.width * fator)))
        g = g.resize((w, _THUMB_H), Image.NEAREST)  # NEAREST: preserva os pixels
        tk_img = ImageTk.PhotoImage(g)
        self._thumbs.append(tk_img)
        return tk_img

    def _montar_linhas(self, palpites: list[tuple[str, Image.Image, str, str]]) -> None:
        self._lbl_status.destroy()
        cabec = ("Imagem (texto real)", "SKU Shopee", "Seller SKU (editavel)")
        for c, txt in enumerate(cabec):
            ctk.CTkLabel(self._scroll, text=txt, font=ctk.CTkFont(weight="bold")).grid(
                row=0, column=c, padx=8, pady=(0, 6), sticky="w"
            )
        for r, (sku, crop, atual, sugestao) in enumerate(palpites, start=1):
            tk_img = self._thumb(crop)
            lbl = ctk.CTkLabel(self._scroll, text="", image=tk_img)
            lbl.grid(row=r, column=0, padx=8, pady=3, sticky="w")
            ctk.CTkLabel(self._scroll, text=sku, anchor="w").grid(
                row=r, column=1, padx=8, pady=3, sticky="ew"
            )
            ent = ctk.CTkEntry(self._scroll, width=220)
            ent.insert(0, sugestao)
            # Borda em destaque quando veio do OCR (nao do catalogo) — revisar.
            if not atual and sugestao:
                ent.configure(border_color="#b58900")
            ent.grid(row=r, column=2, padx=8, pady=3, sticky="ew")
            self._entries[sku] = ent

        self._btn_salvar.configure(state="normal")
        self._lbl_rodape.configure(
            text=f"{len(palpites)} SKUs. Campos em destaque vieram do OCR — confira."
        )

    def _salvar(self) -> None:
        confirmados = {
            sku: ent.get().strip().upper()
            for sku, ent in self._entries.items()
            if ent.get().strip()
        }
        self._on_save(confirmados)
        self.destroy()
