"""Speech synthesis, for tools that answer out loud."""

from miss_quote.tts.cache import SpeechCache, shared_cache
from miss_quote.tts.client import Speech, SynthesisError, synthesize

__all__ = [
    "Speech",
    "SpeechCache",
    "SynthesisError",
    "shared_cache",
    "synthesize",
]
