#!/usr/bin/env python3
"""primdump.py -- read back, inspect and diff the PVR's own input.

src/dc/primdump.h records one frame's worth of every pvr_prim: polygon
headers, vertices, the textures those headers name and the palette RAM.
The same lines come out of two places, which is the point of the format:

  console   -DDB_PRIM_DUMP=<tic> plus EXTRA_LDFLAGS=-Wl,--wrap=pvr_prim.
            sc1pintro.c calls primdump_track(tic) from each crowd
            fighter's display proc; on update <tic> every pvr_prim is
            copied and on the next update the lot goes to serial.
  host      `make -C src/game/ssb64 hosttest BAKER=1` builds bakehost.c,
            whose hoststubs/bakepvr.c hands every submission to the same
            recorder. Same lines, no console.

What the stream is good for is A/B. A framebuffer dump (tools/check/fbdump.py)
answers "does this look right" at 640x480 over a 115200-baud line, one frame
at a time; this answers "is this the same input" bit for bit, and it is the
only instrument that reaches a console-only question. src/dc/mtx.h's SH4ZAM
backend is the one that needs it: off-target SH4ZAM is SHZ_SW, so FSCA and
FSRRA do not exist on the build host and no host test can see the divergence
they cause. On the console they are real, and this is where they show up.

  python3 tools/check/primdump.py LOG                 one dump per log: what is in it
  python3 tools/check/primdump.py A.log B.log         the dumps, diffed by tic
  python3 tools/check/primdump.py A.log B.log --drift the same run, but the
                                                      numbers measured
                                                      instead of matched

--drift is the mode for a scalar-vs-SH4ZAM comparison, which is *meant* to
differ: FSCA quantises the angle to a 16-bit index and FSRRA is good to about
2^-21, so a strict diff would report every frame as a failure and say nothing.
Drift mode instead pairs the two streams and reports the largest change per
vertex field, which is the number the backend was signed off against -- the
same split tools/check/figatree_check.py makes between double precision (must
agree exactly) and single (report how far the two drift). Use the strict diff
for a refactor, which is meant to be inert, and --drift for an approximation,
which is not.

Exit: 0 identical (or, under --drift, structurally identical), 1 differ,
2 usage or parse error. A log with no complete dump is an error, not an empty
comparison -- silence here reads as agreement.

THE FORMAT. src/dc/primdump.c's flush() is what writes it and is the
authority; src/dc/primdump.h's summary of it is stale in one place, noted
below. Every record but the framing lines is `search`ed for rather than
matched, so a dbglog prefix on the console's lines does not matter.

  primdump: begin <tic> <units> [OVERFLOW]   the frame, and its unit count
  pd <unit index> <hex>                      up to 4 units, 32 bytes each
  pdt <vram offset> <len>                    a texture the frame named
  pdx <vram offset> <hex>                    64 bytes of it, 128 hex chars
  pdp <first entry> <hex>                    8 palette entries, 32 bytes
  primdump: pal cfg <n>                      the palette's entry format, 0..3
  primdump: end <tic>

The .h says `pdt` carries the hex and does not mention `pdx` at all. The C
writes a bare `pdt` header and then the bytes as `pdx`, which is what
tools/lib/pvrsoft.py parses too -- pvrsoft is the renderer this stream feeds,
so its reading wins over the comment. Both are accepted here: a `pdt` line
that does carry hex is taken as the texture's first block.

WHY IT MATTERS THAT A UNIT IS 32 BYTES. The PVR consumes one 32-byte record
per command, so a unit is either a polygon header or a vertex and nothing
else, and the two are told apart by the top three bits of the first word --
this is the same test primdump.c makes when it decides which units name a
texture (1 in 8). That is what lets the diff say "unit 4711, a vertex, v
differs" rather than "byte 150752 differs".
"""
import argparse
import re
import struct
import sys

UNIT = 32
WORDS = UNIT // 4

# The TA command in a unit's first word. A polygon header is 0b100 in the
# top three bits; a vertex is 0b111, with PVR_CMD_VERTEX_EOL setting bit 28.
CMD_POLYHDR = 4
CMD_VERTEX = 7
CMD_VERTEX_EOL = 0xF

