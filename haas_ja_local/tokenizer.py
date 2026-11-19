"""SentencePieceトークナイザの薄いラッパー。"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

import sentencepiece as spm


class Tokenizer:
    def __init__(self, model: bytes | Path) -> None:
        if isinstance(model, bytes):
            self._sp = spm.SentencePieceProcessor(model_proto=model)
        else:
            self._sp = spm.SentencePieceProcessor(model_file=str(model))
        self.vocab_size: int = self._sp.get_piece_size()

    def decode(self, ids: Sequence[int]) -> str:
        return self._sp.decode([int(i) for i in ids])

    def pieces(self, ids: Sequence[int]) -> list[str]:
        return [self._sp.id_to_piece(int(i)) for i in ids]

    def encode(self, text: str) -> list[int]:
        """テスト補助。推論では使わない。"""
        return list(self._sp.encode(text))
