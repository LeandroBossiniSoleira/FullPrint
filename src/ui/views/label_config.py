"""Dialog de configuracao de modelos de etiqueta.

Permite criar/editar/excluir modelos (dimensoes da bobina, colunas, margens) e
imprimir uma etiqueta de teste para calibrar o alinhamento na bobina real.
"""
from __future__ import annotations

import re
from tkinter import messagebox
from typing import Callable

import customtkinter as ctk

from ...core.label_models import (
    MODO_COMPOSTO,
    MODO_PASS_THROUGH,
    LabelModel,
    LabelModelStore,
)

# (label visivel, atributo, tipo) dos campos numericos do modo composto.
CAMPOS_COMPOSTO = [
    ("Largura (mm)", "largura_mm", float),
    ("Altura (mm)", "altura_mm", float),
    ("Colunas", "colunas", int),
    ("Margem esquerda (mm)", "margem_esq_mm", float),
    ("Margem direita (mm)", "margem_dir_mm", float),
    ("Margem topo (mm)", "margem_topo_mm", float),
    ("Vao entre colunas (mm)", "gap_colunas_mm", float),
    ("Vao entre linhas (mm)", "gap_linhas_mm", float),
    ("Tamanho do QR (mm)", "qr_mm", float),
    ("Zona segura da borda (mm)", "pad_interno_mm", float),
    ("DPI da impressora", "dpi", int),
]


def _slug(texto: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "_", texto.lower()).strip("_")
    return s or "modelo"


