#!/usr/bin/env python3
"""filter_playlists.py -- give the XDJ-700 a browse "filter" with no firmware.

The stock 700 has no browse filter and its firmware carries none: no FILTER
message, no filter screen, nothing to switch on (docs/TRACK_FILTER_1.1.md).
What the deck *does* show natively is the playlist tree in the stick's
``PIONEER/rekordbox/export.pdb`` (BROWSE > PLAYLIST).  So this tool turns a
filter definition into real playlists on the stick:

    python tools/filter_playlists.py /path/to/stick --bpm 124-128 --rating 4
    python tools/filter_playlists.py /path/to/stick --key Am,F#m --colour red
    python tools/filter_playlists.py /path/to/stick --search "dub" --limit 40
    python tools/filter_playlists.py /path/to/stick --list

Filters can be combined (all of them must match).  Only rows are appended to
the playlist-tree and playlist-entry tables; every existing track, playlist
and entry is left exactly as it was.  ``export.pdb`` is backed up first
(``export.pdb.bak-filter-N``), and the result is read back with the
independent vendored parser before the tool reports success.

What this is not: the deck's own filter UI.  There is no category switch, no
"FILTER" screen, and my-tag filtering (the extension database
``exportExt.pdb`` has no writer yet).  What you get is a saved playlist per
filter, which behaves the same way in a set: BROWSE > PLAYLIST > your filter.
"""

from __future__ import annotations

import argparse
import re
import shutil
import struct
import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent / "vendor"))

from rekordbox_pdb import Database as PdbReader      # noqa: E402  (vendored)
from rekordbox_pdb.edit import PdbEditor            # noqa: E402  (vendored)

# rekordbox stores a 0-5 star rating one of two ways depending on the
# exporter: as the star count itself, or scaled to 0/51/102/153/204/255.
_RATING_SCALE_5 = 51


# ---------------------------------------------------------------------------
# Library context
# ---------------------------------------------------------------------------

@dataclass
class Library:
    db: PdbReader

    def __post_init__(self):
        self.keys = {k.id: (k.name or "") for k in self.db.keys}
        self.colours = {c.id: (c.name or "") for c in self.db.colors}
        self.genres = {g.id: (g.name or "") for g in self.db.genres}
        self.artists = {a.id: (getattr(a, "name", "") or "")
                        for a in self.db.artists}

    @property
    def rating_scale(self) -> int:
        """51 when the export uses the scaled 0..255 form, else 1."""
        peak = max((t.rating for t in self.db.tracks), default=0)
        return _RATING_SCALE_5 if peak > 5 else 1

    def stars(self, track) -> int:
        raw = track.rating
        if raw <= 0:
            return 0
        scale = self.rating_scale
        return max(0, min(5, round(raw / scale)))

    def key_name(self, track) -> str:
        return self.keys.get(track.key_id, "")

    def colour_name(self, track) -> str:
        return self.colours.get(track.color_id, "")

    def genre_name(self, track) -> str:
        return self.genres.get(track.genre_id, "")

    def artist_name(self, track) -> str:
        return self.artists.get(track.artist_id, "")

    def bpm(self, track) -> float:
        return track.tempo / 100.0


def open_library(pdb_path: Path) -> Library:
    return Library(PdbReader(pdb_path.read_bytes()))


# ---------------------------------------------------------------------------
# Filter specification
# ---------------------------------------------------------------------------

_BPM_RANGE = re.compile(
    r"^(?P<op>>=|<=|>|<)?\s*(?P<lo>\d+(?:\.\d+)?)\s*"
    r"(?:(?:-|\.\.|to)\s*(?P<hi>\d+(?:\.\d+)?))?$")


def parse_bpm(spec: str) -> tuple[float, float, str | None]:
    """'124-128' -> (124, 128, None); '>=124' -> (124, inf, '>=')."""
    text = spec.strip().lower().replace("bpm", "").strip()
    m = _BPM_RANGE.match(text)
    if not m:
        raise ValueError(f"cannot read the BPM filter {spec!r} "
                         "(try 124-128, >=124, <=128 or 126)")
    op = m.group("op")
    lo = float(m.group("lo"))
    hi = float(m.group("hi")) if m.group("hi") else None
    if op:
        if hi is not None:
            raise ValueError(f"{spec!r}: use either a range or an operator, "
                             "not both")
        return (lo, float("inf"), op) if op in (">=", ">") \
            else (0.0, lo, op)
    if hi is None:
        return lo, lo, None
    return (lo, hi, None) if lo <= hi else (hi, lo, None)


