# 60-second deck test: a track replaced by its stems

Run this once after the 2026-11-02 chunk fix, on any WAV or AIFF track that
came off a real stick. It is the only thing left that the emulator cannot
prove -- everything about the file (chunk lengths, tags, duration) is already
checked in `tools/stems4.py selftest`.

## Make the file (2 minutes, in the app)

1. Library page, pick a **WAV or AIFF** track that is already on your stick.
2. **Make stems**, all four parts, a key that suits it.
3. In the dialog, press Make. It runs once and replaces the track.

When it finishes, look at the stick in Finder. Next to the track you should
see two files with almost the same name:

    your track.wav            <- now the stems version (same name, same spot)
    your track.wav.pre-stems  <- your original, untouched

The `.pre-stems` file is your undo. Rename it back over the new file if you
ever want the plain track again.

## Play it (1 minute, on the deck)

1. Eject, stick into the deck, power on.
2. BROWSE > ARTIST, find the track where it always was, load it.

**Good:** it loads, the waveform draws, the time shows the real length, and
it plays. Then tap the PERFORM stem pads: turning VOCALS off should take the
vocal out of the track you already know.

**Stalls** (spinning / "Loading" that never ends): that is the bug this fix
targets, so it matters.

## If it stalls, send this

1. The format of the track: WAV or AIFF.
2. Whether the `.pre-stems` original still loads on the same stick. Load it
   first -- if the original also stalls, the problem is the stick, not the
   stems.
3. The file itself if it is not enormous, or this dump from Terminal, which
   is all I need to see the layout:

       python3 -c "
       import sys, struct
       d = open(sys.argv[1], 'rb').read()
       print(d[:4], struct.unpack('<I', d[4:8])[0], 'bytes, file', len(d))
       i = 12
       while i + 8 <= len(d):
           cid = d[i:i+4]; sz = struct.unpack('<I', d[i+4:i+8])[0]
           print(i, cid, sz, 'EVEN' if sz % 2 == 0 else 'ODD  <-- this one')
           i += 8 + sz + (sz & 1)
       " "/Volumes/YOURSTICK/Contents/.../your track.wav"

Every line should say EVEN. An ODD line names the chunk the deck trips on.
