"""
Homivis Image Library UI
Run: python app2.py
Open: http://localhost:5000
"""

import io
import json
import os
import subprocess
import tempfile
import requests
import imageio_ffmpeg
from flask import Flask, jsonify, request, Response, send_from_directory, send_file
from PIL import Image

FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()

app = Flask(__name__, static_folder="static", template_folder="templates")

LIBRARY_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "image_library.json")
VIDEOS_DIR   = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "videos")

os.makedirs(VIDEOS_DIR, exist_ok=True)

# ── Supabase credentials ──────────────────────────────────────────────────────
SUPABASE_URL = os.environ.get("SUPABASE_URL", "YOUR_SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "YOUR_SUPABASE_SERVICE_ROLE_KEY")


def load_library():
    with open(LIBRARY_PATH, encoding="utf-8") as f:
        return json.load(f)


def save_library(data):
    with open(LIBRARY_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def fetch_as_png(url):
    """Download an image URL and return PNG bytes."""
    r = requests.get(url, timeout=8, headers={"User-Agent": "Mozilla/5.0"})
    r.raise_for_status()
    img = Image.open(io.BytesIO(r.content)).convert("RGB")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return buf.read()


def fetch_approved_tracks():
    """Fetch all approved tracks from Supabase artist_submissions."""
    url = f"{SUPABASE_URL}/rest/v1/artist_submissions?status=eq.APPROVED&select=artist_name,tracks"
    headers = {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json"
    }
    r = requests.get(url, headers=headers, timeout=10)
    r.raise_for_status()
    tracks = []
    for artist in r.json():
        artist_name = artist.get("artist_name", "Unknown")
        for t in (artist.get("tracks") or []):
            if t.get("url"):
                tracks.append({
                    "url": t["url"],
                    "trackName": t.get("trackName", "Untitled"),
                    "artistName": artist_name,
                    "label": f"{t.get('trackName', 'Untitled')} — {artist_name}"
                })
    return sorted(tracks, key=lambda x: x["label"].lower())


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return send_from_directory("templates", "index.html")


@app.route("/api/library")
def api_library():
    return jsonify(load_library())


@app.route("/api/music")
def api_music():
    """List all approved tracks from Supabase artist_submissions."""
    try:
        tracks = fetch_approved_tracks()
        return jsonify(tracks)
    except Exception as e:
        print(f"  Music fetch error: {e}")
        return jsonify([]), 200


@app.route("/api/mark_posted", methods=["POST"])
def api_mark_posted():
    data = request.json
    library = load_library()
    prof = next((p for p in library["professionals"]
                 if p["professional_id"] == data["professional_id"]), None)
    if not prof:
        return jsonify({"error": "not found"}), 404
    prof["images"][data["image_index"]]["posted"] = data["posted"]
    nxt = next((i for i, img in enumerate(prof["images"]) if not img["posted"]), len(prof["images"]))
    prof["next_image_index"] = nxt
    save_library(library)
    return jsonify({"ok": True})


@app.route("/api/download")
def api_download():
    url = request.args.get("url", "")
    if not url.startswith("http"):
        return "Bad URL", 400
    try:
        png = fetch_as_png(url)
        return Response(png, content_type="image/png")
    except Exception as e:
        return str(e), 502


@app.route("/api/create_video", methods=["POST"])
def api_create_video():
    """
    Multipart form:
      json  - JSON string with {professional_id, image_order, duration, format,
                                music_track (filename from library), music_start_sec (int)}
      music - optional uploaded MP3/audio file (overrides music_track)
    Returns: MP4 file download
    """
    if request.content_type and "application/json" in request.content_type:
        data = request.get_json(force=True) or {}
    else:
        data = json.loads(request.form.get("json", "{}"))

    print(f"  create_video data: {data}")

    library = load_library()
    prof = next((p for p in library["professionals"]
                 if p["professional_id"] == data["professional_id"]), None)
    if not prof:
        return jsonify({"error": "not found"}), 404

    order      = data.get("image_order", list(range(len(prof["images"]))))
    duration   = int(data.get("duration", 3))
    fmt        = data.get("format", "square")
    if fmt == "square":
        width, height = 1080, 1080
    elif fmt == "reel":
        width, height = 1080, 1920
    else:
        width, height = 1080, 1350

    # Music source: uploaded file takes priority, then library track URL
    music_file   = request.files.get("music")
    music_url    = data.get("music_url")          # Cloudinary URL from library
    music_start  = int(data.get("music_start_sec", 0))

    FPS        = 30
    XFADE_DUR  = 0.5          # dissolve duration in seconds
    XFADE_F    = int(FPS * XFADE_DUR)

    # Ken Burns motion patterns — alternated per image
    KB_PATTERNS = [
        # (zoom_start, zoom_end, x_start, y_start, x_end, y_end)  values relative to W/H
        (1.0, 1.08, 0.0,  0.0,  -0.04, -0.04),   # zoom in, drift top-left → bottom-right
        (1.08, 1.0, -0.04, -0.04, 0.0,  0.0),    # zoom out, drift bottom-right → top-left
        (1.0, 1.08, -0.04, 0.0,  0.0, -0.04),    # zoom in, drift top-right → bottom-left
        (1.08, 1.0, 0.0, -0.04, -0.04, 0.0),     # zoom out, drift bottom-left → top-right
        (1.0, 1.06, 0.0,  0.0,  0.0,  -0.03),    # zoom in, pan up
        (1.06, 1.0, 0.0, -0.03,  0.0,   0.0),    # zoom out, pan down
    ]

    with tempfile.TemporaryDirectory() as tmp:
        # Download images and scale to slightly larger canvas for Ken Burns room
        frame_paths = []
        for seq, idx in enumerate(order):
            img_data = prof["images"][idx]
            try:
                png = fetch_as_png(img_data["image_url"])
                img = Image.open(io.BytesIO(png)).convert("RGB")
                # Scale to 112% of target for Ken Burns room
                scale_w, scale_h = int(width * 1.12), int(height * 1.12)
                if fmt == "reel":
                    # Fill crop: scale so image covers full canvas, then centre-crop
                    ratio = max(scale_w / img.width, scale_h / img.height)
                    new_w = int(img.width  * ratio)
                    new_h = int(img.height * ratio)
                    img   = img.resize((new_w, new_h), Image.LANCZOS)
                    left  = (new_w - scale_w) // 2
                    top   = (new_h - scale_h) // 2
                    img   = img.crop((left, top, left + scale_w, top + scale_h))
                    canvas = img
                else:
                    img.thumbnail((scale_w, scale_h), Image.LANCZOS)
                    canvas = Image.new("RGB", (scale_w, scale_h), (15, 15, 15))
                    x = (scale_w - img.width)  // 2
                    y = (scale_h - img.height) // 2
                    canvas.paste(img, (x, y))
                path = os.path.join(tmp, f"frame_{seq:04d}.png")
                canvas.save(path, "PNG")
                frame_paths.append(path)
            except Exception as e:
                print(f"  Skipping image {idx}: {e}")

        if not frame_paths:
            return jsonify({"error": "no images could be downloaded"}), 500

        n            = len(frame_paths)
        total_frames = duration * FPS
        clip_dur     = duration + XFADE_DUR   # each clip slightly longer for xfade overlap

        # Pass 1: render each image as a short clip with Ken Burns motion
        clip_paths = []
        for i, img_path in enumerate(frame_paths):
            kb = KB_PATTERNS[i % len(KB_PATTERNS)]
            zs, ze, xs, ys, xe, ye = kb

            zoom_expr = (
                f"if(eq(on\\,1)\\,{zs:.4f}\\,"
                f"min(zoom+({ze-zs:.6f}/{total_frames})\\,{max(zs,ze):.4f}))"
            )
            x_expr = (
                f"iw*{abs(xs):.4f}+on/{total_frames}*(iw*{abs(xe-xs):.4f})"
                if xe != xs else f"iw*{abs(xs):.4f}"
            )
            y_expr = (
                f"ih*{abs(ys):.4f}+on/{total_frames}*(ih*{abs(ye-ys):.4f})"
                if ye != ys else f"ih*{abs(ys):.4f}"
            )

            clip_path = os.path.join(tmp, f"clip_{i:04d}.mp4")
            clip_cmd = [
                FFMPEG, "-y",
                "-loop", "1", "-t", str(clip_dur), "-i", img_path,
                "-vf", (
                    f"zoompan=z='{zoom_expr}':x='{x_expr}':y='{y_expr}'"
                    f":d={total_frames}:s={width}x{height}:fps={FPS},"
                    f"trim=duration={clip_dur},setpts=PTS-STARTPTS"
                ),
                "-c:v", "libx264", "-pix_fmt", "yuv420p",
                "-r", str(FPS), "-an",
                clip_path
            ]
            r = subprocess.run(clip_cmd, capture_output=True, text=True)
            if r.returncode != 0:
                print(f"  Clip {i} error: {r.stderr[-400:]}")
                return jsonify({"error": f"clip {i} failed", "detail": r.stderr[-400:]}), 500
            clip_paths.append(clip_path)
            print(f"  Clip {i+1}/{n} rendered")

        out_path = os.path.join(tmp, "slideshow.mp4")

        # Pass 2: join clips with xfade dissolve
        if n == 1:
            # Single image — just trim to exact duration
            cmd = [
                FFMPEG, "-y", "-i", clip_paths[0],
                "-t", str(duration),
                "-c:v", "libx264", "-pix_fmt", "yuv420p", "-movflags", "+faststart",
                out_path
            ]
            result = subprocess.run(cmd, capture_output=True, text=True)
        else:
            # Build xfade chain
            inputs_cmd = []
            for cp in clip_paths:
                inputs_cmd += ["-i", cp]

            filter_parts = []
            prev = "[0:v]"
            for i in range(1, n):
                offset = duration * i - XFADE_DUR * i
                label  = f"[xf{i}]" if i < n - 1 else "[vout]"
                filter_parts.append(
                    f"{prev}[{i}:v]xfade=transition=dissolve"
                    f":duration={XFADE_DUR}:offset={offset:.3f}{label}"
                )
                prev = label

            cmd = [
                FFMPEG, "-y",
                *inputs_cmd,
                "-filter_complex", ";".join(filter_parts),
                "-map", "[vout]",
                "-c:v", "libx264", "-profile:v", "high", "-level", "4.0",
                "-pix_fmt", "yuv420p", "-movflags", "+faststart",
                "-r", str(FPS),
                out_path
            ]
            result = subprocess.run(cmd, capture_output=True, text=True)

        if result.returncode != 0:
            print(f"  ffmpeg join error: {result.stderr[-800:]}")
            return jsonify({"error": "ffmpeg join failed", "detail": result.stderr[-500:]}), 500

        print(f"  Slideshow rendered: {os.path.getsize(out_path)} bytes")

        # Resolve audio source
        audio_path = None
        if music_file:
            ext = os.path.splitext(music_file.filename or "audio.mp3")[1] or ".mp3"
            audio_path = os.path.join(tmp, "music_upload" + ext)
            music_file.save(audio_path)
            print(f"  Uploaded music: {audio_path} ({os.path.getsize(audio_path)} bytes)")
        elif music_url:
            # Download track from Cloudinary
            ext = os.path.splitext(music_url.split("?")[0])[1] or ".mp3"
            audio_path = os.path.join(tmp, "music_library" + ext)
            print(f"  Downloading track: {music_url}")
            r = requests.get(music_url, timeout=30, headers={"User-Agent": "Mozilla/5.0"})
            r.raise_for_status()
            with open(audio_path, "wb") as f:
                f.write(r.content)
            print(f"  Track downloaded: {os.path.getsize(audio_path)} bytes")

        # Mix audio if we have a source
        if audio_path:
            mixed_path = os.path.join(tmp, "slideshow_music.mp4")
            mix_cmd = [
                FFMPEG, "-y",
                "-i", out_path,
                "-ss", str(music_start),          # seek to start position
                "-stream_loop", "-1", "-i", audio_path,
                "-map", "0:v:0", "-map", "1:a:0",
                "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
                "-shortest", "-movflags", "+faststart",
                mixed_path
            ]
            mix_result = subprocess.run(mix_cmd, capture_output=True, text=True)
            print(f"  Mix return code: {mix_result.returncode}")
            if mix_result.returncode != 0:
                print(f"  Mix stderr: {mix_result.stderr[-600:]}")
                return jsonify({"error": "audio mix failed", "detail": mix_result.stderr[-400:]}), 500
            out_path = mixed_path
            print(f"  Final video size: {os.path.getsize(out_path)} bytes")

        with open(out_path, "rb") as fh:
            video_bytes = fh.read()

    safe_name = "".join(c if c.isalnum() or c in "-_" else "_"
                        for c in prof["professional_name"])
    filename  = f"{safe_name}_slideshow.mp4"
    return send_file(
        io.BytesIO(video_bytes),
        mimetype="video/mp4",
        as_attachment=True,
        download_name=filename
    )


if __name__ == "__main__":
    print("Homivis Image Library UI")
    print("Open: http://localhost:5000")
    app.run(debug=False, port=5000)
