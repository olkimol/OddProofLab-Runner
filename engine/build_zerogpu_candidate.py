from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import subprocess
from pathlib import Path

SCENE_DUR = 5.96
TOTAL_DUR = 29.8


def run(cmd, cwd=None):
    print("+", " ".join(map(str, cmd)), flush=True)
    subprocess.run(cmd, cwd=cwd, check=True)


def probe(path: Path):
    return json.loads(subprocess.check_output([
        "ffprobe","-v","error","-show_streams","-show_format","-of","json",str(path)
    ], text=True))


def duration(path: Path) -> float:
    return float(probe(path)["format"]["duration"])


def stamp(sec: float) -> str:
    ms = int(round(sec * 1000))
    h, rem = divmod(ms, 3600000)
    m, rem = divmod(rem, 60000)
    s, ms = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def motion_score(video: Path) -> float:
    import cv2
    import numpy as np
    cap = cv2.VideoCapture(str(video))
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    if total < 20:
        cap.release()
        return 0.0
    idxs = [int(total*x) for x in (0.10,0.30,0.50,0.70,0.90)]
    frames = []
    for idx in idxs:
        cap.set(cv2.CAP_PROP_POS_FRAMES, idx)
        ok, frame = cap.read()
        if ok:
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            frames.append(cv2.resize(gray, (256,144)))
    cap.release()
    if len(frames) < 4:
        return 0.0
    diffs = [float(np.mean(cv2.absdiff(a,b))) for a,b in zip(frames,frames[1:])]
    return sum(diffs)/len(diffs)


