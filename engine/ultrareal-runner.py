from __future__ import annotations

import argparse
import gc
import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path('/kaggle/working') if Path('/kaggle/working').exists() else Path.cwd()
WORK = ROOT / 'oddproof-ultrareal-work'
OUT = ROOT / 'oddproof-output'
CACHE = ROOT / 'oddproof-model-cache'
SKY = WORK / 'SkyReels-V3'


def run(cmd, cwd=None, env=None):
    print('+', ' '.join(map(str, cmd)), flush=True)
    subprocess.run(cmd, cwd=cwd, env=env, check=True)


def out(cmd):
    return subprocess.check_output(cmd, text=True).strip()


def pip(*args):
    run([sys.executable, '-m', 'pip', *args])


def gpu_info():
    raw = out(['nvidia-smi', '--query-gpu=name,memory.total', '--format=csv,noheader,nounits'])
    rows = [x.strip() for x in raw.splitlines() if x.strip()]
    if not rows:
        raise SystemExit('NO_NVIDIA_GPU')
    first = rows[0].rsplit(',', 1)
    return {'name': first[0].strip(), 'vram_mib': int(first[1].strip()), 'gpu_count': len(rows)}


def install_runtime():
    os.environ.setdefault('HF_HOME', str(CACHE / 'huggingface'))
    os.environ.setdefault('PYTORCH_CUDA_ALLOC_CONF', 'expandable_segments:True')
    CACHE.mkdir(parents=True, exist_ok=True)
    WORK.mkdir(parents=True, exist_ok=True)
    OUT.mkdir(parents=True, exist_ok=True)
    run(['bash', '-lc', 'command -v ffmpeg >/dev/null || (apt-get -qq update && apt-get -qq -y install ffmpeg)'])
    run(['bash', '-lc', 'command -v espeak-ng >/dev/null || (apt-get -qq update && apt-get -qq -y install espeak-ng)'])
    pip('install', '-q', '-U', 'pip', 'setuptools', 'wheel', 'packaging', 'ninja')
    pip('install', '-q', '-U', 'diffusers', 'transformers', 'accelerate', 'safetensors', 'huggingface_hub', 'soundfile', 'kokoro>=0.9.4', 'opencv-python-headless', 'av')
    if not SKY.exists():
        run(['git', 'clone', '--depth', '1', 'https://github.com/SkyworkAI/SkyReels-V3.git', str(SKY)])

    # SkyReels imports xFuser helpers at module import time even when sequence
    # parallelism is disabled. OddProofLab uses single-GPU --low_vram mode,
    # so xFuser is not needed at runtime. Make that optional instead of
    # failing on Kaggle Python 3.12 where the pinned wheel is unavailable.
    xfuser_import = 'from xfuser.core.distributed import get_sp_group'
    xfuser_fallback = (
        'try:\n'
        '    from xfuser.core.distributed import get_sp_group\n'
        'except ImportError:\n'
        '    def get_sp_group():\n'
        '        raise RuntimeError("xFuser unavailable; sequence parallelism is disabled in OddProofLab single-GPU mode")'
    )
    for py in SKY.rglob('*.py'):
        text = py.read_text()
        if xfuser_import in text:
            py.write_text(text.replace(xfuser_import, xfuser_fallback))
            print('PATCHED_OPTIONAL_XFUSER', py)

    req = WORK / 'skyreels-requirements.txt'
    lines = []
    for line in (SKY / 'requirements.txt').read_text().splitlines():
        s = line.strip().lower()
        if s.startswith('torch==') or s.startswith('torchvision==') or s.startswith('flash_attn==') or s.startswith('flash-attn==') or s.startswith('xfuser==') or s.startswith('xfuser>='):
            continue
        lines.append(line)
    req.write_text('\n'.join(lines) + '\n')
    pip('install', '-q', '-r', str(req))
    run([sys.executable, '-m', 'pip', 'cache', 'purge'])
    run(['bash', '-lc', 'rm -rf /var/lib/apt/lists/* /tmp/* || true'])
    print('PIP_CACHE_PURGED=1', flush=True)
    free_disk_space('AFTER_RUNTIME')


def free_disk_space(label):
    usage = shutil.disk_usage(ROOT)
    free_gib = usage.free / (1024 ** 3)
    print(f'DISK_FREE_{label}={free_gib:.2f}GiB', flush=True)
    return free_gib


