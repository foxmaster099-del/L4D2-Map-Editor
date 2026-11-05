# L4D2_Map_Editor.py - L4D2 Map Editor: edit L4D2 maps through .lmp patches (invisible walls, entities)
# Made by Rune. Open source: free to use, change and share. Please credit Rune if you share it.
#
#   python L4D2_Map_Editor.py                  opens the window (Walls, Entities and Build modes)
#   python L4D2_Map_Editor.py <map.bsp|addon.vpk>
#   python L4D2_Map_Editor.py check [Left 4 Dead 2 folder]     finds problems with your patches and addons
#
#   python L4D2_Map_Editor.py <map.bsp> write <map>_l_<N>.lmp all
#   python L4D2_Map_Editor.py <addon.vpk> <map> write <map>_l_<N>.lmp all
#   ... write <out.lmp> box X0 Y0 X1 Y1   |   write <out.lmp> <brush> [brush ...]   |   list X0 Y0 X1 Y1
#
# A .lmp replaces one part ("lump") of a map when it loads. Walls mode writes the brushes lump (18) with
# the player-clip flag turned off on the walls you pick. Entities mode writes the entity lump (0): delete,
# move, copy, add and edit entities. Build mode places the game's own props (prop_dynamic) and invisible
# walls around them. Props are drawn with their real shapes, read from the game's model files (.mdl/.vvd/.vtx).
# Nothing is recompiled and the map itself is never shipped.
#
# The tool reads the patches that already exist for the map (Valve's in update/maps, any inside the same
# addon .vpk), builds on them so their fixes are kept, picks free file numbers, and also writes your change
# into Valve's game-mode versions of the patch (_h_, _s_) when the map has them.
import lzma, math, os, re, struct, sys, time

PLAYERCLIP, SOLID = 0x10000, 0x1
LUMP_ENTITIES, LUMP_BRUSHES = 0, 18
SKIP_SURF = 0x2 | 0x4 | 0x40 | 0x80 | 0x100 | 0x200   # sky, sky2d, trigger, nodraw, hint, skip


# ---------------------------------------------------------------- vpk

def vpk_tree(path):
    """{'maps/x.bsp': entry} for a .vpk (single file or *_dir.vpk)"""
    with open(path, "rb") as f:
        head = f.read(28)
        sig, ver, size = struct.unpack_from("<III", head)
        if sig != 0x55AA1234:
            raise ValueError("%s is not a .vpk (for split packs, open the *_dir.vpk)" % os.path.basename(path))
        hdr = 12 if ver == 1 else 28
        f.seek(hdr)
        t = f.read(size)
    p, out = 0, {}

    def s():
        nonlocal p
        e = t.index(b"\0", p)
        v = t[p:e].decode("latin-1")
        p = e + 1
        return v

    while True:
        ext = s()
        if not ext:
            break
        while True:
            folder = s()
            if not folder:
                break
            while True:
                name = s()
                if not name:
                    break
                _crc, pre, arc, ofs, ln, _end = struct.unpack_from("<IHHIIH", t, p)
                p += 18
                full = ("" if folder == " " else folder + "/") + name + "." + ext
                out[full.lower()] = (arc, ofs, ln, t[p:p + pre], hdr + size)
                p += pre
    return out


def vpk_read(path, entry, limit=None):
    arc, ofs, ln, pre, data_start = entry
    if limit is not None and len(pre) >= limit:
        return pre[:limit]
    if not ln:
        return pre
    n = ln if limit is None else min(ln, limit - len(pre))
    part = path if arc == 0x7FFF else re.sub(r"_dir\.vpk$", "_%03d.vpk" % arc, path, flags=re.I)
    with open(part, "rb") as f:
        f.seek(data_start + ofs if arc == 0x7FFF else ofs)
        return pre + f.read(n)


def maps_in_vpk(path):
    return sorted(k[5:-4] for k in vpk_tree(path) if k.startswith("maps/") and k.endswith(".bsp"))


# ---------------------------------------------------------------- bsp