def synth_voice(text: str, out: Path, target=29.35):
    import numpy as np
    import soundfile as sf
    from kokoro import KPipeline

    pipeline = KPipeline(lang_code="a")
    voice = "af_heart"

    def synth(speed: float):
        chunks = []
        for _, _, audio in pipeline(text, voice=voice, speed=float(speed)):
            if audio is None:
                continue
            if hasattr(audio, "detach"):
                audio = audio.detach().cpu().numpy()
            chunks.append(np.asarray(audio))
        if not chunks:
            raise RuntimeError("KOKORO_RETURNED_NO_AUDIO")
        data = np.concatenate(chunks)
        tmp = out.parent / "voice-temp.wav"
        sf.write(tmp, data, 24000)
        d = float(subprocess.check_output([
            "ffprobe","-v","error","-show_entries","format=duration",
            "-of","default=nw=1:nk=1",str(tmp)
        ], text=True).strip())
        return tmp, d

    _, d0 = synth(1.0)
    speed = max(0.90, min(1.15, d0/target))
    tmp, d1 = synth(speed)
    tmp.replace(out)
    if d1 > TOTAL_DUR - 0.12:
        raise RuntimeError(f"VOICE_TOO_LONG_{d1:.3f}")
    return d1, speed, voice


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--manifest", required=True)
    ap.add_argument("--scenes-dir", required=True)
    ap.add_argument("--report", required=True)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    manifest = json.loads(Path(args.manifest).read_text())
    scenes_dir = Path(args.scenes_dir)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    report = json.loads(Path(args.report).read_text())
    backend_by_scene = {
        int(x["scene"]): x.get("backend","unknown")
        for x in report if x.get("status") in ("ok","cached")
    }

    normalized = []
    scores = []
    for i in range(1,6):
        src = scenes_dir / f"scene-{i:02d}.mp4"
        if not src.exists() or src.stat().st_size < 100000:
            raise SystemExit(f"MISSING_SCENE_{i}")
        d = duration(src)
        if d < 2.5:
            raise SystemExit(f"SCENE_TOO_SHORT_{i}_{d:.3f}")
        factor = SCENE_DUR / d
        dst = out / f"visual-{i:02d}.mp4"
        vf = (
            f"setpts={factor:.8f}*PTS,"
            "scale=1080:1920:force_original_aspect_ratio=increase,"
            "crop=1080:1920,fps=30,"
            "eq=contrast=1.02:saturation=1.02,"
            "unsharp=5:5:0.10"
        )
        run([
            "ffmpeg","-y","-loglevel","error","-i",str(src),
            "-vf",vf,"-t",str(SCENE_DUR),"-an",
            "-c:v","libx264","-preset","fast","-crf","17","-pix_fmt","yuv420p",str(dst)
        ])
        score = motion_score(dst)
        print(f"SCENE_{i}_MOTION_SCORE={score:.3f}", flush=True)
        if score < 1.20:
            raise SystemExit(f"SCENE_{i}_MOTION_QA_FAIL_{score:.3f}")
        normalized.append(dst)
        scores.append(score)

    voice = out / "voice.wav"
    voice_duration, voice_speed, voice_name = synth_voice(manifest["narration"], voice)

    srt = out / "captions.srt"
    blocks = []
    captions = manifest["captions"]
    if len(captions) != 5:
        raise SystemExit("CAPTION_COUNT_NOT_5")
    for i, text in enumerate(captions,1):
        a = (i-1)*SCENE_DUR + 0.12
        b = min(i*SCENE_DUR - 0.12, TOTAL_DUR-0.08)
        blocks.append(f"{i}\n{stamp(a)} --> {stamp(b)}\n{text}\n")
    srt.write_text("\n".join(blocks), encoding="utf-8")

    concat = out / "clips.txt"
    concat.write_text("".join(f"file '{p.name}'\n" for p in normalized))
    raw = out / "visual-raw.mp4"
    run([
        "ffmpeg","-y","-loglevel","error","-f","concat","-safe","0",
        "-i",str(concat),"-t",str(TOTAL_DUR),"-an",
        "-c:v","libx264","-preset","fast","-crf","16","-pix_fmt","yuv420p",str(raw)
    ], cwd=out)

    candidate = out / "candidate.mp4"
    filt = (
        "[0:v]subtitles=captions.srt:"
        "force_style='FontName=DejaVu Sans,FontSize=15,Bold=1,"
        "PrimaryColour=&H00FFFFFF,OutlineColour=&HCC000000,Outline=3,"
        "Shadow=1,Alignment=2,MarginV=32',"
        "drawtext=fontfile=/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf:"
        "text='OddProofLab':fontsize=24:fontcolor=white@0.14:x=34:y=46[v];"
        "[1:a]loudnorm=I=-14:TP=-1.5:LRA=7,apad=pad_dur=1[a]"
    )
    run([
        "ffmpeg","-y","-loglevel","error","-i",str(raw),"-i",str(voice),
        "-filter_complex",filt,"-map","[v]","-map","[a]","-t",str(TOTAL_DUR),
        "-c:v","libx264","-preset","fast","-crf","16","-pix_fmt","yuv420p",
        "-c:a","aac","-b:a","192k","-movflags","+faststart",str(candidate)
    ], cwd=out)

    q = probe(candidate)
    v = next(x for x in q["streams"] if x.get("codec_type")=="video")
    a = next(x for x in q["streams"] if x.get("codec_type")=="audio")
    dur = float(q["format"]["duration"])
    if (int(v["width"]),int(v["height"])) != (1080,1920):
        raise SystemExit("FINAL_RESOLUTION_FAIL")
    if v.get("codec_name") != "h264" or a.get("codec_name") != "aac":
        raise SystemExit("FINAL_CODEC_FAIL")
    if not 29.65 <= dur <= 29.95:
        raise SystemExit(f"FINAL_DURATION_FAIL_{dur:.3f}")

    run([
        "ffmpeg","-y","-loglevel","error","-i",str(candidate),
        "-vf","trim=start=0:end=1.25,fps=4,scale=216:384,tile=5x1:padding=4:margin=4",
        "-frames:v","1",str(out/"hook-sheet.jpg")
    ])
    run([
        "ffmpeg","-y","-loglevel","error","-i",str(candidate),
        "-vf","fps=1/3.72,scale=216:384,tile=4x2:padding=4:margin=4",
        "-frames:v","1",str(out/"contact-sheet.jpg")
    ])

    sha = hashlib.sha256(candidate.read_bytes()).hexdigest()
    result = {
        "slug": manifest["slug"],
        "title": manifest["title"],
        "description": f"{manifest['title']} — a cinematic OddProofLab science Short. #Shorts #Science #OddProofLab",
        "tags": ["Shorts","Science","OddProofLab"],
        "primary_hook": captions[0],
        "quality_standard": "photorealistic-cinematic",
        "candidate_sha256": sha,
        "duration_seconds": dur,
        "scene_motion_scores": scores,
        "backends": sorted(set(backend_by_scene.values())),
        "backend_by_scene": backend_by_scene,
        "voice_backend": "Kokoro-82M",
        "voice": voice_name,
        "voice_speed": voice_speed,
        "voice_duration_seconds": voice_duration,
        "technical_qa": "PASS",
        "motion_qa": "PASS",
        "semantic_visual_qa": "REQUIRED_BEFORE_PUBLISH",
        "publish_allowed": False
    }
    (out/"manifest.json").write_text(json.dumps(result,indent=2)+"\n")
    (out/"source-topic.json").write_text(json.dumps(manifest,indent=2)+"\n")
    print("CANDIDATE_SHA256="+sha)
    print("TECHNICAL_QA=PASS")
    print("MOTION_QA=PASS")
    print("SEMANTIC_VISUAL_QA=REQUIRED_BEFORE_PUBLISH")


if __name__ == "__main__":
    main()
