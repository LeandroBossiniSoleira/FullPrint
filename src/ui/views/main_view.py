"""Tela principal do MVP."""
from __future__ import annotations

import threading
import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog, ttk

import customtkinter as ctk
from PIL import Image, ImageTk

from ...config.settings import Settings
from ...core import grf_decoder, label_renderer, ocr_seller, zpl_renderer
from ...core.agrupador import EtiquetaAgrupador
from ...core.label_models import MODO_PASS_THROUGH, LabelModelStore
from ...core.parser import ShopeeZPLParser
from ...core.sku_catalog import SKUCatalog
from ...services.printer import ZebraPrinterService
from ...services.spooler_worker import PrintJob, PrintQueueManager
from ...utils.logger import get_logger
from .label_config import LabelConfigDialog
from .ocr_batch_dialog import OcrBatchDialog

log = get_logger("ui.main")

# Limites defensivos: o gargalo histórico foi criar um widget CTk por linha do
# preview. Treeview aguenta milhares, mas mantemos o teto para o caso de algum
# arquivo absurdo (centenas de milhares).
MAX_PREVIEW_ROWS = 5000
MAX_LOG_LINES = 500


class MainView(ctk.CTkFrame):
    def __init__(
        self,
        master,
        settings: Settings,
        printer_service: ZebraPrinterService,
        worker: PrintQueueManager,
    ) -> None:
        super().__init__(master)
        self.settings = settings
        self.printer_service = printer_service
        self.worker = worker

        # Catalogo persistente: SKU numerico Shopee -> Seller SKU manual.
        # Constroi-se ao longo do uso: duplo-clique numa linha do preview salva.
        data_dir = Path(settings.printer_dev_output_dir).parent
        self.catalog = SKUCatalog(data_dir / "sku_catalog.json")
        # Modelos de etiqueta configuraveis (fiel 10x15 ou composto p/ bobina propria).
        self.label_store = LabelModelStore(data_dir / "label_models.json")

        self.parser = ShopeeZPLParser(
            encoding_primario=settings.printer_encoding,
            progress_callback=lambda msg: self.after(0, self._progresso_parse, msg),
            catalog=self.catalog,
        )
        self.agrupador = EtiquetaAgrupador()

        self._arquivo_atual: Path | None = None
        self._lote_bytes: bytes | None = None  # bytes originais p/ pass-through
        self._etiquetas_atuais: list = []
        self._grupos_atuais = None
        self._iid_etiqueta: dict[str, object] = {}
        self._tk_img_atual = None   # referencia viva do PhotoImage (evita GC da imagem Tk)
        self._preview_seq = 0       # token anti-corrida do render assincrono de ZPL
        self._et_selecionada = None  # etiqueta atual (alvo do "Interpretar ZPL")
        self._modo_preview = "Por SKU"
        self._busy = False
        self._log_lines = 0

        self._build_layout()
        self._popular_impressoras()
        self._popular_modelos()
        self.log_status("info", "Aplicacao iniciada.")

    # ------------------------------------------------------------------ layout
    def _build_layout(self) -> None:
        self.grid_columnconfigure(0, weight=1)
        self.grid_columnconfigure(1, weight=0)
        self.grid_rowconfigure(2, weight=1)

        topo = ctk.CTkFrame(self, corner_radius=8)
        topo.grid(row=0, column=0, columnspan=2, sticky="ew", padx=12, pady=(12, 6))
        topo.grid_columnconfigure(1, weight=1)
        topo.grid_columnconfigure(3, weight=1)

        ctk.CTkLabel(topo, text="Impressora:").grid(row=0, column=0, padx=(12, 6), pady=10, sticky="w")
        self.cb_impressora = ctk.CTkComboBox(topo, values=["(carregando...)"], width=300)
        self.cb_impressora.grid(row=0, column=1, padx=6, pady=10, sticky="ew")

        ctk.CTkLabel(topo, text="Modelo:").grid(row=0, column=2, padx=(18, 6), pady=10, sticky="w")
        self.cb_modelo = ctk.CTkOptionMenu(topo, values=["..."], command=self._on_modelo_selecionado, width=220)
        self.cb_modelo.grid(row=0, column=3, padx=6, pady=10, sticky="ew")
        self.btn_config = ctk.CTkButton(topo, text="Configurar...", width=110, command=self._abrir_config)
        self.btn_config.grid(row=0, column=4, padx=(6, 12), pady=10)

        anexo = ctk.CTkFrame(self, corner_radius=8)
        anexo.grid(row=1, column=0, columnspan=2, sticky="ew", padx=12, pady=6)
        anexo.grid_columnconfigure(1, weight=1)

        self.btn_anexar = ctk.CTkButton(anexo, text="Anexar arquivo TXT...", command=self._on_anexar, width=200)
        self.btn_anexar.grid(row=0, column=0, padx=12, pady=10)

        self.lbl_arquivo = ctk.CTkLabel(anexo, text="Nenhum arquivo selecionado.", anchor="w")
        self.lbl_arquivo.grid(row=0, column=1, padx=6, pady=10, sticky="ew")

        self.btn_processar = ctk.CTkButton(
            anexo, text="Processar", command=self._on_processar, width=120, state="disabled"
        )
        self.btn_processar.grid(row=0, column=2, padx=12, pady=10)

        preview = ctk.CTkFrame(self, corner_radius=8)
        preview.grid(row=2, column=0, sticky="nsew", padx=(12, 6), pady=6)
        preview.grid_columnconfigure(0, weight=1)
        preview.grid_rowconfigure(1, weight=1)

        cabecalho_preview = ctk.CTkFrame(preview, fg_color="transparent")
        cabecalho_preview.grid(row=0, column=0, sticky="ew", padx=12, pady=(10, 0))
        cabecalho_preview.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(
            cabecalho_preview, text="Preview", font=ctk.CTkFont(weight="bold")
        ).grid(row=0, column=0, sticky="w")
        # Pre-preenche o Seller SKU via OCR (sugestao) para confirmacao em lote.
        # So aparece habilitado se o Tesseract estiver disponivel e houver lote.
        self.btn_ocr = ctk.CTkButton(
            cabecalho_preview,
            text="Seller SKU via OCR...",
            width=170,
            command=self._on_prefill_ocr,
            state="disabled",
        )
        self.btn_ocr.grid(row=0, column=1, sticky="e", padx=(0, 8))
        self.toggle_modo = ctk.CTkSegmentedButton(
            cabecalho_preview,
            values=["Por SKU", "Individual"],
            command=self._on_trocar_modo,
            width=240,
        )
        self.toggle_modo.set("Por SKU")
        self.toggle_modo.grid(row=0, column=2, sticky="e")

        self._build_preview_tree(preview)
        self._build_painel_imagem(preview)

        log_frame = ctk.CTkFrame(self, corner_radius=8, width=320)
        log_frame.grid(row=2, column=1, sticky="nsew", padx=(6, 12), pady=6)
        log_frame.grid_propagate(False)
        log_frame.grid_columnconfigure(0, weight=1)
        log_frame.grid_rowconfigure(1, weight=1)

        ctk.CTkLabel(log_frame, text="Log de impressao", font=ctk.CTkFont(weight="bold")).grid(
            row=0, column=0, sticky="w", padx=12, pady=(10, 0)
        )
        self.log_box = ctk.CTkTextbox(log_frame, width=300, state="disabled", wrap="word")
        self.log_box.grid(row=1, column=0, sticky="nsew", padx=12, pady=10)

        rodape = ctk.CTkFrame(self, corner_radius=8)
        rodape.grid(row=3, column=0, columnspan=2, sticky="ew", padx=12, pady=(6, 12))
        rodape.grid_columnconfigure(0, weight=1)

        self.lbl_resumo = ctk.CTkLabel(rodape, text="Aguardando arquivo.", anchor="w")
        self.lbl_resumo.grid(row=0, column=0, padx=12, pady=10, sticky="ew")

        self.btn_imprimir = ctk.CTkButton(
            rodape,
            text="Imprimir Lote",
            command=self._on_imprimir,
            width=200,
            height=44,
            font=ctk.CTkFont(size=15, weight="bold"),
            state="disabled",
        )
        self.btn_imprimir.grid(row=0, column=1, padx=12, pady=10)

    def _build_preview_tree(self, parent: ctk.CTkFrame) -> None:
        """Treeview ttk nativo: muito mais leve que centenas de CTkLabel."""
        style = ttk.Style()
        # Tenta tema escuro compatível com CTk dark; cai para o default se indisponível.
        try:
            style.theme_use("clam")
        except Exception:  # noqa: BLE001
            pass
        bg = "#2b2b2b"
        fg = "#e6e6e6"
        sel_bg = "#1f6aa5"
        style.configure(
            "Preview.Treeview",
            background=bg,
            fieldbackground=bg,
            foreground=fg,
            rowheight=24,
            borderwidth=0,
        )
        style.configure(
            "Preview.Treeview.Heading",
            background="#1f1f1f",
            foreground=fg,
            relief="flat",
        )
        style.map(
            "Preview.Treeview",
            background=[("selected", sel_bg)],
            foreground=[("selected", "white")],
        )

        container = ctk.CTkFrame(parent, fg_color="transparent")
        container.grid(row=1, column=0, sticky="nsew", padx=12, pady=10)
        container.grid_columnconfigure(0, weight=1)
        container.grid_rowconfigure(0, weight=1)

        self.tree = ttk.Treeview(
            container,
            columns=("idx", "seller", "sku", "qtd"),
            show="headings",
            style="Preview.Treeview",
            selectmode="browse",
        )
        self.tree.heading("idx", text="#")
        self.tree.heading("seller", text="Seller SKU")
        self.tree.heading("sku", text="SKU Shopee")
        self.tree.heading("qtd", text="Qtd")
        self.tree.column("idx", width=50, anchor="e", stretch=False)
        self.tree.column("seller", width=200, anchor="w", stretch=False)
        self.tree.column("sku", anchor="w")
        self.tree.column("qtd", width=60, anchor="e", stretch=False)

        # Duplo-clique numa linha abre dialog pra editar/cadastrar o Seller SKU
        self.tree.bind("<Double-Button-1>", self._on_editar_seller_sku)
        # Selecao mostra a imagem real do sticker (recortada do bitmap GRF)
        self.tree.bind("<<TreeviewSelect>>", self._on_selecionar_linha)

        vsb = ttk.Scrollbar(container, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")

    def _build_painel_imagem(self, parent: ctk.CTkFrame) -> None:
        """Painel fixo abaixo da tabela: imagem real do sticker selecionado."""
        painel = ctk.CTkFrame(parent, corner_radius=8, height=210, fg_color="#1f1f1f")
        painel.grid(row=2, column=0, sticky="ew", padx=12, pady=(0, 10))
        painel.grid_propagate(False)
        painel.grid_columnconfigure(0, weight=1)
        painel.grid_rowconfigure(0, weight=1)

        # tk.Label nativo (nao CTkLabel): o CTkImage tem um bug conhecido de
        # rastreamento ("image pyimageN doesn't exist") ao trocar a imagem
        # repetidamente. Gerenciamos o PhotoImage manualmente, sem esse bug.
        self.lbl_imagem = tk.Label(
            painel,
            text="Selecione uma linha para ver o sticker.",
            bg="#1f1f1f",
            fg="#cfcfcf",
        )
        self.lbl_imagem.grid(row=0, column=0, sticky="nsew", padx=8, pady=(8, 0))

        rodape_img = ctk.CTkFrame(painel, fg_color="transparent")
        rodape_img.grid(row=1, column=0, sticky="ew", padx=8, pady=(0, 6))
        rodape_img.grid_columnconfigure(0, weight=1)
        self.lbl_imagem_info = ctk.CTkLabel(
            rodape_img, text="", font=ctk.CTkFont(size=11), text_color="#9a9a9a"
        )
        self.lbl_imagem_info.grid(row=0, column=0, sticky="ew")
        # Interpreta o ZPL bruto da etiqueta selecionada (Node/zpl-renderer-js):
        # substituto local do Labelary — funciona para qualquer ZPL, inclusive
        # o texto puro e o que o app gera.
        self.btn_interpretar = ctk.CTkButton(
            rodape_img,
            text="Interpretar ZPL",
            width=130,
            height=26,
            command=self._on_interpretar_zpl,
            state="disabled",
        )
        self.btn_interpretar.grid(row=0, column=1, sticky="e", padx=(6, 0))

    # ----------------------------------------------------------------- helpers
    def _popular_impressoras(self) -> None:
        impressoras = self.printer_service.listar_impressoras()
        if not impressoras:
            impressoras = ["(nenhuma encontrada)"]
        self.cb_impressora.configure(values=impressoras)
        padrao = self.printer_service.impressora_padrao() or self.settings.printer_default
        if padrao and padrao in impressoras:
            self.cb_impressora.set(padrao)
        else:
            self.cb_impressora.set(impressoras[0])

    def _popular_modelos(self) -> None:
        modelos = self.label_store.listar()
        self._nome_modelo_por_id = {m.id: m.nome for m in modelos}
        self._id_modelo_por_nome = {m.nome: m.id for m in modelos}
        self.cb_modelo.configure(values=[m.nome for m in modelos])
        self.cb_modelo.set(self.label_store.ativo().nome)

    def _on_modelo_selecionado(self, nome: str) -> None:
        model_id = self._id_por_nome_modelo(nome)
        if model_id:
            self.label_store.set_ativo(model_id)
            m = self.label_store.ativo()
            self.log_status("info", f"Modelo de etiqueta: {m.nome} ({m.modo}).")
            # Atualiza o preview da linha selecionada para o novo modelo.
            self._on_selecionar_linha()

    def _id_por_nome_modelo(self, nome: str) -> str | None:
        return getattr(self, "_id_modelo_por_nome", {}).get(nome)

    def _abrir_config(self) -> None:
        LabelConfigDialog(
            self,
            store=self.label_store,
            on_change=self._popular_modelos,
            on_test=self._imprimir_teste,
        )

    def log_status(self, level: str, mensagem: str) -> None:
        ts = datetime.now().strftime("%H:%M:%S")
        prefixo = {"info": "[i]", "ok": "[OK]", "erro": "[X]", "aviso": "[!]"}.get(level, "[ ]")
        linha = f"{ts} {prefixo} {mensagem}\n"
        self.log_box.configure(state="normal")
        self.log_box.insert("end", linha)
        self._log_lines += 1
        # Rotaciona o buffer para impedir crescimento ilimitado.
        if self._log_lines > MAX_LOG_LINES:
            excedente = self._log_lines - MAX_LOG_LINES
            self.log_box.delete("1.0", f"{excedente + 1}.0")
            self._log_lines = MAX_LOG_LINES
        self.log_box.see("end")
        self.log_box.configure(state="disabled")
        log.log({"erro": 40, "aviso": 30, "ok": 20, "info": 20}.get(level, 20), mensagem)

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        estado_processar = "disabled" if busy or not self._arquivo_atual else "normal"
        estado_anexar = "disabled" if busy else "normal"
        estado_imprimir = "disabled" if busy or not self._lote_bytes else "normal"
        self.btn_processar.configure(state=estado_processar)
        self.btn_anexar.configure(state=estado_anexar)
        self.btn_imprimir.configure(state=estado_imprimir)
        # OCR so com lote processado, fora de processamento e com Tesseract.
        ocr_ok = (not busy) and bool(self._etiquetas_atuais) and ocr_seller.is_available()
        self.btn_ocr.configure(state="normal" if ocr_ok else "disabled")

    # ------------------------------------------------------------------- acoes
    def _on_trocar_modo(self, valor: str) -> None:
        self._modo_preview = valor
        if self._grupos_atuais is not None:
            self._renderizar_preview()

    def _on_anexar(self) -> None:
        if self._busy:
            return
        diretorio_inicial = str(self.settings.default_input_dir) if self.settings.default_input_dir.exists() else None
        caminho = filedialog.askopenfilename(
            title="Selecione o arquivo TXT/ZPL da Shopee",
            initialdir=diretorio_inicial,
            filetypes=[("Arquivos ZPL/TXT", "*.txt *.zpl"), ("Todos", "*.*")],
        )
        if not caminho:
            return
        self._arquivo_atual = Path(caminho)
        self._lote_bytes = None
        self._etiquetas_atuais = []
        self._grupos_atuais = None
        self._iid_etiqueta = {}
        self._et_selecionada = None
        self.btn_interpretar.configure(state="disabled")
        self.btn_ocr.configure(state="disabled")
        self.tree.delete(*self.tree.get_children())
        self._mostrar_imagem(None, "Selecione uma linha para ver o sticker.")
        self.lbl_arquivo.configure(text=str(self._arquivo_atual))
        self.btn_processar.configure(state="normal")
        self.btn_imprimir.configure(state="disabled")
        self.log_status("info", f"Arquivo selecionado: {self._arquivo_atual.name}")

    def _progresso_parse(self, mensagem: str) -> None:
        self.lbl_resumo.configure(text=mensagem)

    def _on_processar(self) -> None:
        if self._busy or not self._arquivo_atual:
            return
        arquivo = self._arquivo_atual
        self._set_busy(True)
        self.lbl_resumo.configure(text=f"Processando {arquivo.name}...")
        self.log_status("info", f"Iniciando parse: {arquivo.name}")

        def worker() -> None:
            try:
                # Le os bytes UMA vez: o mesmo buffer alimenta o parse (preview)
                # e a impressao (pass-through fiel, sem decode/re-encode).
                dados = arquivo.read_bytes()
                etiquetas = self.parser.parse_bytes(dados)
                grupos = self.agrupador.agrupar_por_sku(etiquetas) if etiquetas else None
                self.after(0, self._on_processar_concluido, dados, etiquetas, grupos, None)
            except Exception as exc:  # noqa: BLE001
                log.exception("Erro no parse")
                self.after(0, self._on_processar_concluido, None, None, None, exc)

        threading.Thread(target=worker, name="ParseWorker", daemon=True).start()

    def _on_processar_concluido(self, dados, etiquetas, grupos, erro) -> None:
        self._set_busy(False)
        if erro is not None:
            messagebox.showerror("Erro ao processar arquivo", str(erro))
            self.log_status("erro", f"Falha no parse: {erro}")
            self.lbl_resumo.configure(text="Falha ao processar arquivo.")
            return
        if not etiquetas:
            self.log_status("aviso", "Nenhum bloco ^XA..^XZ encontrado no arquivo.")
            messagebox.showwarning("Nada encontrado", "Nenhuma etiqueta ZPL foi detectada no arquivo.")
            self.lbl_resumo.configure(text="Arquivo sem etiquetas ZPL.")
            return

        self._lote_bytes = dados
        self._etiquetas_atuais = etiquetas
        self._grupos_atuais = grupos
        self._renderizar_preview()

        blocos = sum(g.qtd for g in grupos.values())
        stickers = sum(g.total_stickers for g in grupos.values())
        nome = self._arquivo_atual.name if self._arquivo_atual else "?"
        if stickers != blocos:
            resumo = (
                f"{len(grupos)} SKUs  |  {blocos} etiquetas  |  "
                f"{stickers} stickers  |  Arquivo: {nome}"
            )
        else:
            resumo = f"{len(grupos)} SKUs  |  {blocos} etiquetas  |  Arquivo: {nome}"
        self.lbl_resumo.configure(text=resumo)
        self.btn_imprimir.configure(state="normal")
        # Habilita o pre-preenchimento via OCR se houver Tesseract disponivel.
        if ocr_seller.is_available():
            self.btn_ocr.configure(state="normal")
        self.log_status(
            "ok",
            f"Processado: {len(grupos)} SKUs / {blocos} etiquetas / {stickers} stickers.",
        )

    def _seller_label(self, sku: str) -> str:
        """Cache manual (catalogo) ou '?' — sem sugestao de OCR."""
        return self.catalog.get(sku) or "?"

    def _renderizar_preview(self) -> None:
        # Limpa em bloco — Treeview lida com isso sem o custo dos widgets CTk.
        self.tree.delete(*self.tree.get_children())
        self._iid_etiqueta = {}
        if self._modo_preview == "Individual":
            itens = self._etiquetas_atuais
            total = len(itens)
            exibir = min(total, MAX_PREVIEW_ROWS)
            for i, et in enumerate(itens[:exibir], start=1):
                iid = f"sku::{et.sku}::{i}"
                self._iid_etiqueta[iid] = et
                self.tree.insert(
                    "",
                    "end",
                    iid=iid,
                    values=(f"{i:03d}", self._seller_label(et.sku), et.sku, 1),
                )
            unidade = "stickers"
        else:
            grupos = self._grupos_atuais or {}
            itens = list(grupos.values())
            total = len(itens)
            exibir = min(total, MAX_PREVIEW_ROWS)
            # 1a etiqueta de cada SKU: fonte da imagem do sticker no painel.
            primeira_por_sku: dict[str, object] = {}
            for et in self._etiquetas_atuais:
                if et.sku not in primeira_por_sku:
                    primeira_por_sku[et.sku] = et
            for i, grupo in enumerate(itens[:exibir], start=1):
                iid = f"sku::{grupo.sku}"
                self._iid_etiqueta[iid] = primeira_por_sku.get(grupo.sku)
                self.tree.insert(
                    "",
                    "end",
                    iid=iid,
                    values=(f"{i:03d}", self._seller_label(grupo.sku), grupo.sku, grupo.qtd),
                )
            unidade = "SKUs"
        if total > exibir:
            self.log_status(
                "aviso",
                f"Preview limitado a {exibir} de {total} {unidade} (lote inteiro sera impresso).",
            )

    def _on_editar_seller_sku(self, _event) -> None:
        sel = self.tree.selection()
        if not sel:
            return
        iid = sel[0]
        if not iid.startswith("sku::"):
            return
        sku_numerico = iid.split("::")[1]
        atual = self.catalog.get(sku_numerico) or ""
        novo = simpledialog.askstring(
            "Seller SKU",
            f"SKU Shopee:\n  {sku_numerico}\n\nSeller SKU (vazio remove o mapeamento):",
            initialvalue=atual,
            parent=self,
        )
        if novo is None:
            return  # cancelou
        novo = novo.strip().upper()
        self.catalog.set(sku_numerico, novo)
        # Atualiza tambem as EtiquetaZPL em memoria pra refletir agora.
        for et in self._etiquetas_atuais:
            if et.sku == sku_numerico:
                et.seller_sku = novo
        # Re-renderiza preview pra mostrar o novo valor na coluna.
        self._renderizar_preview()
        self.log_status(
            "ok",
            f"Seller SKU '{novo}' salvo para {sku_numerico} (catalogo: {self.catalog.total()} mapeamentos).",
        )

    def _on_prefill_ocr(self) -> None:
        """Abre o dialogo de pre-preenchimento do Seller SKU via OCR (em lote).

        O OCR e so sugestao (fonte ~6px da Shopee erra); o usuario confere e
        corrige antes de salvar. A impressao usa so o texto confirmado.
        """
        if self._busy or not self._etiquetas_atuais:
            return
        if not ocr_seller.is_available():
            messagebox.showwarning(
                "OCR indisponivel",
                ocr_seller.unavailable_reason() or "OCR indisponivel.",
            )
            return
        # 1a etiqueta (com sticker) de cada SKU -> recorte do Seller SKU.
        itens: list[tuple[str, Image.Image, str]] = []
        vistos: set[str] = set()
        for et in self._etiquetas_atuais:
            st = et.metadados.get("sticker")
            folha = et.metadados.get("imagem_folha")
            if st is None or folha is None or et.sku in vistos:
                continue
            vistos.add(et.sku)
            crop = grf_decoder.crop_seller_sku(folha, st)
            itens.append((et.sku, crop, self.catalog.get(et.sku) or ""))
        if not itens:
            messagebox.showinfo(
                "Nada a mapear", "Nenhum sticker com QR para pre-preencher."
            )
            return
        OcrBatchDialog(
            self,
            itens=itens,
            ocr_func=ocr_seller.ocr_seller,
            on_save=self._on_ocr_confirmado,
        )

    def _on_ocr_confirmado(self, confirmados: dict[str, str]) -> None:
        """Persiste os Seller SKUs confirmados no lote e atualiza preview."""
        for sku, seller in confirmados.items():
            self.catalog.set(sku, seller)
        # Reflete nas EtiquetaZPL em memoria pra o preview/impressao usarem agora.
        for et in self._etiquetas_atuais:
            valor = self.catalog.get(et.sku)
            if valor:
                et.seller_sku = valor
        self._renderizar_preview()
        self.log_status(
            "ok",
            f"OCR: {len(confirmados)} Seller SKUs confirmados "
            f"(catalogo: {self.catalog.total()} mapeamentos).",
        )

    def _impressora_valida(self) -> str | None:
        impressora = self.cb_impressora.get()
        if not impressora or impressora.startswith("("):
            messagebox.showwarning("Impressora", "Selecione uma impressora valida.")
            return None
        return impressora

    def _on_imprimir(self) -> None:
        if self._busy or not self._lote_bytes:
            return
        impressora = self._impressora_valida()
        if not impressora:
            return
        lote_id = datetime.now().strftime("%Y%m%d%H%M%S")
        modelo = self.label_store.ativo()

        if modelo.modo == MODO_PASS_THROUGH:
            # Fiel: envia os bytes ORIGINAIS do arquivo (sem re-render/re-encode).
            job = PrintJob(printer_name=impressora, job_name=f"Lote-{lote_id}", zpl_content=self._lote_bytes)
            self.log_status("info", f"Lote {lote_id} (fiel) enviado para fila de impressao.")
        else:
            etiquetas = self._etiquetas_atuais
            sem_qr = sum(1 for et in etiquetas if et.metadados.get("sticker") is None)
            if sem_qr:
                self.log_status("aviso", f"{sem_qr} etiqueta(s) sem QR nao serao compostas.")

            # Compor no worker (off-UI): recorta QRs/textos e monta o ZPL ^GFA.
            def builder() -> str:
                zpl, n, ign = label_renderer.gerar_zpl_de_etiquetas(etiquetas, modelo, lote_id=lote_id)
                return zpl

            job = PrintJob(printer_name=impressora, job_name=f"Lote-{lote_id}", builder=builder)
            self.log_status(
                "info", f"Lote {lote_id} (composto: {modelo.nome}) enviado para fila de impressao."
            )
        self.worker.submit(job)

    def _imprimir_teste(self, modelo) -> None:
        """Imprime UMA etiqueta de teste com o modelo (calibracao de alinhamento)."""
        impressora = self._impressora_valida()
        if not impressora:
            return
        if modelo.modo == MODO_PASS_THROUGH:
            messagebox.showinfo(
                "Teste", "Modo fiel imprime o arquivo original; nao ha etiqueta de teste.", parent=self
            )
            return
        amostra = next((et for et in self._etiquetas_atuais if et.metadados.get("sticker") is not None), None)
        if amostra is None:
            messagebox.showinfo(
                "Teste", "Processe um arquivo da Shopee antes de imprimir o teste.", parent=self
            )
            return

        def builder() -> str:
            item = label_renderer._item_etiqueta(amostra)
            img = label_renderer.compor_linha([item], modelo)
            return label_renderer.gerar_zpl([img], modelo, lote_id="TESTE")

        self.worker.submit(PrintJob(printer_name=impressora, job_name="Teste-Etiqueta", builder=builder))
        self.log_status("info", f"Etiqueta de teste enviada ({modelo.nome}).")

    # ----------------------------------------------------------- painel imagem
    def _on_selecionar_linha(self, _event=None) -> None:
        sel = self.tree.selection()
        if not sel:
            return
        et = self._iid_etiqueta.get(sel[0])
        if et is None:
            return
        self._et_selecionada = et
        # Botao "Interpretar ZPL" disponivel sempre que ha ZPL bruto + Node ok.
        tem_zpl = bool(getattr(et, "zpl_raw", "").strip())
        self.btn_interpretar.configure(
            state="normal" if (tem_zpl and zpl_renderer.is_available()) else "disabled"
        )
        folha = et.metadados.get("imagem_folha")
        if folha is None:
            # Sem bitmap GRF (ZPL "texto puro"): antes ficava cego. Agora
            # interpretamos o ZPL de verdade via Node (texto, barcodes, QR...).
            if tem_zpl and zpl_renderer.is_available():
                self._render_zpl_async(et.zpl_raw, f"ZPL interpretado  -  SKU {et.sku}", self.label_store.ativo())
            else:
                motivo = zpl_renderer.unavailable_reason() or "Sem imagem para este formato."
                self._mostrar_imagem(None, motivo)
            return
        st = et.metadados.get("sticker")
        modelo = self.label_store.ativo()
        if st is not None and modelo.modo != MODO_PASS_THROUGH:
            # Preview = etiqueta JA composta no modelo ativo (igual ao que imprime).
            img = label_renderer.preview_etiqueta(et, modelo)
            legenda = f"{modelo.nome}  -  SKU {et.sku}"
        elif st is not None:
            img = grf_decoder.crop_sticker(folha, st)
            legenda = f"SKU {et.sku}  -  folha {et.metadados.get('grf_indice', '?')} (fiel 10x15)"
        else:
            img = folha  # QR nao detectado: mostra a folha inteira
            legenda = f"{et.sku}  -  folha inteira (QR nao detectado)"
        self._mostrar_imagem(img, legenda)

    def _on_interpretar_zpl(self) -> None:
        """Interpreta o ZPL via Node (zpl-renderer-js) para conferir o resultado.

        - Modo COMPOSTO: interpreta o ZPL que o app GERA para esta etiqueta (=
          o que sera impresso, igual ao preview). O ``zpl_raw`` bruto e a folha
          10x15 inteira e, renderizado na etiqueta pequena, sairia cortado e com
          QRs duplicados.
        - Modo PASS-THROUGH: interpreta o ``zpl_raw`` original (folha 10x15 fiel).
        """
        et = self._et_selecionada
        if et is None:
            return
        modelo = self.label_store.ativo()
        if modelo.modo != MODO_PASS_THROUGH and et.metadados.get("sticker") is not None:
            zpl = label_renderer.gerar_zpl_preview_etiqueta(et, modelo)
            if zpl is None:
                self._mostrar_imagem(None, "Etiqueta sem QR para compor.")
                return
            self._render_zpl_async(zpl, f"ZPL composto interpretado  -  SKU {et.sku}", modelo)
            return
        if not getattr(et, "zpl_raw", "").strip():
            return
        self._render_zpl_async(et.zpl_raw, f"ZPL interpretado  -  SKU {et.sku}", modelo)

    def _render_zpl_async(self, zpl: str, legenda: str, modelo) -> None:
        """Interpreta o ``zpl`` fora da UI (subprocess Node leva ~1s) e mostra a
        imagem. Um token sequencial descarta resultados de selecoes antigas."""
        self._preview_seq += 1
        seq = self._preview_seq
        self._mostrar_imagem(None, "Interpretando ZPL...")

        def worker() -> None:
            try:
                img = zpl_renderer.render_for_model(zpl, modelo)
                self.after(0, self._concluir_render_zpl, seq, img, legenda, None)
            except Exception as exc:  # noqa: BLE001 (RendererError e afins)
                self.after(0, self._concluir_render_zpl, seq, None, legenda, exc)

        threading.Thread(target=worker, name="ZplRenderWorker", daemon=True).start()

    def _concluir_render_zpl(self, seq: int, img, legenda: str, erro) -> None:
        if seq != self._preview_seq:
            return  # selecao mudou enquanto renderizava — descarta resultado antigo
        if erro is not None:
            self._mostrar_imagem(None, f"Falha ao interpretar ZPL: {erro}")
            self.log_status("erro", f"Render ZPL: {erro}")
            return
        self._mostrar_imagem(img, legenda)

    def _mostrar_imagem(self, img, legenda: str) -> None:
        if img is None:
            self._tk_img_atual = None
            self.lbl_imagem.configure(image="", text=legenda)
            self.lbl_imagem_info.configure(text="")
            return
        img = img.convert("L")
        w, h = img.size
        # Cabe no painel (~210px de altura, com folga p/ legenda); sem upscale.
        fator = min(170 / h, 760 / w, 1.0)
        tamanho = (max(1, int(w * fator)), max(1, int(h * fator)))
        # tk.Label nao reescala sozinho (CTkImage reescalava via size): fazemos o
        # resize no PIL e guardamos o PhotoImage (referencia viva evita GC).
        # LANCZOS: downscale forte (ate ~7x) sem o borrao do BICUBIC default.
        img = img.resize(tamanho, Image.LANCZOS)
        self._tk_img_atual = ImageTk.PhotoImage(img)
        self.lbl_imagem.configure(image=self._tk_img_atual, text="")
        self.lbl_imagem_info.configure(text=legenda)
