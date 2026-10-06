import os, re, shutil, subprocess, tempfile
from pathlib import Path
from urllib.parse import urlparse

import requests
from flask import Flask, request, jsonify, send_file
import imageio_ffmpeg

app = Flask(__name__)

API_KEY = os.environ.get("RENDERER_API_KEY", "")
FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()
MAX_SCENES = 20


def run(cmd):
    p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if p.returncode != 0:
        raise RuntimeError(p.stderr[-8000:])
    return p


def safe_id(value):
    value = re.sub(r"[^A-Za-z0-9_-]+", "_", str(value or "episode"))
    return value[:80] or "episode"


def download(url, dest):
    if urlparse(url).scheme not in ("http", "https"):
        raise ValueError("Unsupported URL scheme")
    with requests.get(url, stream=True, timeout=(20, 180), allow_redirects=True) as r:
        r.raise_for_status()
        with open(dest, "wb") as f:
            for chunk in r.iter_content(1024 * 1024):
                if chunk:
                    f.write(chunk)


def media_info(path):
    p = subprocess.run(
        [FFMPEG, "-hide_banner", "-i", str(path)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    text = p.stderr or ""
    return {
        "has_audio": " Audio:" in text,
        "probe_text": text,
    }


def ass_time(seconds):
    seconds = max(0.0, float(seconds))
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = seconds % 60
    return f"{h}:{m:02d}:{s:05.2f}"


def ass_escape(text):
    return (
        str(text or "")
        .replace("\\", r"\\")
        .replace("{", r"\{")
        .replace("}", r"\}")
        .replace("\n", r"\N")
    )


def split_caption_chunks(text, max_words=3):
    """
    Split a scene subtitle into short Shorts/TikTok-style caption chunks.
    Prefers punctuation boundaries, otherwise caps chunks at max_words.
    """
    words = str(text or "").strip().split()
    if not words:
        return []

    chunks = []
    current = []

    for word in words:
        current.append(word)

        ends_phrase = bool(re.search(r"[,.!?;:…—-]$", word))
        if len(current) >= max_words or (ends_phrase and len(current) >= 2):
            chunks.append(" ".join(current))
            current = []

    if current:
        if chunks and len(current) == 1 and len(chunks[-1].split()) <= 2:
            chunks[-1] = chunks[-1] + " " + current[0]
        else:
            chunks.append(" ".join(current))

    return chunks


def make_ass(scenes, durations, path, width, height):
    font_size = 56 if width >= 700 else 44
    margin_v = 175 if height >= 1200 else 135

    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {width}
PlayResY: {height}
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name,Fontname,Fontsize,PrimaryColour,SecondaryColour,OutlineColour,BackColour,Bold,Italic,Underline,StrikeOut,ScaleX,ScaleY,Spacing,Angle,BorderStyle,Outline,Shadow,Alignment,MarginL,MarginR,MarginV,Encoding
Style: Default,DejaVu Sans,{font_size},&H00FFFFFF,&H000000FF,&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,5,1,2,50,50,{margin_v},1

[Events]
Format: Layer,Start,End,Style,Name,MarginL,MarginR,MarginV,Effect,Text
"""

    events = []
    cursor = 0.0

    for scene, dur in zip(scenes, durations):
        txt = str(scene.get("subtitle_text") or "").strip()
        status = str(scene.get("subtitle_status") or "").upper()

        if txt and status not in ("NOT_REQUIRED", "SUBTITLE_ERROR"):
            chunks = split_caption_chunks(txt, max_words=3)

            # Keep a tiny gap at the beginning/end of the scene.
            scene_start = cursor + 0.06
            scene_end = cursor + max(0.20, dur - 0.06)
            usable = max(0.20, scene_end - scene_start)

            # Timing is weighted by number of words so short chunks do not linger
            # as long as long chunks. This gives a much more dynamic Shorts feel.
            weights = [max(1, len(chunk.split())) for chunk in chunks]
            total_weight = max(1, sum(weights))

            chunk_start = scene_start

            for idx, (chunk, weight) in enumerate(zip(chunks, weights)):
                if idx == len(chunks) - 1:
                    chunk_end = scene_end
                else:
                    chunk_end = chunk_start + usable * (weight / total_weight)

                # Prevent ultra-short flashes.
                if chunk_end - chunk_start < 0.38:
                    chunk_end = min(scene_end, chunk_start + 0.38)

                if chunk_end > chunk_start:
                    events.append(
                        f"Dialogue: 0,{ass_time(chunk_start)},{ass_time(chunk_end)},Default,,0,0,0,,{ass_escape(chunk)}"
                    )

                chunk_start = chunk_end

        cursor += dur

    Path(path).write_text(header + "\n".join(events) + "\n", encoding="utf-8")
    return bool(events)


@app.get("/health")
def health():
    return jsonify({
        "ok": True,
        "service": "ai-media-machine-ffmpeg-renderer",
        "ffmpeg": True,
    })


@app.post("/render")
def render():
    if not API_KEY:
        return jsonify({"error": "RENDERER_API_KEY not configured"}), 500

    if request.headers.get("X-Renderer-Key", "") != API_KEY:
        return jsonify({"error": "unauthorized"}), 401

    data = request.get_json(silent=True) or {}
    episode_id = safe_id(data.get("episode_id"))
    scenes = data.get("scenes") or data.get("master_scenes") or []

    if not isinstance(scenes, list) or not scenes:
        return jsonify({"error": "scenes must be a non-empty array"}), 400

    if len(scenes) > MAX_SCENES:
        return jsonify({"error": f"too many scenes; max={MAX_SCENES}"}), 400

    width = int(data.get("output_width") or 720)
    height = int(data.get("output_height") or 1280)
    fps = int(data.get("fps") or 30)
    crf = int(data.get("crf") or 22)
    burn_subtitles = bool(data.get("burn_subtitles", True))

    work = Path(tempfile.mkdtemp(prefix=f"{episode_id}_"))

    try:
        normalized = []
        durations = []

        for idx, scene in enumerate(scenes, start=1):
            url = str(scene.get("final_scene_url") or "").strip()
            if not url:
                return jsonify({"error": f"scene {idx} missing final_scene_url"}), 400

            target_dur = float(scene.get("duration_seconds") or 0)
            if target_dur <= 0:
                target_dur = 3.5

            src = work / f"scene_{idx:02d}_src.mp4"
            dst = work / f"scene_{idx:02d}_norm.mp4"
            download(url, src)

            info = media_info(src)

            vf = (
                f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
                f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,"
                f"fps={fps},setsar=1"
            )

            common = [
                "-t", str(target_dur),
                "-vf", vf,
                "-c:v", "libx264",
                "-preset", "ultrafast",
                "-crf", str(crf),
                "-pix_fmt", "yuv420p",
                "-c:a", "aac",
                "-ar", "48000",
                "-ac", "2",
                "-b:a", "128k",
                "-movflags", "+faststart",
            ]

            if info["has_audio"]:
                cmd = [FFMPEG, "-y", "-i", str(src)] + common + [str(dst)]
            else:
                cmd = [
                    FFMPEG, "-y",
                    "-i", str(src),
                    "-f", "lavfi",
                    "-i", "anullsrc=channel_layout=stereo:sample_rate=48000",
                    "-shortest",
                ] + common + [str(dst)]

            run(cmd)
            normalized.append(dst)
            durations.append(target_dur)

        concat_file = work / "concat.txt"
        concat_file.write_text(
            "\n".join(f"file '{p.as_posix()}'" for p in normalized) + "\n",
            encoding="utf-8",
        )

        joined = work / f"{episode_id}_joined.mp4"
        run([
            FFMPEG, "-y",
            "-f", "concat",
            "-safe", "0",
            "-i", str(concat_file),
            "-c", "copy",
            str(joined),
        ])

        final = work / f"{episode_id}_MASTER.mp4"
        ass_file = work / "subtitles.ass"
        has_subtitles = make_ass(scenes, durations, ass_file, width, height)

        if burn_subtitles and has_subtitles:
            run([
                FFMPEG, "-y",
                "-i", str(joined),
                "-vf", f"ass={ass_file.as_posix()}",
                "-c:v", "libx264",
                "-preset", "ultrafast",
                "-crf", str(crf),
                "-pix_fmt", "yuv420p",
                "-c:a", "copy",
                "-movflags", "+faststart",
                str(final),
            ])
        else:
            shutil.copy2(joined, final)

        response = send_file(
            final,
            mimetype="video/mp4",
            as_attachment=True,
            download_name=f"{episode_id}_MASTER.mp4",
        )
        response.headers["X-Episode-Id"] = episode_id
        response.headers["X-Scene-Count"] = str(len(scenes))
        response.headers["X-Master-Duration"] = f"{sum(durations):.3f}"
        return response

    except requests.HTTPError as e:
        return jsonify({"error": "download_failed", "detail": str(e)}), 502
    except Exception as e:
        return jsonify({"error": "render_failed", "detail": str(e)}), 500


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)
