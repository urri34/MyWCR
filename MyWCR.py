#!/usr/bin/env python3
import os
import re
import sys
import time
import signal
import subprocess
import threading
from datetime import datetime
from pathlib import Path
from shutil import which
from collections import deque

# Acceptem expansions: {$VAR}, ${VAR}, $VAR
VAR_PATTERNS = [
    re.compile(r"\{\$(\w+)\}"),  # {$VAR}
    re.compile(r"\$\{(\w+)\}"),  # ${VAR}
    re.compile(r"\$(\w+)")       # $VAR
]


def load_env_ini(path: Path) -> dict:
    env = {}
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        k, v = line.split("=", 1)
        k = k.strip()
        v = v.strip()
        if (v.startswith('"') and v.endswith('"')) or (v.startswith("'") and v.endswith("'")):
            v = v[1:-1]
        env[k] = v
    return env


def expand_vars(value: str, env: dict) -> str:
    if not isinstance(value, str):
        return value

    def repl(match):
        var = match.group(1)
        return env.get(var, os.environ.get(var, ""))

    cur = value
    for _ in range(10):
        prev = cur
        for pat in VAR_PATTERNS:
            cur = pat.sub(repl, cur)
        if cur == prev:
            break
    return cur


def expand_env(env: dict) -> dict:
    out = dict(env)
    for _ in range(10):
        changed = False
        for k, v in list(out.items()):
            new_v = expand_vars(v, out)
            if new_v != v:
                out[k] = new_v
                changed = True
        if not changed:
            break
    return out


def ts() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def log_line(logf, line: str):
    logf.write(f"[{ts()}] {line}")
    if not line.endswith("\n"):
        logf.write("\n")
    logf.flush()


def sanitize_ffmpeg_path(raw: str) -> str:
    raw = (raw or "").strip()
    if not raw:
        return "/usr/bin/ffmpeg"
    p = Path(raw)
    if p.exists():
        return raw
    if "ffmpeg" in raw:
        m = re.search(r".*ffmpeg", raw)
        if m:
            return m.group(0)
    return raw


def start_process(cmd_list, env, cwd: Path, log_path: Path, name: str) -> subprocess.Popen:
    with open(log_path, "a", encoding="utf-8") as logf:
        log_line(logf, f"--- START {name} ---")
        log_line(logf, f"{name} CMD: {cmd_list}")
    proc = subprocess.Popen(
        cmd_list,
        shell=False,
        cwd=str(cwd) if cwd else None,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        universal_newlines=True,
        preexec_fn=os.setsid
    )
    return proc


def stop_process(proc: subprocess.Popen, log_path: Path, name: str, timeout=5):
    if proc is None:
        return
    with open(log_path, "a", encoding="utf-8") as logf:
        if proc.poll() is not None:
            log_line(logf, f"--- {name} already exited with code {proc.returncode} ---")
            return
        try:
            log_line(logf, f"--- STOP {name} (SIGTERM group) ---")
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            log_line(logf, f"--- {name} did not stop, SIGKILL group ---")
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            proc.wait(timeout=timeout)
        finally:
            log_line(logf, f"--- {name} exited with code {proc.returncode} ---")


def _read_cmdline(pid: str) -> str:
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            data = f.read().replace(b"\x00", b" ").strip()
        return data.decode("utf-8", errors="ignore")
    except Exception:
        return ""


def find_pids_by_cmd_contains(needles) -> list[int]:
    pids = []
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        cmd = _read_cmdline(entry)
        if not cmd:
            continue
        ok = True
        for n in needles:
            if n and n not in cmd:
                ok = False
                break
        if ok:
            pids.append(int(entry))
    return pids


def port_in_use_ss(port: int) -> bool:
    if which("ss") is None:
        return False
    try:
        r = subprocess.run(["ss", "-ltnp"], capture_output=True, text=True)
        if r.returncode != 0:
            return False
        return f":{port} " in r.stdout or f":{port}\n" in r.stdout
    except Exception:
        return False


