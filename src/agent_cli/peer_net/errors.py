"""The receiver's one error type. Its detail is a fixed string, a field name at most, because
the response crosses the network to another device: never input text, paths or exception text."""

from __future__ import annotations


class PeerNetError(Exception):
    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail
