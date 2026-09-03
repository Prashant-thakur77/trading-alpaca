#!/usr/bin/env bash
# Mux each scene's video with its narration, then concatenate into demo.mp4.
#
# Sync by construction: gen_audio.py fixed each scene's audio length first, and
# record.js recorded each video to that length plus a tail. Here each scene is
# cut to EXACTLY the audio duration (video is always the longer stream, so
# trimming it never cuts speech), normalised to one codec/rate/size so the
# concat demuxer joins them without re-timing anything.
set -euo pipefail
cd "$(dirname "$0")"
mkdir -p mux
: > mux/list.txt

for wav in audio/*.wav; do
  id=$(basename "$wav" .wav)
  vid="video/$id.webm"
  [ -f "$vid" ] || { echo "missing $vid"; exit 1; }
  dur=$(ffprobe -v error -show_entries format=duration -of csv=p=0 "$wav")
  ffmpeg -y -v error \
    -i "$vid" -i "$wav" \
    -t "$dur" \
    -map 0:v:0 -map 1:a:0 \
    -vf "scale=1920:1080:flags=lanczos,fps=25,format=yuv420p" \
    -c:v libx264 -preset medium -crf 18 -pix_fmt yuv420p \
    -af "loudnorm=I=-16:TP=-1.5:LRA=11" \
    -c:a aac -b:a 192k -ar 48000 \
    -movflags +faststart \
    "mux/$id.mp4"
  echo "file '$id.mp4'" >> mux/list.txt
  printf "  %-16s %6.1fs\n" "$id" "$dur"
done

cd mux
ffmpeg -y -v error -f concat -safe 0 -i list.txt -c copy ../demo.mp4
cd ..

echo
ffprobe -v error -show_entries format=duration:stream=codec_name,width,height,r_frame_rate,sample_rate \
  -of default=nw=1 demo.mp4 | sed 's/^/  /'
echo "  -> $(pwd)/demo.mp4  ($(du -h demo.mp4 | cut -f1))"
