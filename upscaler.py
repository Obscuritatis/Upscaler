"""
Video Upscaler 4K
=================

Schaalt (oude) video's en foto's op naar 4K met Real-ESRGAN op de grafische kaart,
en gebruikt ffmpeg voor uitpakken, ontrafelen (deinterlace) en coderen.
Optioneel: minder flikkering, vloeiender beeld (RIFE) en gezichtsherstel (GFPGAN).

Starten:   dubbelklik op start.bat   (of:  py upscaler.py)
Zonder venster (voor gevorderden):
           py upscaler.py --cli video.mp4 foto.jpg map\\ [--uit map] [--sterkte 75] [--proef] ...
           (py upscaler.py --cli --help toont alle opties)

Bij de eerste start downloadt het programma zelf ffmpeg en Real-ESRGAN
naar de map "tools" naast dit bestand (ca. 200 MB). De extra's worden pas
gedownload als je ze voor het eerst aanzet.
"""

import argparse
import hashlib
import importlib
import json
import os
import queue
import random
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
import zipfile
from fractions import Fraction
from pathlib import Path

FROZEN = getattr(sys, "frozen", False)  # True in de .exe-versie (PyInstaller)
APP_DIR = Path(sys.executable).resolve().parent if FROZEN else Path(__file__).resolve().parent
TOOLS_DIR = APP_DIR / "tools"
EXE = ".exe" if os.name == "nt" else ""

FFMPEG_URL = "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-win64-gpl.zip"
ESRGAN_URL = "https://github.com/xinntao/Real-ESRGAN/releases/download/v0.2.5.0/realesrgan-ncnn-vulkan-20220424-windows.zip"
RIFE_URL = "https://github.com/nihui/rife-ncnn-vulkan/releases/download/20221029/rife-ncnn-vulkan-20221029-windows.zip"
RIFE_MODEL = "rife-v4.6"
FACE_MODELS = {
    "gfpgan_1.4.onnx": "https://github.com/facefusion/facefusion-assets/releases/download/models-3.0.0/gfpgan_1.4.onnx",
    "yoloface_8n.onnx": "https://github.com/facefusion/facefusion-assets/releases/download/models-3.0.0/yoloface_8n.onnx",
}

TARGET_W, TARGET_H = 3840, 2160
PREVIEW_SECONDS = 10

MODELS = {
    # sleutel: (ncnn-modelnaam zonder schaal, mogelijke schaalfactoren)
    "echt": ("realesrgan-x4plus", [4]),
    "animatie": ("realesr-animevideov3", [2, 3, 4]),
}

CODECS = {
    "h265": ["hevc_nvenc", "libx265", "libx264"],
    "h264": ["h264_nvenc", "libx264"],
}

VIDEO_EXTS = {".mp4", ".mkv", ".avi", ".mov", ".mpg", ".mpeg", ".m2ts", ".mts", ".ts", ".vob", ".wmv", ".flv",
              ".3gp", ".webm", ".dv", ".m4v"}
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".bmp", ".tif", ".tiff", ".webp"}

# Mengverhouding gezichtsherstel: lager = gezicht blijft dichter bij het origineel
FACE_WEIGHT = 0.7

NO_WINDOW = 0x08000000 if os.name == "nt" else 0  # CREATE_NO_WINDOW


class Cancelled(Exception):
    pass


class UpscaleError(Exception):
    pass


class Skipped(Exception):
    pass


def is_image(path):
    return Path(path).suffix.lower() in IMAGE_EXTS


def is_output_name(path):
    stem = Path(path).stem
    return stem.endswith("_4K") or stem.endswith("_4K_proef")


def collect_media(paths):
    """Losse bestanden blijven staan; mappen worden (met submappen) doorzocht op video's en foto's."""
    found = []
    for p in map(Path, paths):
        if p.is_dir():
            for root, dirs, files in os.walk(p):
                dirs[:] = sorted(d for d in dirs if not d.startswith(".upscale_"))
                for f in sorted(files):
                    fp = Path(root) / f
                    if fp.suffix.lower() in VIDEO_EXTS | IMAGE_EXTS and not is_output_name(fp):
                        found.append(fp)
        else:
            found.append(p)
    return found


# --------------------------------------------------------------------------
# Hulpprogramma's vinden of downloaden
# --------------------------------------------------------------------------

def tool_paths():
    return {
        "ffmpeg": os.environ.get("UPSCALER_FFMPEG") or str(TOOLS_DIR / "ffmpeg" / f"ffmpeg{EXE}"),
        "ffprobe": os.environ.get("UPSCALER_FFPROBE") or str(TOOLS_DIR / "ffmpeg" / f"ffprobe{EXE}"),
        "esrgan": os.environ.get("UPSCALER_ESRGAN") or str(TOOLS_DIR / "realesrgan" / f"realesrgan-ncnn-vulkan{EXE}"),
        "rife": os.environ.get("UPSCALER_RIFE") or str(TOOLS_DIR / "rife" / f"rife-ncnn-vulkan{EXE}"),
        "faces": Path(os.environ.get("UPSCALER_FACES_DIR") or TOOLS_DIR / "faces"),
    }