def lump_bytes(d, i):
    _ver, ofs, ln, cc = struct.unpack_from("<iii4s", d, 8 + i * 16)
    raw = d[ofs:ofs + ln]
    if cc != b"\0\0\0\0" and raw[:4] == b"LZMA":          # compressed lump
        size, _packed = struct.unpack_from("<II", raw, 4)
        props = raw[12:17]
        lc, rest = props[0] % 9, props[0] // 9
        filt = {"id": lzma.FILTER_LZMA1, "dict_size": struct.unpack_from("<I", props, 1)[0],
                "lc": lc, "lp": rest % 5, "pb": rest // 5}
        dec = lzma.LZMADecompressor(lzma.FORMAT_RAW, filters=[filt])
        return dec.decompress(raw[17:], size)[:size]
    return raw


def lump_version(d, i):
    return struct.unpack_from("<i", d, 8 + i * 16)[0]


def records(d, i, fmt):
    b = lump_bytes(d, i)
    n = struct.calcsize(fmt)
    return [struct.unpack_from(fmt, b, k) for k in range(0, len(b) - n + 1, n)]


def game_root(path):
    p = os.path.dirname(os.path.abspath(path))
    for _ in range(5):
        if os.path.isdir(os.path.join(p, "update")) and os.path.isdir(os.path.join(p, "left4dead2")):
            return p
        p = os.path.dirname(p)
    return None


def lmp_header(raw):
    return struct.unpack_from("<5i", raw, 0)          # offset, lump id, version, length, map revision


class Map:
    def __init__(self, path, inner=None):
        """path = .bsp, or .vpk + inner map name"""
        self.path = path
        if path.lower().endswith(".vpk"):
            tree = vpk_tree(path)
            names = maps_in_vpk(path)
            if not names:
                raise ValueError("there are no maps in %s" % os.path.basename(path))
            if inner is None and len(names) == 1:
                inner = names[0]
            if inner not in names:
                raise ValueError("pick one of the maps in this .vpk: %s" % ", ".join(names))
            self.name = inner
            self.d = vpk_read(path, tree["maps/%s.bsp" % inner.lower()])
        else:
            tree = {}
            self.name = os.path.splitext(os.path.basename(path))[0]
            self.d = open(path, "rb").read()
        if self.d[:4] != b"VBSP":
            raise ValueError("not a Source map (.bsp)")
        self.revision = struct.unpack_from("<i", self.d, 8 + 64 * 16)[0]
        # existing patches: {prefix: {number: (label, reader)}}
        self.lmps = {}
        root = game_root(path)
        self.found_game = root is not None
        pat = re.compile(re.escape(self.name.lower()) + r"_([a-z])_(\d+)\.lmp$")
        found = []
        if root:
            md = os.path.join(root, "update", "maps")
            for f in os.listdir(md) if os.path.isdir(md) else []:
                m = pat.match(f.lower())
                if m:
                    fp = os.path.join(md, f)
                    found.append((m, "Valve's " + f, lambda fp=fp: open(fp, "rb").read()))
        for k in tree:
            m = pat.match(k[5:]) if k.startswith("maps/") else None
            if m:
                found.append((m, "this .vpk's " + k[5:], lambda k=k: vpk_read(path, tree[k])))
        for m, label, read in found:
            self.lmps.setdefault(m.group(1), {})[int(m.group(2))] = (label, read)
        self._cache = {}
        self.brushes, self.base = self.base_lump("l", LUMP_BRUSHES)
        self.existing = self.lmps.get("l", {})

    def prefixes(self):
        return ["l"] + sorted(p for p in self.lmps if p != "l")

    def read_lmp(self, prefix, n):
        key = (prefix, n)
        if key not in self._cache:
            raw = self.lmps[prefix][n][1]()
            o, lid, _v, length, _r = lmp_header(raw)
            self._cache[key] = (lid, raw[o:o + length])
        return self._cache[key]

    def base_lump(self, prefix, lump_id):
        """(bytes, label of the patch it comes from or None) - the lump as the game sees it for this prefix"""
        bsp = lump_bytes(self.d, lump_id)
        for n in sorted(self.lmps.get(prefix, {}), reverse=True):
            lid, body = self.read_lmp(prefix, n)
            if lid == lump_id:
                if lump_id == LUMP_BRUSHES and len(body) != len(bsp):
                    continue
                return body, self.lmps[prefix][n][0]
        return bsp, None

    def plan(self, lump_id, folder):
        """[(prefix, number, path, overwriting)] - where to save a patch of this lump"""
        own = {}
        if folder and os.path.isdir(folder):
            pat = re.compile(re.escape(self.name.lower()) + r"_([a-z])_(\d+)\.lmp$")
            for f in os.listdir(folder):
                m = pat.match(f.lower())
                if m:
                    try:
                        with open(os.path.join(folder, f), "rb") as fh:
                            lid = lmp_header(fh.read(20))[1]
                    except (OSError, struct.error):
                        lid = None
                    own[(m.group(1), int(m.group(2)))] = lid
        out = []
        for p in self.prefixes():
            taken = set(self.lmps.get(p, {}))
            same = [n for (q, n), lid in own.items() if q == p and lid == lump_id and n not in taken]
            if same:
                n, over = max(same), True
            else:
                n, over = 0, False
                while n in taken or (p, n) in own:
                    n += 1
            out.append((p, n, os.path.join(folder or "", "%s_%s_%d.lmp" % (self.name, p, n)), over))
        return out

    def out_name(self):
        return self.plan(LUMP_BRUSHES, None)[0][2]

    def notes(self):
        s = "Save as %s" % self.out_name()
        if self.base:
            s += "  (keeps %s, which already patches walls here)" % self.base
        extra = [p for p in self.prefixes() if p != "l"]
        if extra:
            s += "  -  also saves the _%s_ game-mode versions" % "_, _".join(extra)
        if not self.found_game:
            s += "  -  map isn't inside the game folder, so Valve's patches in update/maps weren't checked"
        return s


def write_lmp_file(out, lump_id, version, revision, body):
    with open(out, "wb") as f:
        f.write(struct.pack("<5i", 20, lump_id, version, len(body), revision) + body)


def brush_patch(brushes, pick):
    body = bytearray(brushes)
    for i in pick:
        c, = struct.unpack_from("<i", body, i * 12 + 8)
        struct.pack_into("<i", body, i * 12 + 8, c & ~PLAYERCLIP)
    return bytes(body)


def write_lmp(m, pick, out):
    """walls patch for the main (_l_) version"""
    write_lmp_file(out, LUMP_BRUSHES, 0, m.revision, brush_patch(m.brushes, pick))


def clip_brushes(d, brushes):
    """[(brush index, mins, maxs)] for every clip-only brush"""
    planes = records(d, 1, "<ffffi")
    sides = [s[0] for s in records(d, 19, "<Hhhh")]
    out = []
    for i in range(len(brushes) // 12):
        first, num, contents = struct.unpack_from("<iii", brushes, i * 12)
        if not contents & PLAYERCLIP or contents & SOLID:
            continue
        mn, mx = [None] * 3, [None] * 3
        for k in range(first, first + num):
            n, dist = planes[sides[k]][:3], planes[sides[k]][3]
            for a in range(3):
                if all(abs(n[j] - (1 if j == a else 0)) < 1e-4 for j in range(3)):
                    mx[a] = dist
                if all(abs(n[j] + (1 if j == a else 0)) < 1e-4 for j in range(3)):
                    mn[a] = -dist
        out.append((i, mn, mx))
    return out


def world_faces(d, min_area=200.0):
    """[(points, normal, plane dist, (r, g, b))] for the map's visible world surfaces, texture-colored"""
    planes = records(d, 1, "<ffffi")
    verts = records(d, 3, "<fff")
    edges = records(d, 12, "<HH")
    surfedges = [s[0] for s in records(d, 13, "<i")]
    texinfo = records(d, 6, "<8f8fii")
    texdata = records(d, 2, "<fffiiiii")
    faces = records(d, 7, "<HBBihhhh4BifiiiiiHHI")
    first, num = records(d, 14, "<fffffffffiii")[0][10:12]
    out = []
    for f in faces[first:first + num]:
        if f[5] < 0 or texinfo[f[5]][16] & SKIP_SURF:
            continue
        n, dist = planes[f[0]][:3], planes[f[0]][3]   # a face's plane already faces outward
        pts = []
        for k in range(f[3], f[3] + f[4]):
            e = surfedges[k]
            pts.append(verts[edges[e][0] if e >= 0 else edges[-e][1]])
        if len(pts) < 3:
            continue
        ax = ay = az = 0.0                       # polygon area (Newell)
        for j in range(len(pts)):
            p, q = pts[j - 1], pts[j]
            ax += (p[1] - q[1]) * (p[2] + q[2])
            ay += (p[2] - q[2]) * (p[0] + q[0])
            az += (p[0] - q[0]) * (p[1] + q[1])
        if (ax * ax + ay * ay + az * az) ** 0.5 / 2 < min_area:
            continue
        td = texinfo[f[5]][17]
        rgb = texdata[td][:3] if 0 <= td < len(texdata) else (0.4, 0.4, 0.4)
        rgb = tuple(min(1.0, max(0.06, c) ** (1 / 2.2) * 1.15) for c in rgb)   # linear -> screen
        out.append((pts, n, dist, rgb))
    return out


def model_bounds(d):
    return [(m[0:3], m[3:6]) for m in records(d, 14, "<fffffffffiii")]


def clip_below(pts, cut):
    """the part of a polygon at or below height `cut`"""
    out = []
    for j in range(len(pts)):
        p, q = pts[j - 1], pts[j]
        pin, qin = p[2] <= cut, q[2] <= cut
        if qin != pin:
            t = (cut - p[2]) / (q[2] - p[2])
            out.append((p[0] + (q[0] - p[0]) * t, p[1] + (q[1] - p[1]) * t, cut))
        if qin:
            out.append(q)
    return out


def split_polygon(pts, axis, value):
    """the parts of a convex polygon below and above `value` along `axis` (a direction vector)"""
    lo, hi = [], []
    d = [p[0] * axis[0] + p[1] * axis[1] + p[2] * axis[2] - value for p in pts]
    for j in range(len(pts)):
        p, q, dp, dq = pts[j - 1], pts[j], d[j - 1], d[j]
        if (dp < 0) != (dq < 0):
            t = dp / (dp - dq)
            x = (p[0] + (q[0] - p[0]) * t, p[1] + (q[1] - p[1]) * t, p[2] + (q[2] - p[2]) * t)
            lo.append(x)
            hi.append(x)
        (lo if dq < 0 else hi).append(q)
    return lo, hi


def tile_faces(faces, step_h=128.0, step_v=96.0):
    """[(points, normal, rgb, face index)] - big faces cut into pieces so they sort right against props"""
    out = []
    for fi, (pts, n, _dist, rgb) in enumerate(faces):
        if abs(n[2]) > 0.7:                       # floors and roofs: a grid
            axes = (((1.0, 0.0, 0.0), step_h), ((0.0, 1.0, 0.0), step_h))
        else:                                     # walls: rows and columns
            hl = (n[0] * n[0] + n[1] * n[1]) ** 0.5 or 1.0
            axes = (((0.0, 0.0, 1.0), step_v), ((-n[1] / hl, n[0] / hl, 0.0), step_h))
        pieces = [pts]
        for axis, step in axes:
            nxt = []
            for poly in pieces:
                vals = [p[0] * axis[0] + p[1] * axis[1] + p[2] * axis[2] for p in poly]
                k = math.floor(min(vals) / step) + 1
                rest = poly
                while k * step < max(vals) - 0.5 and len(rest) >= 3:
                    a, rest = split_polygon(rest, axis, k * step)
                    if len(a) >= 3:
                        nxt.append(a)
                    k += 1
                if len(rest) >= 3:
                    nxt.append(rest)
            pieces = nxt
        out.extend((p, n, rgb, fi) for p in pieces)
    return out


def in_box(mn, mx, box):
    x0, x1 = sorted((box[0], box[2]))
    y0, y1 = sorted((box[1], box[3]))
    return None not in mn and mx[0] > x0 and mn[0] < x1 and mx[1] > y0 and mn[1] < y1


# ---------------------------------------------------------------- entities

class Ent:
    """one entity: ordered (key, value) pairs; `raw` keeps its original text until it is changed"""
    __slots__ = ("pairs", "raw")

    def __init__(self, pairs, raw=None):
        self.pairs, self.raw = tuple(pairs), raw

    def get(self, key, default=""):
        for k, v in self.pairs:
            if k.lower() == key.lower():
                return v
        return default

    def with_value(self, key, value):
        out, done = [], False
        for k, v in self.pairs:
            if k.lower() == key.lower() and not done:
                out.append((k, value))
                done = True
            elif k.lower() == key.lower():
                continue
            else:
                out.append((k, v))
        if not done:
            out.append((key, value))
        return Ent(out)

    def without(self, index):
        return Ent(self.pairs[:index] + self.pairs[index + 1:])

    def ident(self):
        hid = self.get("hammerid")
        if hid:
            return ("hammerid", hid)
        return ("noid", self.get("classname"), self.get("targetname"), self.get("origin"), self.get("model"))

    def origin(self):
        try:
            x, y, z = (float(v) for v in self.get("origin", "0 0 0").split()[:3])
            return x, y, z
        except ValueError:
            return 0.0, 0.0, 0.0

    def text(self, nl="\n"):
        if self.raw is not None:
            return self.raw
        return "{" + nl + "".join('"%s" "%s"%s' % (k, v, nl) for k, v in self.pairs) + "}" + nl


def parse_entities(raw):
    """(prefix text, [Ent], newline style) - untouched entities keep their exact original text"""
    t = raw.decode("latin-1").rstrip("\0")
    ents, i, n = [], 0, len(t)
    first = t.find("{")
    prefix = t[:first] if first > 0 else ""
    i = max(first, 0)
    starts = []
    while i < n:
        c = t[i]
        if c == "{":
            start, i, pairs, key = i, i + 1, [], None
            while True:
                while i < n and t[i] in " \t\r\n":
                    i += 1
                if i >= n:
                    raise ValueError("entity text ends inside an entity")
                if t[i] == "}":
                    i += 1
                    break
                if t[i] != '"':
                    raise ValueError("unexpected %r in entity text at %d" % (t[i], i))
                j = t.index('"', i + 1)
                s = t[i + 1:j]
                i = j + 1
                if key is None:
                    key = s
                else:
                    pairs.append((key, s))
                    key = None
            starts.append(start)
            ents.append(Ent(pairs))
        elif c in " \t\r\n":
            i += 1
        else:
            raise ValueError("unexpected %r in entity text at %d" % (c, i))
    for k, e in enumerate(ents):                       # raw text = up to the next entity
        e.raw = t[starts[k]:starts[k + 1] if k + 1 < len(starts) else n]
    nl = "\r" if "\r" in t[:400] and "\n" not in t[:400] else ("\r\n" if "\r\n" in t[:400] else "\n")
    return prefix, ents, nl


def dump_entities(prefix, ents, nl):
    return (prefix + "".join(e.text(nl) for e in ents)).encode("latin-1") + b"\0"


def keyed(ents):
    """{identity: [indexes]}"""
    out = {}
    for i, e in enumerate(ents):
        out.setdefault(e.ident(), []).append(i)
    return out


def entity_changes(base, edited):
    """deleted identities, {identity: new Ent}, [added Ents] between two entity lists"""
    b = keyed(base)
    seen, modified, added = set(), {}, []
    for e in edited:
        k = e.ident()
        if k in b and k not in seen:
            seen.add(k)
            old = base[b[k][0]]
            if e.pairs != old.pairs:
                modified[k] = e
        else:
            added.append(e)
    deleted = [k for k in b if k not in seen]
    return deleted, modified, added


def apply_changes(ents, deleted, modified, added):
    """apply changes to another version of the entity list; returns (new list, identities not found)"""
    k = keyed(ents)
    missing = [x for x in list(deleted) + list(modified) if x not in k]
    gone = {k[x][0] for x in deleted if x in k}
    out = []
    for i, e in enumerate(ents):
        if i in gone:
            continue
        key = e.ident()
        out.append(modified[key] if key in modified and k[key][0] == i else e)
    return out + list(added), missing


OUTPUT_SEP = "\x1b"


def output_target(value):
    return (value.split(OUTPUT_SEP) if OUTPUT_SEP in value else value.split(","))[0].strip()


def broken_references(ents, deleted_names):
    """[(entity, key, target)] - outputs and links that point at names no entity has anymore"""
    names = {e.get("targetname").lower() for e in ents if e.get("targetname")}
    lost = {n.lower() for n in deleted_names if n} - names
    out = []
    if not lost:
        return out
    for e in ents:
        for k, v in e.pairs:
            if k.lower().startswith("on") or k.lower() in ("parentname", "target", "filtername", "glow"):
                tgt = output_target(v) if k.lower().startswith("on") else v
                if tgt.lower() in lost:
                    out.append((e, k, tgt))
    return out


CATEGORIES = [
    ("Blockers & clips", ("env_physics_blocker", "env_player_blocker", "func_playerinfected_clip",
                          "func_playerghostinfected_clip", "func_nav_blocker", "func_brush", "func_wall_toggle",
                          "func_clip_vphysics", "func_wall"), "#ff9f0a"),
    ("Ladders", ("func_simpleladder", "func_ladder"), "#64d2ff"),
    ("Doors & breakables", ("prop_door", "func_door", "func_breakable", "func_movelinear", "func_button"), "#0a84ff"),
    ("Props", ("prop_",), "#e5e5ea"),
    ("Weapons & items", ("weapon_", "upgrade_", "item_"), "#ffd60a"),
    ("Triggers", ("trigger_",), "#bf5af2"),
    ("Spawns & nav", ("info_survivor", "info_player", "info_zombie", "info_changelevel", "info_landmark",
                      "info_director", "commentary"), "#30d158"),
]
OTHER = ("Logic & other", (), "#8e8e93")


def category(classname):
    c = classname.lower()
    for name, prefixes, color in CATEGORIES:
        if any(c == p or c.startswith(p) for p in prefixes):
            return name, color
    return OTHER[0], OTHER[2]


TEMPLATES = {
    "Invisible wall (blocks everyone)": [
        ("classname", "env_physics_blocker"), ("origin", "0 0 0"), ("mins", "-64 -8 0"), ("maxs", "64 8 128"),
        ("boxmins", "-64 -8 0"), ("boxmaxs", "64 8 128"), ("initialstate", "1"), ("BlockType", "0")],
    "Concrete barrier prop": [
        ("classname", "prop_dynamic"), ("origin", "0 0 0"), ("angles", "0 0 0"),
        ("model", "models/props_fortifications/concrete_wall001_96_reference.mdl"), ("solid", "6"),
        ("disableshadows", "1")],
}


# ---------------------------------------------------------------- props: game files, models, static props

class GameFiles:
    """read game files by path: the map's packed files first, then its addon .vpk, then the game's .vpk files"""

    def __init__(self, game=None, d=None, vpk=None):
        self.srcs = []                                      # (kind, handle, {path: entry})
        if d is not None:
            try:
                import io, zipfile
                z = zipfile.ZipFile(io.BytesIO(lump_bytes(d, 40)))
                self.srcs.append(("zip", z, {n.lower().replace("\\", "/"): n for n in z.namelist()}))
            except Exception:
                pass
        if vpk:
            try:
                self.srcs.append(("vpk", vpk, vpk_tree(vpk)))
            except (OSError, ValueError, struct.error):
                pass
        if game:
            for sub in OFFICIAL_DIRS:
                p = os.path.join(game, sub, "pak01_dir.vpk")
                try:
                    self.srcs.append(("vpk", p, vpk_tree(p)))
                except (OSError, ValueError, struct.error):
                    pass

    @staticmethod
    def norm(path):
        return path.lower().replace("\\", "/").lstrip("/")

    def read(self, path, limit=None):
        path = self.norm(path)
        for kind, h, tree in self.srcs:
            if path in tree:
                try:
                    if kind == "zip":
                        b = h.read(tree[path])
                        return b if limit is None else b[:limit]
                    return vpk_read(h, tree[path], limit)
                except Exception:
                    return None
        return None

    def has(self, path):
        path = self.norm(path)
        return any(path in t for _k, _h, t in self.srcs)

    def models(self):
        out = set()
        for _k, _h, t in self.srcs:
            out.update(p for p in t if p.endswith(".mdl"))
        return out


NOT_PROPS = ("models/infected/", "models/survivors/", "models/v_models/", "models/w_models/", "models/weapons/",
             "models/editor/", "models/player/", "models/props_skybox/", "models/skybox/", "models/characters/",
             "models/extras/", "models/f18/", "models/missiles/")


FOLIAGE = re.compile(r"foliage|(?<!s)tree|bush|shrub|hedge|palm|vine|grass|ivy|leaves|leaf")


def linear_to_screen(rgb):
    return tuple(min(1.0, max(0.06, c) ** (1 / 2.2) * 1.15) for c in rgb)


def material_color(files, mat, cache):
    """average color of a material (from its texture's stored reflectivity), or None"""
    mat = GameFiles.norm(mat)
    if mat in cache:
        return cache[mat]
    rgb = None
    try:
        vmt = files.read("materials/%s.vmt" % mat)
        for _ in range(3):                                   # follow "patch" materials
            if not vmt:
                break
            toks = [a or b for a, b in re.findall(r'"([^"]*)"|([^\s"{}]+)', vmt.decode("latin-1"))]
            low = [t.lower() for t in toks]
            if "$basetexture" in low and low.index("$basetexture") + 1 < len(toks):
                tex = re.sub(r"\.vtf$", "", toks[low.index("$basetexture") + 1], flags=re.I)
                vtf = files.read("materials/%s.vtf" % GameFiles.norm(tex), limit=48)
                if vtf and vtf[:4] == b"VTF\0" and len(vtf) >= 44:
                    rgb = linear_to_screen(struct.unpack_from("<3f", vtf, 32))
                break
            if "include" in low and low.index("include") + 1 < len(toks):
                vmt = files.read(toks[low.index("include") + 1])
                continue
            break
    except Exception:
        rgb = None
    cache[mat] = rgb
    return rgb


class Mesh:
    """a model, simplified for drawing: verts, tris (i, j, k), one color per tri; bounds; collision"""
    __slots__ = ("verts", "tris", "cols", "mins", "maxs", "solid", "full_tris", "light", "leafy")

    def __init__(self, verts, tris, cols, mins, maxs, solid, full_tris):
        self.verts, self.tris, self.cols = verts, tris, cols
        self.mins, self.maxs, self.solid, self.full_tris = mins, maxs, solid, full_tris
        self.light = simplify(verts, tris, cols, 28)        # lighter version for when it's small on screen
        self.leafy = False


def _mdl_triangles(mdl, vtx, sg_size):
    """[(vvd vertex index, j, k, mesh material)] of LOD 0, first model of each body part"""
    numbp, bpi = struct.unpack_from("<ii", mdl, 232)
    vbpo = struct.unpack_from("<i", vtx, 32)[0]
    out = []
    for b in range(numbp):
        mb = bpi + b * 16
        nmodels, modelindex = struct.unpack_from("<i", mdl, mb + 4)[0], struct.unpack_from("<i", mdl, mb + 12)[0]
        vb = vbpo + b * 8
        _vnm, vmo = struct.unpack_from("<ii", vtx, vb)
        if nmodels < 1:
            continue
        mm = mb + modelindex
        nmesh, meshindex, _nv, vertexindex = struct.unpack_from("<iiii", mdl, mm + 72)
        vm = vb + vmo
        _nl, lodo = struct.unpack_from("<ii", vtx, vm)
        vl = vm + lodo
        vnmesh, meshoff = struct.unpack_from("<ii", vtx, vl)
        for k in range(min(nmesh, vnmesh)):
            ms = mm + meshindex + k * 116
            mat, _mi, _nvm, voff = struct.unpack_from("<iiii", mdl, ms)
            vme = vl + meshoff + k * 9
            nsg, sgo = struct.unpack_from("<ii", vtx, vme)
            base = vertexindex // 48 + voff
            for g in range(nsg):
                sg = vme + sgo + g * sg_size
                nv, vo, ni, io_ = struct.unpack_from("<iiii", vtx, sg)
                if nv < 0 or ni < 0 or ni % 3 or sg + io_ + ni * 2 > len(vtx) or sg + vo + nv * 9 > len(vtx):
                    raise ValueError("bad strip group")
                ids = [struct.unpack_from("<H", vtx, sg + vo + j * 9 + 4)[0] for j in range(nv)]
                idx = struct.unpack_from("<%dH" % ni, vtx, sg + io_)
                if ni and max(idx) >= nv:
                    raise ValueError("bad index")
                for t in range(0, ni, 3):
                    out.append((base + ids[idx[t]], base + ids[idx[t + 1]], base + ids[idx[t + 2]], mat))
    return out


def _cross(a, b, c):
    ux, uy, uz = b[0] - a[0], b[1] - a[1], b[2] - a[2]
    vx, vy, vz = c[0] - a[0], c[1] - a[1], c[2] - a[2]
    return uy * vz - uz * vy, uz * vx - ux * vz, ux * vy - uy * vx


def simplify(verts, tris, cols, limit):
    """vertex clustering: merge vertices on a grid until there are at most `limit` triangles"""
    if len(tris) <= limit:
        return verts, tris, cols
    xs, ys, zs = zip(*verts)
    lo = (min(xs), min(ys), min(zs))
    ext = (max(xs) - lo[0], max(ys) - lo[1], max(zs) - lo[2])
    g, tried = 12.0, {}
    for _ in range(6):
        gi = max(2, int(g))
        if gi in tried:
            break
        f = [gi / max(ext[a], 0.25 * gi) for a in range(3)]    # cells per unit; thin axes keep both sides
        keys = zip([int((x - lo[0]) * f[0]) for x in xs], [int((y - lo[1]) * f[1]) for y in ys],
                   [int((z - lo[2]) * f[2]) for z in zs])
        cid = {}
        remap = [cid.setdefault(k, len(cid)) for k in keys]
        seen, nt, nc = set(), [], []
        for (a, b, c), col in zip(tris, cols):
            a, b, c = remap[a], remap[b], remap[c]
            if a == b or b == c or a == c:
                continue
            k = (a, b, c) if a < b and a < c else ((b, c, a) if b < c else (c, a, b))
            if k not in seen:
                seen.add(k)
                nt.append(k)
                nc.append(col)
        tried[gi] = (remap, len(cid), nt, nc)
        if len(nt) <= limit:
            break
        g = gi * (limit / len(nt)) ** 0.5 * 0.9                 # triangles grow with the grid's area
    ok = [k for k in tried if len(tried[k][2]) <= limit]
    remap, n, nt, nc = tried[max(ok) if ok else min(tried)]
    sx, sy, sz, cnt = [0.0] * n, [0.0] * n, [0.0] * n, [0] * n
    for i, c in enumerate(remap):
        x, y, z = verts[i]
        sx[c] += x; sy[c] += y; sz[c] += z; cnt[c] += 1
    used = sorted({i for t in nt for i in t})
    pos = {o: k for k, o in enumerate(used)}
    return ([(sx[i] / cnt[i], sy[i] / cnt[i], sz[i] / cnt[i]) for i in used],
            [(pos[a], pos[b], pos[c]) for a, b, c in nt], nc)


def load_mesh(files, name, mat_cache, limit=160):
    """Mesh for a .mdl, or None when it can't be read"""
    name = GameFiles.norm(name)
    mdl = files.read(name)
    if not mdl or len(mdl) < 408 or mdl[:4] != b"IDST":
        return None
    stem = name[:-4]
    vvd = files.read(stem + ".vvd")
    vtx = files.read(stem + ".dx90.vtx") or files.read(stem + ".vtx") or files.read(stem + ".dx80.vtx")
    if not vvd or not vtx or vvd[:4] != b"IDSV":
        return None
    try:
        tris = None
        for sg_size in (25, 33):                             # older / newer strip group layout
            try:
                tris = _mdl_triangles(mdl, vtx, sg_size)
                if tris:
                    break
            except (ValueError, struct.error, IndexError):
                tris = None
        if not tris:
            return None
        nverts0 = struct.unpack_from("<i", vvd, 16)[0]
        nfix, fixstart, vstart = struct.unpack_from("<iii", vvd, 48)
        order = None
        if nfix:
            order = []
            for k in range(nfix):
                lod, src, num = struct.unpack_from("<iii", vvd, fixstart + k * 12)
                if lod >= 0:
                    order.extend(range(src, src + num))
        total = len(order) if order else nverts0
        # materials: skin family 0 -> texture -> first cd folder that has it
        numtex, texidx, numcd, cdidx = struct.unpack_from("<iiii", mdl, 204)
        nskinref, _nfam, skinidx = struct.unpack_from("<iii", mdl, 220)
        cds = [cstr(mdl, struct.unpack_from("<i", mdl, cdidx + 4 * i)[0]) for i in range(numcd)]
        texcols = []
        for t in range(numtex):
            to = texidx + t * 64
            tname = cstr(mdl, to + struct.unpack_from("<i", mdl, to)[0])
            col = None
            for cd in cds:
                col = material_color(files, cd + tname, mat_cache)
                if col:
                    break
            texcols.append(col)
        grey = (0.62, 0.6, 0.57)
        verts, vmap, vnorm, ts, cs = [], {}, [], [], []
        for a, b, c, mat in tris:
            if max(a, b, c) >= total:
                return None
            tri = []
            for i in (a, b, c):
                if i not in vmap:
                    src = order[i] if order else i
                    vmap[i] = len(verts)
                    verts.append(struct.unpack_from("<3f", vvd, vstart + src * 48 + 16))
                    vnorm.append(struct.unpack_from("<3f", vvd, vstart + src * 48 + 28))
                tri.append(vmap[i])
            ts.append(tuple(tri))
            ref = struct.unpack_from("<h", mdl, skinidx + 2 * mat)[0] if 0 <= mat < nskinref else mat
            cs.append((texcols[ref] if 0 <= ref < len(texcols) else None) or grey)
        # which winding faces outward: compare with the stored vertex normals
        score = 0.0
        for a, b, c in ts:
            n = _cross(verts[a], verts[b], verts[c])
            m = [vnorm[a][k] + vnorm[b][k] + vnorm[c][k] for k in range(3)]
            score += n[0] * m[0] + n[1] * m[1] + n[2] * m[2]
        if score < 0:
            ts = [(a, c, b) for a, b, c in ts]
        leafy = bool(FOLIAGE.search(name.rsplit("/", 2)[-2] + "/" + name.rsplit("/", 1)[-1]))
        if leafy:
            # leaf textures are mostly see-through, so their stored color is near black: use a leafy green
            cs = [(0.36, 0.46, 0.25) if max(c) - min(c) < 0.06 else c for c in cs]
        full = len(ts)
        mins = tuple(min(v[k] for v in verts) for k in range(3))      # exact size, before simplifying
        maxs = tuple(max(v[k] for v in verts) for k in range(3))
        verts, ts, cs = simplify(verts, ts, cs, min(limit, 64) if leafy else limit)   # leaves need little detail
        mesh = Mesh(verts, ts, cs, mins, maxs, files.has(stem + ".phy"), full)
        mesh.leafy = leafy
        return mesh
    except (struct.error, IndexError, ValueError):
        return None


def cstr(b, o):
    e = b.find(b"\0", o)
    return b[o:e if e >= 0 else len(b)].decode("latin-1")


def angle_matrix(pitch, yaw, roll):
    """Source angles (degrees) -> the model's forward, left and up axes in the world"""
    p, y, r = math.radians(pitch), math.radians(yaw), math.radians(roll)
    sp, cp, sy, cy, sr, cr = math.sin(p), math.cos(p), math.sin(y), math.cos(y), math.sin(r), math.cos(r)
    return ((cp * cy, cp * sy, -sp),
            (sr * sp * cy - cr * sy, sr * sp * sy + cr * cy, sr * cp),
            (cr * sp * cy + sr * sy, cr * sp * sy - sr * cy, cr * cp))


def parse_vec(s, default=(0.0, 0.0, 0.0)):
    try:
        v = tuple(float(x) for x in s.split()[:3])
        return v if len(v) == 3 else default
    except ValueError:
        return default


class Placed:
    """a model placed in the world: world-space vertices, triangle normals and bounds"""
    __slots__ = ("mesh", "full", "lite", "mins", "maxs", "box", "boxn", "boxc", "corners")

    def __init__(self, mesh, origin, angles, scale=1.0):
        f, l, u = angle_matrix(*angles)
        ox, oy, oz = origin
        self.mesh = mesh

        def world(verts, tris, cols):
            wv = [(ox + scale * (x * f[0] + y * l[0] + z * u[0]),
                   oy + scale * (x * f[1] + y * l[1] + z * u[1]),
                   oz + scale * (x * f[2] + y * l[2] + z * u[2])) for x, y, z in verts]
            out = []
            for (a, b, c), col in zip(tris, cols):
                n = _cross(wv[a], wv[b], wv[c])
                ln = (n[0] * n[0] + n[1] * n[1] + n[2] * n[2]) ** 0.5 or 1.0
                out.append(((wv[a], wv[b], wv[c]), (n[0] / ln, n[1] / ln, n[2] / ln), col))
            return out, wv
        self.full, _wv = world(mesh.verts, mesh.tris, mesh.cols)
        self.lite = world(*mesh.light)[0] if len(mesh.light[1]) < len(mesh.tris) else self.full
        # a plain box in the model's average color, for when it's small on screen
        cols = mesh.cols or [(0.6, 0.6, 0.6)]
        self.boxc = tuple(sum(c[k] for c in cols) / len(cols) for k in range(3))
        lo, hi = mesh.mins, mesh.maxs
        corner = lambda i, j, k: (ox + scale * ((lo, hi)[i][0] * f[0] + (lo, hi)[j][1] * l[0] + (lo, hi)[k][2] * u[0]),
                                  oy + scale * ((lo, hi)[i][0] * f[1] + (lo, hi)[j][1] * l[1] + (lo, hi)[k][2] * u[1]),
                                  oz + scale * ((lo, hi)[i][0] * f[2] + (lo, hi)[j][1] * l[2] + (lo, hi)[k][2] * u[2]))
        quads = [((0, 0, 1), (1, 0, 1), (1, 1, 1), (0, 1, 1)), ((0, 0, 0), (0, 1, 0), (1, 1, 0), (1, 0, 0)),
                 ((1, 0, 0), (1, 1, 0), (1, 1, 1), (1, 0, 1)), ((0, 0, 0), (0, 0, 1), (0, 1, 1), (0, 1, 0)),
                 ((0, 1, 0), (0, 1, 1), (1, 1, 1), (1, 1, 0)), ((0, 0, 0), (1, 0, 0), (1, 0, 1), (0, 0, 1))]
        self.box = [[corner(*c) for c in q] for q in quads]
        bn = []
        for q in self.box:
            n = _cross(q[0], q[1], q[2])
            ln = (n[0] * n[0] + n[1] * n[1] + n[2] * n[2]) ** 0.5 or 1.0
            bn.append((n[0] / ln, n[1] / ln, n[2] / ln))
        self.boxn = bn
        self.box = [(q, n, self.boxc) for q, n in zip(self.box, bn)]
        self.corners = [corner(i, j, k) for i in (0, 1) for j in (0, 1) for k in (0, 1)]
        self.mins = tuple(min(v[k] for v in self.corners) for k in range(3))
        self.maxs = tuple(max(v[k] for v in self.corners) for k in range(3))


def convex_hull(pts):
    """outline around 2D points (monotone chain)"""
    pts = sorted(set(pts))
    if len(pts) < 3:
        return pts

    def half(seq):
        out = []
        for p in seq:
            while len(out) >= 2 and ((out[-1][0] - out[-2][0]) * (p[1] - out[-2][1]) -
                                     (out[-1][1] - out[-2][1]) * (p[0] - out[-2][0])) <= 0:
                out.pop()
            out.append(p)
        return out
    lo, hi = half(pts), half(reversed(pts))
    return lo[:-1] + hi[:-1]


def static_props(d):
    """[(model, origin, angles, solid)] for the props compiled into the map (prop_static)"""
    try:
        gl = lump_bytes(d, 35)
        n, = struct.unpack_from("<i", gl, 0)
        for k in range(n):
            gid, fl, _ver, ofs, ln = struct.unpack_from("<iHHii", gl, 4 + k * 16)
            if gid != 0x73707270:                            # 'sprp'
                continue
            raw = d[ofs:ofs + ln]
            if raw[:4] == b"LZMA":
                size, _packed = struct.unpack_from("<II", raw, 4)
                props = raw[12:17]
                lc, rest = props[0] % 9, props[0] // 9
                filt = {"id": lzma.FILTER_LZMA1, "dict_size": struct.unpack_from("<I", props, 1)[0],
                        "lc": lc, "lp": rest % 5, "pb": rest // 5}
                raw = lzma.LZMADecompressor(lzma.FORMAT_RAW, filters=[filt]).decompress(raw[17:], size)[:size]
            p = 0
            nd, = struct.unpack_from("<i", raw, p)
            p += 4
            names = [cstr(raw[p + i * 128:p + i * 128 + 128] + b"\0", 0) for i in range(nd)]
            p += nd * 128
            nl, = struct.unpack_from("<i", raw, p)
            p += 4 + nl * 2
            npr, = struct.unpack_from("<i", raw, p)
            p += 4
            if npr <= 0:
                return []
            size = (len(raw) - p) // npr
            out = []
            for i in range(npr):
                o = p + i * size
                ox, oy, oz, ax, ay, az, pt = struct.unpack_from("<6fH", raw, o)
                solid = raw[o + 30]
                if pt < nd:
                    out.append((names[pt], (ox, oy, oz), (ax, ay, az), solid))
            return out
    except (struct.error, IndexError, lzma.LZMAError, ValueError):
        pass
    return []


# ---------------------------------------------------------------- problem check and fixes

OFFICIAL_DIRS =["update", "left4dead2_dlc3", "left4dead2_dlc2", "left4dead2_dlc1", "left4dead2"]
LMP_NAME = re.compile(r"^(.+)_([a-z])_(\d+)\.lmp$", re.I)


def find_game():
    """the Left 4 Dead 2 folder, from Steam's settings, or None"""
    roots = []
    try:
        import winreg
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam")
        steam = winreg.QueryValueEx(key, "SteamPath")[0]
        roots.append(steam)
        vdf = os.path.join(steam, "steamapps", "libraryfolders.vdf")
        if os.path.exists(vdf):
            text = open(vdf, encoding="utf-8", errors="replace").read()
            roots += [p.replace("\\\\", "\\") for p in re.findall(r'"path"\s+"([^"]+)"', text)]
    except Exception:
        pass
    roots += [r"C:\Program Files (x86)\Steam", r"C:\Program Files\Steam", r"C:\Steam"]
    for r in roots:
        p = os.path.normpath(os.path.join(r, "steamapps", "common", "Left 4 Dead 2"))
        if os.path.isdir(os.path.join(p, "left4dead2")):
            return p
    return None


def game_running():
    try:
        import subprocess
        out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq left4dead2.exe"], capture_output=True, text=True,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
        return "left4dead2.exe" in out.lower()
    except Exception:
        return False


def read_addonlist(game):
    """{addon file name (lower, forward slashes): enabled}"""
    p = os.path.join(game, "left4dead2", "addonlist.txt")
    out = {}
    if os.path.exists(p):
        text = open(p, encoding="utf-8", errors="replace").read()
        for name, val in re.findall(r'"([^"]+\.vpk)"\s+"(\d)"', text, re.I):
            out[name.replace("\\", "/").lower()] = val == "1"
    return out


def write_vpk(path, files):
    """single-file VPK v1 from {'maps/x.lmp': bytes}"""
    import zlib
    groups = {}
    for name in sorted(files):
        folder, base = os.path.split(name.replace("\\", "/"))
        stem, ext = base.rsplit(".", 1) if "." in base else (base, "")
        groups.setdefault(ext, {}).setdefault(folder or " ", []).append((stem, files[name]))
    tree, blob = bytearray(), bytearray()
    for ext, folders in groups.items():
        tree += ext.encode("latin-1") + b"\0"
        for folder, items in folders.items():
            tree += folder.encode("latin-1") + b"\0"
            for stem, data in items:
                tree += stem.encode("latin-1") + b"\0"
                tree += struct.pack("<IHHIIH", zlib.crc32(data) & 0xFFFFFFFF, 0, 0x7FFF, len(blob), len(data), 0xFFFF)
                blob += data
            tree += b"\0"
        tree += b"\0"
    tree += b"\0"
    tmp = path + ".tmp"
    with open(tmp, "wb") as f:
        f.write(struct.pack("<III", 0x55AA1234, 1, len(tree)) + bytes(tree) + bytes(blob))
    os.replace(tmp, path)


def vpk_files(path):
    """every file of a single-file .vpk as {name: bytes}; None if it's split into parts"""
    tree = vpk_tree(path)
    if any(e[0] != 0x7FFF for e in tree.values()):
        return None
    return {k: vpk_read(path, e) for k, e in tree.items()}


def bsp_lump_from(src, lump_id):
    """a lump of a map given as ('path', file) or ('vpk', vpk, entry)"""
    if src[0] == "path":
        with open(src[1], "rb") as f:
            head = f.read(8 + 64 * 16 + 4)
            if head[:4] != b"VBSP":
                return None
            _v, ofs, ln, cc = struct.unpack_from("<iii4s", head, 8 + lump_id * 16)
            if cc != b"\0\0\0\0":
                return lump_bytes(open(src[1], "rb").read(), lump_id)
            f.seek(ofs)
            return f.read(ln)
    d = vpk_read(src[1], src[2])
    return lump_bytes(d, lump_id) if d[:4] == b"VBSP" else None


class Addon:
    def __init__(self, name, path, kind, enabled, workshop):
        self.name, self.path, self.kind, self.enabled, self.workshop = name, path, kind, enabled, workshop
        self.tree = {}

    def writable(self):
        return not self.workshop


class Patch:
    def __init__(self, label, mapname, prefix, n, kind, addon, inner, read):
        self.label, self.map, self.prefix, self.n = label, mapname, prefix, n
        self.kind, self.addon, self.inner, self._read = kind, addon, inner, read
        self.raw, self.lid, self.body, self.ok = None, None, None, False

    def active(self):
        return self.kind in ("valve", "loose") or (self.kind == "addon" and self.addon.enabled is not False)

    def load(self):
        if self.raw is None:
            self.raw = self._read()
            if len(self.raw) >= 20:
                o, self.lid, _v, ln, _r = lmp_header(self.raw)
                self.body = self.raw[o:o + ln] if o == 20 else self.raw[20:]
                self.ok = o == 20 and o + ln == len(self.raw)
        return self.raw


class Scan:
    def __init__(self, game, progress=None):
        self.game = game
        self.findings = []           # (level, title, detail, fix) - fix = (button text, description, function) or None
        self.maps, self.addons, self.patches, self.models = {}, [], [], set()
        self._lumps = {}
        self.progress = progress or (lambda m: None)

    def say(self, lvl, title, detail="", fix=None):
        self.findings.append((lvl, title, detail, fix))

    # ---- what's there
    def gather(self):
        g = self.game
        for d in OFFICIAL_DIRS:
            md = os.path.join(g, d, "maps")
            for f in sorted(os.listdir(md)) if os.path.isdir(md) else []:
                full = os.path.join(md, f)
                if f.lower().endswith(".bsp"):
                    self.maps.setdefault(f[:-4].lower(), ("path", full))
                elif f.lower().endswith(".lmp"):
                    m = LMP_NAME.match(f)
                    kind = "valve" if d == "update" else "loose"
                    if not m:
                        self.say("warn", "Badly named patch: %s/maps/%s" % (d, f),
                                 "Patch names must look like <map>_l_<number>.lmp, or the game never loads them.")
                        continue
                    self.patches.append(Patch("%s/maps/%s" % (d, f), m.group(1).lower(), m.group(2).lower(),
                                              int(m.group(3)), kind, None, full,
                                              lambda full=full: open(full, "rb").read()))
            pv = os.path.join(g, d, "pak01_dir.vpk")
            if os.path.exists(pv):
                try:
                    self.models |= {k for k in vpk_tree(pv) if k.startswith("models/")}
                except (ValueError, OSError, struct.error):
                    pass
            mdir = os.path.join(g, d, "models")
            if os.path.isdir(mdir):
                for root, _dirs, files in os.walk(mdir):
                    rel = os.path.relpath(root, os.path.join(g, d)).replace("\\", "/").lower()
                    self.models |= {rel + "/" + f.lower() for f in files}
        listed = read_addonlist(g)
        ad = os.path.join(g, "left4dead2", "addons")
        entries = []
        if os.path.isdir(ad):
            for f in sorted(os.listdir(ad)):
                p = os.path.join(ad, f)
                if f.lower().endswith(".vpk"):
                    entries.append((f, p, False))
                elif os.path.isdir(p) and f.lower() != "workshop":
                    self.addons.append(Addon(f, p, "folder", None, False))
            wd = os.path.join(ad, "workshop")
            if os.path.isdir(wd):
                entries += [("workshop/" + f, os.path.join(wd, f), True) for f in sorted(os.listdir(wd))
                            if f.lower().endswith(".vpk")]
        for k, (name, p, ws) in enumerate(entries):
            self.progress("Reading addons... %d/%d" % (k + 1, len(entries)))
            a = Addon(name, p, "vpk", listed.get(name.replace("\\", "/").lower()), ws)
            try:
                a.tree = vpk_tree(p)
            except (ValueError, OSError, struct.error, IndexError) as e:
                self.say("error", "Broken addon file: %s" % name,
                         "It can't be read (%s). Re-download it, or pack it again." % e)
                continue
            self.addons.append(a)
            for key in a.tree:
                if key.startswith("models/"):
                    self.models.add(key)
                if key.startswith("maps/") and key.endswith(".bsp"):
                    mname = key[5:-4]
                    if mname in self.maps and self.maps[mname][0] == "path":
                        a.replaces_map = getattr(a, "replaces_map", []) + [mname]
                    self.maps.setdefault(mname, ("vpk", p, a.tree[key]))
        for a in self.addons:
            if a.kind == "folder":
                md = os.path.join(a.path, "maps")
                for f in sorted(os.listdir(md)) if os.path.isdir(md) else []:
                    m = LMP_NAME.match(f)
                    if m:
                        full = os.path.join(md, f)
                        self.patches.append(Patch("addons/%s/maps/%s" % (a.name, f), m.group(1).lower(),
                                                  m.group(2).lower(), int(m.group(3)), "folder", a, full,
                                                  lambda full=full: open(full, "rb").read()))
                continue
            for key in sorted(a.tree):
                if not key.endswith(".lmp"):
                    continue
                f = key.split("/")[-1]
                m = LMP_NAME.match(f)
                if not key.startswith("maps/") or key.count("/") != 1:
                    self.say("error", "Patch in the wrong folder: %s : %s" % (a.name, key),
                             "Patches must be directly in the addon's maps/ folder, or the game never finds them.",
                             self.fix_move(a, key, "maps/" + f) if a.writable() else None)
                    continue
                if not m:
                    self.say("warn", "Badly named patch in %s: %s" % (a.name, key),
                             "Patch names must look like <map>_l_<number>.lmp, or the game never loads them.")
                    continue
                self.patches.append(Patch("%s : %s" % (a.name, key), m.group(1).lower(), m.group(2).lower(),
                                          int(m.group(3)), "addon", a, key,
                                          lambda p=a.path, e=a.tree[key]: vpk_read(p, e)))

    def base_entities(self, mname, prefix, below=None):
        """entity list the game uses for a map before patches numbered >= `below` (Valve's patch or the map)"""
        best = None
        for p in self.patches:
            if p.map == mname and p.prefix == prefix and p.kind == "valve" and (below is None or p.n < below):
                p.load()
                if p.lid == LUMP_ENTITIES and (best is None or p.n > best.n):
                    best = p
        if best:
            return parse_entities(best.body)[1], best
        src = self.maps.get(mname)
        raw = bsp_lump_from(src, LUMP_ENTITIES) if src else None
        return (parse_entities(raw)[1] if raw else None), None

    def base_brushes(self, mname, prefix, below=None):
        bsp = self.map_lump(mname, LUMP_BRUSHES)
        best = None
        for p in self.patches:
            if p.map == mname and p.prefix == prefix and p.kind == "valve" and (below is None or p.n < below):
                p.load()
                if p.lid == LUMP_BRUSHES and (best is None or p.n > best.n) and bsp is not None \
                        and len(p.body) == len(bsp):
                    best = p
        return (best.body, best) if best else (bsp, None)

    def map_lump(self, mname, lid):
        key = (mname, lid)
        if key not in self._lumps:
            src = self.maps.get(mname)
            try:
                self._lumps[key] = bsp_lump_from(src, lid) if src else None
            except (OSError, struct.error, ValueError, lzma.LZMAError):
                self._lumps[key] = None
        return self._lumps[key]

    # ---- the checks
    def run(self):
        import difflib
        g = self.game
        if not g or not os.path.isdir(os.path.join(g, "left4dead2")):
            self.say("error", "Left 4 Dead 2 folder not found",
                     "Click \"Pick game folder...\" and choose the folder that contains left4dead2.exe "
                     "(usually ...\\steamapps\\common\\Left 4 Dead 2).")
            return self
        self.say("ok", "Left 4 Dead 2 folder: %s" % os.path.normpath(g))
        if sys.version_info < (3, 8):
            self.say("warn", "Python %d.%d is old" % sys.version_info[:2], "Python 3.8 or newer is recommended.")
        self.gather()
        vpks = [a for a in self.addons if a.kind == "vpk"]
        self.say("ok", "%d addon(s) found (%d from the Workshop)" % (len(vpks), sum(a.workshop for a in vpks)))
        if game_running():
            self.say("info", "The game is running", "Fixes are disabled until you close it, and changes only show "
                                                    "after the map loads again.")
        for a in self.addons:
            if a.kind == "folder":
                has = any(p.addon is a for p in self.patches)
                packed = os.path.exists(a.path.rstrip("\\/") + ".vpk")
                if has and packed:
                    self.say("info", "addons/%s is also packed as %s.vpk" % (a.name, a.name),
                             "The game uses the .vpk. You can delete the folder (or keep it to edit and pack again).")
                elif has:
                    self.say("warn", "Unpacked addon folder: addons/%s" % a.name,
                             "Addons load from .vpk files, so these patches don't load yet.",
                             self.fix_pack(a))
                continue
            mine = [p for p in self.patches if p.addon is a]
            for mname in getattr(a, "replaces_map", []):
                self.say("info", "%s replaces the whole map %s" % (a.name, mname),
                         "Patches for %s must be made from this addon's version of the map, not the official one."
                         % mname)
            if not mine:
                continue
            if "addoninfo.txt" not in a.tree:
                self.say("info", "%s has no addoninfo.txt" % a.name,
                         "It still loads, but the Add-ons menu shows no title.",
                         self.fix_addoninfo(a) if a.writable() else None)
            if a.enabled is False:
                self.say("error", "Addon turned off: %s" % a.name, "Its patches don't load.", self.fix_enable(a))
            elif a.enabled is None:
                self.say("info", "%s isn't in the Add-ons list yet" % a.name,
                         "The game adds new addons the next time it starts. Check it's on in the Add-ons menu.",
                         self.fix_enable(a))
        mine = [p for p in self.patches if p.kind != "valve"]
        if not mine:
            self.say("info", "No patches of your own found",
                     "No .lmp files in addons or loose map folders. If you made one, check it's in the addon's maps/ "
                     "folder.")
        for k, p in enumerate(mine):
            self.progress("Checking patches... %d/%d" % (k + 1, len(mine)))
            try:
                self.check_patch(p, difflib)
            except Exception as e:                       # one odd file must not stop the whole check
                self.say("warn", "Couldn't fully check %s" % p.label, "%s: %s" % (type(e).__name__, e))
        self.check_chains()
        if not any(f[0] in ("error", "warn") for f in self.findings):
            self.say("ok", "No problems found")
        return self

    def check_patch(self, p, difflib):
        raw = p.load()
        if p.kind == "loose":
            self.say("info", "Loose patch outside an addon: %s" % p.label,
                     "The game loads it for everyone on this PC. Fine for testing; ship it in an addon.")
        if len(raw) < 20:
            self.say("error", "Broken patch: %s" % p.label, "Too short to be a .lmp. Save it again from the tool.")
            return
        if p.map not in self.maps:
            close = difflib.get_close_matches(p.map, list(self.maps), n=1, cutoff=0.75)
            self.say("error", "No map called \"%s\": %s" % (p.map, p.label),
                     ("Did you mean %s? " % close[0] if close else "") +
                     "The file name must start with the exact map name, or the game never loads it.",
                     self.fix_rename_map(p, close[0]) if close and self.can_write(p) else None)
            return
        if p.lid not in (LUMP_ENTITIES, LUMP_BRUSHES):
            self.say("info", "%s patches lump %d" % (p.label, p.lid), "Not walls or entities, so not checked further.")
            return
        repaired = None
        if not p.ok:
            o, _lid, _v, ln, _r = lmp_header(raw)
            repaired = raw[20:]
            if p.lid == LUMP_BRUSHES:
                bsp = self.map_lump(p.map, LUMP_BRUSHES)
                fixable = bsp is not None and len(repaired) == len(bsp)
            else:
                try:
                    parse_entities(repaired)
                    fixable = True
                except ValueError:
                    fixable = False
            self.say("error", "Broken header: %s" % p.label,
                     "The file's header says offset %d, length %d, but the file has %d bytes. The game may skip it."
                     % (o, ln, len(raw)), self.fix_header(p) if fixable and self.can_write(p) else None)
            if not fixable:
                return
            p.body = repaired
        if p.lid == LUMP_BRUSHES:
            self.check_walls(p)
        else:
            self.check_entities(p)

    def check_walls(self, p):
        bsp = self.map_lump(p.map, LUMP_BRUSHES)
        if bsp is None:
            return
        body = p.body
        if len(body) != len(bsp):
            self.say("error", "Walls patch doesn't fit the map: %s" % p.label,
                     "It has %d brushes, the map has %d: it was made for another map or an older version of it. Open "
                     "the map in the tool and save the walls again." % (len(body) // 12, len(bsp) // 12))
            return
        c = lambda b, i: struct.unpack_from("<i", b, i * 12 + 8)[0]
        n = len(body) // 12
        opened = [i for i in range(n) if c(bsp, i) & PLAYERCLIP and not c(bsp, i) & SOLID and not c(body, i) & PLAYERCLIP]
        other = [i for i in range(n) if body[i * 12:i * 12 + 8] != bsp[i * 12:i * 12 + 8]
                 or (c(body, i) ^ c(bsp, i)) & ~PLAYERCLIP]
        valve_body, valve = self.base_brushes(p.map, p.prefix, below=p.n)
        if valve is not None:
            vdiff = [i for i in range(n) if valve_body[i * 12:i * 12 + 12] != bsp[i * 12:i * 12 + 12]]
            missing = [i for i in vdiff if body[i * 12:i * 12 + 12] == bsp[i * 12:i * 12 + 12]
                       and not (c(bsp, i) & PLAYERCLIP and not c(body, i) & PLAYERCLIP)]
            if missing:
                self.say("warn", "%s drops Valve's own fix" % p.label,
                         "Valve's %s changes %d brushes on this map; this patch replaces it without them. Rebuild it "
                         "on top of Valve's patch." % (valve.label.split("/")[-1], len(missing)),
                         self.fix_rebase_walls(p, valve) if self.can_write(p) else None)
        if not opened and not other:
            self.say("warn", "%s changes nothing" % p.label, "It removes no walls. Save it again from the tool.")
        elif other and valve is None:
            self.say("info", "%s changes %d brushes in other ways than removing walls" % (p.label, len(other)),
                     "Not made by this tool. Usually fine.")
        self.say("ok", "%s: walls patch, %d walls removed" % (p.label, len(opened)))

    def check_entities(self, p):
        try:
            _pre, ents, _nl = parse_entities(p.body)
        except ValueError as e:
            self.say("error", "Broken entities in %s" % p.label,
                     "The entity text can't be read (%s). The map may not load. Save it again from the tool." % e)
            return
        fixes = []
        if not ents or ents[0].get("classname").lower() != "worldspawn":
            has = any(e.get("classname").lower() == "worldspawn" for e in ents)
            self.say("error", "%s: worldspawn isn't the first entity" % p.label,
                     "The map breaks without it in first place." + ("" if has else " It's missing entirely: save "
                                                                    "the patch again from the tool."),
                     self.fix_entities(p, "worldspawn") if has and self.can_write(p) else None)
        nocls = [e for e in ents if not e.get("classname")]
        if nocls:
            self.say("warn", "%s: %d entities have no classname" % (p.label, len(nocls)),
                     "The game skips them.", self.fix_entities(p, "noclass") if self.can_write(p) else None)
        base, valve = self.base_entities(p.map, p.prefix, below=p.n)
        if base is not None:
            deleted, modified, added = entity_changes(base, ents)
            if valve is not None:
                bsp_raw = self.map_lump(p.map, LUMP_ENTITIES)
                if bsp_raw:
                    bsp_ents = parse_entities(bsp_raw)[1]
                    vd, vm, va = entity_changes(bsp_ents, base)
                    have = keyed(ents)
                    bk = keyed(bsp_ents)
                    vm_here = [k for k in vm if k in have]          # ones you deleted yourself don't count
                    kept = sum(1 for e in va if e.ident() in have) + sum(1 for k in vd if k not in have) +                         sum(1 for k in vm_here if ents[have[k][0]].pairs != bsp_ents[bk[k][0]].pairs)
                    total = len(va) + len(vd) + len(vm_here)
                    if total >= 5 and kept < total * 0.3:
                        self.say("warn", "%s drops Valve's own fixes" % p.label,
                                 "Valve's %s changes %d entities on this map; this patch has only %d of those "
                                 "changes, so it was likely built on the map without them (older tool or another "
                                 "editor). Rebuild it on top of Valve's patch, keeping your changes."
                                 % (valve.label.split("/")[-1], total, kept),
                                 self.fix_rebase_entities(p, valve) if self.can_write(p) else None)
            names = [base[keyed(base)[k][0]].get("targetname") for k in deleted]
            refs = broken_references(ents, names)
            if refs:
                self.say("info", "%s: %d output(s) point at deleted entities" % (p.label, len(refs)),
                         "For example %s -> %s. The game ignores them; just check it's intended."
                         % (refs[0][0].get("targetname") or refs[0][0].get("classname"), refs[0][2]))
            changed = list(modified.values()) + list(added)
            if any(e.get("classname").lower() in ("func_simpleladder", "func_ladder") for e in added):
                navs = {"maps/%s.nav" % p.map}
                has_nav = p.addon is not None and p.addon.kind == "vpk" and any(k in p.addon.tree for k in navs)
                if not has_nav:
                    self.say("info", "%s adds ladders but ships no .nav" % p.label,
                             "Players can climb them; bots won't until the nav mesh has a ladder there: in game "
                             "nav_edit 1, nav_build_ladder, nav_save, then ship maps/%s.nav in the addon." % p.map)
            missing_models = sorted({e.get("model").lower().replace("\\", "/") for e in changed
                                     if e.get("model").lower().endswith(".mdl")
                                     and e.get("model").lower().replace("\\", "/") not in self.models})
            if missing_models and self.models:
                self.say("warn", "%s uses %d model(s) the game doesn't have" % (p.label, len(missing_models)),
                         "%s. Those props show as errors or nothing. Copy a model path from a prop already in the "
                         "map." % ", ".join(missing_models[:3]))
            flipped = []
            for e in changed:
                try:
                    a = [float(v) for v in e.get("mins").split()]
                    b = [float(v) for v in e.get("maxs").split()]
                    if len(a) == 3 and len(b) == 3 and any(a[i] > b[i] for i in range(3)):
                        flipped.append(e)
                except ValueError:
                    continue
            if flipped:
                self.say("warn", "%s: %d blocker(s) with mins bigger than maxs" % (p.label, len(flipped)),
                         "Each of mins' numbers must be smaller than maxs', or the box doesn't block. Fix the values "
                         "in Entities mode.")
            if not (deleted or modified or added):
                self.say("warn", "%s changes nothing" % p.label,
                         "It's the same as the map's own entities. Save it again after making changes.")
            self.say("ok", "%s: entities patch, %d deleted, %d changed, %d added"
                     % (p.label, len(deleted), len(modified), len(added)))
        else:
            self.say("ok", "%s: entities patch, %d entities" % (p.label, len(ents)))

    def check_chains(self):
        chains = {}
        for p in self.patches:
            if p.active() and p.map in self.maps:          # a mistyped map name is reported on its own
                chains.setdefault((p.map, p.prefix), []).append(p)
        for (mname, prefix), items in sorted(chains.items()):
            by_n = {}
            for p in items:
                by_n.setdefault(p.n, []).append(p)
            for n, same in sorted(by_n.items()):
                if len(same) > 1:
                    movable = [p for p in same if p.kind != "valve" and self.can_write(p)]
                    self.say("error", "%s_%s_%d.lmp exists %d times" % (mname, prefix, n, len(same)),
                             "Only one of them loads: %s." % "; ".join(p.label for p in same)
                             + ("" if movable else self.why_not(same[-1])),
                             self.fix_renumber(movable[-1]) if movable else None)
            gap = 0
            while gap in by_n:
                gap += 1
            for n in sorted(by_n):
                if n > gap:
                    for p in by_n[n]:
                        self.say("error", "Never loads: %s" % p.label,
                                 "The game reads %s_%s_0, _1, _2... and stops at the first missing number. "
                                 "_%s_%d is missing, so this one is never reached." % (mname, prefix, prefix, gap)
                                 + ("" if self.can_write(p) else self.why_not(p)),
                                 self.fix_renumber(p) if p.kind != "valve" and self.can_write(p) else None)
            loaded = [p for n in sorted(by_n) if n < gap for p in by_n[n]]
            for p in loaded:
                p.load()
            for lid in (LUMP_ENTITIES, LUMP_BRUSHES):
                ps = [p for p in loaded if p.lid == lid]
                if len(ps) < 2:
                    continue
                top = max(ps, key=lambda p: p.n)
                for p in ps:
                    if p is top or p.kind == "valve" or p.n == top.n:     # same number = the duplicate case
                        continue
                    what = "walls" if lid == LUMP_BRUSHES else "entities"
                    can = self.can_write(top) and self.can_write(p)
                    self.say("warn", "%s is replaced by %s" % (p.label, top.label),
                             "Both change the %s of %s and only the higher number counts, so the first one's changes "
                             "are lost." % (what, mname), self.fix_merge(p, top) if can else None)

    # ---- fixes
    def why_not(self, p):
        """extra advice when a patch can't be fixed here"""
        if p.kind == "addon" and p.addon.workshop:
            return " It's a Workshop addon, so it isn't changed here (Steam would undo it): tell its author, or turn "                    "it off in the Add-ons menu."
        if p.kind == "addon" and not vpk_is_single(p.addon.path):
            return " The addon is split into several .vpk parts, so fix it by hand."
        return ""

    def can_write(self, p):
        return p.kind in ("loose", "folder") or (p.kind == "addon" and p.addon.writable() and
                                                 vpk_is_single(p.addon.path))

    def fix_enable(self, a):
        def do(fx):
            fx.set_addon(a.name, True)
            return "Turned on %s in the Add-ons list." % a.name
        return ("Turn it on", "Set %s to on in left4dead2/addonlist.txt." % a.name, do)

    def fix_addoninfo(self, a):
        def do(fx):
            title = os.path.splitext(os.path.basename(a.name))[0]
            info = ('"AddonInfo"\n{\n    addontitle      "%s"\n    addonversion    1\n    addonContent_Map 0\n}\n'
                    % title).encode()
            fx.edit_vpk(a.path, lambda files: files.__setitem__("addoninfo.txt", info))
            return "Added addoninfo.txt to %s." % a.name
        return ("Add it", "Add a minimal addoninfo.txt inside %s." % a.name, do)

    def fix_pack(self, a):
        def do(fx):
            out = a.path.rstrip("\\/") + ".vpk"
            if os.path.exists(out):
                raise RuntimeError("%s already exists. Remove or rename it first." % os.path.basename(out))
            files = {}
            for root, _d, fs in os.walk(a.path):
                for f in fs:
                    full = os.path.join(root, f)
                    files[os.path.relpath(full, a.path).replace("\\", "/").lower()] = open(full, "rb").read()
            write_vpk(out, files)
            fx.created.append(out)
            fx.set_addon(os.path.basename(out), True)
            return ("Packed addons/%s into %s (%d files) and turned it on. The folder itself was left as it is; "
                    "you can delete it." % (a.name, os.path.basename(out), len(files)))
        return ("Pack it", "Create addons/%s.vpk from the folder and turn it on." % a.name, do)

    def _rename(self, fx, p, new_name):
        if p.kind == "addon":
            def edit(files):
                if new_name in files:
                    raise RuntimeError("%s already exists in the addon." % new_name)
                files[new_name] = files.pop(p.inner)
            fx.edit_vpk(p.addon.path, edit)
        else:
            dst = os.path.join(os.path.dirname(p.inner), new_name.split("/")[-1])
            if os.path.exists(dst):
                raise RuntimeError("%s already exists." % dst)
            fx.backup(p.inner)
            os.replace(p.inner, dst)

    def free_number(self, mname, prefix, lid=None, skip=None):
        used = {q.n for q in self.patches if q.map == mname and q.prefix == prefix and q.active() and q is not skip}
        n = 0
        while n in used:
            n += 1
        return n

    def fix_move(self, a, key, new_key):
        def do(fx):
            fx.edit_vpk(a.path, lambda files: files.__setitem__(new_key, files.pop(key)))
            return "Moved %s to %s inside %s." % (key, new_key, a.name)
        return ("Move it", "Move %s to %s inside %s." % (key, new_key, a.name), do)

    def fix_renumber(self, p):
        target = self.free_number(p.map, p.prefix, skip=p)
        new = "maps/%s_%s_%d.lmp" % (p.map, p.prefix, target)

        def do(fx):
            self._rename(fx, p, new)
            return "Renamed %s to %s." % (p.label, new.split("/")[-1])
        return ("Renumber", "Rename it to %s (the first free number)." % new.split("/")[-1], do)

    def fix_rename_map(self, p, mname):
        target = self.free_number(mname, p.prefix)
        new = "maps/%s_%s_%d.lmp" % (mname, p.prefix, target)

        def do(fx):
            self._rename(fx, p, new)
            return "Renamed %s to %s." % (p.label, new.split("/")[-1])
        return ("Rename", "Rename it to %s." % new.split("/")[-1], do)

    def _rewrite(self, fx, p, body):
        data = struct.pack("<5i", 20, p.lid, 0, len(body), lmp_header(p.raw)[4]) + body
        if p.kind == "addon":
            fx.edit_vpk(p.addon.path, lambda files: files.__setitem__(p.inner, data))
        else:
            fx.backup(p.inner)
            with open(p.inner, "wb") as f:
                f.write(data)

    def fix_header(self, p):
        def do(fx):
            self._rewrite(fx, p, p.raw[20:])
            return "Repaired the header of %s." % p.label
        return ("Repair", "Rewrite the header of %s so it matches its contents." % p.label, do)

    def fix_entities(self, p, what):
        def do(fx):
            pre, ents, nl = parse_entities(p.body)
            if what == "worldspawn":
                w = [e for e in ents if e.get("classname").lower() == "worldspawn"][:1]
                ents = w + [e for e in ents if e not in w]
                msg = "Moved worldspawn to the top of %s." % p.label
            else:
                n = sum(1 for e in ents if not e.get("classname"))
                ents = [e for e in ents if e.get("classname")]
                msg = "Removed %d entities without a classname from %s." % (n, p.label)
            self._rewrite(fx, p, dump_entities(pre, ents, nl))
            return msg
        return ("Fix it", "Rewrite %s with the problem fixed." % p.label, do)

    def fix_rebase_walls(self, p, valve):
        def do(fx):
            bsp = self.map_lump(p.map, LUMP_BRUSHES)
            c = lambda b, i: struct.unpack_from("<i", b, i * 12 + 8)[0]
            mine = {i for i in range(len(bsp) // 12) if c(bsp, i) & PLAYERCLIP and not c(p.body, i) & PLAYERCLIP}
            self._rewrite(fx, p, brush_patch(valve.body, mine))
            return "Rebuilt %s on Valve's %s, keeping your %d removed walls." % (
                p.label, valve.label.split("/")[-1], len(mine))
        return ("Rebuild", "Rebuild %s on top of Valve's %s, keeping the walls it removes."
                % (p.label, valve.label.split("/")[-1]), do)

    def fix_rebase_entities(self, p, valve):
        def do(fx):
            bsp_ents = parse_entities(self.map_lump(p.map, LUMP_ENTITIES))[1]
            pre, mine, nl = parse_entities(p.body)
            d, m, a = entity_changes(bsp_ents, mine)
            vpre, vents, vnl = parse_entities(valve.body)
            out, missing = apply_changes(vents, d, m, a)
            self._rewrite(fx, p, dump_entities(vpre, out, vnl))
            return "Rebuilt %s on Valve's %s, keeping your %d deleted, %d changed, %d added%s." % (
                p.label, valve.label.split("/")[-1], len(d), len(m), len(a),
                (" (%d changes were for entities Valve's version doesn't have)" % len(missing)) if missing else "")
        return ("Rebuild", "Rebuild %s on top of Valve's %s, keeping your changes."
                % (p.label, valve.label.split("/")[-1]), do)

    def fix_merge(self, low, top):
        def do(fx):
            if low.lid == LUMP_BRUSHES:
                base, _v = self.base_brushes(low.map, low.prefix, below=min(low.n, top.n))
                c = lambda b, i: struct.unpack_from("<i", b, i * 12 + 8)[0]
                n = len(base) // 12
                both = {i for i in range(n) if c(base, i) & PLAYERCLIP and
                        (not c(low.body, i) & PLAYERCLIP or not c(top.body, i) & PLAYERCLIP)}
                self._rewrite(fx, top, brush_patch(base, both))
                msg = "Merged the walls of both into %s (%d walls removed)." % (top.label, len(both))
            else:
                base, _v = self.base_entities(low.map, low.prefix, below=min(low.n, top.n))
                tpre, tents, tnl = parse_entities(top.body)
                td, tm, _ta = entity_changes(base, tents)
                d, m, a = entity_changes(base, parse_entities(low.body)[1])
                busy = set(td) | set(tm)                          # where both changed the same entity, top wins
                have = keyed(tents)
                d = [k for k in d if k not in busy]
                m = {k: v for k, v in m.items() if k not in busy}
                a = [e for e in a if e.ident() not in have]
                out, missing = apply_changes(tents, d, m, a)
                self._rewrite(fx, top, dump_entities(tpre, out, tnl))
                msg = "Merged the entity changes of %s into %s (%d deleted, %d changed, %d added)%s." % (
                    low.label, top.label, len(d), len(m), len(a),
                    (", %d didn't apply" % len(missing)) if missing else "")
            if low.kind == "addon":
                fx.edit_vpk(low.addon.path, lambda files: files.pop(low.inner))
            else:
                fx.backup(low.inner)
                os.remove(low.inner)
            msg += " Removed %s." % low.label
            # close the gap the removed file leaves, or everything above it stops loading
            above = sorted((q for q in self.patches if q.map == low.map and q.prefix == low.prefix and q.active()
                            and q.n > low.n and q is not low), key=lambda q: q.n)
            for q in above:
                if q.kind == "valve" or not self.can_write(q):
                    msg += " Couldn't renumber %s; check again." % q.label
                    break
                new = "maps/%s_%s_%d.lmp" % (q.map, q.prefix, q.n - 1)
                self._rename(fx, q, new)
                msg += " Renumbered %s to %s." % (q.label, new.split("/")[-1])
            return msg
        return ("Merge", "Put the changes of %s into %s, remove the first file, and renumber the files above it."
                % (low.label, top.label), do)


def vpk_is_single(path):
    try:
        return all(e[0] == 0x7FFF for e in vpk_tree(path).values())
    except (ValueError, OSError, struct.error):
        return False


class Fixer:
    """applies fixes; every file is backed up once before it's changed"""

    def __init__(self, game):
        import time
        self.game = game
        self.dir = os.path.join(game, "left4dead2", "map_editor_backups", time.strftime("%Y%m%d-%H%M%S"))
        self.saved, self.created = set(), []

    def backup(self, path):
        if path in self.saved or not os.path.exists(path):
            return
        import shutil
        rel = os.path.relpath(path, self.game)
        dst = os.path.join(self.dir, rel)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copy2(path, dst)
        self.saved.add(path)

    def edit_vpk(self, path, fn):
        files = vpk_files(path)
        if files is None:
            raise RuntimeError("%s is split into several files; edit it by hand." % os.path.basename(path))
        fn(files)
        self.backup(path)
        write_vpk(path, files)

    def set_addon(self, name, on):
        p = os.path.join(self.game, "left4dead2", "addonlist.txt")
        self.backup(p)
        text = open(p, encoding="utf-8", errors="replace").read() if os.path.exists(p) else '"AddonList"\n{\n}\n'
        want = name.replace("\\", "/").lower()
        found = []

        def sub(m):
            if m.group(1).replace("\\", "/").lower() == want:
                found.append(1)
                return '"%s"%s"%s"' % (m.group(1), m.group(2), "1" if on else "0")
            return m.group(0)
        text = re.sub(r'"([^"]+\.vpk)"(\s+)"(\d)"', sub, text, flags=re.I)
        if not found:
            i = text.rfind("}")
            if i < 0:
                text, i = '"AddonList"\n{\n}\n', len('"AddonList"\n{\n')
            text = text[:i] + '\t"%s"\t\t"%s"\n' % (name.replace("/", "\\"), "1" if on else "0") + text[i:]
        with open(p, "w", encoding="utf-8") as f:
            f.write(text)


def check_setup(game, progress=None):
    return Scan(game, progress).run().findings


def apply_fix(game, fix, fx=None):
    """run one fix; returns its message. Refuses while the game runs."""
    if game_running():
        raise RuntimeError("Close Left 4 Dead 2 first: it keeps addon files open and rewrites addonlist.txt on exit.")
    fx = fx or Fixer(game)
    msg = fix[2](fx)
    if fx.saved:
        msg += "\nBackups: %s" % fx.dir
    return msg


def run_fixes(game, wanted=None, limit=60):
    """apply fixes, re-checking between each one; wanted = titles to fix, or None for everything fixable
    (including problems that only show up after an earlier fix). Returns the messages."""
    msgs, tried = [], set()
    fx = Fixer(game)                          # one backup folder for the whole run
    for _ in range(limit):
        todo = [f for f in check_setup(game) if len(f) > 3 and f[3] and f[1] not in tried
                and (wanted is None or f[1] in wanted)]
        if not todo:
            break
        title, fix = todo[0][1], todo[0][3]
        tried.add(title)
        try:
            msgs.append(apply_fix(game, fix, fx))
        except Exception as e:
            msgs.append("Couldn't fix \"%s\": %s" % (title, e))
    return msgs


def report_text(results):
    mark = {"error": "[PROBLEM]", "warn": "[CHECK]  ", "info": "[NOTE]   ", "ok": "[OK]     "}
    lines = ["L4D2 Map Editor problem check (by Rune)", ""]
    for r in results:
        lvl, title, detail = r[:3]
        fix = r[3] if len(r) > 3 else None
        lines.append("%s %s" % (mark[lvl], title))
        if detail:
            lines.append("           " + detail)
        if fix:
            lines.append("           Fix available: " + fix[1])
    return "\n".join(lines)


# ---------------------------------------------------------------- window

RED, RED_SIDE, GREEN, GREEN_SIDE, HOVER = "#ff453a", "#a8281f", "#30d158", "#1f8a3a", "#ffd60a"
SELECT = "#ffd60a"
ENT_MODES = ("ents", "build")
BUILD_CATS = ("Props", "Blockers & clips")
LIB_CATS = [
    ("All props", ()),
    ("Fences & railings", ("fence", "rail", "gate", "chainlink", "guardrail")),
    ("Walls & barricades", ("wall", "barricade", "barrier", "fortification", "plywood", "board", "sandbag",
                            "concrete", "panel", "sheetrock")),
    ("Containers & crates", ("crate", "container", "pallet", "box", "barrel", "dumpster", "cargo", "tank")),
    ("Vehicles", ("vehicle", "car", "truck", "bus", "van", "boat", "trailer", "forklift", "humvee", "train")),
    ("Furniture", ("furniture", "chair", "table", "couch", "sofa", "desk", "shelf", "bed", "cabinet", "bench",
                   "counter", "interiors")),
    ("Debris & junk", ("debris", "rubble", "junk", "trash", "garbage", "rock", "pile", "wreck")),
    ("Stairs & platforms", ("stair", "step", "ramp", "platform", "scaffold", "ladder", "catwalk", "dock")),
]

HELP = """HOW TO USE  -  L4D2 Map Editor by Rune

Open a map: a .bsp, or an addon .vpk for custom / workshop maps (it lists the maps inside).
Official maps are in Left 4 Dead 2/left4dead2/maps, left4dead2_dlc1-3/maps and update/maps.

VIEW
   "2D" is a flat map from above, "Isometric" is 3D. Rotate turns the map, Fit shows all of it.
   Drag "Max height" down to cut away roofs and see the street level.
   Mouse wheel = zoom, right-drag = move the view. Props (cars, fences, trees...) are drawn with
   their real shapes; untick "Props" to hide them.

WALLS MODE  (remove invisible walls)
   Red boxes are the invisible walls. Click one to remove it (green), click again to keep it.
   Shift+drag picks every wall inside a box. "Pick all" / "Pick none" are at the bottom right.

BUILD MODE  (add props: fences, walls, crates, cars...)
   Find a prop in the list (search or pick a category), check it in the preview, then double-click
   it and click on the map to place it. Select a placed prop to turn it (R key, Shift+R back),
   move it (arrows, PageUp/PageDown), "Drop to floor", or add the next one in line to build a fence.
   "Invisible wall around it" adds a blocker the size of the prop, so nobody squeezes through.
   New props can't be new map shapes: they're the game's own models, like Valve's later map fixes.

ENTITIES MODE  (doors, ladders, blockers, props, triggers...)
   Click an entity on the map or in the list to select it. Ctrl+click adds to the selection,
   Shift+drag selects a box. Its keys and values show on the right: double-click a value to edit.
   Delete = remove.  Ctrl+D = copy.  Drag (2D view) or arrow keys = move (Shift = bigger steps),
   PageUp/PageDown = up/down.  "Add..." places a new invisible wall, a prop, or a copy of the
   selected entity (like a ladder) where you click next.  Ctrl+Z / Ctrl+Y = undo / redo.

SAVE
   "Save .lmp" asks for your addon's maps folder (addons/<addon>/maps/) and writes the file(s).
   The tool keeps the fixes Valve already made to the map and picks the right file numbers.
   "Load .lmp" opens a patch you saved before so you can keep working on it.
   In game: "sv_cheats 1" + "r_drawclipbrushes 2" shows invisible walls. Bots need a nav ladder
   (nav_edit 1, nav_build_ladder, nav_save) before they use a ladder you added.

SOMETHING DOESN'T WORK?
   Click "Check problems": it checks your addons and patches, explains what's wrong, and can
   fix most of it (with backups). "Copy report" gives you a text to paste when asking for help."""


class App:
    def __init__(self, path=None, inner=None):
        import tkinter as tk
        from tkinter import filedialog, messagebox, ttk
        self.tk, self.fd, self.mb, self.ttk = tk, filedialog, messagebox, ttk
        try:  # sharp text on scaled Windows screens
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass
        self.root = tk.Tk()
        self.root.title("L4D2 Map Editor by Rune")
        self.root.minsize(1280, 640)
        icon = os.path.join(getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__))), "icon.ico")
        if os.path.exists(icon):                 # bundled in the .exe; optional next to the .py
            try:
                self.root.iconbitmap(icon)
            except Exception:
                pass
        self.m = None
        self.view, self.rot, self.mode = "iso", 0, "walls"
        self.S, self.T = None, (0.0, 0.0)     # projection -> canvas: p * S + T
        self.save_dir = None
        self.undo_stack, self.redo_stack = [], []
        self.sel = set()
        self.placing = None
        self.hover = None
        self.files, self.meshes, self.mat_cache, self.placed_cache = None, {}, {}, {}
        self.statics, self.static_placed = [], []
        self.load_gen, self.load_msg = 0, ""
        self.build_yaw = 0
        self.library = []
        self._build()
        self.root.report_callback_exception = self._crash
        if path:
            self.root.after(50, lambda: self.open_path(path, inner))
        else:
            self.root.after(50, self.welcome)

    # ---- layout
    def _build(self):
        tk, ttk = self.tk, self.ttk
        try:   # list rows tall enough for the font on scaled screens
            import tkinter.font as tkfont
            line = tkfont.nametofont("TkDefaultFont").metrics("linespace")
            ttk.Style(self.root).configure("Treeview", rowheight=line + 6)
        except Exception:
            pass
        bar = tk.Frame(self.root)
        bar.pack(fill="x")
        tk.Button(bar, text="Open map...", command=self.ask_open).pack(side="left", padx=(4, 2), pady=3)

        modes = tk.Frame(bar)
        modes.pack(side="left", padx=(6, 2))
        self.mode_var = tk.StringVar(value=self.mode)
        for label, value in (("Walls", "walls"), ("Entities", "ents"), ("Build", "build")):
            tk.Radiobutton(modes, text=label, value=value, variable=self.mode_var, indicatoron=0, padx=8,
                           selectcolor="#ffe1a8", command=self.set_mode).pack(side="left")

        views = tk.Frame(bar)
        views.pack(side="left", padx=(6, 2))
        self.view_var = tk.StringVar(value=self.view)
        for label, value in (("2D", "top"), ("Isometric", "iso")):
            tk.Radiobutton(views, text=label, value=value, variable=self.view_var, indicatoron=0, padx=8,
                           selectcolor="#cfe3ff", command=self.set_view).pack(side="left")
        tk.Button(bar, text="Rotate", command=self.rotate).pack(side="left", padx=2)
        tk.Button(bar, text="Fit", command=self.refit).pack(side="left", padx=2)
        tk.Label(bar, text="Max height").pack(side="left", padx=(6, 0))
        self.cut = tk.Scale(bar, orient="horizontal", length=95, showvalue=True, command=self._cut_moved)
        self.cut.pack(side="left")
        self.props_on = tk.BooleanVar(value=True)
        tk.Checkbutton(bar, text="Props", variable=self.props_on,
                       command=lambda: self.m and self.redraw()).pack(side="left", padx=(2, 0))

        tk.Button(bar, text="Help", command=self.show_help).pack(side="right", padx=2)
        tk.Button(bar, text="Check problems", command=self.check_problems).pack(side="right", padx=2)
        tk.Button(bar, text="Save .lmp", command=self.save).pack(side="right", padx=2)
        tk.Button(bar, text="Load .lmp", command=self.load_lmp).pack(side="right", padx=2)
        bottom = tk.Frame(self.root)
        bottom.pack(fill="x", side="bottom")
        self.status = tk.Label(bottom, anchor="w")
        self.status.pack(side="left", fill="x", expand=True)
        self.walls_bar = tk.Frame(bottom)
        self.walls_bar.pack(side="right")
        tk.Button(self.walls_bar, text="Pick none", command=lambda: self.set_all(False)).pack(side="right", padx=4)
        tk.Button(self.walls_bar, text="Pick all", command=lambda: self.set_all(True)).pack(side="right", padx=4)
        self.note = tk.Label(self.root, anchor="w", fg="#666")
        self.note.pack(fill="x", side="bottom")

        body = tk.Frame(self.root)
        body.pack(fill="both", expand=True)
        self.cv = tk.Canvas(body, width=900, height=740, bg="#1d1f22", highlightthickness=0)
        self.cv.pack(side="left", fill="both", expand=True)

        # entity side panel
        side = self.side = tk.Frame(body, width=400)
        top = tk.Frame(side)
        top.pack(fill="x", padx=6, pady=(6, 2))
        tk.Label(top, text="Search").pack(side="left")
        self.search = tk.StringVar()
        self.search.trace_add("write", lambda *_: self._filter_changed())
        tk.Entry(top, textvariable=self.search).pack(side="left", fill="x", expand=True, padx=4)
        self.cat_var = tk.StringVar(value="All")
        cats = ["All"] + [c[0] for c in CATEGORIES] + [OTHER[0]]
        ttk.Combobox(side, textvariable=self.cat_var, values=cats, state="readonly").pack(fill="x", padx=6)
        self.cat_var.trace_add("write", lambda *_: self._filter_changed())
        lf = tk.Frame(side)
        lf.pack(fill="both", expand=True, padx=6, pady=4)
        self.lst = ttk.Treeview(lf, columns=("cls", "name"), show="headings", height=9, selectmode="extended")
        self.lst.heading("cls", text="Class")
        self.lst.heading("name", text="Name")
        self.lst.column("cls", width=190)
        self.lst.column("name", width=170)
        sb = tk.Scrollbar(lf, command=self.lst.yview)
        self.lst.config(yscrollcommand=sb.set)
        self.lst.pack(side="left", fill="both", expand=True)
        sb.pack(side="left", fill="y")
        self.lst.bind("<<TreeviewSelect>>", self._list_selected)

        acts = tk.Frame(side)
        acts.pack(fill="x", padx=6)
        tk.Button(acts, text="Delete", command=self.delete_sel).pack(side="left")
        tk.Button(acts, text="Copy", command=self.duplicate_sel).pack(side="left", padx=4)
        add = tk.Menubutton(acts, text="Add...", relief="raised")
        menu = tk.Menu(add, tearoff=0)
        for name in TEMPLATES:
            menu.add_command(label=name, command=lambda n=name: self.start_place(n))
        menu.add_command(label="Copy of the selected entity (ladders, props...)",
                         command=lambda: self.start_place(None))
        add["menu"] = menu
        add.pack(side="left")
        tk.Button(acts, text="Undo", command=self.undo).pack(side="right")
        tk.Button(acts, text="Redo", command=self.redo).pack(side="right", padx=4)

        tk.Label(side, text="Keys and values  (double-click a value to edit)", anchor="w").pack(fill="x", padx=6,
                                                                                              pady=(8, 0))
        pf = tk.Frame(side)
        pf.pack(fill="both", expand=True, padx=6, pady=2)
        self.props = ttk.Treeview(pf, columns=("k", "v"), show="headings", height=8)
        self.props.heading("k", text="Key")
        self.props.heading("v", text="Value")
        self.props.column("k", width=120)
        self.props.column("v", width=250)
        self.props.pack(fill="both", expand=True)
        self.props.bind("<Double-Button-1>", self._edit_value)
        pb = tk.Frame(side)
        pb.pack(fill="x", padx=6, pady=(0, 6))
        tk.Button(pb, text="Add key", command=self.add_key).pack(side="left")
        tk.Button(pb, text="Remove key", command=self.remove_key).pack(side="left", padx=4)
        self.changes = tk.Label(side, anchor="w", fg="#444")
        self.changes.pack(fill="x", padx=6, pady=(0, 6))

        # build side panel: props library, preview, and tools for the selected props
        bs = self.build_side = tk.Frame(body, width=400)
        top = tk.Frame(bs)
        top.pack(fill="x", padx=6, pady=(6, 2))
        tk.Label(top, text="Find a prop").pack(side="left")
        self.lib_search = tk.StringVar()
        self.lib_search.trace_add("write", lambda *_: self._lib_changed())
        tk.Entry(top, textvariable=self.lib_search, width=14).pack(side="left", fill="x", expand=True, padx=4)
        self.lib_cat = tk.StringVar(value=LIB_CATS[0][0])
        ttk.Combobox(top, textvariable=self.lib_cat, values=[c[0] for c in LIB_CATS], state="readonly",
                     width=17).pack(side="left")
        self.lib_cat.trace_add("write", lambda *_: self._lib_changed())
        # bottom part first, so the list (which stretches) gets what's left on small screens
        self.build_info = tk.Label(bs, anchor="w", justify="left", fg="#444", wraplength=385)
        self.build_info.pack(side="bottom", fill="x", padx=6, pady=(0, 6))
        row = tk.Frame(bs)
        row.pack(side="bottom", fill="x", padx=6, pady=2)
        tk.Button(row, text="Delete", command=self.delete_sel).pack(side="left")
        tk.Button(row, text="Copy", command=self.duplicate_sel).pack(side="left", padx=4)
        tk.Button(row, text="Drop to floor", command=self.drop_sel).pack(side="left")
        tk.Button(row, text="Redo", command=self.redo).pack(side="right")
        tk.Button(row, text="Undo", command=self.undo).pack(side="right", padx=4)
        row = tk.Frame(bs)
        row.pack(side="bottom", fill="x", padx=6, pady=2)
        tk.Label(row, text="Next in line").pack(side="left")
        tk.Button(row, text="<", width=3, command=lambda: self.line_sel(-1)).pack(side="left", padx=(4, 1))
        tk.Button(row, text=">", width=3, command=lambda: self.line_sel(1)).pack(side="left")
        wall = tk.Menubutton(row, text="Invisible wall around it", relief="raised")
        wm = tk.Menu(wall, tearoff=0)
        wm.add_command(label="Same size as the prop", command=lambda: self.block_sel(0))
        wm.add_command(label="Taller, so nobody can jump over it", command=lambda: self.block_sel(128))
        wall["menu"] = wm
        wall.pack(side="right")
        row = tk.Frame(bs)
        row.pack(side="bottom", fill="x", padx=6, pady=(6, 2))
        tk.Label(row, text="Selected:", font=("Segoe UI", 9, "bold")).pack(side="left")
        tk.Label(row, text=" turn").pack(side="left")
        for txt, deg in (("-90", -90), ("-15", -15), ("+15", 15), ("+90", 90)):
            tk.Button(row, text=txt, width=4, command=lambda d=deg: self.rotate_sel(d)).pack(side="left", padx=1)
        tk.Button(bs, text="Place it on the map  (or double-click the prop)",
                  command=self.place_from_library).pack(side="bottom", fill="x", padx=6, pady=(2, 0))
        self.preview_info = tk.Label(bs, anchor="w", justify="left", fg="#333", wraplength=385)
        self.preview_info.pack(side="bottom", fill="x", padx=6)
        self.preview = tk.Canvas(bs, height=100, bg="#26292e", highlightthickness=0)
        bs.bind("<Configure>", lambda e: self.preview.config(height=100 if e.height > 640 else 64))
        self.preview.pack(side="bottom", fill="x", padx=6, pady=(2, 0))
        lf = tk.Frame(bs)
        lf.pack(fill="both", expand=True, padx=6, pady=4)
        self.lib = ttk.Treeview(lf, columns=("name", "folder"), show="headings", height=3, selectmode="browse")
        self.lib.heading("name", text="Prop")
        self.lib.heading("folder", text="Folder")
        self.lib.column("name", width=210)
        self.lib.column("folder", width=150)
        sb = tk.Scrollbar(lf, command=self.lib.yview)
        self.lib.config(yscrollcommand=sb.set)
        self.lib.pack(side="left", fill="both", expand=True)
        sb.pack(side="left", fill="y")
        self.lib.bind("<<TreeviewSelect>>", lambda e: self.show_preview())
        self.lib.bind("<Double-Button-1>", lambda e: self.place_from_library())
        self.lib.bind("<Return>", lambda e: self.place_from_library())

        cv = self.cv
        cv.bind("<Motion>", self.on_move)
        cv.bind("<Button-1>", self.on_click)
        cv.bind("<Control-Button-1>", lambda e: self.on_click(e, add=True))
        cv.bind("<B1-Motion>", self.drag_entity)
        cv.bind("<ButtonRelease-1>", self.drop_entity)
        cv.bind("<Shift-Button-1>", self.box_start)
        cv.bind("<Shift-B1-Motion>", self.box_move)
        cv.bind("<Shift-ButtonRelease-1>", self.box_end)
        cv.bind("<ButtonPress-3>", self.pan_start)
        cv.bind("<B3-Motion>", self.pan_move)
        cv.bind("<MouseWheel>", lambda e: self.zoom(e, 1.25 if e.delta > 0 else 0.8))
        cv.bind("<Button-4>", lambda e: self.zoom(e, 1.25))
        cv.bind("<Button-5>", lambda e: self.zoom(e, 0.8))
        r = self.root
        r.bind("<Delete>", lambda e: self._key(self.delete_sel))
        r.bind("<Control-d>", lambda e: self._key(self.duplicate_sel))
        r.bind("<Control-z>", lambda e: self._key(self.undo))
        r.bind("<Control-y>", lambda e: self._key(self.redo))
        r.bind("<Escape>", lambda e: self.cancel_place())
        for key, d in (("Left", (-1, 0, 0)), ("Right", (1, 0, 0)), ("Up", (0, 1, 0)), ("Down", (0, -1, 0)),
                       ("Prior", (0, 0, 1)), ("Next", (0, 0, -1))):
            r.bind("<%s>" % key, lambda e, d=d: self._key(lambda: self.nudge(d, 8)))
            r.bind("<Shift-%s>" % key, lambda e, d=d: self._key(lambda: self.nudge(d, 64)))
        for key, deg in (("r", 15), ("R", -15), ("bracketleft", -15), ("bracketright", 15)):
            r.bind("<%s>" % key, lambda e, d=deg: self._key(lambda: self.rotate_sel(d)))
        self.box = {}
        self.dragging = None

    def _key(self, fn):
        w = self.root.focus_get()
        if self.mode in ENT_MODES and self.m and not (w is not None and w.winfo_class() in ("Entry", "TEntry")):
            fn()
        return "break"

    # ---- problem check and crash reports
    def check_problems(self, game=None):
        tk = self.tk
        game = game or (game_root(self.m.path) if self.m else None) or find_game()
        win = tk.Toplevel(self.root)
        win.title("Check for problems - L4D2 Map Editor by Rune")
        head = tk.Label(win, anchor="w", text="Checking...", font=("Segoe UI", 10, "bold"))
        head.pack(fill="x", padx=10, pady=(8, 2))
        frame = tk.Frame(win)
        frame.pack(fill="both", expand=True, padx=10)
        txt = tk.Text(frame, width=110, height=24, wrap="word", font=("Segoe UI", 10), padx=8, pady=6)
        sb = tk.Scrollbar(frame, command=txt.yview)
        txt.config(yscrollcommand=sb.set)
        txt.pack(side="left", fill="both", expand=True)
        sb.pack(side="left", fill="y")
        for tag, color in (("error", "#c0201a"), ("warn", "#b26a00"), ("info", "#2b5fa8"), ("ok", "#1f7a35")):
            txt.tag_config(tag, foreground=color, font=("Segoe UI", 10, "bold"))
        txt.tag_config("detail", lmargin1=28, lmargin2=28, foreground="#333")
        txt.tag_config("fixdesc", foreground="#555")
        state = {"results": [], "game": game}

        def run():
            txt.config(state="normal")
            txt.delete("1.0", "end")
            win.config(cursor="watch")

            def progress(msg):
                head.config(text=msg)
                win.update()
            try:
                res = check_setup(state["game"], progress)
            except Exception as e:                       # the checker itself must never crash the tool
                import traceback
                res = [("error", "The check itself failed: %s" % e, traceback.format_exc())]
            state["results"] = res
            mark = {"error": "PROBLEM  ", "warn": "CHECK    ", "info": "NOTE     ", "ok": "OK       "}
            order = {"error": 0, "warn": 1, "info": 2, "ok": 3}
            for r in sorted(res, key=lambda r: order[r[0]]):
                lvl, title, detail = r[:3]
                fix = r[3] if len(r) > 3 else None
                txt.insert("end", mark[lvl], lvl)
                txt.insert("end", title + "\n")
                if detail:
                    txt.insert("end", detail + "\n", "detail")
                if fix:
                    b = tk.Button(txt, text=fix[0], cursor="hand2", command=lambda f=fix, t=title: do_fixes([(t, f)]))
                    txt.insert("end", "    ", "detail")
                    txt.window_create("end", window=b)
                    txt.insert("end", "  " + fix[1] + "\n", "fixdesc")
                txt.insert("end", "\n")
            txt.config(state="disabled")
            errors = sum(r[0] == "error" for r in res)
            warns = sum(r[0] == "warn" for r in res)
            fixable = sum(1 for r in res if len(r) > 3 and r[3])
            head.config(text=(("%d problem(s), %d thing(s) to check" % (errors, warns)) if errors or warns
                              else "No problems found") + (("   -   %d can be fixed automatically" % fixable)
                                                           if fixable else ""))
            fix_all.config(state="normal" if fixable else "disabled")
            win.config(cursor="")

        def do_fixes(chosen, everything=False):
            """apply fixes after one confirmation; re-checks between fixes so each sees the current files"""
            if game_running():
                self.mb.showerror("Fix", "Close Left 4 Dead 2 first, then fix.", parent=win)
                return
            listing = "\n".join("  - " + f[1] for _t, f in chosen)
            if everything:
                listing += "\n  - and anything fixable that shows up after these"
            if not self.mb.askyesno("Fix", "This will:\n%s\n\nEvery file is backed up first, in "
                                           "left4dead2/map_editor_backups.\nGo ahead?" % listing, parent=win):
                return
            win.config(cursor="watch")
            head.config(text="Fixing...")
            win.update()
            msgs = run_fixes(state["game"], None if everything else {t for t, _f in chosen})
            win.config(cursor="")
            text = "\n\n".join(m.split("\nBackups:")[0] for m in msgs) or "Nothing left to fix."
            backups = [m.split("\nBackups: ")[1] for m in msgs if "\nBackups: " in m]
            if backups:
                text += "\n\nBackups of the changed files: %s" % backups[-1].rsplit("\\", 1)[0]
            self.mb.showinfo("Fix", text, parent=win)
            run()

        def pick():
            d = self.fd.askdirectory(title="Pick your Left 4 Dead 2 folder (the one with left4dead2.exe)",
                                     parent=win)
            if d:
                state["game"] = d
                run()

        def copy():
            self.root.clipboard_clear()
            self.root.clipboard_append(report_text(state["results"]))
            head.config(text="Report copied: paste it when you ask for help.")

        btns = tk.Frame(win)
        btns.pack(fill="x", padx=10, pady=8)
        fix_all = tk.Button(btns, text="Fix all", state="disabled", command=lambda: do_fixes(
            [(r[1], r[3]) for r in state["results"] if len(r) > 3 and r[3]], everything=True))
        fix_all.pack(side="left")
        tk.Button(btns, text="Copy report", command=copy).pack(side="left", padx=6)
        tk.Button(btns, text="Check again", command=run).pack(side="left")
        tk.Button(btns, text="Pick game folder...", command=pick).pack(side="left", padx=6)
        tk.Button(btns, text="Close", command=win.destroy).pack(side="right")
        win.after(50, run)
        self.check_window = (win, state, do_fixes, run)

    def _crash(self, exc, val, tb):
        import traceback
        details = "L4D2 Map Editor by Rune - error report\n%s\nPython %s\n\n%s" % (
            (self.m.name if self.m else "no map open"), sys.version.split()[0],
            "".join(traceback.format_exception(exc, val, tb)))
        try:
            self.root.clipboard_clear()
            self.root.clipboard_append(details)
        except Exception:
            pass
        self.root.config(cursor="")
        self.mb.showerror("Something went wrong",
                          "%s\n\nNothing was saved by this action. The details are copied to your clipboard: paste "
                          "them when asking for help. \"Check problems\" may also tell you what's wrong." % val)

    def welcome(self):
        self.cv.delete("all")
        self.root.update_idletasks()
        w = max(self.cv.winfo_width(), 1000)
        col = (w - 90) // 2
        # each section: heading line + its indented lines joined into one paragraph, so it wraps cleanly
        sections = []
        for block in HELP.split("\n\n"):
            lines = block.split("\n")
            head = [l for l in lines if not l.startswith("   ")]
            body = " ".join(l.strip() for l in lines if l.startswith("   "))
            sections.append("\n".join(head + ([body] if body else [])))
        cut = next(i for i, s in enumerate(sections) if s.startswith("ENTITIES MODE"))
        for k, part in enumerate((sections[:cut], sections[cut:])):
            self.cv.create_text(30 + k * (col + 30), 18 if k == 0 else 18, anchor="nw", fill="#e6e6e6",
                                text="\n\n".join(part), font=("Segoe UI", 10), width=col)

    def show_help(self):
        tk = self.tk
        win = tk.Toplevel(self.root)
        win.title("How to use L4D2 Map Editor (by Rune)")
        txt = tk.Text(win, width=100, height=34, wrap="word", font=("Segoe UI", 10), padx=12, pady=10)
        txt.insert("1.0", HELP)
        txt.config(state="disabled")
        txt.pack(fill="both", expand=True)
        tk.Button(win, text="Close", command=win.destroy).pack(pady=6)

    # ---- loading
    def ask_open(self):
        path = self.fd.askopenfilename(title="Open a map (.bsp) or an addon / pak01_dir (.vpk)",
                                       filetypes=[("Maps and addons", "*.bsp *.vpk"), ("All files", "*.*")])
        if path:
            self.open_path(path)

    def open_path(self, path, inner=None):
        """open a .bsp, or a .vpk (asking which map when it holds several)"""
        if inner is None and path.lower().endswith(".vpk"):
            try:
                names = maps_in_vpk(path)
            except (ValueError, OSError) as e:
                self.mb.showerror("L4D2 Map Editor", str(e))
                return
            if not names:
                self.mb.showinfo("L4D2 Map Editor", "No maps in this .vpk.")
                return
            inner = names[0] if len(names) == 1 else self.choose(names)
            if not inner:
                return
        self.open(path, inner)

    def choose(self, names):
        tk = self.tk
        win = tk.Toplevel(self.root)
        win.title("Pick a map")
        win.transient(self.root)
        lb = tk.Listbox(win, width=40, height=min(20, len(names)))
        for n in names:
            lb.insert("end", n)
        lb.pack(padx=8, pady=8)
        lb.selection_set(0)
        res = []

        def ok(_=None):
            if lb.curselection():
                res.append(names[lb.curselection()[0]])
            win.destroy()
        lb.bind("<Double-Button-1>", ok)
        tk.Button(win, text="Open", command=ok).pack(pady=(0, 8))
        win.grab_set()
        self.root.wait_window(win)
        return res[0] if res else None

    def open(self, path, inner=None):
        if self.m and self.dirty() and not self.mb.askyesno(
                "L4D2 Map Editor", "You have unsaved changes on %s. Open another map anyway?" % self.m.name):
            return
        self.root.config(cursor="watch")
        self.root.update()
        try:
            m = Map(path, inner)
            clips = [c for c in clip_brushes(m.d, m.brushes) if None not in c[1] and None not in c[2]]
            faces = world_faces(m.d)
            ent_raw, ent_from = m.base_lump("l", LUMP_ENTITIES)
            prefix, ents, nl = parse_entities(ent_raw)
            models = model_bounds(m.d)
        except (ValueError, OSError, struct.error, lzma.LZMAError) as e:
            self.root.config(cursor="")
            self.mb.showerror("L4D2 Map Editor", "Can't read this map: %s" % e)
            return
        self.root.config(cursor="")
        self.m, self.faces, self.models = m, faces, models
        self.tiles = {}
        self.clips = {i: (mn, mx) for i, mn, mx in clips}
        self.picked = set()
        self.ent_prefix, self.ent_nl, self.ent_from = prefix, nl, ent_from
        self.base_ents = list(ents)
        self.ents = list(ents)
        self.sel = set()
        self.undo_stack, self.redo_stack = [], []
        self.placing = None
        self.root.title("L4D2 Map Editor by Rune - " + m.name)
        self.start_props(path)
        fz = sorted(p[2] for f in faces for p in f[0]) or [0, 1]
        self.zlo, self.zhi = fz[len(fz) // 100], fz[-1 - len(fz) // 100]
        lo = min([self.zlo] + [mn[2] for mn, mx in self.clips.values()])
        hi = max([fz[-1]] + [mx[2] for mn, mx in self.clips.values()])
        self.cut.config(from_=int(lo), to=int(hi) + 1, resolution=8, command="")
        self.cut.set(int(hi) + 1)
        self.cut.config(command=self._cut_moved)
        self._update_note()
        self.fill_list()
        self.refit()

    # ---- props: loaded a few at a time after the map opens, so the map shows right away
    def start_props(self, path):
        self.load_gen += 1
        game = game_root(path) or find_game()
        if self.files is None or self.files_key != (game, path) or self.m.d is not getattr(self, "_files_d", None):
            self.files = GameFiles(game, self.m.d, path if path.lower().endswith(".vpk") else None)
            self.files_key, self._files_d = (game, path), self.m.d
            self.meshes, self.mat_cache, self.placed_cache = {}, {}, {}
        self.statics = static_props(self.m.d)
        self.static_placed = []
        self.floor_faces = None
        names = {s[0].lower() for s in self.statics}
        names |= {e.get("model").lower() for e in self.ents if e.get("model").lower().endswith(".mdl")}
        self._queue = sorted(n for n in names if n not in self.meshes)
        self._queue_total = len(self._queue)
        self.library = sorted(p for p in self.files.models() if not p.startswith(NOT_PROPS))
        self._lib_changed()
        self.load_msg = "loading props..." if self._queue else ""
        self.root.after(30, lambda g=self.load_gen: self._load_some(g))

    def _load_some(self, gen):
        import time
        if gen != self.load_gen or not self.m:
            return
        end = time.perf_counter() + 0.06
        while self._queue and time.perf_counter() < end:
            n = self._queue.pop()
            self.meshes[n] = load_mesh(self.files, n, self.mat_cache)
        if self._queue:
            done = self._queue_total - len(self._queue)
            self.load_msg = "loading props %d%%" % (100 * done // max(self._queue_total, 1))
            self._update_note()
            self.root.after(1, lambda: self._load_some(gen))
            return
        self.static_placed = []
        for name, o, a, _solid in self.statics:
            mesh = self.meshes.get(name.lower())
            if mesh:
                self.static_placed.append(Placed(mesh, o, a))
        missing = sum(1 for s in self.statics if not self.meshes.get(s[0].lower()))
        self.load_msg = ("%d props" % len(self.static_placed)) + (
            ("  (%d couldn't be read)" % missing) if missing else "")
        self._update_note()
        self.redraw()

    def mesh(self, name):
        """the drawable model, loading it now if needed (None if the game doesn't have it)"""
        key = GameFiles.norm(name)
        if key not in self.meshes and self.files:
            self.meshes[key] = load_mesh(self.files, key, self.mat_cache)
        return self.meshes.get(key)

    def placed_for(self, e):
        """Placed model for an entity with a .mdl model, or None"""
        model = e.get("model")
        if not model.lower().endswith(".mdl"):
            return None
        key = (model.lower(), e.get("origin"), e.get("angles"), e.get("modelscale"))
        if key in self.placed_cache:
            return self.placed_cache[key]
        if GameFiles.norm(model) not in self.meshes and getattr(self, "_queue", None):
            return None                                      # still loading: drawn once it's ready
        mesh = self.mesh(model)
        p = None
        if mesh:
            try:
                scale = float(e.get("modelscale") or 1) or 1.0
            except ValueError:
                scale = 1.0
            p = Placed(mesh, e.origin(), parse_vec(e.get("angles")), scale)
        if len(self.placed_cache) > 20000:
            self.placed_cache.clear()
        self.placed_cache[key] = p
        return p

    def _update_note(self):
        m = self.m
        if self.mode == "walls":
            s = "  " + m.notes()
        else:
            s = "  Entities from %s" % (self.ent_from or "the map itself")
            extra = [p for p in m.prefixes() if p != "l"]
            if extra:
                s += "  -  saving also updates Valve's _%s_ game-mode versions" % "_, _".join(extra)
        if self.load_msg:
            s += "   |   " + self.load_msg
        self.note.config(text=s)

    def dirty(self):
        return bool(self.picked) or self.ents != self.base_ents

    # ---- modes and views
    def set_mode(self):
        self.mode = self.mode_var.get()
        self.side.pack_forget()
        self.build_side.pack_forget()
        if self.mode in ENT_MODES:
            self.walls_bar.pack_forget()
            panel = self.side if self.mode == "ents" else self.build_side
            panel.pack(side="right", fill="y")
            panel.pack_propagate(False)
        else:
            self.walls_bar.pack(side="right")
        self.cancel_place()
        if self.m:
            if self.mode == "build":
                self.sel = {i for i in self.sel if category(self.ents[i].get("classname"))[0] in BUILD_CATS}
            self.fill_list()
            self.show_props()
            self._update_note()
            self.redraw()

    def rot_xy(self, x, y):
        return [(x, y), (-y, x), (-x, -y), (y, -x)][self.rot]

    def unrot_xy(self, x, y):
        return [(x, y), (y, -x), (-x, -y), (-y, x)][self.rot]

    def proj(self, x, y, z):
        x, y = self.rot_xy(x, y)
        if self.view == "top":
            return x, -y
        return (x - y) * 0.866, -((x + y) * 0.5 + z)

    def to_canvas(self, x, y, z):
        px, py = self.proj(x, y, z)
        return px * self.S + self.T[0], py * self.S + self.T[1]

    def _cut_moved(self, _v):
        if self.m:
            if getattr(self, "_after", None):
                self.root.after_cancel(self._after)
            self._after = self.root.after(250, self.redraw)

    def set_view(self):
        if self.view_var.get() != self.view:
            self.view = self.view_var.get()
            self.refit()

    def toggle_view(self):
        self.view_var.set("top" if self.view == "iso" else "iso")
        self.set_view()

    def rotate(self):
        self.rot = (self.rot + 1) % 4
        self.refit()

    def refit(self):
        self.S = None
        self.redraw()

    def pan_start(self, e):
        self._pan = (e.x, e.y)

    def pan_move(self, e):
        dx, dy = e.x - self._pan[0], e.y - self._pan[1]
        self._pan = (e.x, e.y)
        self.cv.move("all", dx, dy)
        self.T = (self.T[0] + dx, self.T[1] + dy)

    def zoom(self, e, f):
        """scale what's drawn at once, then redraw properly when the wheel stops (keeps zooming smooth)"""
        if not self.m:
            return
        cx, cy = self.cv.canvasx(e.x), self.cv.canvasy(e.y)
        self.S *= f
        self.T = (cx + (self.T[0] - cx) * f, cy + (self.T[1] - cy) * f)
        self.cv.scale("all", cx, cy, f, f)
        if getattr(self, "_zoom_after", None):
            self.root.after_cancel(self._zoom_after)
        self._zoom_after = self.root.after(220, self.redraw)

    # ---- drawing
    def redraw(self):
        if not self.m:
            return
        if getattr(self, "_zoom_after", None):
            self.root.after_cancel(self._zoom_after)
            self._zoom_after = None
        t_start = time.perf_counter()
        cv, cut = self.cv, float(self.cut.get())
        cv.delete("all")
        self.item_brush, self.brush_items, self.area = {}, {}, {}
        self.item_face, self.item_ent, self.ent_items = {}, {}, {}
        iso = self.view == "iso"
        L = (-0.42, -0.58, 0.70)
        zspan = max(self.zhi - self.zlo, 1)
        rot, proj = self.rot_xy, self.proj

        if self.S is None:        # fit, ignoring far-off bits like the 3D skybox
            pp = [proj(*p) for f in self.faces for p in f[0]]
            xs, ys = sorted(p[0] for p in pp) or [0, 1], sorted(p[1] for p in pp) or [0, 1]
            q = lambda v, f: v[min(len(v) - 1, int(len(v) * f))]
            x0, x1, y0, y1 = q(xs, 0.01), q(xs, 0.99), q(ys, 0.01), q(ys, 0.99)
            w = max(cv.winfo_width(), 400) - 60
            h = max(cv.winfo_height(), 300) - 60
            self.S = min(w / max(x1 - x0, 1), h / max(y1 - y0, 1))
            self.T = (30 + (w - (x1 - x0) * self.S) / 2 - x0 * self.S,
                      30 + (h - (y1 - y0) * self.S) / 2 - y0 * self.S)
        s, (tx, ty) = self.S, self.T
        cw, ch = max(cv.winfo_width(), 400), max(cv.winfo_height(), 300)
        # walls are cut in pieces (so props sort right against them); smaller pieces when zoomed in
        step = 128
        while step < 2048 and step * s < 70:
            step *= 2
        if step not in self.tiles:
            self.tiles[step] = tile_faces(self.faces, step, step * 0.75)
        x_lo, x_hi, y_lo, y_hi = -tx / s, (cw - tx) / s, -ty / s, (ch - ty) / s    # visible area, projected units

        polys = []   # (sort key, points, fill, kind, ref)
        for pts, n, rgb, fi in self.tiles[step]:
            nx, ny = rot(n[0], n[1])
            if iso:
                if -nx - ny + n[2] <= 0.02:
                    continue
            elif n[2] < 0.6:
                continue
            if max(p[2] for p in pts) > cut:
                pts = clip_below(pts, cut)
                if len(pts) < 3:
                    continue
            pp = [proj(*p) for p in pts]
            px, py = [a for a, _b in pp], [b for _a, b in pp]
            if max(px) < x_lo or min(px) > x_hi or max(py) < y_lo or min(py) > y_hi:
                continue
            zavg = sum(p[2] for p in pts) / len(pts)
            light = 0.42 + 0.58 * max(0.0, nx * L[0] + ny * L[1] + n[2] * L[2])
            if iso:
                key = (0, sum(-a - b for a, b in (rot(p[0], p[1]) for p in pts)) / len(pts) + zavg)
            else:
                light *= 0.65 + 0.35 * min(1.0, max(0.0, (zavg - self.zlo) / zspan))
                key = (0, zavg)
            col = "#%02x%02x%02x" % tuple(min(255, int(c * light * 255)) for c in rgb)
            polys.append((key, pp, col, "face", fi))

        if self.props_on.get():
            self.prop_polys(polys, cut, iso, L, zspan)

        if self.mode == "walls":
            self.shown = {i: v for i, v in self.clips.items() if v[0][2] <= cut}
            for i, (mn, mx) in self.shown.items():
                for part, ps, key in self.box_faces(mn, mx, cut):
                    polys.append((key, ps, part, "wall", i))
                self.area[i] = (mx[0] - mn[0]) * (mx[1] - mn[1])
        else:
            self.shown = {}
            for idx, (mn, mx, point, color) in self.visible_ents(cut).items():
                if point:      # dots stay on top of props too: they mark what you can edit
                    polys.append(((2, 0), [proj(*mn)], color, "point", idx))
                else:
                    for part, ps, key in self.box_faces(mn, mx, cut):
                        polys.append(((1,) + key[1:], ps, color, "ebox" + part, idx))
                self.area[idx] = 0 if point else (mx[0] - mn[0]) * (mx[1] - mn[1])
        polys.sort(key=lambda p: p[0])
        self._t_build = time.perf_counter() - t_start

        s, (tx, ty) = self.S, self.T
        create = cv.create_polygon
        for _key, ps, fill, kind, ref in polys:
            flat = [c for (x, y) in ps for c in (x * s + tx, y * s + ty)]
            if kind == "face":
                self.item_face[create(flat, fill=fill, outline="")] = ref
            elif kind == "prop":
                create(flat, fill=fill, outline="")
            elif kind == "leaf":                  # trees and bushes: see-through, so the street shows
                create(flat, fill=fill, outline="", stipple="gray50")
            elif kind == "emesh":
                it = create(flat, fill=fill, outline="")
                self.item_ent[it] = ref
                self.ent_items.setdefault(ref, []).append((it, "emesh", fill))
            elif kind == "wall":
                it = create(flat, fill=RED, outline=RED, stipple="gray25", width=1)
                self.item_brush[it] = ref
                self.brush_items.setdefault(ref, []).append((it, fill))
            elif kind == "point":
                x, y = flat
                r = 5
                it = cv.create_oval(x - r, y - r, x + r, y + r, fill=fill, outline="#000000", width=1)
                self.item_ent[it] = ref
                self.ent_items.setdefault(ref, []).append((it, "point", fill))
            else:
                it = create(flat, fill=fill, outline=fill, stipple="gray25", width=1)
                self.item_ent[it] = ref
                self.ent_items.setdefault(ref, []).append((it, kind, fill))
        if self.mode == "walls":
            for i in self.shown:
                self.paint(i)
        else:
            for idx in self.sel:
                self.paint_ent(idx)
        self.hover = None
        self.count()
        self._t_draw = time.perf_counter() - t_start
        self._n_polys = len(polys)

    def prop_polys(self, polys, cut, iso, L, zspan):
        """add the props' triangles (or plain boxes when small on screen); returns entities drawn this way"""
        cw, ch = max(self.cv.winfo_width(), 400), max(self.cv.winfo_height(), 300)
        s, (tx, ty) = self.S, self.T
        rot, proj = self.rot_xy, self.proj
        items = [(p, "leaf" if p.mesh.leafy else "prop", None) for p in self.static_placed]
        ent_mode = self.mode in ENT_MODES
        for idx, e in enumerate(self.ents):
            if e.get("model")[-4:].lower() != ".mdl":
                continue
            p = self.placed_for(e)
            if p is not None:
                items.append((p, "emesh" if ent_mode and self.ent_matches(e) else "prop", idx))
        meshed = set()
        zlo, L0, L1, L2 = self.zlo, L[0], L[1], L[2]
        for p, kind, idx in items:
            mn, mx = p.mins, p.maxs
            if mn[2] > cut:
                continue
            dx, dy, dz = mx[0] - mn[0], mx[1] - mn[1], mx[2] - mn[2]
            size = (dx * dx + dy * dy + dz * dz) ** 0.5 * s
            cx, cy = proj(mn[0] + dx / 2, mn[1] + dy / 2, mn[2] + dz / 2)
            sx, sy = cx * s + tx, cy * s + ty
            if size < 4 or sx < -size or sy < -size or sx > cw + size or sy > ch + size:
                continue
            if size < 40 and mx[2] <= cut:                   # small on screen: one flat outline
                hull = convex_hull([proj(*q) for q in p.corners])
                c = p.boxc
                if iso:
                    key = (0, -sum(rot(mn[0] + dx / 2, mn[1] + dy / 2)) + mn[2] + dz / 2)
                    light = 0.85
                else:
                    key = (0, mx[2])
                    light = 0.85 * (0.65 + 0.35 * min(1.0, max(0.0, (mx[2] - zlo) / zspan)))
                polys.append((key, hull, "#%02x%02x%02x" % (min(255, int(c[0] * light * 255)), min(
                    255, int(c[1] * light * 255)), min(255, int(c[2] * light * 255))), kind, idx))
                if kind == "emesh":
                    meshed.add(idx)
                    self.area[idx] = dx * dy
                continue
            src = p.box if size < 40 else (p.lite if size < 150 else p.full)
            clipped = mx[2] > cut
            for pts, n, rgb in src:
                nx, ny = rot(n[0], n[1])
                nz = n[2]
                if iso:
                    if -nx - ny + nz <= 0.02:
                        continue
                elif nz < 0.05:
                    continue
                if clipped and max(q[2] for q in pts) > cut:
                    pts = clip_below(pts, cut)
                    if len(pts) < 3:
                        continue
                zavg = sum(q[2] for q in pts) / len(pts)
                light = 0.42 + 0.58 * max(0.0, nx * L0 + ny * L1 + nz * L2)
                if iso:
                    key = (0, sum(-a - b for a, b in (rot(q[0], q[1]) for q in pts)) / len(pts) + zavg)
                else:
                    light *= 0.65 + 0.35 * min(1.0, max(0.0, (zavg - zlo) / zspan))
                    key = (0, zavg)
                col = "#%02x%02x%02x" % (min(255, int(rgb[0] * light * 255)), min(255, int(rgb[1] * light * 255)),
                                         min(255, int(rgb[2] * light * 255)))
                polys.append((key, [proj(*q) for q in pts], col, kind, idx))
            if kind == "emesh":
                meshed.add(idx)
                self.area[idx] = dx * dy
        return meshed

    def box_faces(self, mn, mx, cut):
        top = min(mx[2], cut)
        a = self.rot_xy(mn[0], mn[1])
        b = self.rot_xy(mx[0], mx[1])
        x0, x1, y0, y1 = min(a[0], b[0]), max(a[0], b[0]), min(a[1], b[1]), max(a[1], b[1])
        P = lambda x, y, z: self.proj(*self.unrot_xy(x, y), z)
        if self.view == "iso":
            key = (1, -(x0 + y0))
            return [("side", [P(x0, y0, mn[2]), P(x1, y0, mn[2]), P(x1, y0, top), P(x0, y0, top)], key),
                    ("side", [P(x0, y0, mn[2]), P(x0, y1, mn[2]), P(x0, y1, top), P(x0, y0, top)], key),
                    ("top", [P(x0, y0, top), P(x1, y0, top), P(x1, y1, top), P(x0, y1, top)], key)]
        return [("top", [P(x0, y0, top), P(x1, y0, top), P(x1, y1, top), P(x0, y1, top)],
                 (1, -(mx[0] - mn[0]) * (mx[1] - mn[1])))]

    def ent_shape(self, e):
        """(mins, maxs, is_point) in world space, or None"""
        o = e.origin()
        model = e.get("model")
        if model.startswith("*"):
            try:
                mn, mx = self.models[int(model[1:])]
            except (ValueError, IndexError):
                return None
            return (tuple(mn[i] + o[i] for i in range(3)), tuple(mx[i] + o[i] for i in range(3)), False)
        if e.get("mins") and e.get("maxs"):
            try:
                a = [float(v) for v in e.get("mins").split()[:3]]
                b = [float(v) for v in e.get("maxs").split()[:3]]
                return (tuple(o[i] + a[i] for i in range(3)), tuple(o[i] + b[i] for i in range(3)), False)
            except ValueError:
                pass
        if not e.get("origin"):
            return None
        return (o, o, True)

    def ent_matches(self, e):
        if self.mode == "build":
            return category(e.get("classname"))[0] in BUILD_CATS
        cat = self.cat_var.get()
        if cat != "All" and category(e.get("classname"))[0] != cat:
            return False
        q = self.search.get().strip().lower()
        if q:
            hay = " ".join((e.get("classname"), e.get("targetname"), e.get("model"), e.get("hammerid"))).lower()
            return all(w in hay for w in q.split())
        return True

    def visible_ents(self, cut):
        out = {}
        for idx, e in enumerate(self.ents):
            if e.get("classname").lower() == "worldspawn" or not self.ent_matches(e):
                continue
            shape = self.ent_shape(e)
            if not shape or shape[0][2] > cut:
                continue
            out[idx] = (shape[0], shape[1], shape[2], category(e.get("classname"))[1])
        return out

    def paint(self, i, hover=False):
        on = i in self.picked
        for it, part in self.brush_items.get(i, []):
            col = (GREEN if part == "top" else GREEN_SIDE) if on else (RED if part == "top" else RED_SIDE)
            self.cv.itemconfig(it, fill=col, outline=HOVER if hover else (GREEN if on else RED),
                               stipple="gray50" if on else "gray25", width=2 if (hover or on) else 1)

    def paint_ent(self, idx, hover=False):
        on = idx in self.sel
        for it, kind, color in self.ent_items.get(idx, []):
            if kind == "emesh":
                self.cv.itemconfig(it, outline=SELECT if (on or hover) else "", width=1)
            elif kind == "point":
                self.cv.itemconfig(it, outline=SELECT if (on or hover) else "#000000", width=3 if on else (2 if hover else 1))
            else:
                self.cv.itemconfig(it, outline=SELECT if (on or hover) else color, width=3 if on else (2 if hover else 1),
                                   stipple="gray50" if on else "gray25")
            if on:
                self.cv.tag_raise(it)

    def count(self):
        if self.mode == "walls":
            hidden = len(self.clips) - len(self.shown)
            self.status.config(text="  %d of %d walls will be removed%s" % (
                len(self.picked), len(self.clips), ("   (%d hidden by the height slider)" % hidden) if hidden else ""))
        else:
            self.status.config(text="  " + (self.place_hint() if self.placing else
                                            "%d selected   -   %s" % (len(self.sel), self.change_text())))

    def change_text(self):
        deleted, modified, added = entity_changes(self.base_ents, self.ents)
        if not (deleted or modified or added):
            return "no changes yet"
        return "%d deleted, %d changed, %d added" % (len(deleted), len(modified), len(added))

    # ---- mouse
    def under(self, e):
        x, y = self.cv.canvasx(e.x), self.cv.canvasy(e.y)
        items = self.cv.find_overlapping(x - 2, y - 2, x + 2, y + 2)
        if self.mode == "walls":
            hits = {self.item_brush[it] for it in items if it in self.item_brush}
        else:
            hits = {self.item_ent[it] for it in items if it in self.item_ent}
        return min(hits, key=lambda i: self.area.get(i, 0)) if hits else None

    def on_move(self, e):
        if not self.m:
            return
        if self.placing:
            p = self.surface_point(e)
            self.status.config(text="  " + self.place_hint() + (
                "   at %d %d %d" % p if p else "   (point at a floor or wall)"))
            self.draw_ghost(p)
            return
        i = self.under(e)
        if self.hover is not None and self.hover != i:
            self.paint(self.hover) if self.mode == "walls" else self.paint_ent(self.hover)
        self.hover = i
        if i is None:
            self.count()
            return
        if self.mode == "walls":
            self.paint(i, hover=True)
            mn, mx = self.clips[i]
            self.status.config(text="  wall %d   x %d..%d   y %d..%d   height %d..%d   %s" % (
                i, mn[0], mx[0], mn[1], mx[1], mn[2], mx[2], "REMOVED" if i in self.picked else "kept"))
        else:
            self.paint_ent(i, hover=True)
            ent = self.ents[i]
            self.status.config(text="  %s   %s   origin %s   %s" % (
                ent.get("classname"), ent.get("targetname") or "(no name)", ent.get("origin") or "-",
                ("hammerid " + ent.get("hammerid")) if ent.get("hammerid") else ""))

    def on_click(self, e, add=False):
        if not self.m:
            return
        if self.placing:
            self.place_at(e)
            return
        i = self.under(e)
        if self.mode == "walls":
            if i is not None:
                self.picked ^= {i}
                self.on_move(e)
            return
        if i is None:
            if not add:
                self.select(set())
            return
        if add:
            self.select(self.sel ^ {i})
        elif i not in self.sel:
            self.select({i})
        if self.view == "top" and i in self.sel:
            self.dragging = {"start": (e.x, e.y), "moved": False}

    def drag_entity(self, e):
        if not self.dragging or self.mode not in ENT_MODES:
            return
        dx, dy = e.x - self.dragging["start"][0], e.y - self.dragging["start"][1]
        if not self.dragging["moved"] and abs(dx) + abs(dy) < 4:
            return
        self.dragging["moved"] = True
        last = self.dragging.get("last", self.dragging["start"])
        for idx in self.sel:
            for it, _k, _c in self.ent_items.get(idx, []):
                self.cv.move(it, e.x - last[0], e.y - last[1])
        self.dragging["last"] = (e.x, e.y)

    def drop_entity(self, e):
        d, self.dragging = self.dragging, None
        if not d or not d["moved"] or self.mode not in ENT_MODES:
            return
        rx = (e.x - d["start"][0]) / self.S
        ry = -(e.y - d["start"][1]) / self.S
        wx, wy = self.unrot_xy(rx, ry)
        self.move_sel(round(wx), round(wy), 0)

    def box_start(self, e):
        self.box["p"] = (self.cv.canvasx(e.x), self.cv.canvasy(e.y))
        self.box["r"] = self.cv.create_rectangle(*self.box["p"], *self.box["p"], outline=HOVER, dash=(4, 3))

    def box_move(self, e):
        if "r" in self.box:
            self.cv.coords(self.box["r"], *self.box["p"], self.cv.canvasx(e.x), self.cv.canvasy(e.y))

    def box_end(self, e):
        if "r" not in self.box:
            return
        rid = self.box.pop("r")
        box = self.cv.coords(rid)
        self.cv.delete(rid)
        inside = self.cv.find_enclosed(*box)
        if self.mode == "walls":
            for i in {self.item_brush[it] for it in inside if it in self.item_brush}:
                self.picked.add(i)
                self.paint(i)
            self.count()
        else:
            self.select(self.sel | {self.item_ent[it] for it in inside if it in self.item_ent})

    def set_all(self, on):
        if not self.m or self.mode != "walls":
            return
        if on:
            self.picked |= set(self.shown)
        else:
            self.picked.clear()
        for i in self.shown:
            self.paint(i)
        self.count()

    # ---- entity list and properties
    def _filter_changed(self):
        if self.m:
            self.fill_list()
            if self.mode in ENT_MODES:
                self.redraw()

    def fill_list(self):
        self.lst.delete(*self.lst.get_children())
        for idx, e in enumerate(self.ents):
            if e.get("classname").lower() == "worldspawn" or not self.ent_matches(e):
                continue
            self.lst.insert("", "end", iid=str(idx), values=(e.get("classname"), e.get("targetname")))
        self._sync_list()

    def _sync_list(self):
        self._syncing = True
        ids = [str(i) for i in self.sel if self.lst.exists(str(i))]
        self.lst.selection_set(ids)
        if ids:
            self.lst.see(ids[0])
        self.root.after_idle(lambda: setattr(self, "_syncing", False))

    def _list_selected(self, _e):
        if getattr(self, "_syncing", False):
            return
        self.select({int(i) for i in self.lst.selection()}, from_list=True)

    def select(self, idxs, from_list=False):
        old = self.sel
        self.sel = set(idxs)
        for i in old | self.sel:
            self.paint_ent(i)
        if not from_list:
            self._sync_list()
        self.show_props()
        self.count()

    def show_props(self):
        if self.mode == "build" and self.m:
            self.update_build_info()
        self.props.delete(*self.props.get_children())
        if len(self.sel) != 1:
            if self.sel:
                self.props.insert("", "end", iid="multi", values=("", "%d entities selected" % len(self.sel)))
            return
        e = self.ents[next(iter(self.sel))]
        for k, (key, value) in enumerate(e.pairs):
            self.props.insert("", "end", iid=str(k), values=(key, value.replace(OUTPUT_SEP, ",")))

    def _edit_value(self, e):
        row = self.props.identify_row(e.y)
        if not row or len(self.sel) != 1 or row == "multi":
            return
        idx = next(iter(self.sel))
        k = int(row)
        key, value = self.ents[idx].pairs[k]
        x, y, w, h = self.props.bbox(row, "v")
        ent = self.tk.Entry(self.props)
        ent.insert(0, value.replace(OUTPUT_SEP, ","))
        ent.place(x=x, y=y, width=w, height=h)
        ent.focus_set()
        ent.select_range(0, "end")

        def commit(_=None):
            new = ent.get()
            ent.destroy()
            if '"' in new:
                self.mb.showerror("L4D2 Map Editor", "Values can't contain the \" character.")
                return
            if OUTPUT_SEP in value:
                new = new.replace(",", OUTPUT_SEP)
            if new != value:
                pairs = list(self.ents[idx].pairs)
                pairs[k] = (key, new)
                self.change({idx: Ent(pairs)})
        ent.bind("<Return>", commit)
        ent.bind("<FocusOut>", commit)
        ent.bind("<Escape>", lambda _e: ent.destroy())

    def add_key(self):
        if len(self.sel) != 1:
            return
        from tkinter import simpledialog
        key = simpledialog.askstring("Add key", "Key name:", parent=self.root)
        if not key:
            return
        value = simpledialog.askstring("Add key", "Value for %s:" % key, parent=self.root) or ""
        if '"' in key + value:
            self.mb.showerror("L4D2 Map Editor", "Keys and values can't contain the \" character.")
            return
        idx = next(iter(self.sel))
        self.change({idx: Ent(self.ents[idx].pairs + ((key, value),))})

    def remove_key(self):
        rows = self.props.selection()
        if len(self.sel) != 1 or not rows or rows[0] == "multi":
            return
        idx = next(iter(self.sel))
        key = self.ents[idx].pairs[int(rows[0])][0]
        if key.lower() == "classname":
            self.mb.showerror("L4D2 Map Editor", "Every entity needs a classname.")
            return
        self.change({idx: self.ents[idx].without(int(rows[0]))})

    # ---- editing (every change goes through here, so undo always works)
    def snapshot(self):
        return (list(self.ents), set(self.sel))

    def change(self, replace=None, delete=(), add=(), select=None):
        self.undo_stack.append(self.snapshot())
        self.redo_stack.clear()
        ents = list(self.ents)
        for idx, e in (replace or {}).items():
            ents[idx] = e
        gone = set(delete)
        keep = [i for i in range(len(ents)) if i not in gone]
        remap = {old: new for new, old in enumerate(keep)}
        ents = [ents[i] for i in keep] + list(add)
        self.ents = ents
        if select is None:
            select = {remap[i] for i in self.sel if i in remap}
        self.sel = set(select)
        self.fill_list()
        self.show_props()
        self.redraw()

    def undo(self):
        if self.undo_stack:
            self.redo_stack.append(self.snapshot())
            self.ents, self.sel = self.undo_stack.pop()
            self.fill_list(); self.show_props(); self.redraw()

    def redo(self):
        if self.redo_stack:
            self.undo_stack.append(self.snapshot())
            self.ents, self.sel = self.redo_stack.pop()
            self.fill_list(); self.show_props(); self.redraw()

    def delete_sel(self):
        if not self.sel:
            return
        names = [self.ents[i].get("targetname") for i in self.sel]
        self.change(delete=self.sel, select=set())
        refs = broken_references(self.ents, names)
        if refs:
            self.status.config(text="  Deleted. Heads up: %d output(s) still point at it, e.g. %s -> %s (%s)" % (
                len(refs), refs[0][0].get("targetname") or refs[0][0].get("classname"), refs[0][2], refs[0][1]))

    def next_hammerid(self):
        ids = [int(e.get("hammerid")) for e in self.ents if e.get("hammerid").isdigit()]
        return max(ids + [0]) + 1

    def fresh_copy(self, e, offset):
        hid = self.next_hammerid()
        new = e.with_value("hammerid", str(hid))
        if offset:
            x, y, z = e.origin()
            new = new.with_value("origin", "%g %g %g" % (x + offset[0], y + offset[1], z + offset[2]))
        return new

    def duplicate_sel(self):
        if not self.sel:
            return
        added, n = [], len(self.ents)
        for k, i in enumerate(sorted(self.sel)):
            e = self.fresh_copy(self.ents[i], (32, 32, 0))
            e = e.with_value("hammerid", str(self.next_hammerid() + k))
            added.append(e)
        self.change(add=added, select=set(range(n, n + len(added))))

    def move_sel(self, dx, dy, dz):
        if not self.sel or not (dx or dy or dz):
            return
        rep = {}
        for i in self.sel:
            x, y, z = self.ents[i].origin()
            rep[i] = self.ents[i].with_value("origin", "%g %g %g" % (x + dx, y + dy, z + dz))
        self.change(replace=rep)

    def nudge(self, d, step):
        if d[2]:
            self.move_sel(0, 0, d[2] * step)
        else:
            wx, wy = self.unrot_xy(d[0], d[1])
            self.move_sel(wx * step, wy * step, 0)

    # ---- placing new entities
    def start_place(self, template):
        if self.mode not in ENT_MODES or not self.m:
            return
        if template is None and len(self.sel) != 1:
            self.mb.showinfo("L4D2 Map Editor", "Select one entity first (a ladder, a prop...), then pick this.")
            return
        self.placing = template or ("copy", next(iter(self.sel)))
        self.cv.config(cursor="crosshair")
        self.count()

    def cancel_place(self):
        self.cv.delete("ghost")
        if self.placing:
            self.placing = None
            self.cv.config(cursor="")
            self.count()

    def place_hint(self):
        if isinstance(self.placing, str):
            what = self.placing
        elif self.placing[0] == "model":
            what = self.placing[1].split("/")[-1][:-4] + " (press R to turn it after placing)"
        else:
            what = "a copy of " + self.ents[self.placing[1]].get("classname")
        return "Click where to put %s. Esc cancels." % what

    def model_offset(self, mesh, yaw):
        """how high above the floor point the origin goes so the model stands on it"""
        return -Placed(mesh, (0, 0, 0), (0, yaw, 0)).mins[2]

    def draw_ghost(self, p):
        """outline of the model under the mouse while placing it"""
        self.cv.delete("ghost")
        if not p or not self.placing or isinstance(self.placing, str) or self.placing[0] != "model":
            return
        mesh = self.mesh(self.placing[1])
        if not mesh:
            return
        ghost = Placed(mesh, (p[0], p[1], p[2] + self.model_offset(mesh, self.build_yaw)), (0, self.build_yaw, 0))
        s, (tx, ty) = self.S, self.T
        for q, _n, _c in ghost.box:
            flat = [c for pt in q for c in (lambda x, y: (x * s + tx, y * s + ty))(*self.proj(*pt))]
            self.cv.create_polygon(flat, fill="", outline=SELECT, dash=(3, 2), tags="ghost")

    def surface_point(self, e):
        """the world point under the mouse, on the map surface you see there"""
        x, y = self.cv.canvasx(e.x), self.cv.canvasy(e.y)
        faces = [it for it in self.cv.find_overlapping(x, y, x, y) if it in self.item_face]
        if not faces:
            return None
        pts, n, dist, _ = self.faces[self.item_face[faces[-1]]]     # topmost = nearest
        X, Y = (x - self.T[0]) / self.S, (y - self.T[1]) / self.S
        nxr, nyr = self.rot_xy(n[0], n[1])
        nz = n[2]
        if self.view == "top":
            if abs(nz) < 1e-3:
                return None
            xr, yr = X, -Y
            z = (dist - nxr * xr - nyr * yr) / nz
        else:
            u, v = X / 0.866, -Y
            den = nxr + nyr - nz
            if abs(den) < 1e-6:
                return None
            yr = (dist - nxr * u - nz * (v - u / 2)) / den
            xr = yr + u
            z = v - yr - u / 2
        wx, wy = self.unrot_xy(xr, yr)
        return round(wx), round(wy), round(z)

    def place_at(self, e):
        p = self.surface_point(e)
        if not p:
            return
        kind = self.placing
        self.cancel_place()
        n = len(self.ents)
        if isinstance(kind, str):
            new = Ent(TEMPLATES[kind]).with_value("origin", "%d %d %d" % p)
            new = new.with_value("hammerid", str(self.next_hammerid()))
        elif kind[0] == "model":
            mesh = self.mesh(kind[1])
            if not mesh:
                self.mb.showerror("L4D2 Map Editor", "Can't read the model %s." % kind[1])
                return
            z = p[2] + self.model_offset(mesh, self.build_yaw)
            new = Ent([("classname", "prop_dynamic"), ("hammerid", str(self.next_hammerid())),
                       ("origin", "%d %d %g" % (p[0], p[1], round(z, 1))), ("angles", "0 %g 0" % self.build_yaw),
                       ("model", kind[1]), ("solid", "6"), ("disableshadows", "1")])
        else:
            src = self.ents[kind[1]]
            shape = self.ent_shape(src)
            o = src.origin()
            if shape and not shape[2]:          # brush entity: land the bottom-center of its box on the point
                mn, mx = shape[0], shape[1]
                c = ((mn[0] + mx[0]) / 2, (mn[1] + mx[1]) / 2, mn[2])
                off = (p[0] - c[0], p[1] - c[1], p[2] - c[2])
            else:
                off = (p[0] - o[0], p[1] - o[1], p[2] - o[2])
            new = self.fresh_copy(src, off)
        self.change(add=[new], select={n})

    # ---- build mode
    def _lib_changed(self):
        if getattr(self, "_lib_after", None):
            self.root.after_cancel(self._lib_after)
        self._lib_after = self.root.after(150, self.fill_library)

    def fill_library(self):
        self._lib_after = None
        words = self.lib_search.get().lower().split()
        keys = dict(LIB_CATS).get(self.lib_cat.get(), ())
        self.lib.delete(*self.lib.get_children())
        shown = 0
        for path in self.library:
            if keys and not any(k in path for k in keys):
                continue
            if words and not all(w in path for w in words):
                continue
            folder, _, name = path[7:].rpartition("/")
            self.lib.insert("", "end", iid=path, values=(name[:-4], folder))
            shown += 1
        if not self.library:
            self.preview_info.config(text="Open a map to see the props you can place. The props come from your "
                                          "Left 4 Dead 2 folder.")

    def show_preview(self):
        sel = self.lib.selection()
        pv = self.preview
        pv.delete("all")
        if not sel:
            return
        name = sel[0]
        mesh = self.meshes.get(("preview", name))
        if mesh is None:
            mesh = load_mesh(self.files, name, self.mat_cache, limit=700) if self.files else None
            self.meshes[("preview", name)] = mesh or False
        if not mesh:
            self.preview_info.config(text="%s\nCan't read this model." % name)
            return
        pv.update_idletasks()
        w, h = max(pv.winfo_width(), 200), max(pv.winfo_height(), 120)
        L = (-0.42, -0.58, 0.70)
        pr = lambda x, y, z: ((x - y) * 0.866, -((x + y) * 0.5 + z))
        polys = []
        for (a, b, c), col in zip(mesh.tris, mesh.cols):
            pts = (mesh.verts[a], mesh.verts[b], mesh.verts[c])
            n = _cross(*pts)
            ln = (n[0] * n[0] + n[1] * n[1] + n[2] * n[2]) ** 0.5 or 1.0
            n = (n[0] / ln, n[1] / ln, n[2] / ln)
            if -n[0] - n[1] + n[2] <= 0.0:
                continue
            light = 0.42 + 0.58 * max(0.0, n[0] * L[0] + n[1] * L[1] + n[2] * L[2])
            key = sum(-p[0] - p[1] + p[2] for p in pts)
            polys.append((key, [pr(*p) for p in pts], "#%02x%02x%02x" % tuple(min(255, int(k * light * 255))
                                                                               for k in col)))
        xs = [x for _k, ps, _c in polys for x, _y in ps] or [0, 1]
        ys = [y for _k, ps, _c in polys for _x, y in ps] or [0, 1]
        sc = min((w - 20) / max(max(xs) - min(xs), 1), (h - 20) / max(max(ys) - min(ys), 1))
        ox = (w - (max(xs) - min(xs)) * sc) / 2 - min(xs) * sc
        oy = (h - (max(ys) - min(ys)) * sc) / 2 - min(ys) * sc
        for _k, ps, col in sorted(polys, key=lambda p: p[0]):
            pv.create_polygon([c for x, y in ps for c in (x * sc + ox, y * sc + oy)], fill=col, outline="")
        size = [mesh.maxs[k] - mesh.mins[k] for k in range(3)]
        info = "%d x %d x %d units (a survivor is 72 high)  -  " % (size[0], size[1], size[2])
        info += "solid" if mesh.solid else "NOT solid: add an invisible wall around it"
        self.preview_info.config(text=info)

    def place_from_library(self):
        sel = self.lib.selection()
        if not sel or not self.m:
            if not self.m:
                self.mb.showinfo("L4D2 Map Editor", "Open a map first.")
            return
        if self.mode != "build":
            self.mode_var.set("build")
            self.set_mode()
        self.placing = ("model", sel[0])
        self.cv.config(cursor="crosshair")
        self.count()

    def sel_models(self):
        """selected entities that are models: [(index, entity, Placed)]"""
        out = []
        for i in sorted(self.sel):
            e = self.ents[i]
            p = self.placed_for(e)
            if p is not None:
                out.append((i, e, p))
        return out

    def rotate_sel(self, deg):
        items = self.sel_models()
        if not items:
            self.status.config(text="  Select a prop first (click it on the map), then turn it.")
            return
        rep = {}
        for i, e, _p in items:
            pitch, yaw, roll = parse_vec(e.get("angles"))
            yaw = (yaw + deg) % 360
            rep[i] = e.with_value("angles", "%g %g %g" % (pitch, round(yaw, 3), roll))
            self.build_yaw = yaw
        self.change(replace=rep)

    def floor_at(self, x, y, below):
        """height of the highest floor under (x, y) that is lower than `below`, or None"""
        if self.floor_faces is None:
            ff = []
            for pts, n, dist, _rgb in self.faces:
                if n[2] > 0.5:
                    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
                    ff.append((min(xs), max(xs), min(ys), max(ys), pts, n, dist))
            self.floor_faces = ff
        best = None
        for x0, x1, y0, y1, pts, n, dist in self.floor_faces:
            if not (x0 <= x <= x1 and y0 <= y <= y1):
                continue
            inside = False
            for j in range(len(pts)):
                (ax, ay, _), (bx, by, _) = pts[j - 1], pts[j]
                if (ay > y) != (by > y) and x < ax + (y - ay) * (bx - ax) / (by - ay):
                    inside = not inside
            if not inside:
                continue
            z = (dist - n[0] * x - n[1] * y) / n[2]
            if z <= below and (best is None or z > best):
                best = z
        return best

    def drop_sel(self):
        items = self.sel_models()
        if not items:
            self.status.config(text="  Select a prop first, then drop it to the floor.")
            return
        rep = {}
        for i, e, p in items:
            x, y, z = e.origin()
            f = self.floor_at(x, y, p.mins[2] + 8)
            if f is not None:
                rep[i] = e.with_value("origin", "%g %g %g" % (x, y, round(z - (p.mins[2] - f), 1)))
        if rep:
            self.change(replace=rep)
        else:
            self.status.config(text="  No floor found under it.")

    def line_sel(self, direction):
        items = self.sel_models()
        if len(items) != 1:
            self.status.config(text="  Select one prop, then add the next one in line.")
            return
        i, e, p = items[0]
        mesh = p.mesh
        ext = [mesh.maxs[k] - mesh.mins[k] for k in range(3)]
        axis = 0 if ext[0] >= ext[1] else 1                  # along the prop's longest side
        f, l, _u = angle_matrix(*parse_vec(e.get("angles")))
        d = (f, l)[axis]
        try:
            scale = float(e.get("modelscale") or 1) or 1.0
        except ValueError:
            scale = 1.0
        step = ext[axis] * scale * direction
        new = self.fresh_copy(e, (d[0] * step, d[1] * step, d[2] * step))
        self.change(add=[new], select={len(self.ents)})

    def block_sel(self, extra):
        items = self.sel_models()
        if not items:
            self.status.config(text="  Select a prop first, then add an invisible wall around it.")
            return
        added, hid = [], self.next_hammerid()
        for k, (_i, _e, p) in enumerate(items):
            mn, mx = p.mins, p.maxs
            cx, cy = round((mn[0] + mx[0]) / 2), round((mn[1] + mx[1]) / 2)
            lo = (round(mn[0] - cx), round(mn[1] - cy), 0)
            hi = (round(mx[0] - cx), round(mx[1] - cy), round(mx[2] - mn[2] + extra))
            v = lambda t: "%d %d %d" % t
            new = Ent(TEMPLATES["Invisible wall (blocks everyone)"])
            for key, val in (("origin", "%d %d %d" % (cx, cy, math.floor(mn[2]))), ("mins", v(lo)), ("maxs", v(hi)),
                             ("boxmins", v(lo)), ("boxmaxs", v(hi)), ("hammerid", str(hid + k))):
                new = new.with_value(key, val)
            added.append(new)
        self.change(add=added)
        self.status.config(text="  Added %d invisible wall(s). They are boxes lined up with the map, so a turned "
                                "prop gets a slightly bigger wall." % len(added))

    def update_build_info(self):
        items = self.sel_models()
        if not self.sel:
            txt = "Click a prop on the map to select it.  R turns it, arrows move it."
        elif not items:
            txt = "%d selected (not a prop)." % len(self.sel)
        elif len(items) == 1:
            i, e, p = items[0]
            sz = [round(p.mesh.maxs[k] - p.mesh.mins[k]) for k in range(3)]
            txt = "%s   %d x %d x %d   turned %g%s" % (
                e.get("model").split("/")[-1][:-4], sz[0], sz[1], sz[2], parse_vec(e.get("angles"))[1],
                "" if p.mesh.solid else "   (not solid)")
        else:
            txt = "%d props selected." % len(items)
        self.build_info.config(text=txt)

    # ---- loading and saving
    def load_lmp(self):
        if not self.m:
            return
        path = self.fd.askopenfilename(title="Load a .lmp you saved for %s" % self.m.name,
                                       filetypes=[("Lump file", "*.lmp")], initialdir=self.save_dir)
        if not path:
            return
        try:
            raw = open(path, "rb").read()
            o, lid, _v, length, _r = lmp_header(raw)
            body = raw[o:o + length]
        except (OSError, struct.error) as e:
            self.mb.showerror("L4D2 Map Editor", "Can't read it: %s" % e)
            return
        self.save_dir = os.path.dirname(path)
        if lid == LUMP_BRUSHES:
            if len(body) != len(self.m.brushes):
                self.mb.showerror("L4D2 Map Editor", "That walls patch doesn't match this map.")
                return
            picked = set()
            for i in self.clips:
                c, = struct.unpack_from("<i", body, i * 12 + 8)
                if not c & PLAYERCLIP:
                    picked.add(i)
            self.picked = picked
            self.mode_var.set("walls")
        elif lid == LUMP_ENTITIES:
            try:
                _p, ents, _nl = parse_entities(body)
            except ValueError as e:
                self.mb.showerror("L4D2 Map Editor", "Can't read the entities in it: %s" % e)
                return
            self.undo_stack.append(self.snapshot())
            self.ents, self.sel = ents, set()
            self.mode_var.set("ents")
        else:
            self.mb.showerror("L4D2 Map Editor", "That .lmp patches lump %d, which this tool doesn't edit." % lid)
            return
        self.set_mode()
        self.fill_list()
        self.redraw()
        self.mb.showinfo("L4D2 Map Editor", "Loaded %s. Keep editing, then Save .lmp to update it."
                         % os.path.basename(path))

    def save(self):
        if not self.m:
            return
        m = self.m
        if self.mode == "walls":
            if not self.picked:
                self.mb.showinfo("L4D2 Map Editor", "No walls picked yet.")
                return
            lump_id, what = LUMP_BRUSHES, "%d walls removed" % len(self.picked)
        else:
            deleted, modified, added = entity_changes(self.base_ents, self.ents)
            if not (deleted or modified or added):
                self.mb.showinfo("L4D2 Map Editor", "No entity changes yet.")
                return
            bad = [e for e in self.ents if not e.get("classname")]
            if bad:
                self.mb.showerror("L4D2 Map Editor", "%d entities have no classname. Fix or delete them first." % len(bad))
                return
            lump_id, what = LUMP_ENTITIES, self.change_text()
        folder = self.fd.askdirectory(title="Pick your addon's maps folder (addons/<addon>/maps)",
                                      initialdir=self.save_dir or os.path.dirname(m.path), mustexist=False)
        if not folder:
            return
        if not os.path.isdir(folder):
            os.makedirs(folder)
        plan = m.plan(lump_id, folder)
        files, warnings = [], []
        for p, n, path, over in plan:
            if lump_id == LUMP_BRUSHES:
                base, _ = m.base_lump(p, LUMP_BRUSHES)
                data = brush_patch(base, self.picked)
            else:
                base_raw, _ = m.base_lump(p, LUMP_ENTITIES)
                if p == "l":
                    data = dump_entities(self.ent_prefix, self.ents, self.ent_nl)
                else:
                    pre, vents, nl = parse_entities(base_raw)
                    out, missing = apply_changes(vents, deleted, modified, added)
                    if missing:
                        warnings.append("_%s_ version: %d change(s) are for entities that version doesn't have, "
                                        "so they were skipped there." % (p, len(missing)))
                    data = dump_entities(pre, out, nl)
            files.append((path, over, data))
        if lump_id == LUMP_ENTITIES:
            names = [self.base_ents[keyed(self.base_ents)[k][0]].get("targetname") for k in deleted]
            refs = broken_references(self.ents, names)
            if refs:
                warnings.append("%d output(s) still point at deleted entities (e.g. %s -> %s). The game just "
                                "ignores them, but check it's what you want." % (
                                    len(refs), refs[0][0].get("targetname") or refs[0][0].get("classname"),
                                    refs[0][2]))
            solid_new = [e for e in added if e.get("classname").lower().startswith(
                ("prop_", "env_physics_blocker", "env_player_blocker"))]
            if solid_new:
                warnings.append("You added %d prop(s) / invisible wall(s). Bots and infected only learn about "
                                "new obstacles from the nav mesh: test with bots, and if they get stuck, see "
                                "\"Bots won't follow\" in the guide." % len(solid_new))
            gone = sorted({e.get("model") for e in added if e.get("model").lower().endswith(".mdl")
                           and self.files and not self.files.has(e.get("model"))})
            if gone:
                warnings.append("%d added prop(s) use a model the game doesn't have (%s): they would show as "
                                "an error model." % (len(gone), gone[0]))
        msg = "%s\n\nWrite to %s:\n%s" % (what, folder, "\n".join(
            "   %s%s" % (os.path.basename(f), "   (replaces your earlier one)" if over else "")
            for f, over, _ in files))
        if warnings:
            msg += "\n\nNote:\n" + "\n".join("   " + w for w in warnings)
        if not self.mb.askyesno("Save .lmp", msg):
            return
        version = lump_version(m.d, lump_id)
        for f, _over, data in files:
            write_lmp_file(f, lump_id, version, m.revision, data)
        self.save_dir = folder
        self.mb.showinfo("L4D2 Map Editor", "Saved %d file(s) to %s.\nPack that folder into your addon .vpk as usual."
                         % (len(files), folder))


# ---------------------------------------------------------------- command line

def cli(args):
    inner = None
    if args[0].lower().endswith(".vpk") and args[1] not in ("write", "list"):
        inner, args = args[1], [args[0]] + args[2:]
    m = Map(args[0], inner)
    print(m.notes())
    clips = clip_brushes(m.d, m.brushes)
    if args[1] == "list":
        box = [float(v) for v in args[2:6]]
        for i, mn, mx in clips:
            if in_box(mn, mx, box):
                print("%5d  min %s  max %s" % (i, [round(v) for v in mn], [round(v) for v in mx]))
    elif args[1] == "write":
        out, rest = args[2], args[3:]
        valid = {i for i, _, _ in clips}
        if rest[:1] == ["all"]:
            pick = set(valid)
        elif rest[:1] == ["box"]:
            box = [float(v) for v in rest[1:5]]
            pick = {i for i, mn, mx in clips if in_box(mn, mx, box)}
        else:
            pick = {int(v) for v in rest}
        if pick - valid:
            sys.exit("not invisible walls: %s" % sorted(pick - valid))
        n = re.search(r"_l_(\d+)\.lmp$", out, re.I)
        if n and int(n.group(1)) in m.existing:
            print("warning: %s already uses _l_%s, use %s instead" % (m.existing[int(n.group(1))][0],
                                                                    n.group(1), m.out_name()))
        write_lmp(m, pick, out)
        print("wrote %s: %d walls removed" % (out, len(pick)))


if __name__ == "__main__":
    if sys.stdout is None:          # the windowed .exe has no console: keep command-line mode from crashing
        sys.stdout = sys.stderr = open(os.devnull, "w")
    if sys.argv[1:2] == ["check"]:
        print(report_text(check_setup(sys.argv[2] if len(sys.argv) > 2 else find_game())))
    elif len(sys.argv) > 2:
        try:
            cli(sys.argv[1:])
        except (ValueError, OSError) as e:
            sys.exit(str(e))
    else:
        App(sys.argv[1] if len(sys.argv) == 2 else None).root.mainloop()