def _names_arg(values: list[str] | None) -> list[str]:
    out: list[str] = []
    for chunk in values or []:
        for piece in chunk.split(","):
            piece = piece.strip()
            if piece:
                out.append(piece)
    return out


@dataclass
class Filters:
    bpm: list[tuple[float, float, str | None]] = field(default_factory=list)
    keys: list[str] = field(default_factory=list)
    colours: list[str] = field(default_factory=list)
    genres: list[str] = field(default_factory=list)
    rating: int = 0
    search: str = ""
    limit: int = 0

    @classmethod
    def from_args(cls, a) -> "Filters":
        return cls(
            bpm=[parse_bpm(s) for s in (a.bpm or [])],
            keys=_names_arg(a.key),
            colours=_names_arg(a.colour),
            genres=_names_arg(a.genre),
            rating=a.rating or 0,
            search=(a.search or "").strip(),
            limit=a.limit or 0,
        )

    @property
    def empty(self) -> bool:
        return not (self.bpm or self.keys or self.colours or self.genres
                    or self.rating or self.search)

    # -- matching -----------------------------------------------------------

    def _bpm_ok(self, bpm: float) -> bool:
        if not self.bpm:
            return True
        for lo, hi, op in self.bpm:
            if op is None:
                if lo <= bpm <= hi:
                    return True
            elif op in (">=", ">"):
                if bpm > lo or (op == ">=" and bpm == lo):
                    return True
            elif op == "<":                     # half-open: lo <= bpm < hi
                if lo <= bpm < hi:
                    return True
            elif bpm < hi or (op == "<=" and bpm == hi):
                return True
        return False

    def _name_in(self, wanted: list[str], actual: str, ids: dict) -> bool:
        if not wanted:
            return True
        low = (actual or "").strip().lower()
        for w in wanted:
            wl = w.strip().lower()
            if wl.isdigit() and ids.get(int(wl)) is not None:
                if actual and low == (ids[int(wl)] or "").lower():
                    return True
                continue
            if wl and (low == wl or (len(wl) >= 3 and low.startswith(wl))):
                return True
        return False

    def matches(self, track, lib: Library) -> bool:
        if not self._bpm_ok(lib.bpm(track)):
            return False
        if self.keys and not self._name_in(self.keys, lib.key_name(track),
                                           lib.keys):
            return False
        if self.colours and not self._name_in(self.colours,
                                              lib.colour_name(track),
                                              lib.colours):
            return False
        if self.genres and not self._name_in(self.genres,
                                             lib.genre_name(track),
                                             lib.genres):
            return False
        if self.rating and lib.stars(track) < self.rating:
            return False
        if self.search:
            hay = " ".join((track.title, track.filename, track.file_path,
                            lib.artist_name(track))).lower()
            if self.search.lower() not in hay:
                return False
        return True

    def select(self, lib: Library, sort: str) -> list:
        hits = [t for t in lib.db.tracks if self.matches(t, lib)]
        if sort == "bpm":
            hits.sort(key=lambda t: (t.tempo, lib.key_name(t), t.title.lower()))
        elif sort == "key":
            hits.sort(key=lambda t: (lib.key_name(t), t.tempo, t.title.lower()))
        elif sort == "title":
            hits.sort(key=lambda t: (t.title.lower(), t.tempo))
        elif sort == "added":
            hits.sort(key=lambda t: (t.date_added, t.tempo))
        else:                                   # library order (track id)
            hits.sort(key=lambda t: t.id)
        return hits[:self.limit] if self.limit else hits

    # -- naming -------------------------------------------------------------

    def playlist_name(self) -> str:
        bits: list[str] = []
        for lo, hi, op in self.bpm:
            if op in (">=", ">"):
                bits.append(f"{op}{_fmt_bpm(lo)}")
            elif op is not None:
                bits.append(f"{op}{_fmt_bpm(hi)}")
            elif lo == hi:
                bits.append(_fmt_bpm(lo))
            else:
                bits.append(f"{_fmt_bpm(lo)}-{_fmt_bpm(hi)}")
        if self.keys:
            bits.append(" ".join(_nice(k) for k in self.keys))
        if self.colours:
            bits.append(" ".join(_nice(c) for c in self.colours))
        if self.genres:
            bits.append(" ".join(_nice(g) for g in self.genres))
        if self.rating:
            bits.append(f"{self.rating}★+")
        if self.search:
            bits.append(f'"{self.search}"')
        name = " ".join(bits) if bits else "ALL TRACKS"
        return name[:64]


