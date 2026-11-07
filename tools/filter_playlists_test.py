#!/usr/bin/env python3
"""Tests for filter_playlists.py.

Builds a small but realistic export.pdb (10 tracks with BPM, key, colour and
rating, plus one existing playlist), then checks that:

* each filter selects exactly the right tracks, in the requested order;
* the playlist lands in the tree (root or a folder) with sequential entries;
* every pre-existing track, playlist and entry is untouched;
* the written file reads back with the independent vendored parser;
* dry runs, empty matches, name clashes and --force behave as documented.

Run:  python tools/filter_playlists_test.py
"""

from __future__ import annotations

import hashlib
import io
import sys
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))
sys.path.insert(0, str(_HERE.parent / "vendor"))

from rekordbox_pdb import Database as PdbReader          # noqa: E402
import filter_playlists as F                            # noqa: E402
import onelibrary_to_pdb as C                           # noqa: E402

KEYS = [(1, "Am"), (2, "F#m"), (3, "C"), (4, "Gm")]
COLOURS = [(1, "Pink"), (2, "Red"), (7, "Blue")]

# title, bpm, key id, colour id, rating (rekordbox's 0/51/.../255 form)
TRACKS = [
    ("Dark Room",      124.00, 1, 2, 204),
    ("Late Train",     125.50, 2, 2,  51),
    ("Glass",          126.00, 3, 7, 255),
    ("Hollow Sun",     127.00, 1, 0,   0),
    ("Kestrel",        128.00, 4, 7, 153),
    ("Low Ceiling",    130.00, 2, 0, 255),
    ("Nine Hours",     122.00, 3, 2, 102),
    ("Open Water",     124.00, 4, 7, 255),
    ("Pale Blue",      125.50, 1, 0, 153),
    ("Quiet Signal",   126.00, 2, 2, 204),
]

STR_SLOTS = 21


def _strings(title: str, i: int) -> list[str]:
    slots = [""] * STR_SLOTS
    name = f"track{i:02d}.mp3"
    slots[10] = "2026-01-0%d" % (1 + i % 9)     # date added
    slots[17] = title
    slots[19] = name
    slots[20] = f"/Contents/Music/{name}"
    return slots


def build_fixture(root: Path) -> Path:
    b = C.PdbBuilder()
    for kid, name in KEYS:
        row, alloc = C.row_key(kid, name)
        b.add(5, row, alloc)
    for cid, name in COLOURS:
        row, alloc = C.row_color(cid, name)
        b.add(6, row, alloc)
    for gid, name in [(1, "House"), (2, "Techno")]:
        row, alloc = C.row_genre_or_label(gid, name)
        b.add(1, row, alloc)
    for aid, name in [(1, "Artist One"), (2, "Artist Two")]:
        row, alloc = C.row_artist(aid, name)
        b.add(2, row, alloc)

    for i, (title, bpm, key_id, colour_id, rating) in enumerate(TRACKS, 1):
        o = {
            "path": f"/Contents/Music/track{i:02d}.mp3",
            "fileName": f"track{i:02d}.mp3",
            "title": title,
            "bpmx100": int(round(bpm * 100)),
            "length": 300 + i,
            "fileSize": 8_000_000,
            "samplingRate": 44100,
            "bitrate": 320,
            "bitDepth": 16,
            "trackNo": i,
            "discNo": 0,
            "djPlayCount": 0,
            "releaseYear": 2024,
            "color_id": colour_id,
            "rating": rating,
        }
        ids = {"key": key_id, "genre": 1 if i % 2 else 2, "artist": 1 + i % 2,
               "album": 0, "label": 0, "artwork": 0, "remixer": 0,
               "original_artist": 0, "composer": 0}
        row, alloc = C.row_track(o, ids, _strings(title, i), i)
        b.add(0, row, alloc)

    # one pre-existing playlist with two entries, to prove it survives
    row, alloc = C.row_tree(0, 0, 1, False, "All Tracks")
    b.add(7, row, alloc)
    for idx, tid in enumerate((1, 2), 1):
        row, alloc = C.row_entry(idx, tid, 1)
        b.add(8, row, alloc)

    pdb = root / "PIONEER" / "rekordbox" / "export.pdb"
    pdb.parent.mkdir(parents=True)
    pdb.write_bytes(b.build())
    return pdb


