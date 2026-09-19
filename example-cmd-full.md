### Script Generator
python3 -m ytscript -t "The Dog Who Waited at the Wrong House" -g \
  --duration 3 --tone "story, sad" --depth deep \
  --art-style "2.5D parallax illustration" --palette "ochre and slate" \
  --keyword aqueduct --keyword legion --avoid "clickbait" \
  -o "/home/kingnit/Desktop/youtube pipline/out/dog-story"
### Audio Generator
python3 tts.py --path '/home/kingnit/Desktop/youtube pipline/out/dog-story/voiceover.md' --reference-id bf322df2096a46f18c579d0baa36f41d --out "/home/kingnit/Desktop/youtube pipline/out/dog-story/audio.mp3"

### Audio Enhancer
uv run enhancer.py "/home/kingnit/Desktop/youtube pipline/out/dog-story/audio.mp3" \
  --output "/home/kingnit/Desktop/youtube pipline/out/dog-story/enhanced_audio.mp3" \
  --preset youtube

### Image Generator
uv run autoimg.py --source "/home/kingnit/Desktop/youtube pipline/out/dog-story/image_prompts.md"

### Subtitle Generator (GPU supported with CPU fallback)
python3 subtitle-gen/generate_subtitles.py \
  --audio "/home/kingnit/Desktop/youtube pipline/out/dog-sad-story/enhanced_audio.mp3" \
  --out "/home/kingnit/Desktop/youtube pipline/out/dog-story/dog-story" \
  --model "subtitle-gen/whisper.cpp/models/ggml-tiny.en.bin" \
  --cli-path "subtitle-gen/whisper.cpp/build/bin/whisper-cli" \
  --gpu true

### Beat Aligner
uv run beatalign.py \
  --beatsource "/home/kingnit/Desktop/youtube pipline/out/dog-sad-story/beat.md" \
  --srtsource "/home/kingnit/Desktop/youtube pipline/out/dog-sad-story/dog-story.srt" \
  --out "/home/kingnit/Desktop/youtube pipline/out/dog-sad-story/beat.json"

### Video Editor (GPU supported with CPU fallback)
python3 editor.py \
  --imagesource "/home/kingnit/Downloads/bulk" \
  --beatpath "/home/kingnit/Desktop/youtube pipline/out/dog-sad-story/beat.json" \
  --animation fadein \
  --out "/home/kingnit/Desktop/youtube pipline/out/dog-sad-story/dog.mp4" \
  --gpu true

### Audio Adder (GPU supported with CPU fallback)
python3 add_audio.py \
  --video "/home/kingnit/Desktop/youtube pipline/out/dog-sad-story/dog.mp4" \
  --audio "/home/kingnit/Desktop/youtube pipline/out/dog-sad-story/enhanced_audio.mp3" \
  --out "/home/kingnit/Desktop/youtube pipline/out/dog-sad-story/final.mp4" \
  --gpu true

### Subtitle Worker (GPU supported with CPU fallback)
python3 subtitle_worker.py \
  --video "/home/kingnit/Desktop/youtube pipline/out/dog-sad-story/final.mp4" \
  --srt "/home/kingnit/Desktop/youtube pipline/out/dog-sad-story/dog-story.srt" \
  --out "/home/kingnit/Desktop/youtube pipline/out/dog-sad-story/final_subtitled.mp4" \
  --style hormozi \
  --animation pop \
  --max-words 3 \
  --gpu true