def _fmt_bpm(v: float) -> str:
    return f"{v:g}"


def _nice(word: str) -> str:
    """Spell a name the way the deck does (blue -> Blue, am -> Am)."""
    return word[:1].upper() + word[1:] if word else word


# ---------------------------------------------------------------------------
# Playlist writing
# ---------------------------------------------------------------------------

def _find_child(ed: PdbEditor, parent_id: int, name: str,
                folder: bool | None = None):
    low = name.strip().lower()
    for node in ed.db.playlist_tree:
        if node.parent_id != parent_id:
            continue
        if node.name.strip().lower() != low:
            continue
        if folder is not None and node.is_folder != folder:
            continue
        return node
    return None


def ensure_parent(ed: PdbEditor, folder: str | None) -> int:
    """Return the playlist-tree id to create the filter playlist under."""
    if not folder:
        return 0
    node = _find_child(ed, 0, folder, folder=True)
    if node is not None:
        return node.id
    clash = _find_child(ed, 0, folder, folder=False)
    if clash is not None:
        raise SystemExit(
            f'there is already a playlist called "{folder}"; pick another '
            "--folder name")
    return ed.create_playlist(folder, 0, is_folder=True)


def unique_name(ed: PdbEditor, parent_id: int, wanted: str,
                force: bool, skip: bool = False) -> str | None:
    """The name to use: refused when taken, numbered when --force, and
    left alone (None) when ``skip`` is set (the preset folder)."""
    taken = _find_child(ed, parent_id, wanted)
    if taken is None:
        return wanted
    if skip and not force:
        return None
    if not force:
        raise SystemExit(
            f'a playlist called "{wanted}" is already on this stick. '
            'Use --force to add a numbered copy, or --name to pick another.')
    n = 2
    while _find_child(ed, parent_id, f"{wanted} ({n})") is not None:
        n += 1
    return f"{wanted} ({n})"


def _backup(path: Path) -> Path:
    n = 1
    while True:
        bak = path.with_name(f"{path.name}.bak-filter-{n}")
        if not bak.exists():
            shutil.copy2(path, bak)
            return bak
        n += 1


def _total_seconds(tracks) -> int:
    return sum(int(t.duration or 0) for t in tracks)


def _describe(lib: Library, track) -> str:
    meta = [f"{lib.bpm(track):6.2f}"]
    key = lib.key_name(track)
    if key:
        meta.append(f"{key:>4}")
    col = lib.colour_name(track)
    if col:
        meta.append(f"{col:<6}")
    stars = lib.stars(track)
    if stars:
        meta.append(f"{stars} stars")
    return "  ".join(meta) + "  " + (track.title or track.filename)


def list_playlists(lib: Library) -> None:
    tree = lib.db.playlist_tree
    by_parent: dict[int, list] = {}
    for node in tree:
        by_parent.setdefault(node.parent_id, []).append(node)
    counts: dict[int, int] = {}
    for e in lib.db.playlist_entries:
        counts[e.playlist_id] = counts.get(e.playlist_id, 0) + 1
    if not tree:
        print("no playlists on this stick")
    for parent in sorted(by_parent):
        for node in by_parent[parent]:
            indent = "  " if parent else ""
            kind = "[folder]" if node.is_folder else \
                f"{counts.get(node.id, 0):3d} track(s)"
            print(f"{indent}{kind:>12}  {node.name}")
    print(f"\n{len(lib.db.tracks)} tracks in the library; "
          f"rating scale: "
          f"{'0-255 (steps of 51)' if lib.rating_scale != 1 else '0-5 stars'}")


