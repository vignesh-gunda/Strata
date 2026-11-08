"""The complete tool surface an agent is allowed to use.

Note what is absent: there is no tool that opens `bin/`, and nothing that
reaches outside `dig-site/`.
"""
from __future__ import annotations

from typing import Any

from .binary import BinaryProcess
from .digsite_fs import DigSiteFS
from .metadata import binary_metadata


class AgentTools:
    def __init__(self, fs: DigSiteFS, binary: BinaryProcess | None = None):
        self._fs = fs
        self._binary = binary or BinaryProcess(fs)

    def read_file(self, path: str) -> str:
        return self._fs.read_text(path)

    def list_dir(self, path: str = ".") -> list[str]:
        return self._fs.listdir(path)

    def binary_metadata(self) -> dict[str, Any]:
        return binary_metadata(self._fs)

    def start_binary(self) -> None:
        self._binary.start()

    def send_request(self, method: str, params: dict[str, Any] | None = None) -> Any:
        return self._binary.send_request(method, params)

    def close(self) -> None:
        self._binary.close()
