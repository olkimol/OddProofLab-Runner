from __future__ import annotations

import importlib.util
from pathlib import Path

HERE = Path(__file__).resolve().parent
BASE = HERE / 'ultrareal-runner.py'

spec = importlib.util.spec_from_file_location('oddproof_ultrareal_base', BASE)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)


def build_candidate_branded(m, scenes, voice, voice_duration):
    clip_list = mod.OUT / 'clips.txt'
    clip_list.write_text(''.join(f"file '{p.name}'\n" for p in scenes))
    raw = mod.OUT / 'visual-raw.mp4'
    mod.run(['ffmpeg','-y','-loglevel','error','-f','concat','-safe','0','-i',str(clip_list),'-t','29.8','-an','-c:v','libx264','-preset','medium','-crf','16','-pix_fmt','yuv420p',str(raw)], cwd=mod.OUT)

    srt = mod.OUT / 'captions.srt'
    blocks=[]
    for i,text in enumerate(m['captions'],1):
        a=(i-1)*5.96
        b=min(i*5.96-0.08,29.72)
        blocks.append(f'{i}\n{mod.stamp(a)} --> {mod.stamp(b)}\n{text}\n')
    srt.write_text('\n'.join(blocks))

    candidate = mod.OUT / 'candidate.mp4'
    filt = (
        "[0:v]scale=1080:1920:flags=lanczos,fps=30,"
        "subtitles=captions.srt:force_style='FontName=DejaVu Sans,FontSize=15,Bold=1,PrimaryColour=&H00FFFFFF,OutlineColour=&HCC000000,Outline=3,Shadow=1,Alignment=2,MarginV=32',"
        "drawtext=fontfile=/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf:text='OddProofLab':fontsize=24:fontcolor=white@0.14:x=34:y=46[v]"
    )
    mod.run(['ffmpeg','-y','-loglevel','error','-i',str(raw),'-i',str(voice),'-filter_complex',filt,'-map','[v]','-map','1:a','-t','29.8','-c:v','libx264','-preset','medium','-crf','16','-pix_fmt','yuv420p','-c:a','aac','-b:a','192k','-movflags','+faststart',str(candidate)], cwd=mod.OUT)
    meta = mod.video_meta(candidate)
    vs = next(s for s in meta['streams'] if s['codec_type']=='video')
    au = next(s for s in meta['streams'] if s['codec_type']=='audio')
    duration=float(meta['format']['duration'])
    tail=duration-voice_duration
    if (int(vs['width']),int(vs['height'])) != (1080,1920):
        raise RuntimeError('FINAL_RESOLUTION_FAILED')
    if vs['codec_name']!='h264' or au['codec_name']!='aac':
        raise RuntimeError('FINAL_CODEC_FAILED')
    if not 29.70 <= duration <= 29.90:
        raise RuntimeError(f'FINAL_DURATION_FAILED_{duration}')
    if not 0.15 <= tail <= 1.0:
        raise RuntimeError(f'VOICE_TAIL_FAILED_{tail}')
    return candidate,duration,tail

mod.build_candidate = build_candidate_branded
mod.main()
