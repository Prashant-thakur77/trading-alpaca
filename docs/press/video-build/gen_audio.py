#!/usr/bin/env python3
"""Narration via Chatterbox TTS, one WAV per scene, plus durations.json.

Runs FIRST. record.js then cuts every video scene to its narration length,
which is what makes the final mux lag-free by construction.

Long narration is split at sentence boundaries: Chatterbox is at its best on
short, complete sentences, and a natural pause between them reads as a
narrator breathing rather than a model gasping. Chunks are joined with a
short silence.
"""
import json
import re
import sys
from pathlib import Path

import torch
import torchaudio as ta
from chatterbox.tts import ChatterboxTTS

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "audio"
OUT.mkdir(exist_ok=True)

# Calm, steady, technical narrator. exaggeration low = no theatrics;
# cfg_weight moderate = stays close to the text, steady pacing.
EXAGGERATION = 0.38
CFG_WEIGHT = 0.35
GAP_S = 0.40            # silence between sentences
LEAD_S = 0.25           # breath before the first word

only = sys.argv[1] if len(sys.argv) > 1 else None
device = "cuda" if torch.cuda.is_available() else "cpu"
print(f"device: {device}")
model = ChatterboxTTS.from_pretrained(device=device)
sr = model.sr


def sentences(text: str) -> list[str]:
    parts = re.split(r"(?<=[.!?])\s+", text.strip())
    return [p.strip() for p in parts if p.strip()]


def silence(seconds: float) -> torch.Tensor:
    return torch.zeros(1, int(sr * seconds))


scenes = json.loads((ROOT / "scenes.json").read_text())["scenes"]
durations = {}
if (OUT / "durations.json").exists():
    durations = json.loads((OUT / "durations.json").read_text())

for s in scenes:
    if only and s["id"] != only:
        continue
    pieces = [silence(LEAD_S)]
    for i, sent in enumerate(sentences(s["narration"])):
        wav = model.generate(sent, exaggeration=EXAGGERATION, cfg_weight=CFG_WEIGHT)
        if wav.dim() == 1:
            wav = wav.unsqueeze(0)
        pieces.append(wav.cpu())
        pieces.append(silence(GAP_S))
        print(f"  {s['id']}  sentence {i+1}: {wav.shape[-1]/sr:.1f}s")
    audio = torch.cat(pieces, dim=1)
    path = OUT / f"{s['id']}.wav"
    ta.save(str(path), audio, sr)
    durations[s["id"]] = round(audio.shape[-1] / sr, 3)
    print(f"✓ {path.name}  {durations[s['id']]:.1f}s")

(OUT / "durations.json").write_text(json.dumps(durations, indent=2))
total = sum(durations.values())
print(f"\ntotal narration: {total:.0f}s  ({total/60:.1f} min)")
