"""Speech synthesis, for tools that answer out loud."""

from tts.cache import SpeechCache, shared_cache
from tts.client import Speech, SynthesisError, synthesize

__all__ = [
    "Speech",
    "SpeechCache",
    "SynthesisError",
    "shared_cache",
    "synthesize",
]