def clean_output():
    if OUT.exists():
        shutil.rmtree(OUT)
    OUT.mkdir(parents=True, exist_ok=True)


def validate_manifest(m):
    if not m.get('slug') or not m.get('title'):
        raise SystemExit('MANIFEST_MISSING_IDENTITY')
    scenes = m.get('scenes', [])
    if len(scenes) != 5:
        raise SystemExit(f'EXPECTED_EXACTLY_5_SCENES_GOT_{len(scenes)}')
    if not m.get('start_frame_prompt'):
        raise SystemExit('MISSING_START_FRAME_PROMPT')
    if not m.get('narration'):
        raise SystemExit('MISSING_NARRATION')
    captions = m.get('captions', [])
    if len(captions) != 5:
        raise SystemExit(f'EXPECTED_5_CAPTIONS_GOT_{len(captions)}')
    for i, scene in enumerate(scenes, 1):
        p = scene.get('prompt', '')
        if len(p) < 120:
            raise SystemExit(f'SCENE_{i}_PROMPT_TOO_WEAK')
        low = p.lower()
        for banned in ['diagram', 'infographic', 'cartoon style', 'static illustration', 'slideshow']:
            if banned in low and ('no ' + banned) not in low:
                raise SystemExit(f'SCENE_{i}_BANNED_VISUAL_MODE_{banned}')


def make_start_frame(m):
    import torch
    from diffusers import StableDiffusionXLPipeline
    prompt = m['start_frame_prompt']
    negative = m.get('negative_prompt', 'cgi, 3d render, illustration, cartoon, diagram, plastic, waxy, text, caption, watermark, deformed anatomy, duplicated objects, low detail, oversharpened')
    pipe = StableDiffusionXLPipeline.from_pretrained(
        'stabilityai/stable-diffusion-xl-base-1.0',
        torch_dtype=torch.float16,
        variant='fp16',
        use_safetensors=True,
    )
    pipe.enable_model_cpu_offload()
    image = pipe(
        prompt=prompt,
        negative_prompt=negative,
        width=576,
        height=1024,
        num_inference_steps=32,
        guidance_scale=6.5,
        generator=torch.Generator(device='cpu').manual_seed(int(m.get('start_frame_seed', 74001))),
    ).images[0]
    path = OUT / 'reference-01.png'
    image.save(path)
    del pipe
    gc.collect()
    torch.cuda.empty_cache()
    return path


def newest_mp4(path, before):
    after = set(path.glob('*.mp4'))
    created = sorted(after - before, key=lambda p: p.stat().st_mtime, reverse=True)
    if created:
        return created[0]
    existing = sorted(after, key=lambda p: p.stat().st_mtime, reverse=True)
    if existing:
        return existing[0]
    raise RuntimeError(f'NO_MP4_IN_{path}')


def generate_scene(reference, scene, index, duration=6):
    result_dir = SKY / 'result' / 'reference_to_video'
    result_dir.mkdir(parents=True, exist_ok=True)
    before = set(result_dir.glob('*.mp4'))
    env = os.environ.copy()
    env['PYTORCH_CUDA_ALLOC_CONF'] = 'expandable_segments:True'
    cmd = [
        sys.executable, str(SKY / 'generate_video.py'),
        '--task_type', 'reference_to_video',
        '--ref_imgs', str(reference),
        '--prompt', scene['prompt'],
        '--duration', str(int(scene.get('duration_seconds', duration))),
        '--resolution', '540P',
        '--seed', str(int(scene.get('seed', 75000 + index))),
        '--offload', '--low_vram',
    ]
    run(cmd, cwd=SKY, env=env)
    source = newest_mp4(result_dir, before)
    target = OUT / f'scene-{index:02d}.mp4'
    shutil.copy2(source, target)
    if target.stat().st_size < 250000:
        raise RuntimeError(f'SCENE_{index}_OUTPUT_TOO_SMALL')
    return target


def extract_last_frame(video, index):
    dst = OUT / f'reference-{index + 1:02d}.png'
    run(['ffmpeg', '-y', '-loglevel', 'error', '-sseof', '-0.08', '-i', str(video), '-frames:v', '1', str(dst)])
    if not dst.exists() or dst.stat().st_size < 10000:
        raise RuntimeError(f'LAST_FRAME_EXTRACTION_FAILED_{index}')
    return dst