# pvr_vertex_t: flags, x, y, z, u, v, argb, oargb -- all little-endian, the
# floats IEEE single.
VTX_FIELDS = ("x", "y", "z", "u", "v")

RE_PD = re.compile(r"\bpd (\d+) ([0-9a-f]+)\s*$")
RE_PDT = re.compile(r"\bpdt (\d+) (\d+)(?: ([0-9a-f]+))?\s*$")
RE_PDX = re.compile(r"\bpdx (\d+) ([0-9a-f]+)\s*$")
RE_PDP = re.compile(r"\bpdp (\d+) ([0-9a-f]+)\s*$")
RE_PALCFG = re.compile(r"primdump: pal cfg (\d+)")
RE_BEGIN = re.compile(r"primdump: begin (\d+) (\d+)( OVERFLOW)?")
RE_END = re.compile(r"primdump: end (\d+)")


def u32(b, i):
    return struct.unpack_from("<I", b, i * 4)[0]


def f32(b, i):
    return struct.unpack_from("<f", b, i * 4)[0]


def kind(u):
    """'hdr', 'vtx' or '?' for a 32-byte unit."""
    top = u32(u, 0) >> 29

    if top == CMD_POLYHDR:
        return "hdr"
    if top == CMD_VERTEX:
        return "vtx"
    return "?"


def describe(u):
    """A unit as the one line the diff prints for it."""
    cmd = u32(u, 0)

    if kind(u) == "hdr":
        # mode2's low three bits are the texture size's log2 (primdump.c
        # tex_bytes), mode3's top three the format; only what is printed
        # here is decoded, the rest is the header's own business.
        tex = " textured" if (cmd & 8) else ""
        return "hdr%s mode1=%08x mode2=%08x mode3=%08x" % (
            tex, u32(u, 1), u32(u, 2), u32(u, 3))
    if kind(u) == "vtx":
        eol = " EOL" if (cmd >> 28) == CMD_VERTEX_EOL else ""
        return ("vtx%s x=%g y=%g z=%g u=%g v=%g argb=%08x oargb=%08x"
                % (eol, f32(u, 1), f32(u, 2), f32(u, 3), f32(u, 4), f32(u, 5),
                   u32(u, 6), u32(u, 7)))
    return "? %08x %08x %08x %08x %08x %08x %08x %08x" % tuple(
        u32(u, i) for i in range(WORDS))


class Dump:
    """One `begin`..`end` block."""

    def __init__(self, tic):
        self.tic = tic
        self.units = b""            # the stream, 32 bytes to a unit
        self.overflow = False
        self.tex_decl = []          # (offset, len) as the frame declared them
        self.tex = {}               # vram offset -> bytes, first write wins
        self.pal = b""              # palette RAM, as dumped
        self.palfmt = None

    def __len__(self):
        return len(self.units) // UNIT

    def unit(self, i):
        return self.units[i * UNIT:(i + 1) * UNIT]


def parse(path):
    """Every complete dump in `path`, in the order they were written."""
    dumps = []
    cur = None
    units = []          # (declared index, bytes), checked below
    bad = 0

    with open(path, errors="replace") as fh:
        for line in fh:
            m = RE_BEGIN.search(line)
            if m:
                cur = Dump(int(m.group(1)))
                cur.overflow = m.group(3) is not None
                units = []
                continue
            if cur is None:
                continue
            m = RE_PD.search(line)
            if m:
                units.append((int(m.group(1)), bytes.fromhex(m.group(2))))
                continue
            m = RE_PDX.search(line)
            if m:
                # first write wins, as tools/lib/pvrsoft.py's read_log does
                cur.tex.setdefault(int(m.group(1)), bytes.fromhex(m.group(2)))
                continue
            m = RE_PDT.search(line)
            if m:
                off, n = int(m.group(1)), int(m.group(2))
                cur.tex_decl.append((off, n))
                if m.group(3):          # a pdt that carried its own hex
                    cur.tex.setdefault(off, bytes.fromhex(m.group(3)))
                continue
            m = RE_PDP.search(line)
            if m:
                off = int(m.group(1)) * 4      # entry index -> byte offset
                blob = bytes.fromhex(m.group(2))
                if len(cur.pal) < off + len(blob):
                    cur.pal += b"\0" * (off + len(blob) - len(cur.pal))
                cur.pal = cur.pal[:off] + blob + cur.pal[off + len(blob):]
                continue
            m = RE_PALCFG.search(line)
            if m:
                cur.palfmt = int(m.group(1))
                continue
            m = RE_END.search(line)
            if m:
                # the declared start of each chunk must be where the last
                # one ended: a dropped serial line would otherwise shift
                # every later unit and read as a difference
                at = 0
                for idx, blob in units:
                    if idx != at:
                        bad += 1
                        break
                    at += len(blob) // UNIT
                cur.units = b"".join(blob for _, blob in units)
                dumps.append(cur)
                cur = None
                continue

    if not dumps:
        print("primdump: no complete dump in %s" % path, file=sys.stderr)
        sys.exit(2)
    if bad:
        print("primdump: %s: %d dump(s) have a gap in the pd unit indices -- "
              "serial data was dropped, and a diff against this log will be "
              "misleading" % (path, bad), file=sys.stderr)
    return dumps