def device_busy_fuser(dev: str) -> bool:
    """
    True si fuser detecta algun procés usant el dispositiu.
    (sense sudo pot ser menys fiable, però ens ajuda).
    """
    if which("fuser") is None:
        return False
    try:
        r = subprocess.run(["fuser", dev], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return r.returncode == 0
    except Exception:
        return False


def pump_output(proc: subprocess.Popen, log_path: Path, name: str, stop_event: threading.Event, tail_store: deque):
    with open(log_path, "a", encoding="utf-8") as logf:
        log_line(logf, f"=== {name} log started ===")
        if proc.stdout is None:
            log_line(logf, "WARNING: proc.stdout is None")
            return
        try:
            for line in proc.stdout:
                if stop_event.is_set():
                    break
                line = line.rstrip("\n")
                tail_store.append(line)
                log_line(logf, line)
        except Exception as e:
            log_line(logf, f"ERROR while reading output: {e}")
        finally:
            log_line(logf, f"=== {name} log pump ended ===")


# ---------------------------
# Stream test amb v4l2-ctl
# ---------------------------

def v4l2_stream_test(dev: str, width: int, height: int, fps: int, pix: str = "MJPG", timeout_s: float = 2.0) -> tuple[bool, str]:
    """
    Fa un test curt de streaming amb v4l2-ctl:
    - set format (pix/width/height)
    - set fps
    - stream-count 1
    Retorna (ok, reason).
    """
    if which("v4l2-ctl") is None:
        return (True, "v4l2-ctl not available (skip test)")

    cmd = [
        "v4l2-ctl", "-d", dev,
        f"--set-fmt-video=width={width},height={height},pixelformat={pix}",
        f"--set-parm={fps}",
        "--stream-mmap=3",
        "--stream-count=1"
    ]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_s)
        if r.returncode == 0:
            return (True, "stream test OK")
        msg = (r.stderr or r.stdout or "").strip()
        return (False, f"stream test failed rc={r.returncode}: {msg[:200]}")
    except subprocess.TimeoutExpired:
        return (False, "stream test timeout")
    except Exception as e:
        return (False, f"stream test exception: {e}")


def parse_size(size_str: str) -> tuple[int, int]:
    if "x" not in size_str:
        return (1920, 1080)
    a, b = size_str.split("x", 1)
    return (int(a), int(b))


def build_rtsp_publish_url(env: dict) -> str:
    """Construeix la URL on FFMPEG publica. Precedència:
    1) MYWCR_RTSP_URL (legacy)
    2) MYWCR_RTSP_PUBLISH_URL (derivada)
    3) rtsp://{PUB_HOST}:{PORT}/{PATH}
    """
    legacy = env.get("MYWCR_RTSP_URL", "").strip()
    if legacy:
        return legacy

    pub = env.get("MYWCR_RTSP_PUBLISH_URL", "").strip()
    if pub:
        return pub

    host = env.get("MYWCR_RTSP_PUB_HOST", "127.0.0.1")
    port = env.get("MYWCR_RTSP_PORT", "8554")
    path = env.get("MYWCR_RTSP_PATH", "mywcr")
    return f"rtsp://{host}:{port}/{path}"


def normalize_rtsp_dest(url: str) -> str:
    """Evita rtsp://0.0.0.0 com a destí de publicació."""
    if "rtsp://0.0.0.0" in url:
        return url.replace("rtsp://0.0.0.0", "rtsp://127.0.0.1")
    return url