def _download(url, dest, log, progress):
    log(f"Downloaden: {url.rsplit('/', 1)[-1]}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    with urllib.request.urlopen(url) as r, open(tmp, "wb") as f:
        total = int(r.headers.get("Content-Length") or 0)
        done = 0
        while True:
            chunk = r.read(1 << 20)
            if not chunk:
                break
            f.write(chunk)
            done += len(chunk)
            if total:
                progress(done / total * 100, f"Downloaden… {done >> 20} / {total >> 20} MB")
    tmp.replace(dest)


def ensure_tools(log, progress, settings=None):
    t = tool_paths()
    settings = settings or {}

    if not (shutil.which(t["ffmpeg"]) and shutil.which(t["ffprobe"])):
        z = TOOLS_DIR / "ffmpeg.zip"
        _download(FFMPEG_URL, z, log, progress)
        out = TOOLS_DIR / "ffmpeg"
        out.mkdir(exist_ok=True)
        with zipfile.ZipFile(z) as zf:
            for name in zf.namelist():
                base = name.rsplit("/", 1)[-1]
                if base in ("ffmpeg.exe", "ffprobe.exe"):
                    (out / base).write_bytes(zf.read(name))
        z.unlink()
        log("ffmpeg geïnstalleerd.")

    if not shutil.which(t["esrgan"]):
        z = TOOLS_DIR / "realesrgan.zip"
        _download(ESRGAN_URL, z, log, progress)
        with zipfile.ZipFile(z) as zf:
            zf.extractall(TOOLS_DIR / "realesrgan")
        z.unlink()
        log("Real-ESRGAN geïnstalleerd.")

    if settings.get("smooth") and not shutil.which(t["rife"]):
        z = TOOLS_DIR / "rife.zip"
        _download(RIFE_URL, z, log, progress)
        out = TOOLS_DIR / "rife"
        with zipfile.ZipFile(z) as zf:
            for name in zf.namelist():
                rel = name.split("/", 1)[-1]  # bovenste map overslaan
                if not rel or name.endswith("/"):
                    continue
                if rel in (f"rife-ncnn-vulkan{EXE}", "vcomp140.dll") or rel.startswith(RIFE_MODEL + "/"):
                    dest = out / rel
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    dest.write_bytes(zf.read(name))
        z.unlink()
        log("RIFE (vloeiender beeld) geïnstalleerd.")

    if settings.get("faces"):
        ensure_face_deps(log)
        for name, url in FACE_MODELS.items():
            if not (t["faces"] / name).exists():
                _download(url, t["faces"] / name, log, progress)
    return t


def ensure_face_deps(log):
    try:
        import cv2  # noqa: F401
        import numpy  # noqa: F401
        import onnxruntime  # noqa: F401
        return
    except ImportError:
        if FROZEN:
            raise UpscaleError("De onderdelen voor gezichtsherstel ontbreken in deze versie van het programma.")
    pkgs = ["numpy", "opencv-python-headless", "onnxruntime-directml" if os.name == "nt" else "onnxruntime"]
    log("Extra onderdelen voor gezichtsherstel installeren (eenmalig, ca. 100 MB)…")
    r = subprocess.run([sys.executable, "-m", "pip", "install", "--disable-pip-version-check", *pkgs],
                       capture_output=True, creationflags=NO_WINDOW)
    if r.returncode != 0:
        raise UpscaleError("Installeren van de onderdelen voor gezichtsherstel is mislukt:\n"
                           + (r.stdout + r.stderr).decode(errors="replace")[-1200:])
    importlib.invalidate_caches()


def keep_awake(on):
    """Voorkom dat Windows in slaapstand gaat terwijl er gewerkt wordt."""
    if os.name == "nt":
        try:
            import ctypes
            ctypes.windll.kernel32.SetThreadExecutionState(0x80000000 | (0x00000001 if on else 0))
        except Exception:
            pass


# --------------------------------------------------------------------------
# Gezichtsherstel (GFPGAN via onnxruntime; op Windows via DirectML op de grafische kaart)
# --------------------------------------------------------------------------

_FACE_RESTORER = None


class FaceRestorer:
    # Positie van ogen, neus en mondhoeken in een 512×512 gezicht (FFHQ-sjabloon)
    TEMPLATE = [[0.37691676, 0.46864664], [0.62285697, 0.46912813], [0.50123859, 0.61331904],
                [0.39308822, 0.72541100], [0.61150205, 0.72490465]]

    def __init__(self, model_dir, log):
        import cv2
        import numpy as np
        import onnxruntime as ort
        self.cv2, self.np = cv2, np
        avail = ort.get_available_providers()
        providers = [p for p in ("DmlExecutionProvider", "CUDAExecutionProvider") if p in avail] + ["CPUExecutionProvider"]
        so = ort.SessionOptions()
        so.enable_mem_pattern = False
        so.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
        so.log_severity_level = 3
        self.det = ort.InferenceSession(str(model_dir / "yoloface_8n.onnx"), so, providers=providers)
        self.gfp = ort.InferenceSession(str(model_dir / "gfpgan_1.4.onnx"), so, providers=providers)
        self.gfp_in = self.gfp.get_inputs()[0].name
        self.det_in = self.det.get_inputs()[0].name
        log("Gezichtsherstel draait op " + ("de grafische kaart." if providers[0] != "CPUExecutionProvider"
                                             else "de processor (trager)."))
        self.template = np.array(self.TEMPLATE, np.float32) * 512
        m = np.ones((512, 512), np.float32)
        blur = int(512 * 0.5 * 0.3)
        edge = max(blur // 2, 1)
        m[:edge, :] = m[-edge:, :] = 0
        m[:, :edge] = m[:, -edge:] = 0
        self.mask = cv2.GaussianBlur(m, (0, 0), blur * 0.25)

    def detect(self, img, threshold=0.5):
        cv2, np = self.cv2, self.np
        h, w = img.shape[:2]
        k = 640 / max(h, w)
        small = cv2.resize(img, (round(w * k), round(h * k)), interpolation=cv2.INTER_AREA)
        pad = np.zeros((640, 640, 3), np.float32)
        pad[:small.shape[0], :small.shape[1]] = small[:, :, ::-1]
        d = self.det.run(None, {self.det_in: (pad / 255.0).transpose(2, 0, 1)[None].astype(np.float32)})[0][0].T
        d = d[d[:, 4] > threshold]
        if not len(d):
            return []
        boxes = [[float((x - bw / 2) / k), float((y - bh / 2) / k), float(bw / k), float(bh / k)]
                 for x, y, bw, bh in d[:, :4]]
        keep = cv2.dnn.NMSBoxes(boxes, d[:, 4].tolist(), threshold, 0.4)
        return [(boxes[i], d[i, 5:].reshape(5, 3)[:, :2] / k) for i in np.array(keep).flatten()]

    def restore(self, img, min_size=48):
        cv2, np = self.cv2, self.np
        h, w = img.shape[:2]
        out = img.astype(np.float32)
        n = 0
        for box, lm in self.detect(img):
            if min(box[2], box[3]) < min_size:
                continue
            M, _ = cv2.estimateAffinePartial2D(lm.astype(np.float32), self.template,
                                               method=cv2.RANSAC, ransacReprojThreshold=100)
            if M is None:
                continue
            crop = cv2.warpAffine(img, M, (512, 512), borderMode=cv2.BORDER_REPLICATE, flags=cv2.INTER_AREA)
            x = ((crop[:, :, ::-1] / 255.0 - 0.5) / 0.5).transpose(2, 0, 1)[None].astype(np.float32)
            y = self.gfp.run(None, {self.gfp_in: x})[0][0]
            y = ((np.clip(y, -1, 1) + 1) / 2).transpose(1, 2, 0)[:, :, ::-1] * 255
            y = y * FACE_WEIGHT + crop.astype(np.float32) * (1 - FACE_WEIGHT)
            inv = cv2.invertAffineTransform(M)
            back = cv2.warpAffine(y, inv, (w, h), borderMode=cv2.BORDER_REPLICATE)
            mk = cv2.warpAffine(self.mask, inv, (w, h)).clip(0, 1)[:, :, None]
            out = out * (1 - mk) + back * mk
            n += 1
        return out.round().clip(0, 255).astype(np.uint8), n

    def restore_file(self, path):
        """Herstelt gezichten in een PNG en overschrijft die. Geeft het aantal gezichten terug."""
        cv2, np = self.cv2, self.np
        img = cv2.imdecode(np.fromfile(str(path), np.uint8), cv2.IMREAD_UNCHANGED)
        if img is None or img.ndim != 3:
            return 0
        alpha = img[:, :, 3:] if img.shape[2] == 4 else None
        out, n = self.restore(np.ascontiguousarray(img[:, :, :3]))
        if n:
            if alpha is not None:
                out = np.concatenate([out, alpha], axis=2)
            ok, buf = cv2.imencode(".png", out, [cv2.IMWRITE_PNG_COMPRESSION, 1])
            if ok:
                buf.tofile(str(path))
        return n


def get_face_restorer(model_dir, log):
    global _FACE_RESTORER
    if _FACE_RESTORER is None:
        _FACE_RESTORER = FaceRestorer(model_dir, log)
    return _FACE_RESTORER


# --------------------------------------------------------------------------
# Video verwerken
# --------------------------------------------------------------------------

class Job:
    _gpu = None  # gevonden GPU-index, gedeeld door alle opdrachten

    def __init__(self, src, out_dir, settings, log, progress, cancel_event):
        self.src = Path(src).resolve()
        self.out_dir = Path(out_dir).resolve() if out_dir else self.src.parent
        self.s = settings
        self.log = log
        self.progress = progress
        self.cancel = cancel_event
        t = tool_paths()
        self.ffmpeg, self.ffprobe, self.esrgan, self.rife = t["ffmpeg"], t["ffprobe"], t["esrgan"], t["rife"]
        self.face_dir = t["faces"]
        self.proc = None
        self.preview_start = 0.0

    # -- processen -------------------------------------------------------

    def run(self, cmd, logfile):
        """Start een proces, wacht erop, en breek af als op Stop gedrukt is."""
        self.check_cancel()
        with open(logfile, "ab") as out:
            self.proc = subprocess.Popen(cmd, stdout=out, stderr=out, stdin=subprocess.DEVNULL, creationflags=NO_WINDOW)
            try:
                while self.proc.poll() is None:
                    if self.cancel.is_set():
                        self.proc.kill()
                    time.sleep(0.3)
                rc = self.proc.wait()
            finally:
                self.proc = None
        self.check_cancel()
        if rc != 0:
            tail = Path(logfile).read_text(errors="replace")[-1500:]
            raise UpscaleError(f"{Path(cmd[0]).name} gaf een fout (code {rc}):\n{tail}")

    def run_watch(self, cmd, logfile, out_dir, total, report):
        """Zoals run(), maar meldt ondertussen hoeveel beelden er al in out_dir staan."""
        self.check_cancel()
        with open(logfile, "ab") as out:
            p = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=out, stdin=subprocess.DEVNULL,
                                 cwd=Path(cmd[0]).parent, creationflags=NO_WINDOW)
            self.proc = p
            try:
                while p.poll() is None:
                    if self.cancel.is_set():
                        p.kill()
                        p.wait()
                        raise Cancelled()
                    time.sleep(1)
                    report(len(list(out_dir.glob("*.png"))))
            finally:
                self.proc = None
        produced = len(list(out_dir.glob("*.png")))
        if p.returncode != 0 or produced < total:
            raise UpscaleError(f"{Path(cmd[0]).stem} is gestopt ({produced} van {total} beelden klaar).\n"
                               + Path(logfile).read_text(errors="replace")[-1200:])

    def check_cancel(self):
        if self.cancel.is_set():
            raise Cancelled()

    def kill(self):
        p = self.proc
        if p and p.poll() is None:
            p.kill()

    # -- analyse ---------------------------------------------------------

    def probe(self):
        r = subprocess.run([self.ffprobe, "-v", "error", "-show_streams", "-show_format", "-of", "json", str(self.src)],
                           capture_output=True, creationflags=NO_WINDOW)
        if r.returncode != 0:
            raise UpscaleError(f"Kan de video niet lezen:\n{r.stderr.decode(errors='replace')}")
        info = json.loads(r.stdout)
        v = next((st for st in info["streams"] if st.get("codec_type") == "video"
                  and not st.get("disposition", {}).get("attached_pic")), None)
        if not v:
            raise UpscaleError("Geen videospoor gevonden in dit bestand.")
        audio = [st for st in info["streams"] if st.get("codec_type") == "audio"]

        fps = None
        for key in ("avg_frame_rate", "r_frame_rate"):
            try:
                f = Fraction(v.get(key, "0/0"))
                if 1 <= f <= 240:
                    fps = f
                    break
            except (ZeroDivisionError, ValueError):
                pass
        fps = fps or Fraction(25)

        try:
            sar = Fraction(v.get("sample_aspect_ratio", "1:1").replace(":", "/"))
            if sar <= 0:
                sar = Fraction(1)
        except (ZeroDivisionError, ValueError):
            sar = Fraction(1)

        duration = float(v.get("duration") or info["format"].get("duration") or 0)
        rotation = 0
        for sd in v.get("side_data_list", []):
            if "rotation" in sd:
                rotation = abs(int(sd["rotation"])) % 180
        w, h = int(v["width"]), int(v["height"])
        if rotation == 90:  # ffmpeg draait automatisch bij het uitpakken
            w, h = h, w
            sar = 1 / sar

        return {
            "w": w, "h": h, "fps": fps, "sar": sar, "duration": duration,
            "interlaced": v.get("field_order") in ("tt", "bb", "tb", "bt"),
            "audio": [a.get("codec_name") for a in audio],
        }

    def detect_gpu(self, work):
        """Zoek welke GPU-index de NVIDIA-kaart is (laptops hebben soms ook een ingebouwde GPU)."""
        if Job._gpu is not None:
            return Job._gpu
        test_in, test_out = work / "gputest.png", work / "gputest_out.png"
        subprocess.run([self.ffmpeg, "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=gray:s=32x32",
                        "-frames:v", "1", str(test_in)], creationflags=NO_WINDOW)
        r = subprocess.run([self.esrgan, "-i", str(test_in), "-o", str(test_out), "-n", "realesr-animevideov3-x2", "-s", "2"],
                           capture_output=True, creationflags=NO_WINDOW, cwd=Path(self.esrgan).parent)
        text = (r.stderr + r.stdout).decode(errors="replace")
        gpus = {}
        for m in re.finditer(r"^\[(\d+) ([^\]]+)\]\s+queueC=", text, re.M):
            gpus[int(m.group(1))] = m.group(2).strip()
        for p in (test_in, test_out):
            p.unlink(missing_ok=True)
        if not gpus:
            raise UpscaleError(
                "Real-ESRGAN vindt geen geschikte grafische kaart (Vulkan).\n"
                "Werk het NVIDIA-stuurprogramma bij via nvidia.com of de GeForce/NVIDIA app.\n\n" + text[-800:])
        pick = next((i for i, n in gpus.items() if re.search(r"nvidia|geforce|rtx", n, re.I)), min(gpus))
        self.log(f"Grafische kaart: {gpus[pick]}")
        if not re.search(r"nvidia|geforce|rtx", gpus[pick], re.I):
            self.log("Let op: geen NVIDIA-kaart gevonden, dit kan erg traag zijn.")
        Job._gpu = pick
        return pick

    def pick_encoder(self):
        for enc in CODECS[self.s["codec"]]:
            r = subprocess.run([self.ffmpeg, "-v", "error", "-f", "lavfi", "-i", "color=c=black:s=1920x1080:d=0.2",
                                "-c:v", enc, "-f", "null", "-"], capture_output=True, creationflags=NO_WINDOW)
            if r.returncode == 0:
                return enc
        raise UpscaleError("Geen bruikbare video-encoder gevonden in ffmpeg.")

    @staticmethod
    def encoder_args(enc):
        if enc == "hevc_nvenc":
            return ["-c:v", enc, "-preset", "p6", "-tune", "hq", "-rc", "vbr", "-cq", "18", "-b:v", "0",
                    "-profile:v", "main10", "-tag:v", "hvc1"], "p010le"
        if enc == "h264_nvenc":
            return ["-c:v", enc, "-preset", "p6", "-tune", "hq", "-rc", "vbr", "-cq", "18", "-b:v", "0",
                    "-profile:v", "high"], "yuv420p"
        if enc == "libx265":
            return ["-c:v", enc, "-preset", "medium", "-crf", "18", "-tag:v", "hvc1",
                    "-x265-params", "log-level=error"], "yuv420p10le"
        return ["-c:v", "libx264", "-preset", "medium", "-crf", "17"], "yuv420p"

    def restore_faces(self, folder, label):
        fr = get_face_restorer(self.face_dir, self.log)
        files = sorted(folder.glob("*.png"))
        found = 0
        for i, f in enumerate(files, 1):
            self.check_cancel()
            found += fr.restore_file(f) > 0
            if i % 10 == 0 or i == len(files):
                self.progress(None, f"{label}: gezichten herstellen {i}/{len(files)}…")
        return found

    # -- hoofdroutine ----------------------------------------------------

    def execute(self):
        s = self.s
        preview = s.get("preview", False)
        out_file = self.out_dir / f"{self.src.stem}{'_4K_proef' if preview else '_4K'}.mp4"
        if out_file.exists() and not preview:
            raise Skipped(f"{out_file.name} bestaat al")

        info = self.probe()
        self.log(f"Bron: {info['w']}×{info['h']}, {float(info['fps']):.3f} beelden/s, "
                 f"{info['duration']:.0f} s{', geïnterlaced' if info['interlaced'] else ''}")

        # Schaalfactor van het AI-model kiezen
        model, scales = MODELS[s["model"]]
        scale = next((f for f in scales if info["h"] * f >= TARGET_H * 0.9), scales[-1])
        model_name = model if len(scales) == 1 else f"{model}-x{scale}"

        # Eindformaat bepalen met de juiste beeldverhouding (oude video heeft vaak niet-vierkante pixels)
        dar = info["w"] * info["sar"] / info["h"]
        if dar >= Fraction(TARGET_W, TARGET_H):
            tw, th = TARGET_W, int(round(TARGET_W / dar / 2)) * 2
        else:
            tw, th = int(round(TARGET_H * dar / 2)) * 2, TARGET_H
        self.log(f"Doel: {tw}×{th}" + (f" (met zwarte balken tot {TARGET_W}×{TARGET_H})"
                                        if s["pad"] and (tw, th) != (TARGET_W, TARGET_H) else ""))

        if info["h"] >= 1440:
            self.log("Let op: deze video is al bijna 4K; opschalen voegt weinig toe en duurt erg lang.")

        # Werkmap (maakt hervatten mogelijk na een onderbreking)
        st = self.src.stat()
        key = json.dumps([str(self.src), st.st_size, st.st_mtime, s], sort_keys=True)
        work = self.out_dir / f".upscale_werk_{self.src.stem[:30]}_{hashlib.sha1(key.encode()).hexdigest()[:8]}"
        work.mkdir(parents=True, exist_ok=True)
        frames, b_in, b_out = work / "frames", work / "b_in", work / "b_out"
        for d in (frames, b_in, b_out):
            d.mkdir(exist_ok=True)
        state_file = work / "state.json"
        state = json.loads(state_file.read_text()) if state_file.exists() else {}
        log_file = work / "log.txt"

        gpu = self.detect_gpu(work)
        enc = self.pick_encoder()
        self.log(f"Encoder: {enc}" + ("" if "nvenc" in enc else " (geen NVENC beschikbaar, codering via processor is trager)"))

        seek = []
        if preview:
            self.preview_start = max(0.0, info["duration"] / 2 - PREVIEW_SECONDS / 2)
            seek = ["-ss", f"{self.preview_start:.3f}", "-t", str(PREVIEW_SECONDS)]

        deint = s["deinterlace"] == "aan" or (s["deinterlace"] == "auto" and info["interlaced"])
        smooth = s.get("smooth") and info["fps"] <= 31

        # 1. Beelden uitpakken -----------------------------------------
        if not state.get("extracted"):
            for p in frames.iterdir():
                p.unlink()
            fps = info["fps"]
            vf = []
            if deint:
                if smooth:
                    # Elk halfbeeld wordt een eigen beeld: echte 50/60 beelden per seconde, zonder te raden
                    vf.append("bwdif=mode=send_field:deint=all")
                    fps *= 2
                    self.log("Ontrafelen naar dubbele beeldsnelheid (vloeiender beeld).")
                else:
                    vf.append("bwdif=mode=send_frame:deint=all")
                    self.log("Ontrafelen (deinterlace) staat aan.")
            if s["denoise"]:
                vf.append("hqdn3d=2:1.5:4:4")
            vf.append("format=rgb24")
            est = info["duration"] if not preview else PREVIEW_SECONDS
            free = shutil.disk_usage(work).free
            need = info["w"] * info["h"] * 2 * float(fps) * est * (2 if smooth and not deint else 1)
            if free < need * 1.3:
                self.log(f"Waarschuwing: mogelijk te weinig schijfruimte (vrij {free / 1e9:.0f} GB, "
                         f"nodig ca. {need * 1.3 / 1e9:.0f} GB).")
            self.progress(0, "Beelden uitpakken…")
            self.log("Beelden uitpakken…")
            self.run([self.ffmpeg, "-hide_banner", "-y", *seek, "-i", str(self.src), "-map", "0:v:0",
                      "-vf", ",".join(vf), "-fps_mode", "cfr", "-r", str(fps),
                      "-compression_level", "1", str(frames / "%08d.png")], log_file)
            total = len(list(frames.glob("*.png")))
            if total == 0:
                raise UpscaleError("Er zijn geen beelden uit de video gehaald.")
            state.update(extracted=True, total=total, fps=str(fps), interpolated=not (smooth and not deint))
            state_file.write_text(json.dumps(state))

        # 1b. Tussenbeelden berekenen met RIFE (alleen bij niet-geïnterlacede video)
        if not state.get("interpolated"):
            n = state["total"]
            new = work / "frames_rife"
            if new.exists():
                shutil.rmtree(new)
            new.mkdir()
            self.log(f"Tussenbeelden berekenen met RIFE ({n} → {n * 2} beelden)…")
            self.run_watch([self.rife, "-i", str(frames), "-o", str(new), "-m", RIFE_MODEL, "-g", str(gpu),
                            "-f", "%08d.png"], log_file, new, n * 2 - 1,
                           lambda c: self.progress(c / (n * 2) * 5, f"Tussenbeelden {c} van {n * 2}…"))
            for i, p in enumerate(sorted(new.glob("*.png")), 1):  # nummering zeker gelijk trekken
                p.rename(new / f"{i:08d}.tmp")
            for p in new.glob("*.tmp"):
                p.rename(p.with_suffix(".png"))
            shutil.rmtree(frames)
            new.rename(frames)
            state.update(interpolated=True, total=len(list(frames.glob("*.png"))),
                         fps=str(Fraction(state["fps"]) * 2))
            state_file.write_text(json.dumps(state))

        if "batch" not in state:
            px = info["w"] * scale * info["h"] * scale
            state["batch"] = int(os.environ.get("UPSCALER_BATCH") or max(30, min(300, int(6e9 / (px * 2)))))
            state_file.write_text(json.dumps(state))
        total, batch, fps = state["total"], state["batch"], state["fps"]

        # Halverwege afgebroken batch terugzetten
        for p in b_in.iterdir():
            p.replace(frames / p.name)
        for p in b_out.iterdir():
            p.unlink()

        # 2. Per stuk opschalen en coderen ------------------------------
        enc_args, pix_fmt = self.encoder_args(enc)
        strength = max(0, min(100, int(s["strength"]))) / 100
        n_batches = (total + batch - 1) // batch
        segments = []
        t0, done_this_run = time.time(), 0
        self.log(f"Opschalen met {model_name} ({total} beelden, {n_batches} stuk(ken))…")

        for k in range(n_batches):
            seg = work / f"seg_{k:05d}.mkv"
            segments.append(seg)
            if seg.exists():
                continue
            first = k * batch + 1
            last = min(total, first + batch - 1)
            names = [f"{i:08d}.png" for i in range(first, last + 1)]
            for n in names:
                (frames / n).replace(b_in / n)

            self.run_watch([self.esrgan, "-i", str(b_in), "-o", str(b_out), "-n", model_name, "-s", str(scale),
                            "-g", str(gpu), "-f", "png"], log_file, b_out, len(names),
                           lambda c: self._report(k * batch + c, total, t0, done_this_run))

            if s.get("faces"):
                self.restore_faces(b_out, f"Stuk {k + 1}/{n_batches}")

            # Coderen: AI-beeld (eventueel gemengd met gewoon vergroot origineel) → 4K
            chain = [f"[0:v]scale={tw}:{th}:flags=lanczos,format=gbrp[ai]"]
            label = "ai"
            if strength < 1:
                chain.append(f"[1:v]scale={tw}:{th}:flags=lanczos,format=gbrp[orig]")
                chain.append(f"[ai][orig]blend=all_mode=normal:all_opacity={strength:.2f}[mix]")
                label = "mix"
            tail = []
            if s.get("deflicker"):
                # Middelt alleen pixels die tussen opeenvolgende beelden bijna gelijk zijn: rustiger detail, geen vegen
                tail.append("atadenoise=0a=0.03:0b=0.06:1a=0.03:1b=0.06:2a=0.03:2b=0.06:s=5")
            tail.append("setsar=1")
            if s["pad"] and (tw, th) != (TARGET_W, TARGET_H):
                tail.append(f"pad={TARGET_W}:{TARGET_H}:(ow-iw)/2:(oh-ih)/2:color=black")
            tail.append(f"scale=out_color_matrix=bt709:out_range=tv,format={pix_fmt}")
            chain.append(f"[{label}]{','.join(tail)}[v]")

            cmd = [self.ffmpeg, "-hide_banner", "-y",
                   "-framerate", fps, "-start_number", str(first), "-i", str(b_out / "%08d.png")]
            if strength < 1:
                cmd += ["-framerate", fps, "-start_number", str(first), "-i", str(b_in / "%08d.png")]
            tmp = seg.with_suffix(".tmp.mkv")
            cmd += ["-filter_complex", ";".join(chain), "-map", "[v]", *enc_args,
                    "-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709",
                    "-frames:v", str(len(names)), str(tmp)]
            self.progress(None, f"Stuk {k + 1}/{n_batches} coderen…")
            self.run(cmd, log_file)
            tmp.replace(seg)

            for p in list(b_in.iterdir()) + list(b_out.iterdir()):
                p.unlink()
            done_this_run += len(names)
            self._report(last, total, t0, done_this_run)

        # 3. Samenvoegen met het originele geluid -------------------------
        self.progress(99, "Samenvoegen met geluid…")
        self.log("Samenvoegen met geluid…")
        lst = work / "segments.txt"
        lst.write_text("".join("file '" + p.as_posix().replace("'", "'\\''") + "'\n" for p in segments), encoding="utf-8")
        copyable = {"aac", "mp3", "ac3", "eac3"}
        audio_args = ["-c:a", "copy"] if info["audio"] and all(c in copyable for c in info["audio"]) \
            else ["-c:a", "aac", "-b:a", "256k"]
        tmp_out = out_file.with_suffix(".tmp.mp4")
        self.run([self.ffmpeg, "-hide_banner", "-y", "-f", "concat", "-safe", "0", "-i", str(lst),
                  *seek, "-i", str(self.src),
                  "-map", "0:v", "-map", "1:a?", "-map_metadata", "1", "-c:v", "copy", *audio_args,
                  "-movflags", "+faststart", str(tmp_out)], log_file)
        tmp_out.replace(out_file)
        shutil.rmtree(work, ignore_errors=True)
        self.progress(100, "Klaar")
        self.log(f"Klaar: {out_file}")
        return out_file

    def _report(self, done, total, t0, done_this_run):
        pct = done / total * 100
        elapsed = time.time() - t0
        msg = f"Beeld {done} van {total}"
        if elapsed > 20 and done_this_run > 0:
            rate = done_this_run / elapsed
            left = (total - done) / rate
            h, m = divmod(int(left // 60), 60)
            msg += f" · {rate:.1f} beelden/s · nog ca. {h} u {m:02d} min" if h else \
                f" · {rate:.1f} beelden/s · nog ca. {m} min"
        self.progress(min(pct, 99), msg)


# --------------------------------------------------------------------------
# Foto's
# --------------------------------------------------------------------------

# EXIF-oriëntatie → ffmpeg-filters om de foto rechtop te zetten
ORIENT_FILTERS = {2: ["hflip"], 3: ["hflip", "vflip"], 4: ["vflip"], 5: ["transpose=0"],
                  6: ["transpose=1"], 7: ["transpose=3"], 8: ["transpose=2"]}


def jpeg_orientation(path):
    """Lees de EXIF-oriëntatie (1-8) uit een JPEG; 1 als er niets bijzonders is."""
    if Path(path).suffix.lower() not in (".jpg", ".jpeg"):
        return 1
    try:
        data = Path(path).read_bytes()[:256 * 1024]
        if data[:2] != b"\xff\xd8":
            return 1
        i = 2
        while i + 4 <= len(data) and data[i] == 0xFF:
            marker, size = data[i + 1], int.from_bytes(data[i + 2:i + 4], "big")
            if marker == 0xE1 and data[i + 4:i + 10] == b"Exif\0\0":
                t = data[i + 10:i + 2 + size]
                bo = "little" if t[:2] == b"II" else "big"
                ifd = int.from_bytes(t[4:8], bo)
                for n in range(int.from_bytes(t[ifd:ifd + 2], bo)):
                    e = ifd + 2 + n * 12
                    if int.from_bytes(t[e:e + 2], bo) == 0x0112:
                        val = int.from_bytes(t[e + 8:e + 10], bo)
                        return val if 1 <= val <= 8 else 1
                return 1
            if marker == 0xDA:
                break
            i += 2 + size
    except (OSError, IndexError, ValueError):
        pass
    return 1


class ImageBatch(Job):
    """Schaalt een reeks foto's in één keer op (het AI-model wordt maar één keer geladen)."""

    def __init__(self, srcs, out_dir, settings, log, progress, cancel_event):
        super().__init__(srcs[0], out_dir, settings, log, progress, cancel_event)
        self.srcs = [Path(p).resolve() for p in srcs]
        self.user_out_dir = Path(out_dir).resolve() if out_dir else None
        self.skipped = []
        self.failed = []

    def out_path(self, src):
        jpg = src.suffix.lower() in (".jpg", ".jpeg")
        return (self.user_out_dir or src.parent) / f"{src.stem}_4K{'.jpg' if jpg else '.png'}"

    def execute(self):
        s = self.s
        base = self.user_out_dir or self.srcs[0].parent
        work = base / f".upscale_foto_werk_{os.getpid()}"
        d_in, d_out = work / "in", work / "out"
        d_in.mkdir(parents=True, exist_ok=True)
        d_out.mkdir(exist_ok=True)
        log_file = work / "log.txt"
        results = []
        try:
            # 1. Alle foto's omzetten naar PNG (zonder automatisch draaien; dat doen we zelf aan het eind)
            items = []
            for n, src in enumerate(self.srcs):
                self.progress(n / len(self.srcs) * 10, f"Foto's voorbereiden… ({n + 1}/{len(self.srcs)})")
                if self.out_path(src).exists() and not s.get("preview"):
                    self.log(f"Overgeslagen ({self.out_path(src).name} bestaat al)")
                    self.skipped.append(src)
                    continue
                r = subprocess.run([self.ffprobe, "-v", "error", "-select_streams", "v:0", "-show_entries",
                                    "stream=width,height,pix_fmt", "-of", "json", str(src)],
                                   capture_output=True, creationflags=NO_WINDOW)
                try:
                    st = json.loads(r.stdout)["streams"][0]
                    w, h = int(st["width"]), int(st["height"])
                except (ValueError, KeyError, IndexError):
                    self.log(f"Mislukt (kan foto niet lezen): {src.name}")
                    self.failed.append(src)
                    continue
                if max(w, h) >= TARGET_W * 0.95 and min(w, h) >= TARGET_H * 0.95:
                    self.log(f"Overgeslagen (is al 4K of groter): {src.name} ({w}×{h})")
                    self.skipped.append(src)
                    continue
                pf = st.get("pix_fmt", "")
                alpha = "a" in pf.replace("gray", "") or pf == "pal8"
                tmp = d_in / f"{n:05d}.png"
                self.run([self.ffmpeg, "-hide_banner", "-y", "-noautorotate", "-i", str(src), "-frames:v", "1",
                          "-vf", "format=rgba" if alpha else "format=rgb24", str(tmp)], log_file)
                items.append((src, tmp, w, h, alpha))
            if not items:
                return results

            # 2. AI-opschaling van de hele map in één keer
            gpu = self.detect_gpu(work)
            model, scales = MODELS[s["model"]]
            scale = scales[-1]
            model_name = model if len(scales) == 1 else f"{model}-x{scale}"
            self.log(f"Opschalen van {len(items)} foto('s) met {model_name}…")
            try:
                self.run_watch([self.esrgan, "-i", str(d_in), "-o", str(d_out), "-n", model_name, "-s", str(scale),
                                "-g", str(gpu), "-f", "png"], log_file, d_out, len(items),
                               lambda c: self.progress(10 + c / len(items) * 80,
                                                       f"Foto {min(c + 1, len(items))} van {len(items)} opschalen…"))
            except UpscaleError as e:
                self.log(str(e).splitlines()[0])  # wat wel gelukt is, gaan we gewoon afmaken

            if s.get("faces"):
                found = self.restore_faces(d_out, "Foto's")
                self.log(f"Gezichten hersteld in {found} foto('s).")

            # 3. Mengen met het origineel, op 4K-maat brengen, rechtop zetten en opslaan
            strength = max(0, min(100, int(s["strength"]))) / 100
            for src, tmp, w, h, alpha in items:
                self.check_cancel()
                ai = d_out / tmp.name
                if not ai.exists():
                    self.log(f"Mislukt: {src.name}")
                    self.failed.append(src)
                    continue
                orient = jpeg_orientation(src)
                dw, dh = (h, w) if orient >= 5 else (w, h)
                box_w, box_h = (TARGET_W, TARGET_H) if dw >= dh else (TARGET_H, TARGET_W)
                f = min(box_w / dw, box_h / dh, 8.0)
                tw, th = max(1, round(w * f)), max(1, round(h * f))

                jpg = src.suffix.lower() in (".jpg", ".jpeg")
                fmt = "gbrap" if alpha and not jpg else "gbrp"
                chain = [f"[0:v]scale={tw}:{th}:flags=lanczos,format={fmt}[ai]"]
                label = "ai"
                if strength < 1:
                    chain.append(f"[1:v]scale={tw}:{th}:flags=lanczos,format={fmt}[orig]")
                    chain.append(f"[ai][orig]blend=all_mode=normal:all_opacity={strength:.2f}[mix]")
                    label = "mix"
                chain.append(f"[{label}]" + ",".join(ORIENT_FILTERS.get(orient, []) + ["null"]) + "[v]")

                out_file = self.out_path(src)
                cmd = [self.ffmpeg, "-hide_banner", "-y", "-i", str(ai)]
                if strength < 1:
                    cmd += ["-i", str(tmp)]
                cmd += ["-filter_complex", ";".join(chain), "-map", "[v]", "-frames:v", "1"]
                cmd += ["-q:v", "1", "-pix_fmt", "yuvj444p"] if jpg else ["-pix_fmt", "rgba" if alpha else "rgb24"]
                cmd += ["-update", "1", str(out_file)]
                self.run(cmd, log_file)
                ow, oh = (th, tw) if orient >= 5 else (tw, th)
                self.log(f"Klaar: {out_file.name} ({ow}×{oh})")
                results.append(out_file)
        finally:
            self.proc = None
            shutil.rmtree(work, ignore_errors=True)
        self.progress(100, "Klaar")
        return results


# --------------------------------------------------------------------------
# Voor/na vergelijken
# --------------------------------------------------------------------------

def make_compare_frames(entry, t, folder, view_w, view_h):
    """Maakt vier PNG's: voor/na op volle grootte en verkleind tot het venster."""
    tp = tool_paths()
    ff, fp = tp["ffmpeg"], tp["ffprobe"]
    src, out = Path(entry["src"]), Path(entry["out"])
    after, before = folder / "na.png", folder / "voor.png"
    seek_out = [] if entry["image"] else ["-ss", f"{t:.3f}"]
    subprocess.run([ff, "-v", "error", "-y", *seek_out, "-i", str(out), "-frames:v", "1", "-vf", "format=rgba",
                    "-update", "1", str(after)], creationflags=NO_WINDOW, check=True)
    r = subprocess.run([fp, "-v", "error", "-show_entries", "stream=width,height", "-of", "json", str(after)],
                       capture_output=True, creationflags=NO_WINDOW, check=True)
    st = json.loads(r.stdout)["streams"][0]
    W, H = int(st["width"]), int(st["height"])
    if entry["image"]:
        pre, seek_src = ORIENT_FILTERS.get(jpeg_orientation(src), []), ["-noautorotate"]
    else:
        pre, seek_src = ["bwdif=deint=interlaced"], ["-ss", f"{entry['offset'] + t:.3f}"]
    vf = ",".join(pre + ["scale=w='iw*if(gt(sar,0),sar,1)':h=ih", "setsar=1",
                         f"scale={W}:{H}:force_original_aspect_ratio=decrease:flags=lanczos",
                         f"pad={W}:{H}:(ow-iw)/2:(oh-ih)/2", "format=rgba"])
    subprocess.run([ff, "-v", "error", "-y", *seek_src, "-i", str(src), "-frames:v", "1", "-vf", vf,
                    "-update", "1", str(before)], creationflags=NO_WINDOW, check=True)
    small = f"scale={view_w}:{view_h}:force_original_aspect_ratio=decrease:flags=area,format=rgba"
    for name in ("voor", "na"):
        subprocess.run([ff, "-v", "error", "-y", "-i", str(folder / f"{name}.png"), "-vf", small, "-update", "1",
                        str(folder / f"{name}_klein.png")], creationflags=NO_WINDOW, check=True)
    return W, H


def media_duration(path):
    r = subprocess.run([tool_paths()["ffprobe"], "-v", "error", "-show_entries", "format=duration", "-of", "json",
                        str(path)], capture_output=True, creationflags=NO_WINDOW)
    try:
        return float(json.loads(r.stdout)["format"]["duration"])
    except (ValueError, KeyError):
        return 0.0


def open_compare(root, entries):
    import tkinter as tk
    from tkinter import ttk

    win = tk.Toplevel(root)
    win.title("Voor / na vergelijken")
    tmp = Path(tempfile.mkdtemp(prefix="upscale_vergelijk_"))
    cw = min(1280, win.winfo_screenwidth() - 120)
    ch = min(720, win.winfo_screenheight() - 260)
    st = {"div": 0.5, "zoom": False, "zx": 0, "zy": 0, "W": 0, "H": 0, "imgs": None, "comp": None, "t": 0.0,
          "busy": False}

    bar = ttk.Frame(win, padding=6)
    bar.pack(fill="x")
    names = [Path(e["out"]).name for e in entries]
    sel = tk.StringVar(value=names[-1])
    ttk.Label(bar, text="Bestand:").pack(side="left")
    cb = ttk.Combobox(bar, textvariable=sel, values=names, state="readonly", width=40)
    cb.pack(side="left", padx=4)
    btn_other = ttk.Button(bar, text="Ander moment")
    btn_other.pack(side="left", padx=4)
    btn_zoom = ttk.Button(bar, text="Inzoomen (100%)")
    btn_zoom.pack(side="left", padx=4)
    info = ttk.Label(win, text="Sleep in het beeld om de scheidslijn te verschuiven. "
                               "Dubbelklik om op die plek in te zoomen.", padding=(6, 0))
    info.pack(fill="x")

    canvas = tk.Canvas(win, width=cw, height=ch, bg="#202020", highlightthickness=0)
    canvas.pack(padx=6, pady=6)
    img_item = canvas.create_image(cw // 2, ch // 2, anchor="center")
    line = canvas.create_line(0, 0, 0, 0, fill="white", width=2)
    lbl_l = canvas.create_text(12, 12, text="VOOR", anchor="nw", fill="white", font=("Segoe UI", 12, "bold"))
    lbl_r = canvas.create_text(cw - 12, 12, text="NA", anchor="ne", fill="white", font=("Segoe UI", 12, "bold"))
    status = canvas.create_text(cw // 2, ch // 2, text="", fill="white", font=("Segoe UI", 12))

    def entry():
        return entries[names.index(sel.get())]

    def view_size():
        if st["zoom"]:
            return min(cw, st["W"]), min(ch, st["H"])
        return st["imgs"]["voor_klein"].width(), st["imgs"]["voor_klein"].height()

    def render():
        if not st["imgs"]:
            return
        vw, vh = view_size()
        if st["comp"] is None or (st["comp"].width(), st["comp"].height()) != (vw, vh):
            st["comp"] = tk.PhotoImage(width=vw, height=vh)
            canvas.itemconfig(img_item, image=st["comp"])
        if st["zoom"]:
            b, a, ox, oy = st["imgs"]["voor"], st["imgs"]["na"], st["zx"], st["zy"]
        else:
            b, a, ox, oy = st["imgs"]["voor_klein"], st["imgs"]["na_klein"], 0, 0
        d = int(st["div"] * vw)
        comp = st["comp"]
        if d > 0:
            comp.tk.call(comp, "copy", b, "-from", ox, oy, ox + d, oy + vh, "-to", 0, 0, "-compositingrule", "set")
        if d < vw:
            comp.tk.call(comp, "copy", a, "-from", ox + d, oy, ox + vw, oy + vh, "-to", d, 0, "-compositingrule", "set")
        x0, y0 = (cw - vw) // 2, (ch - vh) // 2
        canvas.coords(line, x0 + d, y0, x0 + d, y0 + vh)
        canvas.coords(lbl_l, x0 + 10, y0 + 10)
        canvas.coords(lbl_r, x0 + vw - 10, y0 + 10)

    def to_image_xy(ev):
        vw, vh = view_size()
        return ev.x - (cw - vw) // 2, ev.y - (ch - vh) // 2, vw, vh

    def on_drag(ev):
        if not st["imgs"]:
            return
        x, _, vw, _ = to_image_xy(ev)
        st["div"] = min(1.0, max(0.0, x / max(vw, 1)))
        render()

    def set_zoom(on, cx=None, cy=None):
        st["zoom"] = on
        if on:
            vw, vh = min(cw, st["W"]), min(ch, st["H"])
            cx = st["W"] / 2 if cx is None else cx
            cy = st["H"] / 2 if cy is None else cy
            st["zx"] = int(min(max(cx - vw / 2, 0), st["W"] - vw))
            st["zy"] = int(min(max(cy - vh / 2, 0), st["H"] - vh))
        btn_zoom.config(text="Heel beeld" if on else "Inzoomen (100%)")
        render()

    def on_double(ev):
        if not st["imgs"]:
            return
        if st["zoom"]:
            set_zoom(False)
            return
        x, y, vw, vh = to_image_xy(ev)
        set_zoom(True, x * st["W"] / vw, y * st["H"] / vh)

    def load(new_time=False):
        if st["busy"]:
            return
        e = entry()
        if not e["image"]:
            dur = media_duration(e["out"])
            st["t"] = random.uniform(0.5, max(0.6, dur - 0.5)) if new_time else dur / 2
        st["busy"] = True
        canvas.itemconfig(status, text="Beelden laden…")
        result = {}

        def bg():
            try:
                result["size"] = make_compare_frames(e, st["t"], tmp, cw, ch)
            except Exception as ex:  # noqa: BLE001
                result["error"] = str(ex)

        th = threading.Thread(target=bg, daemon=True)
        th.start()

        def wait():
            if th.is_alive():
                win.after(100, wait)
                return
            st["busy"] = False
            if "error" in result:
                canvas.itemconfig(status, text="Kon de beelden niet laden.")
                return
            canvas.itemconfig(status, text="")
            st["W"], st["H"] = result["size"]
            st["imgs"] = {n: tk.PhotoImage(file=str(tmp / f"{n}.png")) for n in ("voor", "na", "voor_klein", "na_klein")}
            st["comp"] = None
            btn_other.config(state="disabled" if e["image"] else "normal")
            set_zoom(st["zoom"])

        wait()

    canvas.bind("<B1-Motion>", on_drag)
    canvas.bind("<Button-1>", on_drag)
    canvas.bind("<Double-Button-1>", on_double)
    btn_other.config(command=lambda: load(True))
    btn_zoom.config(command=lambda: set_zoom(not st["zoom"]))
    cb.bind("<<ComboboxSelected>>", lambda _e: load())

    def close():
        shutil.rmtree(tmp, ignore_errors=True)
        win.destroy()

    win.protocol("WM_DELETE_WINDOW", close)
    load()
    return win


# --------------------------------------------------------------------------
# Een lijst bestanden verwerken (gedeeld door venster en opdrachtregel)
# --------------------------------------------------------------------------

def process_all(paths, out_dir, settings, log, progress, cancel, on_job=lambda j: None):
    """Verwerkt alles; een fout bij één bestand stopt de rest niet. Geeft (gelukt, overgeslagen, mislukt) terug."""
    done, skipped, failed = [], [], []
    images = [p for p in paths if is_image(p)]
    videos = [p for p in paths if not is_image(p)]
    if images:
        log(f"— {len(images)} foto('s)")
        job = ImageBatch(images, out_dir, settings, log, progress, cancel)
        on_job(job)
        try:
            for out in job.execute():
                done.append({"src": None, "out": out, "offset": 0.0, "image": True})
        except Cancelled:
            raise
        except Exception as e:  # noqa: BLE001
            log(f"FOUT bij foto's: {e}")
            failed.extend(images)
        else:
            src_by_out = {job.out_path(p): p for p in images}
            for d in done:
                d["src"] = src_by_out.get(d["out"])
            skipped += job.skipped
            failed += job.failed
    for i, f in enumerate(videos, 1):
        log(f"— Video {i} van {len(videos)}: {Path(f).name}")
        job = Job(f, out_dir, settings, log, progress, cancel)
        on_job(job)
        try:
            out = job.execute()
            done.append({"src": f, "out": out, "offset": job.preview_start, "image": False})
        except Cancelled:
            raise
        except Skipped as e:
            log(f"Overgeslagen ({e}).")
            skipped.append(f)
        except Exception as e:  # noqa: BLE001
            log(f"FOUT: {e}")
            failed.append(f)
    return done, skipped, failed


# --------------------------------------------------------------------------
# Venster (GUI)
# --------------------------------------------------------------------------

def run_gui():
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk

    try:
        from ctypes import windll
        windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass
    root = tk.Tk()
    root.title("Video Upscaler 4K")

    msgs = queue.Queue()
    cancel = threading.Event()
    files = []
    results = []
    worker = {"thread": None, "job": None}

    pad = {"padx": 10, "pady": 3}
    bold = ("Segoe UI", 10, "bold")
    frm = ttk.Frame(root, padding=10)
    frm.pack(fill="both", expand=True)
    frm.columnconfigure(0, weight=1)

    # 1. Bestanden
    ttk.Label(frm, text="1. Kies video's en/of foto's", font=bold).grid(row=0, column=0, sticky="w", **pad)
    lb = tk.Listbox(frm, height=5, selectmode="extended")
    lb.grid(row=1, column=0, columnspan=3, sticky="ew", **pad)
    count_lbl = ttk.Label(frm, text="")

    def refresh_list():
        lb.delete(0, "end")
        for f in files:
            lb.insert("end", str(f))
        nv = sum(not is_image(f) for f in files)
        count_lbl.config(text=f"{nv} video('s), {len(files) - nv} foto('s)" if files else "")
        if files and not out_var.get():
            out_var.set(str(Path(files[0]).parent))

    def add(paths):
        for f in collect_media(paths):
            if f not in files:
                files.append(f)
        refresh_list()

    def add_files():
        add(filedialog.askopenfilenames(title="Kies video's of foto's", filetypes=[
            ("Video's en foto's", " ".join(f"*{e}" for e in sorted(VIDEO_EXTS | IMAGE_EXTS))),
            ("Foto's", " ".join(f"*{e}" for e in sorted(IMAGE_EXTS))),
            ("Video", " ".join(f"*{e}" for e in sorted(VIDEO_EXTS))),
            ("Alle bestanden", "*.*")]))

    def add_folder():
        d = filedialog.askdirectory(title="Kies een map (bijvoorbeeld een seizoen)")
        if d:
            before = len(files)
            add([d])
            if len(files) == before:
                messagebox.showinfo("Video Upscaler 4K", "Geen video's of foto's gevonden in deze map.")

    def remove_selected():
        for i in reversed(lb.curselection()):
            del files[i]
        refresh_list()

    def clear_files():
        files.clear()
        refresh_list()

    bf = ttk.Frame(frm)
    bf.grid(row=2, column=0, columnspan=3, sticky="ew", **pad)
    ttk.Button(bf, text="Bestanden toevoegen…", command=add_files).pack(side="left")
    ttk.Button(bf, text="Hele map toevoegen…", command=add_folder).pack(side="left", padx=6)
    ttk.Button(bf, text="Verwijderen", command=remove_selected).pack(side="left")
    ttk.Button(bf, text="Lijst leegmaken", command=clear_files).pack(side="left", padx=6)
    count_lbl.pack(in_=bf, side="right")

    # 2. Uitvoermap
    ttk.Label(frm, text="2. Opslaan in map", font=bold).grid(row=3, column=0, sticky="w", **pad)
    out_var = tk.StringVar()
    ttk.Entry(frm, textvariable=out_var).grid(row=4, column=0, columnspan=2, sticky="ew", **pad)
    ttk.Button(frm, text="Bladeren…", command=lambda: out_var.set(filedialog.askdirectory() or out_var.get())
               ).grid(row=4, column=2, **pad)
    ttk.Label(frm, text="Leeg laten = naast elk origineel. Bestanden die al opgeschaald zijn, worden overgeslagen.",
              foreground="#666").grid(row=5, column=0, columnspan=3, sticky="w", padx=10)

    # 3. Instellingen
    ttk.Label(frm, text="3. Instellingen", font=bold).grid(row=6, column=0, sticky="w", **pad)
    opt = ttk.Frame(frm)
    opt.grid(row=7, column=0, columnspan=3, sticky="ew", **pad)
    grey = {"foreground": "#666"}
    opt.columnconfigure(0, pad=16)

    model_var = tk.StringVar(value="echt")
    ttk.Label(opt, text="Soort beeld:").grid(row=0, column=0, sticky="nw")
    ttk.Radiobutton(opt, text="Echte opnames (film, VHS, camcorder, foto's)", variable=model_var, value="echt"
                    ).grid(row=0, column=1, sticky="w")
    ttk.Radiobutton(opt, text="Tekenfilm / animatie", variable=model_var, value="animatie").grid(row=1, column=1, sticky="w")

    strength_var = tk.IntVar(value=75)
    ttk.Label(opt, text="AI-sterkte:").grid(row=2, column=0, sticky="w", pady=(6, 0))
    sf = ttk.Frame(opt)
    sf.grid(row=2, column=1, sticky="w", pady=(6, 0))
    sl = ttk.Label(sf, text="75%", width=5)
    ttk.Scale(sf, from_=0, to=100, length=220, variable=strength_var,
              command=lambda v: sl.config(text=f"{int(float(v))}%")).pack(side="left")
    sl.pack(side="left", padx=4)
    ttk.Label(opt, text="Lager = natuurlijker en trouwer aan het origineel; hoger = scherper, maar meer 'AI-look'.",
              **grey).grid(row=3, column=1, sticky="w")

    deint_var = tk.StringVar(value="auto")
    ttk.Label(opt, text="Strepen bij beweging:").grid(row=4, column=0, sticky="w", pady=(6, 0))
    df = ttk.Frame(opt)
    df.grid(row=4, column=1, sticky="w", pady=(6, 0))
    ttk.Combobox(df, textvariable=deint_var, values=["auto", "aan", "uit"], state="readonly", width=6).pack(side="left")
    ttk.Label(df, text="  (deinterlace; voor VHS, DV en DVD)", **grey).pack(side="left")

    codec_var = tk.StringVar(value="H.265 (kleiner bestand)")
    ttk.Label(opt, text="Bestandsformaat:").grid(row=5, column=0, sticky="w", pady=(6, 0))
    ttk.Combobox(opt, textvariable=codec_var, state="readonly", width=28,
                 values=["H.265 (kleiner bestand)", "H.264 (speelt overal af)"]).grid(row=5, column=1, sticky="w", pady=(6, 0))

    denoise_var = tk.BooleanVar(value=False)
    pad_var = tk.BooleanVar(value=True)
    ttk.Checkbutton(opt, text="Ruis/korrel eerst licht verminderen (voor erg korrelige VHS)", variable=denoise_var
                    ).grid(row=6, column=1, sticky="w", pady=(6, 0))
    ttk.Checkbutton(opt, text="Zwarte balken toevoegen tot precies 3840×2160 (voor 4:3-video)", variable=pad_var
                    ).grid(row=7, column=1, sticky="w")

    # Extra's
    ttk.Label(frm, text="4. Extra's", font=bold).grid(row=8, column=0, sticky="w", **pad)
    ext = ttk.Frame(frm)
    ext.grid(row=9, column=0, columnspan=3, sticky="ew", **pad)
    deflicker_var = tk.BooleanVar(value=True)
    smooth_var = tk.BooleanVar(value=False)
    faces_var = tk.BooleanVar(value=False)
    shutdown_var = tk.BooleanVar(value=False)
    rows = [
        (deflicker_var, "Minder flikkering", "Maakt fijne details (haar, gras, muren) rustiger van beeld tot beeld."),
        (smooth_var, "Vloeiender beeld (50/60 beelden per seconde)",
         "Berekent tussenbeelden. Opschalen duurt dan ongeveer twee keer zo lang."),
        (faces_var, "Gezichten herstellen",
         "Maakt wazige gezichten scherp. Kan gezichten licht veranderen; vergelijk eerst met een proefstuk."),
        (shutdown_var, "Pc uitzetten als alles klaar is", "Je krijgt 2 minuten om dat nog te annuleren."),
    ]
    for i, (var, text, hint) in enumerate(rows):
        ttk.Checkbutton(ext, text=text, variable=var).grid(row=i, column=0, sticky="w")
        ttk.Label(ext, text=hint, **grey).grid(row=i, column=1, sticky="w", padx=(8, 0))

    # Start / voortgang
    act = ttk.Frame(frm)
    act.grid(row=10, column=0, columnspan=3, sticky="ew", pady=(10, 4), padx=10)
    btn_preview = ttk.Button(act, text="Proefstuk (10 sec)")
    btn_start = ttk.Button(act, text="Start opschalen")
    btn_stop = ttk.Button(act, text="Stop", state="disabled")
    btn_compare = ttk.Button(act, text="Voor/na bekijken", state="disabled")
    btn_abort_shutdown = ttk.Button(act, text="Uitzetten annuleren")
    btn_preview.pack(side="left")
    btn_start.pack(side="left", padx=6)
    btn_stop.pack(side="left")
    btn_compare.pack(side="left", padx=6)

    pbar = ttk.Progressbar(frm, maximum=100)
    pbar.grid(row=11, column=0, columnspan=3, sticky="ew", padx=10)
    status = ttk.Label(frm, text="Klaar om te beginnen.")
    status.grid(row=12, column=0, columnspan=3, sticky="w", padx=10)

    logbox = tk.Text(frm, height=9, wrap="word", state="disabled", font=("Consolas", 9))
    logbox.grid(row=13, column=0, columnspan=3, sticky="nsew", padx=10, pady=(6, 0))
    frm.rowconfigure(13, weight=1)

    def log(text):
        msgs.put(("log", text))

    def progress(pct, text):
        msgs.put(("progress", (pct, text)))

    def poll():
        try:
            while True:
                kind, val = msgs.get_nowait()
                if kind == "log":
                    logbox.config(state="normal")
                    logbox.insert("end", time.strftime("%H:%M  ") + val + "\n")
                    logbox.see("end")
                    logbox.config(state="disabled")
                elif kind == "progress":
                    pct, text = val
                    if pct is not None:
                        pbar["value"] = pct
                    status.config(text=text)
                elif kind == "done":
                    btn_start.config(state="normal")
                    btn_preview.config(state="normal")
                    btn_stop.config(state="disabled")
                    btn_compare.config(state="normal" if results else "disabled")
                    text, compare = val
                    if compare and results:
                        open_compare(root, results)
                    elif text:
                        messagebox.showinfo("Video Upscaler 4K", text)
                elif kind == "shutdown":
                    btn_abort_shutdown.pack(side="left", padx=6)
        except queue.Empty:
            pass
        root.after(200, poll)

    def start(preview):
        if not files:
            messagebox.showwarning("Video Upscaler 4K", "Kies eerst een of meer video's of foto's.")
            return
        settings = {
            "model": model_var.get(), "strength": strength_var.get(), "deinterlace": deint_var.get(),
            "denoise": denoise_var.get(), "pad": pad_var.get(),
            "codec": "h264" if codec_var.get().startswith("H.264") else "h265", "preview": preview,
            "deflicker": deflicker_var.get(), "smooth": smooth_var.get(), "faces": faces_var.get(),
        }
        out_dir = out_var.get() or None
        todo = list(files) if not preview else [files[lb.curselection()[0] if lb.curselection() else 0]]
        shutdown = shutdown_var.get() and not preview
        cancel.clear()
        btn_start.config(state="disabled")
        btn_preview.config(state="disabled")
        btn_compare.config(state="disabled")
        btn_stop.config(state="normal")

        def work():
            keep_awake(True)
            try:
                ensure_tools(log, progress, settings)
                done, skipped, failed = process_all(todo, out_dir, settings, log, progress, cancel,
                                                    on_job=lambda j: worker.update(job=j))
                results.extend(done)
                summary = f"Klaar: {len(done)} gelukt"
                if skipped:
                    summary += f", {len(skipped)} overgeslagen"
                if failed:
                    summary += f", {len(failed)} mislukt (zie logboek)"
                log(summary + ".")
                progress(100, summary + ".")
                if shutdown:
                    subprocess.run(["shutdown", "/s", "/t", "120", "/c", "Video Upscaler 4K is klaar. "
                                    "De pc gaat over 2 minuten uit."], creationflags=NO_WINDOW)
                    log("De pc gaat over 2 minuten uit. Klik op 'Uitzetten annuleren' om dat te voorkomen.")
                    msgs.put(("shutdown", None))
                    msgs.put(("done", (None, False)))
                else:
                    msgs.put(("done", (summary + "." if not preview else None, preview and bool(done))))
            except Cancelled:
                log("Gestopt. Start opnieuw met dezelfde instellingen om verder te gaan waar je was.")
                progress(None, "Gestopt.")
                msgs.put(("done", (None, False)))
            except Exception as e:  # noqa: BLE001 - alles tonen aan de gebruiker
                log(f"FOUT: {e}")
                progress(None, "Er ging iets mis, zie het logboek hieronder.")
                msgs.put(("done", (f"Er ging iets mis:\n\n{str(e)[:1500]}", False)))
            finally:
                keep_awake(False)

        worker["thread"] = threading.Thread(target=work, daemon=True)
        worker["thread"].start()

    def stop():
        cancel.set()
        if worker["job"]:
            worker["job"].kill()
        status.config(text="Stoppen…")

    def abort_shutdown():
        subprocess.run(["shutdown", "/a"], creationflags=NO_WINDOW)
        btn_abort_shutdown.pack_forget()
        log("Uitzetten geannuleerd.")

    btn_preview.config(command=lambda: start(True))
    btn_start.config(command=lambda: start(False))
    btn_stop.config(command=stop)
    btn_compare.config(command=lambda: open_compare(root, results))
    btn_abort_shutdown.config(command=abort_shutdown)

    def on_close():
        if worker["thread"] and worker["thread"].is_alive():
            if not messagebox.askyesno("Video Upscaler 4K", "Er loopt nog een opdracht. Stoppen en afsluiten?\n"
                                                            "(Je kunt later verdergaan waar je was.)"):
                return
            stop()
        root.destroy()

    root.protocol("WM_DELETE_WINDOW", on_close)
    log("Welkom! Tip: maak eerst een proefstuk van 10 seconden; daarna opent de voor/na-vergelijking vanzelf.")
    root.update_idletasks()
    root.minsize(root.winfo_reqwidth(), min(root.winfo_reqheight(), root.winfo_screenheight() - 80))
    poll()
    root.mainloop()


# --------------------------------------------------------------------------
# Opdrachtregel
# --------------------------------------------------------------------------

def run_cli(argv):
    ap = argparse.ArgumentParser(prog="upscaler.py --cli", description="Video's en foto's naar 4K opschalen")
    ap.add_argument("bestand", nargs="+", help="video's, foto's en/of mappen")
    ap.add_argument("--uit", help="uitvoermap (standaard: naast het origineel)")
    ap.add_argument("--animatie", action="store_true", help="model voor tekenfilm/animatie")
    ap.add_argument("--sterkte", type=int, default=75, help="AI-sterkte 0-100 (standaard 75)")
    ap.add_argument("--deinterlace", choices=["auto", "aan", "uit"], default="auto")
    ap.add_argument("--ontruis", action="store_true")
    ap.add_argument("--geen-balken", action="store_true")
    ap.add_argument("--h264", action="store_true")
    ap.add_argument("--geen-antiflikker", action="store_true")
    ap.add_argument("--vloeiend", action="store_true", help="50/60 beelden per seconde")
    ap.add_argument("--gezichten", action="store_true", help="gezichten herstellen")
    ap.add_argument("--proef", action="store_true", help="alleen 10 seconden uit het midden")
    a = ap.parse_args(argv)
    settings = {"model": "animatie" if a.animatie else "echt", "strength": a.sterkte, "deinterlace": a.deinterlace,
                "denoise": a.ontruis, "pad": not a.geen_balken, "codec": "h264" if a.h264 else "h265",
                "preview": a.proef, "deflicker": not a.geen_antiflikker, "smooth": a.vloeiend, "faces": a.gezichten}
    log = lambda t: print(t, flush=True)  # noqa: E731
    last = {"t": 0}

    def progress(pct, text):
        if time.time() - last["t"] > 5 or pct == 100:
            last["t"] = time.time()
            print(f"  [{pct:5.1f}%] {text}" if pct is not None else f"  {text}", flush=True)

    ensure_tools(log, progress, settings)
    keep_awake(True)
    try:
        done, skipped, failed = process_all(collect_media(a.bestand), a.uit, settings, log, progress, threading.Event())
    finally:
        keep_awake(False)
    print(f"Klaar: {len(done)} gelukt, {len(skipped)} overgeslagen, {len(failed)} mislukt.")
    sys.exit(1 if failed else 0)


def run_selftest(out_path):
    """Controle na het bouwen van de .exe: zijn alle onderdelen meegekomen?"""
    lines = []
    import tkinter  # noqa: F401
    import cv2
    import numpy
    import onnxruntime
    lines.append(f"tkinter ok, numpy {numpy.__version__}, opencv {cv2.__version__}, onnxruntime {onnxruntime.__version__}")
    lines.append("providers: " + ",".join(onnxruntime.get_available_providers()))
    Path(out_path).write_text("\n".join(lines) + "\n", encoding="utf-8")


if __name__ == "__main__":
    if FROZEN and sys.stdout is None:  # .exe zonder consolevenster: meldingen naar een logbestand
        try:
            sys.stdout = sys.stderr = open(APP_DIR / "upscaler-log.txt", "a", encoding="utf-8", buffering=1)
        except OSError:
            sys.stdout = sys.stderr = open(os.devnull, "w")
    if len(sys.argv) > 2 and sys.argv[1] == "--selftest":
        run_selftest(sys.argv[2])
    elif len(sys.argv) > 1 and sys.argv[1] == "--cli":
        run_cli(sys.argv[2:])
    else:
        run_gui()