def by_tic(dumps):
    out = {}
    for d in dumps:
        out.setdefault(d.tic, d)
    return out


# ---- one log on its own ----------------------------------------------------

def info(path):
    for d in parse(path):
        print("tic %d: %d units, %d texture(s), palette %d entries%s"
              % (d.tic, len(d), len(d.tex), len(d.pal) // 4,
                 " OVERFLOW" if d.overflow else ""))
        if d.palfmt is not None:
            print("    pal cfg %d" % d.palfmt)
        # what the frame is made of, which is the quickest way to see that a
        # dump is of the scene you meant and not, say, an empty draw
        seen = {}
        for i in range(len(d)):
            k = kind(d.unit(i))
            seen[k] = seen.get(k, 0) + 1
        print("    %s" % ", ".join("%s %d" % (k, seen[k])
                                   for k in sorted(seen)))


# ---- two logs --------------------------------------------------------------

def cmp_bytes(name, a, b):
    if a == b:
        return True
    print("  %s differs" % name)
    for i in range(max(len(a), len(b))):
        if a[i:i + 1] != b[i:i + 1]:
            print("    byte %d: %s vs %s"
                  % (i, a[i:i + 1].hex() or "(end)", b[i:i + 1].hex() or "(end)"))
            break
    print("    %d bytes vs %d" % (len(a), len(b)))
    return False


def diff_unit(i, a, b, ctx):
    """The first difference at unit `i`, with a little either side."""
    print("  first difference at unit %d (%s)" % (i, kind(a.unit(i))))
    lo = max(0, i - ctx)
    for k in range(lo, min(len(a), len(b), i + ctx + 1)):
        ua, ub = a.unit(k), b.unit(k)
        if ua == ub:
            print("    %5d  %s" % (k, describe(ua)[:100]))
        else:
            print("    %5d  A %s" % (k, describe(ua)[:100]))
            print("    %5d  B %s" % (k, describe(ub)[:100]))


def drift_unit(i, a, b):
    """The first structural difference, or None when the shapes agree."""
    if kind(a.unit(i)) != kind(b.unit(i)):
        print("  unit %d is a %s in A and a %s in B"
              % (i, kind(a.unit(i)), kind(b.unit(i))))
        return True
    return False


def diff_dump(a, b, ctx):
    """True when the two frames are the same stream."""
    same = True

    if a.overflow or b.overflow:
        print("  OVERFLOW: %s%s -- the frame had more than the recorder's "
              "4 MB and was cut, so this comparison is partial"
              % ("A " if a.overflow else "", "B" if b.overflow else ""))
        same = False
    if len(a) != len(b):
        print("  %d units vs %d" % (len(a), len(b)))
        same = False

    n = min(len(a), len(b))
    for i in range(n):
        if a.unit(i) != b.unit(i):
            diff_unit(i, a, b, ctx)
            same = False
            break

    same &= cmp_bytes("palette", a.pal, b.pal)
    if a.palfmt != b.palfmt:
        print("  pal cfg %s vs %s" % (a.palfmt, b.palfmt))
        same = False
    if a.tex != b.tex:
        only_a = sorted(set(a.tex) - set(b.tex))
        only_b = sorted(set(b.tex) - set(a.tex))
        if only_a or only_b:
            print("  textures only in A: %s; only in B: %s"
                  % (["%#x" % o for o in only_a], ["%#x" % o for o in only_b]))
        for o in sorted(set(a.tex) & set(b.tex)):
            if a.tex[o] != b.tex[o]:
                cmp_bytes("texture at %#x" % o, a.tex[o], b.tex[o])
        same = False
    return same


