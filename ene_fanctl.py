#!/usr/bin/env python3
"""ene_fanctl.py — Linux driver for Maxsun ENE 6K7750 motherboard ARGB controller.
Protocol decoded from Maxsun SK_64.dll / EneEc_x64.dll disassembly:
  READ : ctrl IN  bmRequestType=0xC0 bRequest=0x81 wValue=reg>>16 wIndex=reg&0xFFFF len
  WRITE: ctrl OUT bmRequestType=0x40 bRequest=0x80 wValue=reg>>16 wIndex=reg&0xFFFF data
LED RAM base 0xE300, 3 bytes per LED (channel order calibrated live: R,B,G).
Reg E0A1 = group count, 0xE0A2+2g = led count (hi,lo) per group.
LED RAM is cumulative across groups: group g starts at 0xE300 + 3*sum(counts<g).
EVERY RAM write must be committed with the per-group effect-slot strobe:
  0x01 @ E021+16g, 0x1b @ +1, then 1 @ E02F+16g — otherwise the chip ACKs but LEDs
  never update (this was the whole 'fans dead' saga).

Commands:
  info                                  groups + led counts
  backup <file> / restore <file>        dump/restore all LED RAM (json)
  fill <group|all> <RRGGBB> [n]         static color (optional LED cap n)
  off [group|all]                       black
  mode <work|gaming|off>                policy apply (daemon used for gaming)
  anim-daemon <mode> [hz] [--leds N] [--colors RRGGBB,RRGGBB,...]
        foreground animation: breathing|rainbow|meteor   (internal; systemd-run)
  read <reg-hex> <len>                  raw register read (debug)
"""
import sys, time, json, math, signal
import usb.core, usb.util

VID, PID = 0x0CF2, 0x7750
LED_RAM = 0xE300
CHUNK = 96  # bytes per control transfer
TAIL = 10   # meteor tail length (LEDs)

def open_dev():
    dev = usb.core.find(idVendor=VID, idProduct=PID)
    if dev is None:
        sys.exit("ENE 6K7750 not found")
    for i in range(2):
        try:
            if dev.is_kernel_driver_active(i):
                dev.detach_kernel_driver(i)
        except Exception:
            pass
    try:
        dev.set_configuration()
    except usb.core.USBError:
        pass
    return dev

def rd(dev, reg, n):
    return bytes(dev.ctrl_transfer(0xC0, 0x81, reg >> 16, reg & 0xFFFF, n, timeout=1000))

def wr(dev, reg, data):
    return dev.ctrl_transfer(0x40, 0x80, reg >> 16, reg & 0xFFFF, list(data), timeout=1000)

def groups(dev):
    ng = rd(dev, 0xE0A1, 1)[0]
    out = []
    for g in range(ng):
        v = rd(dev, 0xE0A2 + 2 * g, 2)
        out.append(v[0] * 0x100 + v[1])
    return out

def group_base(dev, gs, g):
    return LED_RAM + 3 * sum(gs[:g])

def strobe(dev, g):
    wr(dev, 0xE021 + 16 * g, [0x01])       # effect slot mode = manual
    wr(dev, 0xE021 + 16 * g + 1, [0x1b])   # speed/duration byte
    wr(dev, 0xE02F + 16 * g, [1])          # commit strobe