class LabelConfigDialog(ctk.CTkToplevel):
    def __init__(
        self,
        master,
        store: LabelModelStore,
        on_change: Callable[[], None],
        on_test: Callable[[LabelModel], None],
    ) -> None:
        super().__init__(master)
        self.store = store
        self.on_change = on_change
        self.on_test = on_test
        self._entries: dict[str, ctk.CTkEntry] = {}

        self.title("Configurar modelos de etiqueta")
        self.geometry("520x620")
        self.transient(master)
        self.grab_set()

        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(2, weight=1)

        # Seletor de modelo + acoes de lista
        topo = ctk.CTkFrame(self)
        topo.grid(row=0, column=0, sticky="ew", padx=12, pady=(12, 6))
        topo.grid_columnconfigure(0, weight=1)
        self.opt_modelo = ctk.CTkOptionMenu(topo, values=["..."], command=self._on_selecionar)
        self.opt_modelo.grid(row=0, column=0, sticky="ew", padx=(8, 6), pady=8)
        ctk.CTkButton(topo, text="Novo", width=70, command=self._on_novo).grid(row=0, column=1, padx=4, pady=8)
        ctk.CTkButton(topo, text="Excluir", width=70, fg_color="#8a3030", command=self._on_excluir).grid(
            row=0, column=2, padx=(4, 8), pady=8
        )

        # Modo
        modo_frame = ctk.CTkFrame(self)
        modo_frame.grid(row=1, column=0, sticky="ew", padx=12, pady=6)
        ctk.CTkLabel(modo_frame, text="Modo de impressao:").grid(row=0, column=0, padx=8, pady=8, sticky="w")
        self.seg_modo = ctk.CTkSegmentedButton(
            modo_frame,
            values=["Fiel (10x15 Shopee)", "Composto (bobina propria)"],
            command=self._on_trocar_modo,
        )
        self.seg_modo.grid(row=0, column=1, padx=8, pady=8, sticky="ew")

        # Campos
        self.form = ctk.CTkScrollableFrame(self, label_text="Parametros")
        self.form.grid(row=2, column=0, sticky="nsew", padx=12, pady=6)
        self.form.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(self.form, text="Nome").grid(row=0, column=0, padx=8, pady=6, sticky="w")
        self.ent_nome = ctk.CTkEntry(self.form)
        self.ent_nome.grid(row=0, column=1, padx=8, pady=6, sticky="ew")

        for i, (rotulo, attr, _tipo) in enumerate(CAMPOS_COMPOSTO, start=1):
            ctk.CTkLabel(self.form, text=rotulo).grid(row=i, column=0, padx=8, pady=6, sticky="w")
            ent = ctk.CTkEntry(self.form)
            ent.grid(row=i, column=1, padx=8, pady=6, sticky="ew")
            self._entries[attr] = ent

        # Rodape
        rodape = ctk.CTkFrame(self)
        rodape.grid(row=3, column=0, sticky="ew", padx=12, pady=(6, 12))
        rodape.grid_columnconfigure(0, weight=1)
        ctk.CTkButton(rodape, text="Imprimir teste", command=self._on_teste).grid(
            row=0, column=0, padx=8, pady=8, sticky="w"
        )
        ctk.CTkButton(rodape, text="Salvar e usar", command=self._on_salvar).grid(row=0, column=1, padx=4, pady=8)
        ctk.CTkButton(rodape, text="Fechar", fg_color="gray30", command=self.destroy).grid(
            row=0, column=2, padx=(4, 8), pady=8
        )

        self._recarregar_lista(selecionar=self.store.ativo().id)

    # ------------------------------------------------------------------ infra
    def _recarregar_lista(self, selecionar: str | None = None) -> None:
        modelos = self.store.listar()
        self._nome_por_id = {m.id: m.nome for m in modelos}
        self._id_por_nome = {m.nome: m.id for m in modelos}
        nomes = [m.nome for m in modelos]
        self.opt_modelo.configure(values=nomes)
        alvo = selecionar or self.store.ativo().id
        nome_alvo = self._nome_por_id.get(alvo, nomes[0])
        self.opt_modelo.set(nome_alvo)
        self._carregar_modelo(self._id_por_nome[nome_alvo])

    def _carregar_modelo(self, model_id: str) -> None:
        m = self.store.get(model_id)
        if not m:
            return
        self._editando = m.id
        self.ent_nome.delete(0, "end")
        self.ent_nome.insert(0, m.nome)
        for attr, ent in self._entries.items():
            ent.delete(0, "end")
            valor = getattr(m, attr)
            ent.insert(0, str(int(valor)) if attr in ("colunas", "dpi") else str(valor))
        modo_label = "Fiel (10x15 Shopee)" if m.modo == MODO_PASS_THROUGH else "Composto (bobina propria)"
        self.seg_modo.set(modo_label)
        self._aplicar_estado_modo(m.modo)

    def _aplicar_estado_modo(self, modo: str) -> None:
        estado = "disabled" if modo == MODO_PASS_THROUGH else "normal"
        for ent in self._entries.values():
            ent.configure(state=estado)

    def _modo_atual(self) -> str:
        return MODO_PASS_THROUGH if self.seg_modo.get().startswith("Fiel") else MODO_COMPOSTO

    # --------------------------------------------------------------- handlers
    def _on_selecionar(self, nome: str) -> None:
        self._carregar_modelo(self._id_por_nome[nome])

    def _on_trocar_modo(self, _valor: str) -> None:
        self._aplicar_estado_modo(self._modo_atual())

    def _on_novo(self) -> None:
        base = LabelModel(id="", nome="Nova etiqueta", modo=MODO_COMPOSTO)
        novo_id = _slug(base.nome)
        existentes = {m.id for m in self.store.listar()}
        i = 1
        while novo_id in existentes:
            i += 1
            novo_id = f"{_slug(base.nome)}_{i}"
        base.id = novo_id
        self.store.salvar_modelo(base)
        self.on_change()
        self._recarregar_lista(selecionar=novo_id)

    def _on_excluir(self) -> None:
        if not messagebox.askyesno("Excluir", f"Excluir o modelo '{self.ent_nome.get()}'?", parent=self):
            return
        if self.store.remover(self._editando):
            self.on_change()
            self._recarregar_lista()
        else:
            messagebox.showwarning("Excluir", "Este modelo nao pode ser removido.", parent=self)

    def _coletar(self) -> LabelModel | None:
        nome = self.ent_nome.get().strip() or "Sem nome"
        valores: dict = {}
        for rotulo, attr, tipo in CAMPOS_COMPOSTO:
            try:
                valores[attr] = tipo(self._entries[attr].get().replace(",", "."))
            except (ValueError, TypeError):
                messagebox.showerror("Valor invalido", f"Campo '{rotulo}' invalido.", parent=self)
                return None
        if valores["colunas"] < 1:
            messagebox.showerror("Valor invalido", "Colunas deve ser >= 1.", parent=self)
            return None
        return LabelModel(id=self._editando, nome=nome, modo=self._modo_atual(), **valores)

    def _on_salvar(self) -> None:
        m = self._coletar()
        if m is None:
            return
        self.store.salvar_modelo(m)
        self.store.set_ativo(m.id)
        self.on_change()
        self._recarregar_lista(selecionar=m.id)
        messagebox.showinfo("Modelo salvo", f"'{m.nome}' salvo e definido como ativo.", parent=self)

    def _on_teste(self) -> None:
        m = self._coletar()
        if m is None:
            return
        self.on_test(m)