class Supervisor:
    def __init__(self, ini_path: Path):
        self.ini_path = ini_path
        self.env_raw = load_env_ini(ini_path)
        self.env_exp = expand_env(self.env_raw)

        self.child_env = os.environ.copy()
        self.child_env.update(self.env_exp)

        self.base = Path(self.env_exp.get("MYWCR_BASE", str(ini_path.parent))).expanduser()
        self.base.mkdir(parents=True, exist_ok=True)

        self.log_dir = self.base / "logs"
        self.log_dir.mkdir(parents=True, exist_ok=True)

        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.mediamtx_log = self.log_dir / f"mediamtx_{stamp}.log"
        self.ffmpeg_log = self.log_dir / f"ffmpeg_{stamp}.log"

        # Config vídeo
        self.cam_dev = self.env_exp.get("MYWCR_CAMERA_DEVICE", "/dev/video0")
        self.size = self.env_exp.get("MYWCR_SIZE", "1920x1080")
        self.fps = int(self.env_exp.get("MYWCR_FPS", "30"))
        self.width, self.height = parse_size(self.size)

        # RTSP
        self.rtsp_port = int(self.env_exp.get("MYWCR_RTSP_PORT", "8554"))
        self.rtsp_path = self.env_exp.get("MYWCR_RTSP_PATH", "mywcr")
        self.rtsp_user = self.env_exp.get("MYWCR_RTSP_USERNAME", "admin")
        self.rtsp_pass = self.env_exp.get("MYWCR_RTSP_PASSWORD", "")
        self.rtsp_host = self.env_exp.get("MYWCR_RTSP_HOST", "127.0.0.1")

        # URL de publicació (ffmpeg -> mediamtx)
        self.rtsp_publish_url = normalize_rtsp_dest(build_rtsp_publish_url(self.env_exp))

        # URL informativa de lectura (per logs)
        read_url = self.env_exp.get("MYWCR_RTSP_READ_URL", "").strip()
        if read_url:
            self.rtsp_read_url = read_url
        else:
            self.rtsp_read_url = f"rtsp://{self.rtsp_user}:{self.rtsp_pass}@{self.rtsp_host}:{self.rtsp_port}/{self.rtsp_path}"

        # Processos
        self.mediamtx_proc = None
        self.ffmpeg_proc = None
        self.mediamtx_stop = threading.Event()
        self.ffmpeg_stop = threading.Event()
        self.mediamtx_thread = None
        self.ffmpeg_thread = None
        self.stopping = False

        # timings
        self.restart_delay_s = 2.0
        self.busy_backoff_s = 30.0  # quan detectem error de streamon
        self.mediamtx_warmup_s = 1.0
        self.reprobe_interval_s = 5.0  # cada quant re-testegem la càmera si falla

        # tails per detectar errors
        self.ffmpeg_tail = deque(maxlen=80)
        self.mediamtx_tail = deque(maxlen=80)

        # Comandes
        self.cmd_mediamtx, self.cmd_ffmpeg = self.build_commands()

        # detectors
        self.ffmpeg_needles = ["ffmpeg", self.cam_dev]
        self.mediamtx_needles = ["mediamtx"]

        # estat intern
        self.last_cam_fail_ts = 0.0

        # Genera un mediamtx.yml fresc a cada arrencada del supervisor
        self.write_mediamtx_yml()

    def mediamtx_yml_path(self) -> Path:
        return self.base / "mediamtx.yml"

    def write_mediamtx_yml(self):
        """Genera mediamtx.yml des de mywcr.ini (env_exp) i l'escriu de manera atòmica."""
        log_level = (self.env_exp.get("MEDIAMTX_LOG_LEVEL", "debug") or "debug").strip()

        # IMPORTANT:
        # - Publisher (ffmpeg) sense password, restringit a localhost
        # - Viewers amb user/pass
        yml = f"""# Auto-generat per mywcr-rtsp.py (NO EDITAR A MÀ)
# Font de veritat: {self.ini_path}

logLevel: {log_level}
logDestinations: [stdout]
logStructured: no

# --- Autenticació ---
authMethod: internal

authInternalUsers:
  # 1) Publisher LOCAL (ffmpeg). Sense password, només localhost
  - user: any
    pass:
    ips: [\"127.0.0.1\", \"::1\"]
    permissions:
      - action: publish
        path: {self.rtsp_path}

  # 2) Viewers remots: OBLIGATORI user/password
  - user: {self.rtsp_user}
    pass: \"{self.rtsp_pass}\"
    ips: []
    permissions:
      - action: read
        path: {self.rtsp_path}
      - action: playback
        path: {self.rtsp_path}

  # 3) Admin local (opcional, però recomanat)
  - user: any
    pass:
    ips: [\"127.0.0.1\", \"::1\"]
    permissions:
      - action: api
      - action: metrics
      - action: pprof

# --- Servidor RTSP ---
rtsp: yes
rtspAddress: :{self.rtsp_port}
rtspAuthMethods: [basic]

# (altres protocols mantenim per defecte)
rtmp: yes
hls: yes
webrtc: yes
srt: yes

# --- Paths ---
paths:
  {self.rtsp_path}:
    source: publisher
"""

        target = self.mediamtx_yml_path()
        tmp = target.with_suffix(".yml.tmp")
        tmp.write_text(yml, encoding="utf-8")
        tmp.replace(target)

    def build_commands(self):
        # MEDIAMTX
        mediamtx_bin = self.env_exp.get("MEDIAMTX_BIN", str(self.base / "mediamtx"))
        mediamtx_bin = expand_vars(mediamtx_bin, self.env_exp)
        mediamtx_yml = str(self.mediamtx_yml_path())
        cmd1 = [mediamtx_bin, mediamtx_yml]

        # FFMPEG
        ffmpeg_bin = sanitize_ffmpeg_path(self.env_exp.get("FFMPEG_BIN", "/usr/bin/ffmpeg"))
        vbitrate = self.env_exp.get("MYWCR_VBITRATE", "4M")
        gop = self.env_exp.get("MYWCR_GOP", "100")

        cmd2 = [
            "nice", "-n", "5",
            "ionice", "-c2", "-n4",
            ffmpeg_bin,
            "-hide_banner", "-loglevel", "info", "-nostdin",
            "-f", "v4l2",
            "-input_format", "mjpeg",
            "-framerate", str(self.fps),
            "-video_size", f"{self.width}x{self.height}",
            "-i", str(self.cam_dev),
            "-vf", "scale=in_range=pc:out_range=tv,colorspace=bt470bg:iall=bt470bg:fast=1,format=yuv420p",
            "-c:v", "h264_v4l2m2m",
            "-b:v", str(vbitrate),
            "-g", str(gop),
            "-bf", "0",
            "-reset_timestamps", "1",
            "-fflags", "+genpts",
            "-use_wallclock_as_timestamps", "1",
            "-map", "0:v:0",
            "-flags", "+global_header",
            "-f", "rtsp",
            "-rtsp_transport", "tcp",
            str(self.rtsp_publish_url),
        ]
        return cmd1, cmd2

    def ffmpeg_already_running(self) -> bool:
        if device_busy_fuser(self.cam_dev):
            return True
        pids = find_pids_by_cmd_contains(self.ffmpeg_needles)
        return len(pids) > 0

    def mediamtx_already_running(self) -> bool:
        # comprovem port configurat, no hardcoded
        if port_in_use_ss(self.rtsp_port):
            return True
        pids = find_pids_by_cmd_contains(self.mediamtx_needles)
        return len(pids) > 0

    def ffmpeg_failed_due_to_streamon_proto(self) -> bool:
        joined = "\n".join(self.ffmpeg_tail)
        return ("VIDIOC_STREAMON" in joined and "Protocol error" in joined)

    def pick_camera_device(self) -> str:
        """
        Prova el device configurat i si falla, prova el "germà" (0<->1)
        per càmeres UVC típiques.
        """
        candidates = [self.cam_dev]
        m = re.match(r"^/dev/video(\d+)$", self.cam_dev)
        if m:
            idx = int(m.group(1))
            alt = f"/dev/video{idx+1}" if idx % 2 == 0 else f"/dev/video{idx-1}"
            if alt not in candidates and Path(alt).exists():
                candidates.append(alt)

        for dev in candidates:
            ok, reason = v4l2_stream_test(dev, self.width, self.height, self.fps, pix="MJPG", timeout_s=2.0)
            if ok:
                return dev
            with open(self.ffmpeg_log, "a", encoding="utf-8") as flog:
                log_line(flog, f"WARNING: stream-test failed on {dev}: {reason}")

        return self.cam_dev

    def start_mediamtx(self):
        # Sempre regenerem config abans d'arrencar MediaMTX
        self.write_mediamtx_yml()
        self.cmd_mediamtx, self.cmd_ffmpeg = self.build_commands()

        self.mediamtx_stop.clear()
        self.mediamtx_proc = start_process(self.cmd_mediamtx, self.child_env, self.base, self.mediamtx_log, "MEDIAMTX")
        self.mediamtx_thread = threading.Thread(
            target=pump_output,
            args=(self.mediamtx_proc, self.mediamtx_log, "MEDIAMTX", self.mediamtx_stop, self.mediamtx_tail),
            daemon=True
        )
        self.mediamtx_thread.start()

    def start_ffmpeg(self):
        now = time.time()
        if now - self.last_cam_fail_ts < self.reprobe_interval_s:
            return

        chosen = self.pick_camera_device()
        if chosen != self.cam_dev:
            with open(self.ffmpeg_log, "a", encoding="utf-8") as flog:
                log_line(flog, f"INFO: switching camera device {self.cam_dev} -> {chosen}")
            self.cam_dev = chosen
            self.cmd_mediamtx, self.cmd_ffmpeg = self.build_commands()

        ok, reason = v4l2_stream_test(self.cam_dev, self.width, self.height, self.fps, pix="MJPG", timeout_s=2.0)
        if not ok:
            self.last_cam_fail_ts = time.time()
            with open(self.ffmpeg_log, "a", encoding="utf-8") as flog:
                log_line(flog, f"ERROR: stream-test still failing on {self.cam_dev}: {reason}")
                log_line(flog, f"INFO: backoff {self.busy_backoff_s}s before retry")
            time.sleep(self.busy_backoff_s)
            return

        self.ffmpeg_stop.clear()
        self.ffmpeg_tail.clear()
        self.ffmpeg_proc = start_process(self.cmd_ffmpeg, self.child_env, self.base, self.ffmpeg_log, "FFMPEG")
        self.ffmpeg_thread = threading.Thread(
            target=pump_output,
            args=(self.ffmpeg_proc, self.ffmpeg_log, "FFMPEG", self.ffmpeg_stop, self.ffmpeg_tail),
            daemon=True
        )
        self.ffmpeg_thread.start()

    def stop_all(self):
        self.stopping = True
        self.ffmpeg_stop.set()
        self.mediamtx_stop.set()
        stop_process(self.ffmpeg_proc, self.ffmpeg_log, "FFMPEG")
        stop_process(self.mediamtx_proc, self.mediamtx_log, "MEDIAMTX")
        self.ffmpeg_proc = None
        self.mediamtx_proc = None

    def run_forever(self):
        with open(self.mediamtx_log, "a", encoding="utf-8") as mlog:
            log_line(mlog, f"=== Supervisor started\n INI={self.ini_path}\n BASE={self.base} ===")
        with open(self.ffmpeg_log, "a", encoding="utf-8") as flog:
            log_line(flog, f"=== Supervisor started\n INI={self.ini_path}\n BASE={self.base} ===")
            log_line(flog, f"INFO: Publish RTSP URL: {self.rtsp_publish_url}")
            log_line(flog, f"INFO: Read   RTSP URL: {self.rtsp_read_url}")

        # mediamtx: no duplicar
        if self.mediamtx_already_running():
            with open(self.mediamtx_log, "a", encoding="utf-8") as mlog:
                log_line(mlog, "INFO: MEDIAMTX ja sembla corrent. No llanço una altra instància.")
        else:
            self.start_mediamtx()
            time.sleep(self.mediamtx_warmup_s)

        # ffmpeg: no duplicar
        if self.ffmpeg_already_running():
            with open(self.ffmpeg_log, "a", encoding="utf-8") as flog:
                log_line(flog, f"INFO: FFMPEG/càmera sembla en ús ({self.cam_dev}). No llanço una altra instància.")
        else:
            self.start_ffmpeg()

        while not self.stopping:
            time.sleep(1.0)

            # si nosaltres no l'hem arrencat i desapareix, l'arrencarem
            if self.mediamtx_proc is None and not self.mediamtx_already_running():
                with open(self.mediamtx_log, "a", encoding="utf-8") as mlog:
                    log_line(mlog, "INFO: MEDIAMTX extern ja no es detecta. Arrenco instància pròpia.")
                self.start_mediamtx()

            if self.ffmpeg_proc is None and not self.ffmpeg_already_running():
                self.start_ffmpeg()

            # watchdog processos propis
            if self.mediamtx_proc is not None and self.mediamtx_proc.poll() is not None:
                code = self.mediamtx_proc.returncode
                with open(self.mediamtx_log, "a", encoding="utf-8") as mlog:
                    log_line(mlog, f"*** MEDIAMTX ha sortit (code={code}). Reinici en {self.restart_delay_s}s ***")
                self.mediamtx_stop.set()
                time.sleep(self.restart_delay_s)
                if not self.stopping:
                    if self.mediamtx_already_running():
                        with open(self.mediamtx_log, "a", encoding="utf-8") as mlog:
                            log_line(mlog, "INFO: Es detecta MEDIAMTX extern. No reinicio.")
                        self.mediamtx_proc = None
                    else:
                        self.start_mediamtx()

            if self.ffmpeg_proc is not None and self.ffmpeg_proc.poll() is not None:
                code = self.ffmpeg_proc.returncode
                with open(self.ffmpeg_log, "a", encoding="utf-8") as flog:
                    log_line(flog, f"*** FFMPEG ha sortit (code={code}). ***")
                self.ffmpeg_stop.set()

                # si detectem el patró VIDIOC_STREAMON Protocol error, backoff llarg
                if self.ffmpeg_failed_due_to_streamon_proto() or self.ffmpeg_already_running():
                    self.last_cam_fail_ts = time.time()
                    with open(self.ffmpeg_log, "a", encoding="utf-8") as flog:
                        log_line(flog, f"INFO: Detectat error de streamon / càmera ocupada. Backoff {self.busy_backoff_s}s.")
                    self.ffmpeg_proc = None
                    time.sleep(self.busy_backoff_s)
                else:
                    with open(self.ffmpeg_log, "a", encoding="utf-8") as flog:
                        log_line(flog, f"INFO: Reinici FFMPEG en {self.restart_delay_s}s.")
                    time.sleep(self.restart_delay_s)
                    if not self.stopping:
                        if self.ffmpeg_already_running():
                            with open(self.ffmpeg_log, "a", encoding="utf-8") as flog:
                                log_line(flog, "INFO: S'ha detectat FFMPEG extern mentre esperava. No reinicio.")
                            self.ffmpeg_proc = None
                        else:
                            self.start_ffmpeg()