def show_library_facts(lib: Library) -> None:
    """What the current stick can be filtered on (shown before writing)."""
    from collections import Counter
    tracks = lib.db.tracks
    bpms = Counter(round(lib.bpm(t) * 2) / 2 for t in tracks)
    if bpms:
        lo, hi = min(bpms), max(bpms)
        print(f"library: {len(tracks)} tracks, {lo:g}-{hi:g} BPM")
    keys = Counter(lib.key_name(t) for t in tracks if lib.key_name(t))
    if keys:
        print("keys: " + ", ".join(f"{k} ({n})" for k, n
                                   in keys.most_common(12)))
    cols = Counter(lib.colour_name(t) for t in tracks if lib.colour_name(t))
    if cols:
        print("colours: " + ", ".join(f"{k} ({n})" for k, n
                                      in cols.most_common(8)))
    rated = sum(1 for t in tracks if lib.stars(t))
    print(f"rated tracks: {rated}")


def _stick_pdb(stick: Path) -> Path | None:
    """The stick's export.pdb, or None after saying why it is not there."""
    pdb = stick / "PIONEER" / "rekordbox" / "export.pdb"
    if pdb.exists():
        return pdb
    other = stick / "PIONEER" / "rekordbox" / "exportLibrary.db"
    if other.exists():
        print("this stick carries OneLibrary only (exportLibrary.db). "
              "Turn it into export.pdb first:\n"
              f"  python tools/onelibrary_to_pdb.py {stick}",
              file=sys.stderr)
    else:
        print(f"no export.pdb under {pdb.parent}", file=sys.stderr)
    return None


def run(stick: Path, filters: Filters, name: str | None,
        folder: str | None, sort: str, dry_run: bool, force: bool,
        quiet: bool) -> int:
    pdb = _stick_pdb(stick)
    if pdb is None:
        return 1

    lib = open_library(pdb)
    if filters.empty:
        print("no filters given -- at least one of --bpm, --key, --colour, "
              "--genre, --rating, --search is needed (see --list)",
              file=sys.stderr)
        return 2

    hits = filters.select(lib, sort)
    if not quiet:
        show_library_facts(lib)
        print()
    if not hits:
        print("nothing in this library matches those filters -- nothing "
              "written", file=sys.stderr)
        return 1

    wanted = name or filters.playlist_name()
    mins = _total_seconds(hits) // 60
    print(f"{len(hits)} track(s) match, {mins} min total")
    shown = hits if len(hits) <= 25 else hits[:20] + [None] + hits[-3:]
    for t in shown:
        if t is None:
            print(f"  ... {len(hits) - 23} more ...")
        else:
            print("  " + _describe(lib, t))
    print()

    if dry_run:
        print(f"dry run: would write the playlist \"{wanted}\" "
              f"({len(hits)} entries); nothing changed")
        return 0

    return _write_plans(pdb, lib, [(name or filters.playlist_name(),
                                   filters, hits)], folder, force, dry_run,
                        quiet)


# ---------------------------------------------------------------------------
# The standard filter folder (--presets)
# ---------------------------------------------------------------------------

def preset_playlists(lib: Library) -> list[tuple[str, Filters]]:
    """The playlists worth having on every stick, from this library's own
    numbers: three BPM bands that tile the range without overlapping, the
    star cut-offs, and the keys with a real number of tracks behind them."""
    specs: list[tuple[str, Filters]] = []
    values = sorted({round(lib.bpm(t), 1) for t in lib.db.tracks})
    if len(values) >= 3:
        k = max(1, len(values) // 3)
        lo, mid1, mid2 = values[0], values[k], values[min(2 * k, len(values) - 1)]
        for label, a, b, op in (("Warm up", lo, mid1, "<"),
                                ("Build", mid1, mid2, "<"),
                                ("Peak", mid2, float("inf"), ">=")):
            if op == "<" and a >= b:
                continue
            name = f"{label} {_fmt_bpm(a)}"
            name += "+" if op == ">=" else f"-{_fmt_bpm(b)}"
            specs.append((name, Filters(bpm=[(a, b, op)])))
    stars = [lib.stars(t) for t in lib.db.tracks]
    if any(s >= 4 for s in stars):
        specs.append(("4 stars and up", Filters(rating=4)))
    if any(s >= 5 for s in stars):
        specs.append(("5 stars", Filters(rating=5)))
    counts = Counter(lib.key_name(t) for t in lib.db.tracks
                     if lib.key_name(t))
    for key, n in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:4]:
        if n >= 3:
            specs.append((f"Key {_nice(key)}", Filters(keys=[key])))
    return specs


