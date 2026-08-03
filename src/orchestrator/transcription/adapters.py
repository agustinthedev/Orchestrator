from __future__ import annotations

import os
from pathlib import Path
from typing import Protocol


class Transcriber(Protocol):
    async def transcribe(self, audio_path: Path) -> str: ...


class NullTranscriber:
    async def transcribe(self, audio_path: Path) -> str:
        raise RuntimeError("Voice transcription is disabled or not configured")


class OpenAITranscriber:
    def __init__(self, api_key_env: str, model: str) -> None:
        self.api_key_env = api_key_env
        self.model = model

    async def transcribe(self, audio_path: Path) -> str:
        api_key = os.getenv(self.api_key_env)
        if not api_key:
            raise RuntimeError(f"Transcription credential is not configured: {self.api_key_env}")
        try:
            from openai import AsyncOpenAI
        except ImportError as exc:
            raise RuntimeError("Install the transcription extra to use OpenAI transcription") from exc
        client = AsyncOpenAI(api_key=api_key)
        with audio_path.open("rb") as audio:
            result = await client.audio.transcriptions.create(model=self.model, file=audio)
        return str(result.text)

