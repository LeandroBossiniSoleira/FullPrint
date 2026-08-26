"""Janela principal da aplicacao (CustomTkinter)."""
from __future__ import annotations

from pathlib import Path

import customtkinter as ctk

from ..config.settings import Settings
from ..services import updater
from ..services.printer import ZebraPrinterService
from ..services.spooler_worker import PrintQueueManager
from ..utils.logger import get_logger
from ..utils.runtime import is_frozen
from .views.main_view import MainView

log = get_logger("app")


class ShopeeZPLApp(ctk.CTk):
    def __init__(self, settings: Settings) -> None:
        super().__init__()
        self.settings = settings

        ctk.set_appearance_mode(settings.theme)
        ctk.set_default_color_theme(settings.color_theme)

        self.title(f"{settings.app_name} v{settings.app_version}")
        # 720 (e nao 680): a faixa de impressao ocupa duas linhas no rodape e a
        # altura extra volta pro preview -- e' la que o operador escolhe o SKU
        # de retomada. Continua dentro de uma tela 1366x768.
        self.geometry("1100x720")
        self.minsize(960, 600)

        self.printer_service = ZebraPrinterService(
            encoding=settings.printer_encoding,
            dev_mode=settings.printer_dev_mode,
            dev_output_dir=settings.printer_dev_output_dir,
        )

        self.main_view = MainView(self, settings, self.printer_service, worker=None)  # type: ignore[arg-type]
        self.worker = PrintQueueManager(
            self.printer_service,
            status_callback=lambda level, msg: self.after(0, self.main_view.log_status, level, msg),
        )
        self.main_view.worker = self.worker
        self.worker.start()

        self.main_view.pack(fill="both", expand=True)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        # Auto-update silencioso: so faz sentido no app instalado (.exe). Em
        # desenvolvimento (rodando do codigo) nao verifica nada.
        self._pending_installer: Path | None = None
        if is_frozen():
            updater.check_and_download_async(self._on_update_ready)

    def _on_update_ready(self, installer_path: Path) -> None:
        """Callback do updater (roda em thread): registra o instalador baixado.

        A atualizacao e aplicada no fechamento do app, em silencio.
        """
        self.after(0, self._set_pending_installer, installer_path)

    def _set_pending_installer(self, installer_path: Path) -> None:
        self._pending_installer = installer_path
        log.info("Atualizacao pronta; sera aplicada ao fechar o app.")

    def _on_close(self) -> None:
        self.worker.stop()
        if self._pending_installer is not None:
            updater.run_installer_silent(self._pending_installer)
        self.destroy()
