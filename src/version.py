"""Fonte unica de versao do FullPrint.

O pipeline de release (GitHub Actions) SOBRESCREVE `__version__` com a tag
do git (ex.: tag `v1.2.0` -> `__version__ = "1.2.0"`) no momento do build.
Em desenvolvimento mantenha aqui a proxima versao a ser lancada.
"""
from __future__ import annotations

__version__ = "0.5.0"