def main():
    ini_path = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("MyWCR.ini")
    if not ini_path.exists():
        print(f"ERROR: No trobo {ini_path}", file=sys.stderr)
        sys.exit(1)

    sup = Supervisor(ini_path)

    def handle_sig(sig, frame):
        print(f"Received signal {sig}. Shutting down...")
        with open(sup.mediamtx_log, "a", encoding="utf-8") as mlog:
            log_line(mlog, f"=== Received signal {sig}. Shutting down... ===")
        with open(sup.ffmpeg_log, "a", encoding="utf-8") as flog:
            log_line(flog, f"=== Received signal {sig}. Shutting down... ===")
        sup.stop_all()
        try:
            os.remove("mediamtx.yml")
        except:
            pass
        sys.exit(0)

    signal.signal(signal.SIGINT, handle_sig)
    signal.signal(signal.SIGTERM, handle_sig)

    print("MyWCR supervisor running.")
    print(f" MEDIAMTX log: {sup.mediamtx_log}")
    print(f" FFMPEG log: {sup.ffmpeg_log}")
    print(f" RTSP url: rtsp://{sup.rtsp_user}:{sup.rtsp_pass}@{sup.rtsp_host}:{sup.rtsp_port}/{sup.rtsp_path}")
    print("Press Ctrl+C to stop.")

    sup.run_forever()


if __name__ == "__main__":
    main()
