#!/usr/bin/env python3
"""thermal-led: whole-case thermal lighting (v2).

WORK mode  (systemctl get-default = multi-user.target):
  GPU             -> heat-map by GPU temp when loaded, cyan when idle
  CPU block (H150i) -> heat-map by CPU package temp when loaded, cyan when idle
  Fans (ENE daemon) -> static cyan (base palette)

GAMING mode (graphical.target):
  Whole case ONE unified color = hotter of GPU/CPU heat band, idle = purple.
  GPU + H150i via OpenRGB Direct; fans/board strips via /run/thermal-led/fan_color
  (read every frame by ene-fanctl static daemon).

Bands: <=50 blue, <=58 cyan, <=65 green, <=72 yellow, <=79 orange, >79 red.
Hysteresis: 3-sample median temps, 30 s load window. Paint only on change.
"""
import subprocess, time, statistics, os

POLL_S = 2
LOAD_WINDOW = 15          # ~30 s at 2 s poll
GPU_UTIL_THRESH = 8
CPU_UTIL_THRESH = 25
GPU_IDLE_FLOOR = 50   # below this a "busy" util reading is a spin artifact, not work
CPU_IDLE_FLOOR = 50   # (idle ComfyUI reported 100% util at 48C — fooled the old test)
CLIENT = "127.0.0.1:6742"
GPU_LEDS, CPU_LEDS = 41, 16
FAN_COLOR_FILE = "/run/thermal-led/fan_color"

BANDS = [(50, "2060FF"), (58, "00FFFF"), (65, "20FF40"),
         (72, "FFE000"), (79, "FF6400"), (10**9, "FF0000")]

def band(t):
    for lim, c in BANDS:
        if t <= lim:
            return c
    return "FF0000"

def gpu_state():
    r = subprocess.run(["nvidia-smi", "--query-gpu=temperature.gpu,utilization.gpu",
                        "--format=csv,noheader,nounits"], capture_output=True, text=True)
    if r.returncode != 0:
        return None
    t, u = r.stdout.strip().split(",")[:2]
    return int(t), int(u)

def cpu_temp():
    try:
        if open("/sys/class/hwmon/hwmon2/name").read().strip() == "coretemp":
            return int(open("/sys/class/hwmon/hwmon2/temp1_input").read()) // 1000
    except (OSError, ValueError):
        pass
    for h in sorted(os.listdir("/sys/class/hwmon")):
        try:
            if open(f"/sys/class/hwmon/{h}/name").read().strip() == "coretemp":
                return int(open(f"/sys/class/hwmon/{h}/temp1_input").read()) // 1000
        except (OSError, ValueError):
            continue
    return None

def cpu_util(prev):
    try:
        f = open("/proc/stat").readline().split()
        vals = [int(x) for x in f[1:8]]
        tot = sum(vals) - (vals[3] + (vals[4] if len(vals) > 4 else 0))
        if prev and tot > prev[1]:
            return 100 * (tot - prev[0]) // (tot - prev[1] + 1)
        return (0, tot) if prev else None if False else (None, tot)
    except Exception:
        return None

def cpu_util_simple(prev_tot):
    try:
        f = open("/proc/stat").readline().split()
        v = [int(x) for x in f[1:8]]
        idle = v[3] + (v[4] if len(v) > 4 else 0)
        tot = sum(v)
        util = None
        if prev_tot is not None and tot > prev_tot[0]:
            util = 100 * ((tot - prev_tot[0]) - (idle - prev_tot[1])) // (tot - prev_tot[0])
        return util, (tot, idle)
    except Exception:
        return None, prev_tot

def pcmode():
    r = subprocess.run(["systemctl", "get-default"], capture_output=True, text=True)
    return "gaming" if r.stdout.strip() == "graphical.target" else "work"

def openrgb_paint(dev, color, leds):
    subprocess.run(["openrgb", "--client", CLIENT, "-d", str(dev),
                    "-m", "Direct", "-c", ",".join([color] * leds)],
                   capture_output=True, timeout=20)

def fan_channel(color):
    try:
        if open(FAN_COLOR_FILE).read().strip() == color:
            return
    except OSError:
        pass
    os.makedirs("/run/thermal-led", exist_ok=True)
    tmp = FAN_COLOR_FILE + ".tmp"
    with open(tmp, "w") as f:
        f.write(color)
    os.replace(tmp, FAN_COLOR_FILE)

def main():
    g_loaded_hist, g_temps = [], []
    c_loaded_hist, c_temps = [], []
    prev_cpu = None
    last_gpu = last_cpu = last_fan = None
    while True:
        mode = pcmode()
        gs = gpu_state()
        ct = cpu_temp()
        cu, prev_cpu = cpu_util_simple(prev_cpu)

        # "loaded" = real work, not spin artifacts (idle ComfyUI reports 100%
        # util at idle temps). Require util AND a temp above the idle floor.
        if gs:
            g_loaded_hist.append(gs[1] >= GPU_UTIL_THRESH and gs[0] > GPU_IDLE_FLOOR)
            g_loaded_hist = g_loaded_hist[-LOAD_WINDOW:]
            g_temps.append(gs[0]); g_temps = g_temps[-3:]
        if ct is not None and cu is not None:
            c_loaded_hist.append(cu >= CPU_UTIL_THRESH and ct > CPU_IDLE_FLOOR)
            c_loaded_hist = c_loaded_hist[-LOAD_WINDOW:]
            c_temps.append(ct); c_temps = c_temps[-3:]

        g_color = band(statistics.median(g_temps)) if (gs and any(g_loaded_hist)) else None
        c_color = band(statistics.median(c_temps)) if (ct and any(c_loaded_hist)) else None

        if mode == "work":
            gpu_c = g_color or "00FFFF"
            cpu_c = c_color or "00FFFF"
            fan_c = None                      # ENE daemon holds its base palette
        else:
            hot = None
            if g_color is not None:
                hot = statistics.median(g_temps)
            if c_color is not None and (hot is None or statistics.median(c_temps) > hot):
                hot = statistics.median(c_temps)
            u = band(hot) if hot is not None else "8000FF"
            gpu_c = cpu_c = fan_c = u

        if gs and gpu_c != last_gpu:
            openrgb_paint(0, gpu_c, GPU_LEDS); last_gpu = gpu_c
        if cpu_c != last_cpu:
            openrgb_paint(2, cpu_c, CPU_LEDS); last_cpu = cpu_c
        if mode == "gaming" and fan_c and fan_c != last_fan:
            fan_channel(fan_c); last_fan = fan_c
        if mode == "work" and last_fan is not None:
            try:
                os.remove(FAN_COLOR_FILE)
            except OSError:
                pass
            last_fan = None
        time.sleep(POLL_S)

if __name__ == "__main__":
    main()