def drift_dump(a, b, ctx):
    """Pair the two streams and measure the change, for an approximation.

    The two are expected to differ. What is reported is the largest change
    per vertex field, over the vertices that line up; a structural
    difference (a unit that is a header in one and a vertex in the other, or
    a different unit count) is not drift and is reported as itself.
    """
    n = min(len(a), len(b))
    worst = {f: (0.0, 0) for f in VTX_FIELDS}
    paired = 0
    argb_bad = 0
    oargb_bad = 0
    # two ways to be the wrong shape, and they read differently: a different
    # number of units, or a unit that is one thing in A and another in B
    count_differs = (len(a) != len(b))
    struct_bad = count_differs

    for i in range(n):
        if drift_unit(i, a, b):
            struct_bad = True
            break
        ua, ub = a.unit(i), b.unit(i)
        if kind(ua) != "vtx":
            if ua != ub:
                struct_bad = True
                print("  unit %d differs, and is not a vertex: %s"
                      % (i, describe(ua)[:100]))
                break
            continue
        paired += 1
        for k, f in enumerate(VTX_FIELDS, start=1):
            d = abs(f32(ua, k) - f32(ub, k))
            if d > worst[f][0]:
                worst[f] = (d, i)
        if u32(ua, 6) != u32(ub, 6):
            argb_bad += 1
        if u32(ua, 7) != u32(ub, 7):
            oargb_bad += 1

    print("  %d vertices paired%s"
          % (paired, " (unit counts differ)" if count_differs else ""))
    for f in VTX_FIELDS:
        d, at = worst[f]
        print("    %-2s max |delta| %.6e%s"
              % (f, d, "" if d == 0.0 else "  (unit %d)" % at))
    # integer fields: a difference is a difference, there is nothing to measure
    if argb_bad:
        print("    argb : %d differ" % argb_bad)
    if oargb_bad:
        print("    oargb: %d differ" % oargb_bad)
    return not struct_bad


def main():
    ap = argparse.ArgumentParser(
        description="read back and diff -DDB_PRIM_DUMP / bake_host streams")
    ap.add_argument("logs", nargs="+", help="one log to inspect, two to diff")
    ap.add_argument("--drift", action="store_true",
                    help="measure the change instead of matching it -- the "
                         "mode for scalar vs SH4ZAM, which is meant to differ")
    ap.add_argument("--context", type=int, default=4,
                    help="units either side of the first difference (default 4)")
    args = ap.parse_args()

    if len(args.logs) == 1:
        info(args.logs[0])
        return 0
    if len(args.logs) > 2:
        ap.error("at most two logs")

    a_all, b_all = parse(args.logs[0]), parse(args.logs[1])
    a, b = by_tic(a_all), by_tic(b_all)
    if not a or not b:
        return 2

    ticks = sorted(set(a) & set(b))
    if not ticks:
        print("primdump: no tic in both logs (A: %s; B: %s)"
              % (sorted(a), sorted(b)), file=sys.stderr)
        return 2
    if len(a) != len(b) or set(a) != set(b):
        print("primdump: A has tics %s, B has %s -- comparing the %d in common"
              % (sorted(a), sorted(b), len(ticks)))

    ok = True
    for t in ticks:
        print("tic %d: %d vs %d units" % (t, len(a[t]), len(b[t])))
        fn = drift_dump if args.drift else diff_dump
        ok &= fn(a[t], b[t], args.context)

    if not ok:
        print("primdump: %s" % ("structural difference" if args.drift
                                else "the two streams differ"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
