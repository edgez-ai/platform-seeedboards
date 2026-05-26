import os
import shutil
import socket
import subprocess
import tempfile
import time
from urllib.parse import urlparse

from SCons.Script import COMMAND_LINE_TARGETS

Import("env")


def _is_monitor_target():
    targets = {str(t).lower() for t in COMMAND_LINE_TARGETS}
    return "monitor" in targets


def _is_upload_target():
    targets = {str(t).lower() for t in COMMAND_LINE_TARGETS}
    return "upload" in targets


def _port_open(host, port, timeout=0.5):
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            return True
    except OSError:
        return False


def _pick_jlink_server_cmd():
    return shutil.which("JLinkGDBServer") or shutil.which("JLinkGDBServerCL")


def _resolve_jlink_device():
    board = env.BoardConfig()
    configured = (board.get("debug.jlink_device") or "").strip()
    mcu = (board.get("build.mcu") or "").strip().lower()

    mcu_map = {
        "nrf54l15": "NRF54L15_M33",
        "nrf54l10": "NRF54L10_M33",
        "nrf54l05": "NRF54L05_M33",
    }

    if configured and not (mcu in mcu_map and configured.lower().endswith("_xxaa")):
        return configured
    return mcu_map.get(mcu, configured or "NRF54L15_M33")


def _parse_monitor_socket_address():
    monitor_port = str(env.GetProjectOption("monitor_port", "socket://127.0.0.1:19021")).strip()
    if monitor_port.startswith("socket://"):
        parsed = urlparse(monitor_port)
        return parsed.hostname or "127.0.0.1", parsed.port or 19021

    host = "127.0.0.1"
    port = 19021
    if ":" in monitor_port:
        parts = monitor_port.rsplit(":", 1)
        if len(parts) == 2 and parts[1].isdigit():
            host = parts[0] or host
            port = int(parts[1])
    return host, port


def _resume_target(device, speed):
    jlink_exe = shutil.which("JLinkExe")
    if not jlink_exe:
        return

    cmd_file_fd, cmd_file = tempfile.mkstemp(prefix="pio-jlink-resume-", suffix=".jlink")
    os.close(cmd_file_fd)

    try:
        with open(cmd_file, "w", encoding="utf-8") as fp:
            fp.write("h\ng\nq\n")

        subprocess.run(
            [
                jlink_exe,
                "-device",
                device,
                "-if",
                "SWD",
                "-speed",
                str(speed),
                "-CommandFile",
                cmd_file,
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            check=False,
        )
    finally:
        try:
            os.remove(cmd_file)
        except OSError:
            pass


def _ensure_jlink_rtt_server():
    # In `pio run -t upload -t monitor`, PlatformIO may invoke monitor
    # after SCons sees only `upload`. Handle both entry points.
    if not (_is_monitor_target() or _is_upload_target()):
        return

    upload_protocol = (env.subst("$UPLOAD_PROTOCOL") or "").strip().lower()
    if not upload_protocol.startswith("jlink"):
        return

    host, rtt_port = _parse_monitor_socket_address()
    if _port_open(host, rtt_port):
        print("Reusing existing RTT server on %s:%d." % (host, rtt_port))
        _resume_target(_resolve_jlink_device(), env.GetProjectOption("debug_speed", "4000"))
        return

    server_cmd = _pick_jlink_server_cmd()
    if not server_cmd:
        print("Warning: JLinkGDBServer/JLinkGDBServerCL not found; RTT monitor may fail.")
        return

    project_dir = env.subst("$PROJECT_DIR")
    log_dir = os.path.join(project_dir, ".pio")
    os.makedirs(log_dir, exist_ok=True)
    log_file = os.path.join(log_dir, "jlink-rtt-server.log")

    device = _resolve_jlink_device()
    speed = env.GetProjectOption("debug_speed", "4000")
    gdb_port = int(env.GetProjectOption("debug_port", "2331"))
    swo_port = int(env.GetProjectOption("jlink_swo_port", "2332"))
    telnet_port = int(env.GetProjectOption("jlink_telnet_port", "2333"))

    # Avoid active-client conflicts from manually launched JLinkRTTClient tools.
    if os.name != "nt":
        subprocess.run(["pkill", "-f", "JLinkRTTClient"], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    print("Starting J-Link RTT server (%s) for %s..." % (server_cmd, device))
    with open(log_file, "a", encoding="utf-8") as fp:
        subprocess.Popen(
            [
                server_cmd,
                "-device",
                device,
                "-if",
                "SWD",
                "-speed",
                str(speed),
                "-port",
                str(gdb_port),
                "-swoport",
                str(swo_port),
                "-telnetport",
                str(telnet_port),
                "-RTTTelnetPort",
                str(rtt_port),
                "-nogui",
            ],
            stdout=fp,
            stderr=fp,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )

    for _ in range(40):
        if _port_open(host, rtt_port):
            print("RTT server ready on %s:%d" % (host, rtt_port))
            _resume_target(device, speed)
            return
        time.sleep(0.25)

    print("Warning: RTT server did not open %s:%d. Check log: %s" % (host, rtt_port, log_file))


_ensure_jlink_rtt_server()
