"""Audio plumbing: frame arithmetic, sources, sinks, deterministic synthetic audio."""

from .frames import (
    INT16_FULL_SCALE,
    SILENCE_DBFS,
    PreRollBuffer,
    bytes_for_ms,
    float32_to_pcm,
    frame_duration_ms,
    frames_for_ms,
    frames_from_pcm,
    ms_for_frames,
    pcm_from_frames,
    pcm_to_float32,
    reseq,
    rms_dbfs,
    silence_pcm,
    total_duration_ms,
    validate_frame,
)
from .sinks import AudioSink, NullSink, PacedSink, SpeakerSink
from .sources import (
    MicrophoneSource,
    QueueFrameSource,
    concat_sources,
    frames_from_iterable,
    frames_from_wav,
    read_wav,
    resample_to_protocol_rate,
    write_wav,
)
from .synth import mix, room_noise_pcm, speech_like_pcm

__all__ = [
    "INT16_FULL_SCALE", "SILENCE_DBFS", "AudioSink", "MicrophoneSource", "NullSink",
    "PacedSink", "PreRollBuffer", "QueueFrameSource", "SpeakerSink", "bytes_for_ms",
    "concat_sources", "float32_to_pcm", "frame_duration_ms", "frames_for_ms",
    "frames_from_iterable", "frames_from_pcm", "frames_from_wav", "mix", "ms_for_frames",
    "pcm_from_frames", "pcm_to_float32", "read_wav", "resample_to_protocol_rate", "reseq",
    "rms_dbfs", "room_noise_pcm", "silence_pcm", "speech_like_pcm", "total_duration_ms",
    "validate_frame", "write_wav",
]
