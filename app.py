import math
import os
import re
import subprocess
import time

from flask import Flask, render_template, request, send_from_directory
from werkzeug.utils import secure_filename
from PIL import Image, ImageOps
import imageio_ffmpeg


# --------------------------------------------------
# 1. APPLICATION CONFIGURATION
# --------------------------------------------------

app = Flask(__name__)

app.config["MAX_CONTENT_LENGTH"] = 500 * 1024 * 1024  # 500 MB

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
UPLOADS = os.path.join(BASE_DIR, "uploads")
OUTPUTS = os.path.join(BASE_DIR, "outputs")

os.makedirs(UPLOADS, exist_ok=True)
os.makedirs(OUTPUTS, exist_ok=True)

FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()

ALLOWED_PRESETS = {
    "1080x1920": (1080, 1920),
    "1080x1080": (1080, 1080),
    "1920x1080": (1920, 1080),
    "1280x720": (1280, 720),
}

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tiff"}
VIDEO_EXTENSIONS = {".mp4", ".mov", ".avi", ".mkv", ".webm", ".m4v"}
MAX_DIMENSION = 10000


# --------------------------------------------------
# 2. IMAGE RESIZING
# --------------------------------------------------

def resize_image(src, dst, width, height, fit, max_kb=None):
    with Image.open(src) as original:
        img = ImageOps.exif_transpose(original).convert("RGB")

    # If only one dimension is supplied, calculate the other
    # dimension to preserve the original aspect ratio.
    if width and not height:
        height = max(1, round(img.height * width / img.width))
    elif height and not width:
        width = max(1, round(img.width * height / img.height))

    if not width or not height:
        raise ValueError("A valid width or height is required.")

    target_size = (width, height)

    if fit == "cover":
        output = ImageOps.fit(
            img, target_size, method=Image.Resampling.LANCZOS
        )
    elif fit == "contain":
        output = ImageOps.pad(
            img,
            target_size,
            method=Image.Resampling.LANCZOS,
            color=(255, 255, 255),
        )
    else:  # fill: stretch to the exact dimensions
        output = img.resize(target_size, Image.Resampling.LANCZOS)

    quality = 90

    while True:
        output.save(dst, format="JPEG", quality=quality, optimize=True)

        if not max_kb or os.path.getsize(dst) / 1024 <= max_kb or quality <= 10:
            break

        quality -= 10

    process = subprocess.run(
        [FFMPEG, "-i", path],
        capture_output=True,
        text=True,
        errors="replace",
    )

    match = re.search(
        r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)",
        process.stderr,
    )

    if not match:
        return 1.0

    hours, minutes, seconds = match.groups()
    duration = int(hours) * 3600 + int(minutes) * 60 + float(seconds)
    return max(duration, 1.0)


def video_filter(width, height, fit):
    # Preserve aspect ratio when only one dimension is entered.
    if width and not height:
        return f"scale={make_even(width)}:-2"

    if height and not width:
        return f"scale=-2:{make_even(height)}"

    width = make_even(width)
    height = make_even(height)

    if fit == "contain":
        return (
            f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
            f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2"
        )

    if fit == "fill":
        return f"scale={width}:{height}"

    # cover: fill the target area and crop the overflow.
    return (
        f"scale={width}:{height}:force_original_aspect_ratio=increase,"
        f"crop={width}:{height}"
    )


def resize_video(src, dst, width, height, fit, max_mb=None):
    command = [
        FFMPEG,
        "-y",
        "-i", src,
        "-vf", video_filter(width, height, fit),
        "-c:v", "libx264",
        "-preset", "veryfast",
        "-pix_fmt", "yuv420p",
        "-c:a", "aac",
        "-b:a", "128k",
        "-movflags", "+faststart",
    ]

    if max_mb:
        duration = get_duration(src)
        total_kbps = (max_mb * 8 * 1024 * 0.95) / duration
        video_kbps = max(100, int(total_kbps - 128))

        command += [
            "-b:v", f"{video_kbps}k",
            "-maxrate", f"{video_kbps}k",
            "-bufsize", f"{video_kbps * 2}k",
        ]
    else:
        command += ["-crf", "23"]

    command.append(dst)

    process = subprocess.run(
        command,
        capture_output=True,
        text=True,
        errors="replace",
    )

    if process.returncode != 0:
        raise RuntimeError(process.stderr[-1500:] or "FFmpeg could not process this video.")

