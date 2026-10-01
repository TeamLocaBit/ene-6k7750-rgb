# ene-6k7750-rgb — Linux driver for ENE 6K7750 motherboards ARGB controller

Reverse-engineered from Linux, no Windows capture required. Lets you control the
addressable-RGB fans and on-board LED strips wired to the ENE 6K7750 ARGB
controller found on **Maxsun** (and sibling-brand) motherboards — which no
version of OpenRGB or other Linux RGB tooling supports.

Tested on **Maxsun MS-Terminator B760M D4** (device `0cf2:7750`), Arch/CachyOS
and should apply to any board enumerating that USB ID (run `lsusb | grep 0cf2`).

## Why this exists

The controller is invisible to OpenRGB. Its HID interface is a decoy that stalls
every control request. On Windows these boards are driven by Maxsun's "Maxsun
Sync" utility, which drops a `SK_64.dll` whose internals contain the full
register protocol. This project decodes that protocol and re-implements it with
pyusb — read-only on registers we mapped, writes only to LED RAM + the effect
commit strobe. Fan PWM registers exist on the same chip and were **deliberately
never touched** — this tool cannot affect your fans' speed.

## Install

```bash
sudo apt install python3-usb        # or: pip install pyusb
sudo cp ene_fanctl.py /usr/local/bin/ene-fanctl && sudo chmod +x /usr/local/bin/ene-fanctl
sudo ene-fanctl info                # should print: groups: [128, 128, 1, 1, 6] (on B760M D4)
```

Runs as root (direct USB control transfers). A udev rule can grant user access:
`SUBSYSTEM=="usb", ATTRS{idVendor}=="0cf2", ATTRS{idProduct}=="7750", MODE="0666"`

## Commands

```
ene-fanctl info                        # enumerate groups / LED counts
ene-fanctl backup file.json            # dump LED RAM  (DO THIS FIRST)
ene-fanctl restore file.json           # put it back
ene-fanctl fill <group|all> RRGGBB     # static color
ene-fanctl off [group|all]             # black
ene-fanctl mode work|gaming|off        # policy modes (see below; modes run a daemon)
ene-fanctl mode gaming-meteor          # meteor animation on fan ring
ene-fanctl read <reg-hex> <len>        # raw register read (debug)
```

## The two gotchas that took two weeks (and would have taken you the same)

1. **RAM byte order is plain R,B,G** — NOT BGR. Writing BGR makes purple render
   as yellow and sends you hunting phantom firmware bugs.
2. **Every RAM write needs the strobe commit** or the chip ACKs and the LEDs
   never change. Per group g: write `0x01,0x1b` @ `0xE021+16g`, then `0x01` @
   `0xE02F+16g`. The driver does this for you; do it manually if you script raw
   writes.
3. **Static fills expire after ~1-2 minutes** — the LED engine sleeps after a
   one-shot paint (its effect-slot duration byte times out). Hardware effects
   and animations never die because they re-strobe. Hence `mode work` runs a
   keep-alive daemon (full RAM rewrite + strobe, once per second) instead of
   trusting a one-shot fill to hold.

## LED map (MS-Terminator B760M D4)

`info` returns group LED counts. On this board: 5 groups `[128, 128, 1, 1, 6]` —

| group | LEDs | what it is |
|---|---|---|
| 0 | 128 | 3 daisy-chained ARGB case fans (16 LEDs each; idx 0-15 fan1, 16-31 fan2, 32-47 fan3, LED idx 0 = 6 o'clock, rising = up the arc) |
| 1 | 128 | header 2 — unpopulated on my build |
| 2,3 | 1,1 | small on-board status LEDs (near I/O shroud) |
| 4 | 6 | on-board accent strips near the rear I/O / PCIe area |

Other boards will differ — `fill <g> FF0000` each group and look inside the case
is the 5-minute calibration.

## Protocol (register map)

Vendor control transfers on USB interface 0:

```
READ : bmRequestType=0xC0  bRequest=0x81  wValue=reg>>16  wIndex=reg&0xFFFF  len
WRITE: bmRequestType=0x40  bRequest=0x80  wValue=reg>>16  wIndex=reg&0xFFFF  data
```

| register | meaning |
|---|---|
| `0xE0A1` | LED group count |
| `0xE0A2 + 2*g` | group g LED count (hi, lo) |
| `0xE300 + 3*i` | LED RAM, R,B,G order; cumulative across groups |
| `0xE021 + 16*g` | effect-slot commit byte 1 (`0x01`) + duration (`0x1b`) |
| `0xE02F + 16*g` | effect-slot commit strobe (`0x01` — **required after any RAM write**) |

The HID interface (IF1, report `0xEC`, vendor page `0xFF72`) is a decoy: all
feature/control requests STALL. Ignore it; everything is on IF0.

## examples/

Optional policy layer used on my machine (`pcmode` work/gaming dual-personality
box): `rgb-apply` sets mode colors at boot and on mode switch; `thermal_led.py`
paints the whole case with a live temperature heat-map (GPU+CPU heat-maps in
work mode; single hottest-component color in gaming mode) and drives the fan
daemon through a color file the strobe loop re-reads every frame. systemd units
included. OpenRGB on a headless server socket handles the other devices
(GPU, AIO block) — units included for that too.

## Suspend/resume

The controller drops off USB during system suspend. A long-lived daemon keeps a
stale handle after resume: its writes ACK but the LEDs stay dark (device
re-enumerates as a new node). Fix: restart the daemon on resume — e.g. a
systemd oneshot with `WantedBy=suspend.target hibernate.target
hybrid-sleep.target` running your apply script (see examples/rgb-apply).

## Safety notes

- Registers `0xE0xx/0xE3xx` only — no fan-PWM/fan-tach registers exist in this
  write path. (`Ec_SetFanDuty`-class functions exist in the chip's SDK and are
  decoded by name only; touching fan speed via a reverse-engineered protocol
  seemed like a bad idea, and probably is.)
- `backup` before experimenting; `restore` returns the factory state Windows
  left behind.
- The chip re-enumerates on the USB bus occasionally under writes; the driver
  handles open/find per invocation so you just re-run the command.

## Credit / prior art

Protocol decoded from disassembly (`capstone` + `pefile`, WinUsb IAT xrefs) of
Maxsun's `SK_64.dll` / `EneEc_x64.dll`. The `Ene6K7750` Windows driver and
`sdk_for_ENE6K7750.pdf` (Zhiwei/ENE vendor SDK, referenced in
[sgtaziz/lian-li-linux](https://github.com/sgtaziz/lian-li-linux)) were the
 Rosetta stone for register names.

## License

MIT — do whatever, attribution appreciated. PR welcome: other-board LED maps,
hardware effect modes (0x23 of them per the SDK), PWM reverse-engineering if you
are braver than me.