def motion_score(video):
    import cv2
    import numpy as np
    cap = cv2.VideoCapture(str(video))
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    if total < 20:
        cap.release(); return 0.0
    indices = [int(total * x) for x in (0.10, 0.30, 0.50, 0.70, 0.90)]
    frames = []
    for idx in indices:
        cap.set(cv2.CAP_PROP_POS_FRAMES, idx)
        ok, f = cap.read()
        if ok:
            frames.append(cv2.resize(cv2.cvtColor(f, cv2.COLOR_BGR2GRAY), (256, 144)))
    cap.release()
    if len(frames) < 4:
        return 0.0
    diffs = [float(np.mean(cv2.absdiff(a, b))) for a, b in zip(frames, frames[1:])]
    return sum(diffs) / len(diffs)


def video_meta(path):
    return json.loads(out(['ffprobe', '-v', 'error', '-show_streams', '-show_format', '-of', 'json', str(path)]))


def generate_voice(m, target=29.50):
    import numpy as np
    import soundfile as sf
    from kokoro import KPipeline
    pipeline = KPipeline(lang_code='a')
    voice_name = m.get('voice', 'af_heart')
    def synth(speed):
        chunks = []
        for _, _, audio in pipeline(m['narration'], voice=voice_name, speed=float(speed)):
            if audio is not None:
                chunks.append(audio.detach().cpu().numpy() if hasattr(audio, 'detach') else np.asarray(audio))
        if not chunks:
            raise RuntimeError('KOKORO_RETURNED_NO_AUDIO')
        audio = np.concatenate(chunks)
        tmp = OUT / 'voice-temp.wav'
        sf.write(tmp, audio, 24000)
        d = float(out(['ffprobe', '-v', 'error', '-show_entries', 'format=duration', '-of', 'default=nw=1:nk=1', str(tmp)]))
        return tmp, d
    _, d0 = synth(1.0)
    speed = max(0.90, min(1.12, d0 / target))
    temp, d1 = synth(speed)
    final = OUT / 'voice.wav'
    shutil.move(temp, final)
    if not 28.8 <= d1 <= 29.65:
        raise RuntimeError(f'NATURAL_VOICE_DURATION_FAILED duration={d1:.3f} speed={speed:.3f}; rewrite narration instead of time-stretching')
    return final, d1, speed, voice_name


def stamp(sec):
    ms = int(round(sec * 1000)); h, r = divmod(ms, 3600000); mi, r = divmod(r, 60000); s, ms = divmod(r, 1000)
    return f'{h:02d}:{mi:02d}:{s:02d},{ms:03d}'


def build_candidate(m, scenes, voice, voice_duration):
    clip_list = OUT / 'clips.txt'
    clip_list.write_text(''.join(f"file '{p.name}'\n" for p in scenes))
    raw = OUT / 'visual-raw.mp4'
    run(['ffmpeg', '-y', '-loglevel', 'error', '-f', 'concat', '-safe', '0', '-i', str(clip_list), '-t', '29.8', '-an', '-c:v', 'libx264', '-preset', 'medium', '-crf', '16', '-pix_fmt', 'yuv420p', str(raw)], cwd=OUT)
    srt = OUT / 'captions.srt'
    blocks = []
    for i, text in enumerate(m['captions'], 1):
        a = (i - 1) * 5.96
        b = min(i * 5.96 - 0.08, 29.72)
        blocks.append(f'{i}\n{stamp(a)} --> {stamp(b)}\n{text}\n')
    srt.write_text('\n'.join(blocks))
    candidate = OUT / 'candidate.mp4'
    filt = "[0:v]scale=1080:1920:flags=lanczos,fps=30,subtitles=captions.srt:force_style='FontName=DejaVu Sans,FontSize=15,Bold=1,PrimaryColour=&H00FFFFFF,OutlineColour=&HCC000000,Outline=3,Shadow=1,Alignment=2,MarginV=32',drawtext=fontfile=/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf:text='OP':fontsize=28:fontcolor=white@0.12:x=34:y=46[v]"
    run(['ffmpeg', '-y', '-loglevel', 'error', '-i', str(raw), '-i', str(voice), '-filter_complex', filt, '-map', '[v]', '-map', '1:a', '-t', '29.8', '-c:v', 'libx264', '-preset', 'medium', '-crf', '16', '-pix_fmt', 'yuv420p', '-c:a', 'aac', '-b:a', '192k', '-movflags', '+faststart', str(candidate)], cwd=OUT)
    meta = video_meta(candidate)
    vs = next(s for s in meta['streams'] if s['codec_type'] == 'video')
    au = next(s for s in meta['streams'] if s['codec_type'] == 'audio')
    duration = float(meta['format']['duration'])
    tail = duration - voice_duration
    if (int(vs['width']), int(vs['height'])) != (1080, 1920): raise RuntimeError('FINAL_RESOLUTION_FAILED')
    if vs['codec_name'] != 'h264' or au['codec_name'] != 'aac': raise RuntimeError('FINAL_CODEC_FAILED')
    if not 29.70 <= duration <= 29.90: raise RuntimeError(f'FINAL_DURATION_FAILED_{duration}')
    if not 0.15 <= tail <= 1.0: raise RuntimeError(f'VOICE_TAIL_FAILED_{tail}')
    return candidate, duration, tail


