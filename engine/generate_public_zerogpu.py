import argparse, json, shutil, time
from pathlib import Path
from gradio_client import Client

NEG = (
    'cartoon, illustration, cheap CGI, plastic skin, low detail, blurry, deformed anatomy, '
    'morphing, duplicate objects, extra fingers, fantasy glow, text, subtitles, watermark, '
    'oversaturated, static frame, broken physics, teleporting objects'
)

# Current public HF ZeroGPU / Gradio routes verified from the public Spaces directory.
# No paid API key is used. The client introspects each live Space API before calling it.
SPACES = [
    ('multimodalart/minimax-h3', 'minimax'),
    ('mrfakename/minimax-h3-ultra-fast', 'minimax'),
    ('techfreakworm/LTX2.3-Studio', 'ltx23'),
    ('multimodalart/minimax-h3-reference', 'minimax'),
    ('zerogpu-aoti/wan2-2-fp8da-aoti', 'wan'),
]

def extract_path(result):
    vals = result if isinstance(result, (list, tuple)) else [result]
    stack = list(vals)
    while stack:
        v = stack.pop(0)
        if isinstance(v, str) and Path(v).exists():
            return Path(v)
        if isinstance(v, (list, tuple)):
            stack.extend(v)
            continue
        if isinstance(v, dict):
            stack.extend(v.values())
            continue
        for key in ('video', 'path', 'name', 'value'):
            p = getattr(v, key, None)
            if isinstance(p, str) and Path(p).exists():
                return Path(p)
    return None

def api_dict(client):
    try:
        d = client.view_api(return_format='dict')
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}

def endpoints(client):
    api = api_dict(client)
    named = api.get('named_endpoints', {}) if isinstance(api, dict) else {}
    keys = list(named.keys())
    preferred = [k for k in keys if any(x in k.lower() for x in ('video','generate','text','t2v'))]
    return preferred + [k for k in keys if k not in preferred], named

def smart_kwargs(param_defs, prompt, seed):
    guarded = prompt + '. Strict photorealism and physical continuity. Avoid: ' + NEG + '.'
    out = {}
    for p in param_defs or []:
        name = str(p.get('parameter_name') or p.get('name') or '').strip()
        if not name:
            continue
        low = name.lower()
        if 'negative' in low and 'prompt' in low:
            out[name] = NEG
        elif 'prompt' in low or low in ('text','caption','description'):
            out[name] = guarded
        elif 'seed' in low and 'random' not in low:
            out[name] = seed
        elif 'randomize' in low:
            out[name] = False
        elif low in ('width','w'):
            out[name] = 676
        elif low in ('height','h'):
            out[name] = 1024
        elif 'duration' in low or low in ('seconds','length'):
            out[name] = 6
        elif 'fps' in low or 'frame_rate' in low:
            out[name] = 24
        elif 'num_frames' in low or low == 'frames':
            out[name] = 121
        elif 'steps' in low or 'inference_steps' in low:
            out[name] = 8
        elif 'guidance' in low:
            out[name] = 3.0
        elif any(x in low for x in ('image','audio','video','file','reference','first_frame','last_frame')):
            out[name] = None
        elif 'enhance' in low:
            out[name] = False
    return out

def variants(kind, prompt, seed):
    guarded = prompt + '. Strict photorealism and physical continuity. Avoid: ' + NEG + '.'
    common = [dict(prompt=guarded, seed=seed), dict(prompt=guarded), dict(text=guarded, seed=seed), dict(text=guarded)]
    if kind == 'ltx23':
        return [
            dict(prompt=guarded, duration=6.0, seed=seed, width=576, height=1024),
            dict(prompt=guarded, duration=6.0, seed=seed),
            *common,
        ]
    if kind == 'minimax':
        return [
            dict(prompt=guarded, negative_prompt=NEG, seed=seed, duration=6.0),
            dict(prompt=guarded, seed=seed, duration=6.0),
            *common,
        ]
    if kind == 'wan':
        return [
            dict(prompt=guarded, negative_prompt=NEG, duration_seconds=6.0, steps=4, seed=seed, randomize_seed=False),
            dict(prompt=guarded, negative_prompt=NEG, seed=seed),
            *common,
        ]
    return common

def call_space(space, kind, prompt, seed):
    c = Client(space, verbose=False)
    eps, named = endpoints(c)
    if not eps:
        eps = ['/generate_video','/generate','/predict']
    errors = []
    for ep in eps[:16]:
        meta = named.get(ep, {}) if isinstance(named, dict) else {}
        param_defs = meta.get('parameters', []) if isinstance(meta, dict) else []
        attempts = []
        auto = smart_kwargs(param_defs, prompt, seed)
        if auto:
            attempts.append(auto)
        attempts.extend(variants(kind, prompt, seed))
        seen = set()
        for kw in attempts:
            sig = json.dumps(kw, sort_keys=True, default=str)
            if sig in seen:
                continue
            seen.add(sig)
            try:
                result = c.predict(api_name=ep, **kw)
                p = extract_path(result)
                if p and p.stat().st_size > 100000:
                    return p, ep
            except Exception as exc:
                errors.append(f'{space} {ep}: {type(exc).__name__}: {exc}')
    raise RuntimeError(' | '.join(errors[-12:]))

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--manifest', required=True)
    ap.add_argument('--out', default='oddproof/gpu-video/public-output')
    args = ap.parse_args()
    manifest = json.loads(Path(args.manifest).read_text())
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    report = []
    for idx, scene in enumerate(manifest['scenes'], 1):
        dst = out / f'scene-{idx:02d}.mp4'
        if dst.exists() and dst.stat().st_size > 100000:
            report.append({'scene': idx, 'status': 'cached', 'file': str(dst)})
            continue
        success = False
        failures = []
        for round_no in range(2):
            for space, kind in SPACES:
                seed = int(scene.get('seed', 62000 + idx * 37)) + round_no * 997
                try:
                    src, ep = call_space(space, kind, scene['prompt'], seed)
                    shutil.copy2(src, dst)
                    report.append({'scene': idx, 'status': 'ok', 'backend': space, 'endpoint': ep, 'seed': seed, 'file': str(dst), 'size': dst.stat().st_size})
                    print('SCENE_OK', idx, space, ep, dst.stat().st_size, flush=True)
                    success = True
                    break
                except Exception as exc:
                    msg = str(exc)[:1800]
                    failures.append({'backend': space, 'error': msg})
                    print('BACKEND_FAIL', idx, space, msg, flush=True)
                    time.sleep(2)
            if success:
                break
        if not success:
            report.append({'scene': idx, 'status': 'missing', 'failures': failures[-10:]})
            print('SCENE_FAILED', idx, flush=True)
    (out / 'generation-report.json').write_text(json.dumps(report, indent=2) + '\n')
    ok = sum(1 for r in report if r['status'] in ('ok', 'cached'))
    print(f'GENERATED_SCENES={ok}/{len(report)}')
    if ok != len(manifest['scenes']):
        raise SystemExit('ZEROGPU_GENERATION_INCOMPLETE')

if __name__ == '__main__':
    main()
