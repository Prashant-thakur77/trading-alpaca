# Demo video build

Reproduces `docs/press/demo.mp4` from this repo's own logs.

    pip install chatterbox-tts playwright && npx playwright install ffmpeg
    python3 -m http.server 8765 --bind 127.0.0.1 &   # snap Chromium cannot read file:// under /tmp
    python3 gen_audio.py       # narration -> audio/*.wav + durations.json  (Chatterbox TTS, GPU)
    node record.js             # each scene recorded to exactly its narration length
    ./compose.sh               # mux + concat -> demo.mp4

`scenes.json` is the single source of truth for both narration and what is on
screen. `scenes/*.txt` are verbatim extracts from `logs/scheduler-*.log`,
`logs/journal.jsonl`, and the `make calibration` / `replay.py --verify` output —
nothing on screen was retyped or mocked.