def sheets(candidate):
    run(['ffmpeg','-y','-loglevel','error','-i',str(candidate),'-vf','trim=start=0:end=1.25,fps=4,scale=216:384,tile=5x1:padding=4:margin=4','-frames:v','1',str(OUT/'hook-sheet.jpg')])
    run(['ffmpeg','-y','-loglevel','error','-i',str(candidate),'-vf','fps=1/3.72,scale=216:384,tile=4x2:padding=4:margin=4','-frames:v','1',str(OUT/'contact-sheet.jpg')])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--manifest', required=True)
    args = ap.parse_args()
    m = json.loads(Path(args.manifest).read_text())
    validate_manifest(m)
    info = gpu_info()
    print('GPU', json.dumps(info), flush=True)
    if info['vram_mib'] < 14500:
        raise SystemExit(f'GPU_VRAM_TOO_LOW_FOR_ULTRAREAL_PIPELINE_{info["vram_mib"]}MiB')
    clean_output()
    install_runtime()
    ref = make_start_frame(m)

    # The SDXL checkpoint is only needed for the first reference frame.
    # Remove it before SkyReels downloads/loads its own model so Kaggle's
    # /kaggle/working disk is not filled by two large model families at once.
    sdxl_cache = CACHE / 'huggingface' / 'hub' / 'models--stabilityai--stable-diffusion-xl-base-1.0'
    if sdxl_cache.exists():
        shutil.rmtree(sdxl_cache)
        print('SDXL_CACHE_REMOVED=1', flush=True)
    gc.collect()
    free_disk_space('BEFORE_SKYREELS')

    scenes = []
    scene_scores = []
    for i, scene in enumerate(m['scenes'], 1):
        vid = generate_scene(ref, scene, i, duration=6)
        score = motion_score(vid)
        print(f'SCENE_{i}_MOTION_SCORE={score:.3f}', flush=True)
        if score < float(m.get('minimum_motion_score', 2.0)):
            raise RuntimeError(f'SCENE_{i}_MOTION_QA_FAILED score={score:.3f}')
        scenes.append(vid)
        scene_scores.append(score)
        if i < 5:
            ref = extract_last_frame(vid, i)
    voice, vd, speed, voice_name = generate_voice(m)
    candidate, duration, tail = build_candidate(m, scenes, voice, vd)
    sheets(candidate)
    sha = hashlib.sha256(candidate.read_bytes()).hexdigest()
    result = {
        'slug': m['slug'], 'title': m['title'], 'candidate_sha256': sha,
        'duration_seconds': duration, 'voice_duration_seconds': vd, 'audio_tail_seconds': tail,
        'video_backend': 'SkyReels-V3-Reference2Video-low-vram',
        'start_frame_backend': 'SDXL-1.0', 'voice_backend': 'Kokoro-82M', 'voice': voice_name, 'voice_speed': speed,
        'scene_motion_scores': scene_scores, 'gpu': info,
        'technical_qa': 'PASS', 'motion_qa': 'PASS', 'semantic_visual_qa': 'REQUIRED_BEFORE_PUBLISH',
        'publish_allowed': False,
        'quality_policy': 'True generated video only. Static-image animation, procedural CGI, LTX-2B fallback and robotic Edge TTS are not publishable.'
    }
    (OUT / 'generation.json').write_text(json.dumps(result, indent=2) + '\n')
    print('CANDIDATE_SHA256=' + sha)
    print('TECHNICAL_QA=PASS')
    print('MOTION_QA=PASS')
    print('SEMANTIC_VISUAL_QA=REQUIRED_BEFORE_PUBLISH')

if __name__ == '__main__':
    main()