def write_leds(dev, gs, g, raw, prev=None):
    """raw: bytes of 3*N in RAM order R,B,G. If prev given, only write the dirty
    window (changed bytes +- margin) — fewer transfers = smoother animation."""
    start = group_base(dev, gs, g)
    if prev is not None and len(prev) == len(raw):
        if raw == prev:
            return
        lo, hi = 0, len(raw)
        while lo < len(raw) and prev[lo] == raw[lo]:
            lo += 1
        while hi > lo and prev[hi - 1] == raw[hi - 1]:
            hi -= 1
        lo = max(0, (lo // 3) * 3)
        hi = min(len(raw), ((hi + 2) // 3 + 4) * 3)
        span = raw[lo:hi]
    else:
        lo, span = 0, raw
    n_ok = 0
    for off in range(0, len(span), CHUNK):
        r = wr(dev, start + lo + off, span[off:off + CHUNK])
        n_ok += 1 if r is not None and r >= 0 else 0
    strobe(dev, g)  # commit — without this the LEDs never update
    return n_ok

def put(buf, i, r, gg, b):
    j = 3 * i
    buf[j], buf[j + 1], buf[j + 2] = r, b, gg  # RAM order R,B,G (calibrated: pos0=R,pos1=B,pos2=G)

def fill(dev, g, rgb, n=None):
    gs = groups(dev)
    targets = range(len(gs)) if g == "all" else [int(g)]
    for t in targets:
        cnt = min(n or gs[t], gs[t])
        buf = bytearray(gs[t] * 3)
        r, gg, b = (rgb >> 16) & 0xFF, (rgb >> 8) & 0xFF, rgb & 0xFF
        for i in range(cnt):
            put(buf, i, r, gg, b)
        write_leds(dev, gs, t, buf)

def hsv2rgb_byte(h, s, v):
    import colorsys
    r, gg, b = colorsys.hsv_to_rgb(h % 1.0, s, v)
    return int(r * 255), int(gg * 255), int(b * 255)

def anim_daemon(mode, hz, led_limit=None, palette=((0xA0, 0x20, 0xFF)),
                fans=3, per_fan=None, speed_mult=1.0):
    stop = [0]
    signal.signal(signal.SIGTERM, lambda *a: stop.__setitem__(0, 1))
    signal.signal(signal.SIGINT, lambda *a: stop.__setitem__(0, 1))
    dev = open_dev()
    gs = groups(dev)
    bufs = [bytearray(n * 3) for n in gs]
    prev = [bytearray(n * 3) for n in gs]
    period = 1.0 / max(1.0, hz)
    t0 = time.time()
    cr, cg, cb = palette if isinstance(palette, tuple) else palette[0]
    while not stop[0]:
        frame_t = time.time()
        t = frame_t - t0
        for g, n_all in enumerate(gs):
            n = min(led_limit or n_all, n_all)
            buf = bufs[g]
            for i in range(len(buf)):
                buf[i] = 0
            if mode == "breathing":
                level = 0.5 - 0.5 * math.cos(t * 1.2)
                for i in range(n):
                    put(buf, i, int(cr * level), int(cg * level), int(cb * level))
            elif mode == "rainbow":
                for i in range(n):
                    put(buf, i, *hsv2rgb_byte(t * 0.12 + i / max(n, 1), 1.0, 1.0))
            elif mode == "static":
                # keep-alive static fill: full RAM rewrite + strobe commit every
                # cycle. The LED engine sleeps ~1-2 min after a one-shot stroke
                # (window tied to the 0x1b duration byte); re-strobing wakes it.
                # This is why animations never died: they strobe every frame.
                # Dynamic palette: if /run/thermal-led/fan_color exists and holds
                # a different RRGGBB, switch to it (thermal-led service drives
                # unified gaming colors through this file).
                try:
                    want = open("/run/thermal-led/fan_color").read().strip()
                    if len(want) == 6:
                        cr = int(want[0:2], 16); cg = int(want[2:4], 16); cb = int(want[4:6], 16)
                except OSError:
                    pass
                for i in range(n):
                    put(buf, i, cr, cg, cb)
            elif mode == "meteor":
                # Physical map (calibrated 2026-09-29): per fan segment of 16,
                # index 0 = 6 o'clock (bottom), higher index = up the arc.
                # Comet head falls TOP (high idx) -> BOTTOM (idx 0); tail trails above.
                pf = per_fan or max(n // max(fans, 1), TAIL + 2)
                for f in range(max(fans, 1)):
                    seg = f * pf
                    cnt = min(pf, n - seg)
                    if cnt <= 0:
                        break
                    cycle = (cnt + TAIL) * 0.02 / speed_mult   # per-step ~20ms
                    # top fan (highest f) leads the cascade
                    pos = ((t / cycle) - (fans - 1 - f) * 0.12) % (cnt + TAIL)
                    head = (cnt - 1) - pos          # travels cnt-1 -> 0 (downward)
                    for k in range(cnt):
                        dd = k - head               # head=0, tail = indices above head
                        if 0 <= dd < TAIL:
                            fi = (1.0 - dd / TAIL) ** 2.0
                            is_head = dd < 1.2
                            put(buf, seg + k,
                                min(255, int(cr * fi) + (150 if is_head else 0)),
                                int(cg * fi),
                                min(255, int(cb * fi) + (150 if is_head else 0)))
            write_leds(dev, gs, g, buf, prev[g])
            prev[g][:] = buf
        elapsed = time.time() - frame_t
        if elapsed < period:
            time.sleep(period - elapsed)
    # park at dark on exit
    for g, n in enumerate(gs):
        write_leds(dev, gs, g, bytearray(n * 3))
    try:
        dev.attach_kernel_driver(1)
    except Exception:
        pass

def parse_opts(argv):
    opts = {"leds": None, "colors": None}
    rest = []
    i = 0
    while i < len(argv):
        if argv[i] == "--leds":
            opts["leds"] = int(argv[i + 1]); i += 2
        elif argv[i] == "--fans":
            opts["fans"] = int(argv[i + 1]); i += 2
        elif argv[i] == "--speed":
            opts["speed"] = float(argv[i + 1]); i += 2
        elif argv[i] == "--colors":
            opts["colors"] = [int(c, 16) for c in argv[i + 1].split(",")]; i += 2
        else:
            rest.append(argv[i]); i += 1
    return rest, opts

def daemon_cmd(mode, hz, opts):
    cmd = [sys.executable, __file__, "anim-daemon", mode, str(hz)]
    if opts["leds"]:
        cmd += ["--leds", str(opts["leds"])]
    elif mode == "meteor":
        cmd += ["--leds", "48"]
    if opts["colors"]:
        cmd += ["--colors", ",".join(f"{c:06X}" for c in opts["colors"])]
    if opts.get("fans"):
        cmd += ["--fans", str(opts["fans"])]
    if opts.get("speed"):
        cmd += ["--speed", str(opts["speed"])]
    return cmd

def apply_mode(mode, opts=None):
    opts = opts or {}
    if mode == "off":
        stop_daemon()
        dev = open_dev()
        fill(dev, "all", 0)
        try: dev.attach_kernel_driver(1)
        except Exception: pass
        return
    stop_daemon()
    dev = open_dev()
    if mode == "work":
        # Static needs the daemon: one-shot fills expire when the LED engine
        # sleeps (~1-2 min). static mode = full rewrite + strobe every cycle.
        fill(dev, "all", 0x00FFFF)  # instant paint before daemon spins up
        try: dev.attach_kernel_driver(1)
        except Exception: pass
        import subprocess
        subprocess.run(["systemd-run", "--collect", "--unit=ene-fan-anim"] +
                       daemon_cmd("static", 1, {"colors": [0x00FFFF], "leds": None}), check=False)
    elif mode in ("gaming", "gaming-meteor"):
        if mode == "gaming":
            # Unified-color mode (2026-10-01): static daemon on purple base;
            # thermal-led service can override live via /run/thermal-led/fan_color.
            fill(dev, "all", 0x8000FF)
            try: dev.attach_kernel_driver(1)
            except Exception: pass
            import subprocess
            subprocess.run(["systemd-run", "--collect", "--unit=ene-fan-anim"] +
                           daemon_cmd("static", 1, {"colors": [0x8000FF], "leds": None}), check=False)
        else:
            # meteor base color first (no dark gap), then daemon animates
            leds = opts.get("leds", 48)   # 3 fans x 16 LEDs
            fill(dev, "all", 0x000000, leds)  # start dark so only the comet shows
            try: dev.attach_kernel_driver(1)
            except Exception: pass
            import subprocess
            hz = opts.get("hz", 60)
            subprocess.run(["systemd-run", "--collect", "--unit=ene-fan-anim"] +
                           daemon_cmd("meteor", hz, opts), check=False)

def stop_daemon():
    import subprocess, os, signal as s
    subprocess.run(["systemctl", "stop", "ene-fan-anim.service"], check=False,
                   capture_output=True)
    out = subprocess.run(["pgrep", "-f", "ene-fanctl anim-daemon"],
                         capture_output=True, text=True).stdout.split()
    for pid in out:
        try:
            os.kill(int(pid), s.SIGTERM)
        except Exception:
            pass
    time.sleep(0.4)

def main():
    argv, opts = parse_opts(sys.argv[1:])
    cmd = argv[0] if argv else "info"
    if cmd == "anim-daemon":
        mode = argv[1]
        hz = float(argv[2]) if len(argv) > 2 else 30.0
        pal = ((0xA0, 0x20, 0xFF))
        if opts["colors"]:
            c = opts["colors"][0]
            pal = ((c >> 16) & 0xFF, (c >> 8) & 0xFF, c & 0xFF)
        anim_daemon(mode, hz, opts["leds"], pal,
                    fans=opts.get("fans", 3), per_fan=opts.get("per_fan"),
                    speed_mult=opts.get("speed", 1.0))
        return
    if cmd == "mode":
        apply_mode(argv[1], opts)
        print("mode:", argv[1])
        return
    dev = open_dev()
    try:
        if cmd == "info":
            print("groups:", groups(dev))
        elif cmd == "read":
            print(rd(dev, int(argv[1], 16), int(argv[2])).hex())
        elif cmd == "backup":
            gs = groups(dev)
            data = {}
            for g, n in enumerate(gs):
                data[g] = rd(dev, group_base(dev, gs, g), 3 * n).hex()
            json.dump(data, open(argv[1], "w"))
            print("backed up", sum(gs), "leds")
        elif cmd == "restore":
            data = json.load(open(argv[1]))
            gs = groups(dev)
            for g, hexs in data.items():
                g = int(g)
                raw = bytes.fromhex(hexs)
                write_leds(dev, gs, g, raw)
            print("restored")
        elif cmd == "fill":
            fill(dev, argv[1], int(argv[2], 16), opts.get("leds"))
            print("ok")
        elif cmd == "ranges":
            # ranges "0-15=FF0000,16-31=00FF00,32-47=0000FF" [hold] [group]
            import subprocess as sp
            sp.run(["pkill", "-f", "anim-daemon"], capture_output=True)
            spec = argv[1]
            hold = float(argv[2]) if len(argv) > 2 else 15
            g = int(argv[3]) if len(argv) > 3 else 0
            gs = groups(dev)
            buf = bytearray(gs[g] * 3)
            for part in spec.split(","):
                rng, col = part.split("=")
                lo, hi = (int(x) for x in rng.split("-"))
                rgb = int(col, 16)
                r, gg, b = (rgb >> 16) & 0xFF, (rgb >> 8) & 0xFF, rgb & 0xFF
                for i in range(lo, hi + 1):
                    put(buf, i, r, gg, b)
            n_ok = write_leds(dev, gs, g, buf)
            print(f"painted {spec} on group {g}, acked {n_ok} transfers, holding {hold}s")
            time.sleep(hold)
            print("done")
        elif cmd == "off":
            fill(dev, argv[1] if len(argv) > 1 else "all", 0)
            print("ok")
    finally:
        try:
            dev.attach_kernel_driver(1)
        except Exception:
            pass

if __name__ == "__main__":
    main()
