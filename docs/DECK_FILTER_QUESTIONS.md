# Deck session -- the six filter facts (XDJ-1000MK2)

Ten minutes at the deck answers everything `docs/TRACK_FILTER_1.1.md` still
leaves open. Nothing gets flashed; the only thing written is the stick, by the
deck itself, because filter conditions are saved on the USB device.

If the deck in front of you is a **700**, and you cannot find TRACK FILTER in
its browse controls, do not go hunting for it: do the Wave 1 checks in
`docs/WAVE1_DECK_CHECKLIST.md` instead and leave this sheet for a 1000.

## Before you start

- Use a **scratch USB stick** exported from rekordbox, with tracks that carry
colour, star ratings and My Tags. Not your performance stick.
- Load it, then hold **TRACK FILTER** for more than 1 second to open the filter.
- Write the numbers down (or photograph the screen) as you go. Do not stop to
  interpret them at the deck: a count of 0 where tracks should match is itself
  the answer.
- **When you are done, leave MY TAG selected and press APPLY, then unplug.**
  That saves the condition to the stick, and the file the deck writes there can
  settle both open questions from data instead of from counting.

## 1. Is a typed BPM one value or a band?

1. RESET, set **BPM 126**, APPLY -- write down "N tracks" and how many tracks
   in the stick are actually 126.
2. RESET, set a BPM that **no track has** (e.g. 125 when nothing is 125),
   APPLY -- write the count.
3. Try the same value with a decimal point if the keypad accepts one (126.0),
   and note whether the point is typeable at all.

Count = only the tracks at that BPM means exact. Count > 0 for a value no track
has means it is a band; compare the two counts to read the band width.

## 2. Is RATING exact or "at least"?

RESET, **RATING 4**, APPLY -- count. RESET, **RATING 5**, APPLY -- count.
Same count means "4 stars and up". 4-stars count higher than 5-stars means
"exactly 4".

## 3. How does it match KEY text?

Type **Am** -- count. Then **A** on its own -- count. Then camelot form **8A**
-- count. This says whether the full key name is required and whether the
camelot wheel is accepted.

## 4. Can several colours be lit at once, and is it a union?

RESET, light **RED** only, APPLY -- count. Go back in, add **BLUE** so both are
lit, APPLY -- count. Union means the second count is red plus blue; write both.

## 5. What does MY TAG show?

Open **MY TAG**. Write down, in order: the tab names across the top, then the
criteria offered under one tab, then what selecting one tag does to the list.
Select a tag that has tracks, APPLY, count. **Leave it selected** -- see
"before you start".

## 6. MASTER FOLLOW (only with two decks linked)

On the follower, with nothing typed: turn **MASTER FOLLOW** on, APPLY. Note
whether it shows the master's BPM and what count it gives, then whether typing
your own BPM turns the follow off.

## Record it here

| # | Check | What you did | Count / what the screen said |
|---|-------|--------------|------------------------------|
| 1 | BPM 126 | | |
| 1 | BPM (no exact match) | | |
| 1 | BPM with a decimal | | |
| 2 | RATING 4 | | |
| 2 | RATING 5 | | |
| 3 | KEY Am | | |
| 3 | KEY A | | |
| 3 | KEY 8A | | |
| 4 | colour red only | | |
| 4 | red + blue | | |
| 5 | MY TAG tabs / criteria | | |
| 5 | one tag applied | | |
| 6 | master follow | | |

## Back at the Mac

Copy the stick's `PIONEER/rekordbox/` folder off it before you use the stick for
anything else, and say so. The deck's saved filter condition is in there, and
`tools/filter_scan.py` can be pointed at whatever it turns out to be. The Wave 1
mod checks are separate: `docs/WAVE1_DECK_CHECKLIST.md`, install with
`tools/deck_session.py --mode rgb --phasemeter --touchfx --brake 6`.