# --------------------------------------------------
# 4. HOME PAGE
# --------------------------------------------------

@app.route("/")
def home():
    return render_template("index.html")


# --------------------------------------------------
# 5. RESIZE ROUTE
# --------------------------------------------------

@app.route("/resize", methods=["POST"])
def resize():
    uploaded_file = request.files.get("file")

    if not uploaded_file or not uploaded_file.filename:
        return render_template(
            "result.html",
            error="Please select an image or video."
        )

    original_filename = secure_filename(uploaded_file.filename)

    if not original_filename:
        return render_template(
            "result.html",
            error="Invalid filename."
        )

    extension = os.path.splitext(original_filename)[1].lower()

    image_extensions = {".jpg", ".jpeg", ".png", ".webp", ".bmp"}
    video_extensions = {".mp4", ".mov", ".avi", ".mkv", ".webm"}

    is_image = extension in image_extensions
    is_video = extension in video_extensions

    if not is_image and not is_video:
        return render_template(
            "result.html",
            error="Unsupported file format."
        )

    # Read selected preset
    preset = request.form.get("preset", "custom")

    width = None
    height = None

    if preset == "custom":

        width_text = request.form.get("width", "").strip()
        height_text = request.form.get("height", "").strip()

        try:
            width = int(width_text) if width_text else None
            height = int(height_text) if height_text else None

        except ValueError:
            return render_template(
                "result.html",
                error="Width and height must be whole numbers."
            )

        if width is None and height is None:
            return render_template(
                "result.html",
                error="Enter at least a width or height."
            )

        if (
            (width is not None and not 16 <= width <= MAX_DIMENSION)
            or (height is not None and not 16 <= height <= MAX_DIMENSION)
        ):
            return render_template(
                "result.html",
                error="Dimensions must be between 16 and the maximum allowed."
            )

    elif preset in ALLOWED_PRESETS:
        width, height = ALLOWED_PRESETS[preset]

    else:
        return render_template(
            "result.html",
            error="Please select a valid size preset."
        )

    # Maximum output size
    try:
        max_size = float(
            request.form.get("maxSize", "").strip() or 0
        )

    except ValueError:
        return render_template(
            "result.html",
            error="Maximum file size must be a valid number."
        )

    if not math.isfinite(max_size) or max_size < 0:
        return render_template(
            "result.html",
            error="Maximum file size must be zero or positive."
        )

    max_size = max_size or None

    fit = request.form.get("fit", "cover")

    if fit not in {"cover", "contain", "fill"}:
        fit = "cover"

    # Generate unique filenames
    stamp = f"{int(time.time() * 1000)}_{os.urandom(3).hex()}"

    source_name = f"{stamp}_{original_filename}"
    output_name = f"{stamp}.jpg" if is_image else f"{stamp}.mp4"

    source_path = os.path.join(UPLOADS, source_name)
    output_path = os.path.join(OUTPUTS, output_name)

    uploaded_file.save(source_path)

    try:
        if is_image:
            resize_image(
                source_path,
                output_path,
                width,
                height,
                fit,
                max_size * 1024 if max_size else None
            )

            kind = "image"

        else:
            resize_video(
                source_path,
                output_path,
                width,
                height,
                fit,
                max_size
            )

            kind = "video"

        output_size_mb = round(
            os.path.getsize(output_path) / (1024 * 1024), 2
        )

    except Exception as error:
        if os.path.exists(output_path):
            os.remove(output_path)

        return render_template(
            "result.html",
            error=f"Processing failed: {error}"
        )

    finally:
        if os.path.exists(source_path):
            os.remove(source_path)

    return render_template(
        "result.html",
        output_name=output_name,
        output_size=output_size_mb,
        kind=kind
    )


# --------------------------------------------------
# 6. DOWNLOAD OUTPUT
# --------------------------------------------------

@app.route("/outputs/<filename>")
def download_file(filename):
    return send_from_directory(
        OUTPUTS,
        filename,
        as_attachment=True
    )


# --------------------------------------------------
# 7. RUN APPLICATION
# --------------------------------------------------

if __name__ == "__main__":
    app.run(debug=True)