def _quiet(fn, *a, **kw):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        try:
            code = fn(*a, **kw)
        except SystemExit as exc:
            code = exc.code if isinstance(exc.code, int) else 1
            err.write(str(exc.code) + "\n")
    return code, out.getvalue(), err.getvalue()


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class FilterPlaylistTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.pdb = build_fixture(self.root)
        self.before = PdbReader(self.pdb.read_bytes())

    def tearDown(self):
        self.tmp.cleanup()

    # -- helpers ------------------------------------------------------------

    def run_tool(self, *args):
        return _quiet(F.main, [str(self.root), *args])

    def db(self):
        return PdbReader(self.pdb.read_bytes())

    def playlist(self, name):
        db = self.db()
        node = next(n for n in db.playlist_tree if n.name == name)
        tracks = {t.id: t.title for t in db.tracks}
        rows = sorted((e.entry_index, e.track_id)
                      for e in db.playlist_entries
                      if e.playlist_id == node.id)
        return node, [(i, tracks[tid]) for i, tid in rows]

    def assert_untouched(self):
        db = self.db()
        self.assertEqual([(t.id, t.title, t.tempo) for t in db.tracks],
                         [(t.id, t.title, t.tempo) for t in self.before.tracks])
        self.assertEqual([(n.id, n.parent_id, n.name, n.is_folder)
                          for n in db.playlist_tree][:len(self.before.playlist_tree)],
                         [(n.id, n.parent_id, n.name, n.is_folder)
                          for n in self.before.playlist_tree])
        self.assertEqual([(e.entry_index, e.track_id, e.playlist_id)
                          for e in db.playlist_entries][:len(self.before.playlist_entries)],
                         [(e.entry_index, e.track_id, e.playlist_id)
                          for e in self.before.playlist_entries])

    # -- the filter itself ---------------------------------------------------

    def test_bpm_parser(self):
        self.assertEqual(F.parse_bpm("124-128"), (124.0, 128.0, None))
        self.assertEqual(F.parse_bpm("128-124"), (124.0, 128.0, None))
        self.assertEqual(F.parse_bpm("126"), (126.0, 126.0, None))
        self.assertEqual(F.parse_bpm(">=124"), (124.0, float("inf"), ">="))
        self.assertEqual(F.parse_bpm("<=126 BPM"), (0.0, 126.0, "<="))
        self.assertEqual(F.parse_bpm("122..124"), (122.0, 124.0, None))
        with self.assertRaises(ValueError):
            F.parse_bpm("fast")
        with self.assertRaises(ValueError):
            F.parse_bpm(">=124-128")

    def test_bpm_band_and_rating(self):
        code, out, err = self.run_tool("--bpm", "124-126", "--rating", "4",
                                      "--name", "BAND")
        self.assertEqual(code, 0, err)
        node, rows = self.playlist("BAND")
        # 124 Dark Room (204 -> 4*), 126 Glass (255 -> 5*),
        # 126 Quiet Signal (204 -> 4*); 124 Open Water is 255 but let's
        # check: Open Water rating 255 -> 5*, and its BPM is 124.
        titles = [t for _i, t in rows]
        self.assertEqual(titles, ["Dark Room", "Open Water", "Glass",
                                  "Quiet Signal"])
        self.assertEqual([i for i, _t in rows], [1, 2, 3, 4])
        self.assert_untouched()

    def test_star_scale_survives_both_encodings(self):
        lib = F.open_library(self.pdb)
        self.assertEqual(lib.rating_scale, 51)
        self.assertEqual(lib.stars(self.before.tracks[0]), 4)   # 204
        self.assertEqual(lib.stars(self.before.tracks[2]), 5)   # 255
        self.assertEqual(lib.stars(self.before.tracks[1]), 1)   # 51

    def test_colour_filter(self):
        code, _out, err = self.run_tool("--colour", "blue")
        self.assertEqual(code, 0, err)
        node, rows = self.playlist("Blue")
        self.assertEqual(sorted(t for _i, t in rows),
                         ["Glass", "Kestrel", "Open Water"])
        self.assertEqual(node.is_folder, False)

    def test_two_colours_on_at_once(self):
        """The deck toggles each colour on its own (`FILTER COLOR CHANGE:
        SelColor %d, OnOff %d`), so more than one can be lit together; our
        colour list therefore has to be a union, not a single value."""
        code, _out, err = self.run_tool("--colour", "red,blue")
        self.assertEqual(code, 0, err)
        _node, rows = self.playlist("Red Blue")
        self.assertEqual(sorted(t for _i, t in rows),
                         ["Dark Room", "Glass", "Kestrel", "Late Train",
                          "Nine Hours", "Open Water", "Quiet Signal"])
        self.assert_untouched()

    def test_key_filter(self):
        code, _out, err = self.run_tool("--key", "am")
        self.assertEqual(code, 0, err)
        _node, rows = self.playlist("Am")
        # default order is BPM: 124 Dark Room, 125.5 Pale Blue, 127 Hollow Sun
        self.assertEqual([t for _i, t in rows],
                         ["Dark Room", "Pale Blue", "Hollow Sun"])

    def test_search_filter(self):
        code, _out, err = self.run_tool("--search", "sun")
        self.assertEqual(code, 0, err)
        _node, rows = self.playlist('"sun"')
        self.assertEqual([t for _i, t in rows], ["Hollow Sun"])

    def test_genre_and_sort_title(self):
        code, _out, err = self.run_tool("--genre", "Techno", "--sort", "title")
        self.assertEqual(code, 0, err)
        _node, rows = self.playlist("Techno")
        titles = [t for _i, t in rows]
        self.assertEqual(titles, sorted(titles))

    def test_limit(self):
        code, _out, err = self.run_tool("--bpm", ">=122", "--sort", "bpm",
                                        "--limit", "3")
        self.assertEqual(code, 0, err)
        _node, rows = self.playlist(">=122")
        self.assertEqual([t for _i, t in rows],
                         ["Nine Hours", "Dark Room", "Open Water"])

    # -- tree/write behaviour ------------------------------------------------

    def test_folder_is_created_and_reused(self):
        code, _out, err = self.run_tool("--bpm", "124-124", "--folder",
                                        "FILTERS")
        self.assertEqual(code, 0, err)
        db = self.db()
        folder = next(n for n in db.playlist_tree if n.name == "FILTERS")
        self.assertTrue(folder.is_folder)
        child = next(n for n in db.playlist_tree if n.name == "124")
        self.assertEqual(child.parent_id, folder.id)

        code, _out, err = self.run_tool("--colour", "red", "--folder",
                                        "FILTERS")
        self.assertEqual(code, 0, err)
        db = self.db()
        self.assertEqual(sum(1 for n in db.playlist_tree
                             if n.name == "FILTERS"), 1)

    def test_duplicate_name_refused_then_forced(self):
        code, _out, _err = self.run_tool("--bpm", "124-124")
        self.assertEqual(code, 0)
        digest = _digest(self.pdb)
        code, _out, err = self.run_tool("--bpm", "124-124")
        self.assertNotEqual(code, 0)
        self.assertIn("already on this stick", err)
        self.assertEqual(_digest(self.pdb), digest)
        code, _out, err = self.run_tool("--bpm", "124-124", "--force")
        self.assertEqual(code, 0, err)
        node, _rows = self.playlist("124 (2)")
        self.assertFalse(node.is_folder)

    def test_dry_run_writes_nothing(self):
        digest = _digest(self.pdb)
        code, out, err = self.run_tool("--bpm", "124-126", "--dry-run")
        self.assertEqual(code, 0, err)
        self.assertIn("dry run", out)
        self.assertEqual(_digest(self.pdb), digest)

    def test_no_match_writes_nothing(self):
        digest = _digest(self.pdb)
        code, _out, err = self.run_tool("--bpm", "170-180")
        self.assertEqual(code, 1)
        self.assertIn("nothing in this library matches", err)
        self.assertEqual(_digest(self.pdb), digest)

    def test_no_filter_is_a_usage_error(self):
        code, _out, err = self.run_tool()
        self.assertEqual(code, 2)
        self.assertIn("no filters given", err)

    def test_backup_is_written_once_per_run(self):
        code, _out, err = self.run_tool("--bpm", "124-124")
        self.assertEqual(code, 0, err)
        self.assertTrue((self.pdb.parent / "export.pdb.bak-filter-1").exists())
        backup = PdbReader((self.pdb.parent
                            / "export.pdb.bak-filter-1").read_bytes())
        self.assertEqual(len(backup.playlist_tree), len(self.before.playlist_tree))
        code, _out, err = self.run_tool("--bpm", "125.5-125.5")
        self.assertEqual(code, 0, err)
        self.assertTrue((self.pdb.parent / "export.pdb.bak-filter-2").exists())
        self.assertEqual(len(self.db().playlist_tree), 3)   # 1 old + 2 new

    def test_list_mode(self):
        code, out, err = _quiet(F.main, [str(self.root), "--list"])
        self.assertEqual(code, 0, err)
        self.assertIn("All Tracks", out)
        self.assertIn("2 track(s)", out)
        self.assertIn("rating scale: 0-255", out)

    # -- the whole filter folder (--presets) ---------------------------------

    def test_presets_build_the_standard_folder(self):
        code, _out, err = self.run_tool("--presets")
        self.assertEqual(code, 0, err)
        db = self.db()
        folder = next(n for n in db.playlist_tree if n.name == "FILTERS")
        self.assertTrue(folder.is_folder)
        names = [n.name for n in db.playlist_tree
                 if n.parent_id == folder.id]
        self.assertEqual(names, ["Warm up 122-125.5", "Build 125.5-127",
                                 "Peak 127+", "4 stars and up", "5 stars",
                                 "Key Am", "Key F#m"])
        self.assert_untouched()

    def test_presets_bpm_bands_cover_every_track_once(self):
        self.assertEqual(self.run_tool("--presets")[0], 0)
        db = self.db()
        bands = {n.id: n.name for n in db.playlist_tree
                 if n.name.startswith(("Warm up", "Build", "Peak"))}
        seen = {}
        for e in db.playlist_entries:
            if e.playlist_id in bands:
                seen[e.track_id] = seen.get(e.track_id, 0) + 1
        self.assertEqual(len(seen), len(TRACKS))
        self.assertEqual(set(seen.values()), {1})

    def test_presets_twice_adds_nothing_the_second_time(self):
        self.assertEqual(self.run_tool("--presets")[0], 0)
        digest = _digest(self.pdb)
        code, out, err = self.run_tool("--presets")
        self.assertEqual(code, 0, err)
        self.assertIn("already there", err)
        self.assertEqual(_digest(self.pdb), digest)

    def test_presets_folder_and_dry_run(self):
        code, out, err = self.run_tool("--presets", "--folder", "MY SET",
                                       "--dry-run")
        self.assertEqual(code, 0, err)
        self.assertIn("Warm up", out)
        self.assertEqual(len(self.db().playlist_tree),
                         len(self.before.playlist_tree))
        self.assertEqual(self.run_tool("--presets", "--folder", "MY SET")[0], 0)
        names = [n.name for n in self.db().playlist_tree]
        self.assertIn("MY SET", names)
        self.assertNotIn("FILTERS", names)

    def test_onelibrary_only_stick_is_pointed_at_the_converter(self):
        stick = self.root / "onelib"
        (stick / "PIONEER" / "rekordbox").mkdir(parents=True)
        (stick / "PIONEER" / "rekordbox" / "exportLibrary.db").write_bytes(b"x")
        code, _out, err = _quiet(F.main, [str(stick), "--bpm", "124"])
        self.assertEqual(code, 1)
        self.assertIn("onelibrary_to_pdb.py", err)


if __name__ == "__main__":
    unittest.main(verbosity=2)
