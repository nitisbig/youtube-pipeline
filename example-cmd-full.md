### Script Generator
python3 -m ytscript -t "The Dog Who Waited at the Wrong House" -g \
  --duration 3 --tone "story, sad" --depth deep \
  --art-style "2.5D parallax illustration" --palette "ochre and slate" \
  --keyword aqueduct --keyword legion --avoid "clickbait" \
  -o "/home/kingnit/Desktop/youtube pipline/out/dog-story"
### Audio Generator
python3 tts.py --path '/home/kingnit/Desktop/youtube pipline/out/dog-story/voiceover.md' --reference-id bf322df2096a46f18c579d0baa36f41d --out "/home/kingnit/Desktop/youtube pipline/out/dog-story/audio.mp3"

### Image Generator
uv run autoimg.py --source "/home/kingnit/Desktop/youtube pipline/out/dog-story/image_prompts.md"

### Subtitle Generator
tmp=$(mktemp --suffix=.wav) && ffmpeg -loglevel error -y   -i "/home/kingnit/Desktop/youtube pipline/out/dog-sad-story/audio.mp3"   -ar 16000 -ac 1 -c:a pcm_s16le "$tmp" && ./build/bin/whisper-cli   -m models/ggml-tiny.en.bin   -f "$tmp"   -osrt   -of "/home/kingnit/Desktop/youtube pipline/out/dog-story" ; rm -f "$tmp"

### Beat Aligner
uv run beatalign.py \
  --beatsource "/home/kingnit/Desktop/youtube pipline/out/dog-sad-story/beat.md" \
  --srtsource "/home/kingnit/Desktop/youtube pipline/out/dog-sad-story/dog-story.srt" \
  --out "/home/kingnit/Desktop/youtube pipline/out/dog-sad-story/beat.json"

### Video Editor
python3 editor.py   --imagesource "/home/kingnit/Downloads/bulk"   --beatpath "/home/kingnit/Desktop/youtube pipline/out/dog-sad-story/beat.json"   --animation fadein   --out "/home/kingnit/Desktop/youtube pipline/out/dog-sad-story/dog.mp4"

### Audio Adder
python3 add_audio.py \
  --video "/home/kingnit/Desktop/youtube pipline/out/dog-sad-story/dog.mp4" \
  --audio "/home/kingnit/Desktop/youtube pipline/out/dog-sad-story/audio.mp3" \
  --out "/home/kingnit/Desktop/youtube pipline/out/dog-sad-story/final.mp4"