def run_presets(stick: Path, folder: str, sort: str, dry_run: bool,
                force: bool, quiet: bool) -> int:
    """Write the whole standard folder in one pass, one backup, one verify.
    A preset that is already on the stick is left alone (no numbered copies
    unless --force), so running it again after a fresh export is safe."""
    pdb = _stick_pdb(stick)
    if pdb is None:
        return 1
    lib = open_library(pdb)
    specs = preset_playlists(lib)
    if not specs:
        print("this library has too few BPM / rating / key values to build "
              "presets -- use the individual filters instead", file=sys.stderr)
        return 1
    if not quiet:
        show_library_facts(lib)
        print()
    plans = [(name, f, f.select(lib, sort)) for name, f in specs]
    return _write_plans(pdb, lib, plans, folder, force, dry_run, quiet,
                        skip_existing=True)


# ---------------------------------------------------------------------------
# Writing (one playlist or a whole folder)
# ---------------------------------------------------------------------------

def _write_plans(pdb: Path, lib: Library, plans, folder: str | None,
                 force: bool, dry_run: bool, quiet: bool,
                 skip_existing: bool = False) -> int:
    ready = []
    for wanted, filters_, hits in plans:
        if hits:
            ready.append((wanted, filters_, hits))
        elif not quiet:
            print(f'skip "{wanted}": nothing in this library matches')
    if not ready:
        print("nothing in this library matches those filters -- nothing "
              "written", file=sys.stderr)
        return 1

    mins = _total_seconds(ready[0][2]) // 60 if len(ready) == 1 else 0

    if dry_run:
        for wanted, _f, hits in ready:
            print(f'dry run: would write "{wanted}" ({len(hits)} entries); '
                  "nothing changed")
        return 0

    before_entries = [(e.entry_index, e.track_id, e.playlist_id)
                      for e in lib.db.playlist_entries]
    before_tree = [(n.id, n.parent_id, n.name, n.is_folder)
                   for n in lib.db.playlist_tree]
    before_tracks = [(t.id, t.title) for t in lib.db.tracks]

    ed = PdbEditor(pdb.read_bytes())
    parent = ensure_parent(ed, folder)
    made = []
    for wanted, filters_, hits in ready:
        final = unique_name(ed, parent, wanted, force, skip=skip_existing)
        if final is None:
            if not quiet:
                print(f'"{wanted}" is already on this stick -- left alone')
            continue
        pl_id = ed.create_playlist(final, parent, is_folder=False)
        for i, t in enumerate(hits, 1):
            ed.add_to_playlist(pl_id, t.id, entry_index=i)
        made.append((final, filters_, hits, pl_id))

    if not made:
        print("nothing to add -- every one of those playlists is already "
              "there", file=sys.stderr)
        return 0

    bak = _backup(pdb)
    ed.save(pdb)

    # -- verify with the vendored parser, on the bytes now on disk ----------
    check = open_library(pdb)
    for final, filters_, hits, pl_id in made:
        node = next((n for n in check.db.playlist_tree if n.id == pl_id),
                    None)
        if node is None or node.name != final or node.is_folder \
                or node.parent_id != parent:
            print(f'VERIFY FAILED: "{final}" is not in the written file',
                  file=sys.stderr)
            return 1
        got = sorted((e.entry_index, e.track_id)
                     for e in check.db.playlist_entries
                     if e.playlist_id == pl_id)
        want = [(i, t.id) for i, t in enumerate(hits, 1)]
        if got != want:
            print(f'VERIFY FAILED: "{final}" entries differ '
                  f"({len(got)} read back, {len(want)} written)",
                  file=sys.stderr)
            return 1
        if not all(filters_.matches(t, check) for t in hits):
            print(f'VERIFY FAILED: a track in "{final}" no longer matches',
                  file=sys.stderr)
            return 1
    if [(e.entry_index, e.track_id, e.playlist_id)
            for e in check.db.playlist_entries][:len(before_entries)] \
            != before_entries:
        print("VERIFY FAILED: existing playlist entries changed",
              file=sys.stderr)
        return 1
    if [(n.id, n.parent_id, n.name, n.is_folder)
            for n in check.db.playlist_tree][:len(before_tree)] != before_tree:
        print("VERIFY FAILED: existing playlists changed", file=sys.stderr)
        return 1
    if [(t.id, t.title) for t in check.db.tracks] != before_tracks:
        print("VERIFY FAILED: tracks changed", file=sys.stderr)
        return 1

    where = f"{folder} > " if folder else ""
    if len(made) == 1:
        final, _f, hits, _pl = made[0]
        print(f'wrote "{final}" in BROWSE > PLAYLIST ({where}{len(hits)} '
              f"tracks, {mins} min)")
    else:
        total = sum(len(h[2]) for h in made)
        print(f"wrote {len(made)} playlists in BROWSE > PLAYLIST ({where}"
              f"{total} entries):")
        for final, _f, hits, _pl in made:
            print(f"  {final}  ({len(hits)} tracks, "
                  f"{_total_seconds(hits) // 60} min)")
    print(f"verified: entries read back exactly; every existing row untouched")
    print(f"backup: {bak.name}")
    print("On the deck: switch it off and on, then BROWSE > PLAYLIST.")
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="Write filter playlists (BPM band, key, colour, rating, "
                    "genre, search) into a stick's export.pdb so the XDJ-700 "
                    "shows them under BROWSE > PLAYLIST.")
    ap.add_argument("stick", help="stick directory (contains PIONEER/)")
    ap.add_argument("--bpm", action="append", metavar="RANGE",
                    help="124-128, >=124, <=128, or 126 (repeatable, OR'ed)")
    ap.add_argument("--key", action="append", metavar="NAMES",
                    help="key names as the deck shows them, e.g. Am,F#m")
    ap.add_argument("--colour", "--color", dest="colour", action="append",
                    metavar="NAMES", help="e.g. red,blue (or ids)")
    ap.add_argument("--genre", action="append", metavar="NAMES")
    ap.add_argument("--rating", type=int, metavar="STARS",
                    help="minimum rating, 1-5")
    ap.add_argument("--search", metavar="TEXT",
                    help="substring of title, artist or file name")
    ap.add_argument("--limit", type=int, metavar="N",
                    help="keep the first N matches")
    ap.add_argument("--sort", choices=["library", "bpm", "key", "title",
                                       "added"], default="bpm",
                    help="entry order (default: BPM ascending)")
    ap.add_argument("--name", metavar="NAME", help="playlist name")
    ap.add_argument("--folder", metavar="NAME",
                    help="create the playlist inside this folder (made if "
                         "missing)")
    ap.add_argument("--force", action="store_true",
                    help="add a numbered copy when the name already exists")
    ap.add_argument("--dry-run", action="store_true",
                    help="show what matches; write nothing")
    ap.add_argument("--presets", action="store_true",
                    help="write the whole standard folder in one go: three "
                         "BPM bands (Warm up / Build / Peak), 4+ and 5 "
                         "stars, and the keys with 3+ tracks")
    ap.add_argument("--list", action="store_true",
                    help="list the stick's playlists and library facts, then "
                         "stop")
    ap.add_argument("-q", "--quiet", action="store_true")
    a = ap.parse_args(argv)

    stick = Path(a.stick)
    if a.rating is not None and not 1 <= a.rating <= 5:
        print("--rating takes 1-5 stars", file=sys.stderr)
        return 2
    if a.list:
        pdb = stick / "PIONEER" / "rekordbox" / "export.pdb"
        if not pdb.exists():
            print(f"no export.pdb under {pdb.parent}", file=sys.stderr)
            return 1
        lib = open_library(pdb)
        list_playlists(lib)
        return 0
    if a.presets:
        return run_presets(stick, a.folder or "FILTERS", a.sort, a.dry_run,
                           a.force, a.quiet)
    try:
        filters = Filters.from_args(a)
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return 2
    return run(stick, filters, a.name, a.folder, a.sort, a.dry_run, a.force,
               a.quiet)


if __name__ == "__main__":
    sys.exit(main())
