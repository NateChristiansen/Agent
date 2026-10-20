# ruff: noqa
from __future__ import annotations


class PeerNetError(Exception):
    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail
