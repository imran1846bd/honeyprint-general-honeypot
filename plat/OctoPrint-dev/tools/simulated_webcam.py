"""Local animated demo camera: run with venv/bin/python tools/simulated_webcam.py."""

import argparse
import io
import math
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

from PIL import Image, ImageDraw


START = time.monotonic()
VIDEO_FRAMES = []


def load_video(path):
    import imageio_ffmpeg
    from PIL import ImageOps

    reader = imageio_ffmpeg.read_frames(
        path, pix_fmt="rgb24", output_params=["-vf", "fps=12", "-t", "120"]
    )
    try:
        metadata = next(reader)
        for raw in reader:
            source = Image.frombytes("RGB", metadata["size"], raw)
            frame = ImageOps.pad(source, (960, 540), color="#11161b")
            buffer = io.BytesIO()
            frame.save(buffer, "JPEG", quality=85)
            VIDEO_FRAMES.append(buffer.getvalue())
    finally:
        reader.close()
    if not VIDEO_FRAMES:
        raise ValueError("The video contains no decodable frames")
    print(f"Loaded {len(VIDEO_FRAMES)} frames for looping playback", flush=True)


def camera_frame():
    elapsed = time.monotonic() - START
    if VIDEO_FRAMES:
        return VIDEO_FRAMES[int(elapsed * 12) % len(VIDEO_FRAMES)]
    return render_frame(elapsed)


def render_frame(elapsed):
    image = Image.new("RGB", (960, 540), "#20252b")
    draw = ImageDraw.Draw(image)
    for y in range(540):
        shade = int(22 + 22 * y / 540)
        draw.line((0, y, 960, y), fill=(shade, shade + 4, shade + 9))

    def point(x, y, z=0):
        return (480 + x * 1.25 - y * 0.72, 338 + x * 0.29 + y * 0.48 - z)

    def polygon(coords, fill, outline=None):
        draw.polygon([point(*p) for p in coords], fill=fill, outline=outline)

    # Frame, rear rails and build plate in a fixed camera perspective.
    draw.ellipse((704, 38, 847, 174), fill="#111418", outline="#555c63", width=8)
    draw.ellipse((732, 63, 819, 148), fill="#ae6536", outline="#d08c52", width=12)
    draw.ellipse((764, 92, 787, 116), fill="#252a30")
    for x in (-225, 225):
        a, b = point(x, -155, 0), point(x, -155, 280)
        draw.line((a, b), fill="#101318", width=26)
        draw.line((a[0] - 5, a[1], b[0] - 5, b[1]), fill="#69727a", width=4)
    draw.line((point(-225, -155, 280), point(225, -155, 280)), fill="#0d1115", width=27)
    polygon([(-225, -170, -15), (225, -170, -15), (225, 170, -15), (-225, 170, -15)], "#101316")
    polygon([(-225, -170), (225, -170), (225, 170), (-225, 170)], "#50565b", "#848b91")
    for v in range(-150, 151, 30):
        draw.line((point(-210, v), point(210, v)), fill="#60666b")
        draw.line((point(v, -160), point(v, 160)), fill="#60666b")

    progress = (elapsed % 90) / 90
    height = 5 + progress * 112
    polygon([(-65, 65), (65, 65), (65, 65, height), (-65, 65, height)], "#ba602c")
    polygon([(65, -65), (65, 65), (65, 65, height), (65, -65, height)], "#85401f")
    for z in range(2, int(height), 3):
        draw.line((point(-65, 65, z), point(65, 65, z)), fill="#d57b3a")
        draw.line((point(65, -65, z), point(65, 65, z)), fill="#a95628")
    polygon([(-65, -65, height), (65, -65, height), (65, 65, height), (-65, 65, height)], "#e6974b")
    for v in range(-60, 65, 8):
        draw.line((point(-62, v, height), point(62, v, height)), fill="#b97235", width=2)

    phase = (elapsed * 0.35) % 4
    corners = [(-65, -65), (65, -65), (65, 65), (-65, 65), (-65, -65)]
    index = int(phase)
    fraction = phase - index
    x, y = [corners[index][i] * (1 - fraction) + corners[index + 1][i] * fraction for i in (0, 1)]
    nx, ny = point(x, y, height)
    draw.line((point(-220, y, height + 80), point(220, y, height + 80)), fill="#171c21", width=22)
    draw.line((point(-220, y, height + 85), point(220, y, height + 85)), fill="#929aa1", width=4)
    draw.line([(775, 106), (690, 55), (nx + 15, 45), (nx, ny - 78)], fill="#de9550", width=3)
    draw.rounded_rectangle((nx - 32, ny - 92, nx + 32, ny - 25), radius=7, fill="#191e23", outline="#6b7379", width=2)
    draw.ellipse((nx - 22, ny - 84, nx + 22, ny - 40), fill="#080c10", outline="#434b53", width=3)
    for angle in range(0, 360, 90):
        a = math.radians(angle + elapsed * 500)
        draw.line((nx, ny - 62, nx + 17 * math.cos(a), ny - 62 + 17 * math.sin(a)), fill="#7b858d", width=5)
    draw.rectangle((nx - 11, ny - 25, nx + 11, ny - 10), fill="#b6afb0")
    draw.polygon([(nx - 8, ny - 10), (nx + 8, ny - 10), (nx, ny)], fill="#d5a54e")
    buffer = io.BytesIO()
    image.save(buffer, "JPEG", quality=85)
    return buffer.getvalue()


class CameraHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        path = urlsplit(self.path).path
        if path not in ("/stream", "/snapshot"):
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Cache-Control", "no-store")
        if path == "/snapshot":
            frame = camera_frame()
            self.send_header("Content-Type", "image/jpeg")
            self.send_header("Content-Length", str(len(frame)))
            self.end_headers()
            self.wfile.write(frame)
            return
        self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
        self.end_headers()
        try:
            while True:
                frame = camera_frame()
                self.wfile.write(b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: " + str(len(frame)).encode() + b"\r\n\r\n" + frame + b"\r\n")
                self.wfile.flush()
                time.sleep(1 / 12)
        except (BrokenPipeError, ConnectionResetError):
            pass


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8081)
    parser.add_argument("--video", help="Loop a local video (up to the first 120 seconds)")
    args = parser.parse_args()
    if args.video:
        load_video(args.video)
    server = ThreadingHTTPServer(("127.0.0.1", args.port), CameraHandler)
    print(f"Demo stream: http://127.0.0.1:{args.port}/stream", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
