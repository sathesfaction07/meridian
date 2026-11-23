# ADR 001: Every event carries both an instant and a local reading, plus how they were mapped

## Status

Accepted

## Context

Sixteen feeds report lifecycle events, each in its own local wall-clock time. Merging them means ordering them, and there are two obvious designs, both wrong.

### Keep only the local reading

Then ordering is impossible. The offsets in this set span **17 hours** — Sydney in DST at UTC+11 against Mexico City at UTC−06, a figure derived from tzdata by a test rather than assumed. Two events both written `09:00` can be 17 hours apart, so string or naive-datetime comparison inverts orders. Two of the sixteen regions are on half-hour offsets, so even an hour-granular integer model truncates them.

### Keep only the instant

Then the report cannot say what the feed said. Regional teams talk about their own events in their own clock: "it went live at 09:00". Converting to UTC and discarding the original means every conversation needs a mental conversion back, and the report has thrown away the only thing the operator recognises.

### And local time is not a function of instants

This is the part that bites regardless of which you keep. Local time is not injective and not surjective onto instants:

- On the day a zone **springs forward**, one wall-clock reading never happens. `2025-03-09 02:30` does not exist in New York.
- On the day a zone **falls back**, one reading happens twice, 60 minutes apart. `2025-11-02 01:30` occurs in both EDT and EST.

Python raises for neither. `naive.replace(tzinfo=zone)` produces a plausible datetime in both cases — for the gap it shifts forward by the DST offset, for the overlap it picks `fold=0`. A timeline built on that is quietly an hour wrong, and nothing in the output indicates it.

The southern hemisphere runs the opposite phase, so any assumption about which month is "summer" is wrong for a quarter of the regions. Sydney's 2025 transitions are 06 April (back) and 05 October (forward).

## Decision

Every `Event` carries three things instead of one.

```python
class Event(BaseModel):
    instant: datetime      # the ordering key. ALWAYS timezone-aware, ALWAYS UTC
    local: datetime        # what the feed said, as a naive wall-clock reading
    resolution: Resolution # HOW the mapping was made
```

**`instant` is validated, not merely documented.** A naive or non-UTC value is refused at construction:

```python
if value.tzinfo is None or value.utcoffset() is None:
    raise ValueError("instant must be timezone-aware")
if value.utcoffset() != timedelta(0):
    raise ValueError(f"instant must be UTC, got offset {value.utcoffset()}")
```

Without this, one unconverted feed poisons every comparison downstream, and the symptom is a plausible-looking timeline in the wrong order rather than an error.

**`local` is naive on purpose.** It is a wall-clock string, not an instant. Attaching a zone would invite somebody to do arithmetic on it, which is exactly the mistake this design exists to prevent.

**`resolution` is one of four values**, and the last two are the reason this ADR exists:

| Resolution | Meaning |
|:--|:--|
| `exact` | an ordinary local reading, mapped unambiguously |
| `offset_given` | the feed supplied an explicit offset, so no zone lookup was needed and neither edge case can apply |
| `ambiguous` | the reading happens twice. Resolved to the **first** occurrence, and flagged |
| `nonexistent` | the reading never happens. Python's forward shift is used, and flagged |

### Classifying the two edge cases

PEP 495 gives `fold` to disambiguate a repeated reading. Both edge cases show up as the two folds disagreeing about the offset, so a second test separates them:

```python
first  = naive.replace(tzinfo=zone, fold=0)
second = naive.replace(tzinfo=zone, fold=1)

if first.utcoffset() == second.utcoffset():
    return EXACT                                    # ordinary time

if first.astimezone(UTC).astimezone(zone).replace(tzinfo=None) != naive:
    return NONEXISTENT                              # a round trip moved the clock
return AMBIGUOUS                                    # a round trip preserved it
```

The round trip is the discriminator. In a gap, Python has already shifted the reading forward, so converting to UTC and back lands on a different wall clock. In an overlap both readings are real, so it lands on itself.

**`ambiguous` resolves to the first occurrence.** Either end would be defensible. The first is the earlier instant, so a duration computed from it can only be under-stated, never over-stated, and consistently choosing one end is what keeps the merge deterministic.

### Consequences for identity

`Event.key` — used for deduplication — is `(product_id, region, kind, instant)`. Keyed on the resolved instant rather than the reported text, so `2025-03-01T09:00:00+09:00` from a regional feed and `2025-03-01T00:00:00Z` from a global one are recognised as one event. `local` and `source` are deliberately excluded: they are reporting fields, and two rows differing only in those describe the same happening.

`Event.sort_key` is `(instant, region, kind)`. The tiebreakers matter more than they look — without them, two events sharing an instant come out in input order, so merging the same data twice can produce two different timelines and nothing about either would look wrong.

## Validation

`tests/unit/test_regions_and_parse.py::TestDstClassificationAgainstTzdata` is parametrized over transitions **derived from the host's tzdata at test time**, not over a hard-coded list of dates. A list in a test file is a snapshot of what somebody believed; tzdata is what the code consults. On this machine it finds **20 real transitions across 16 regions** and all 20 classify correctly, including Sydney's inverted phases.

The same fixture derives which regions have no DST at all, so `test_a_region_without_dst_never_flags_anything` cannot go stale either. There is a guard test asserting the derived set is non-empty, because a silent zero would make every case below it vacuously pass.

`TestDurationsAreInstantBased` pins the consequence: 47 hours across a spring-forward, 49 across a fall-back, 48 in a zone without DST. `setup.sh` re-derives all of it on a fresh clone.

## Consequences

**Good**

- Ordering is correct across all sixteen regions, and the UTC invariant is enforced rather than hoped for
- A report can show `09:00 local` and still be sorted correctly
- The two DST edge cases are visible in output instead of being an invisible hour of error. `show` lists them; `resolve` explains one on demand and exits non-zero
- Durations are computed from instants, so a span across a transition reports 23 or 25 hours rather than 24
- Deduplication works across feeds that describe the same event from different clocks

**Bad**

- Three fields where one would do, and the invariant that only one of them may be used for arithmetic is a convention the type system does not enforce. `local` is naive partly to discourage misuse, which is a weak guard
- `Resolution` is four values that a casual reader will not want. `doctor` and the `resolve` command exist largely to explain them
- The classifier depends on `fold` semantics and on a round-trip identity that is not locally obvious. It is nine lines with a long docstring, and it is still the part most likely to be "simplified" wrongly
- Nothing detects a feed that has *already* converted to UTC but still labels itself local. That is indistinguishable from correct data

**Neutral**

- `offset_given` exists to record that no inference was needed. It could be collapsed into `exact`, but then a reader could not tell a trusted offset from an inferred one
- A date with no time is read as local midnight, which can itself be nonexistent or ambiguous in a zone that transitions at midnight. Handled by the same path, so no special case
- The negative-DST modelling that some tzdata builds use for Europe/Dublin is not present on this build, and nothing here depends on it either way. An earlier version of `regions.py` claimed it was — see the README for why that comment was removed
