"""The regional feeds and their clocks.

Sixteen regions, each with an IANA timezone. The set is deliberately spread across
awkward cases rather than being a tidy list of UTC offsets:

  in  Asia/Kolkata        UTC+05:30, a HALF-HOUR offset
  au  Australia/Sydney    southern-hemisphere DST, so it is UTC+11 in January and
                            UTC+10 in July -- the opposite phase to every northern
                            region. Its 2025 transitions are 04-06 (back) and 10-05
                            (forward), which is why a northern-hemisphere assumption
                            about which month is "summer" produces a one-hour error
  uk / ie vs de / es      the whole EU changes at the SAME instant (01:00 UTC on
                            2025-03-30) but at DIFFERENT local readings: 02:00 in
                            London and Dublin, 03:00 in Berlin and Madrid. Grouping
                            events by local wall-clock time therefore splits one
                            transition into two
  mx  America/Mexico_City no DST since 2022, unlike its northern neighbours
  br  America/Sao_Paulo   no DST since 2019, and southern hemisphere
  jp / sg / ae            no DST at all

Anything that merges these has to work on instants, not on wall-clock strings. The widest
simultaneous spread is 17 hours (Sydney in DST at UTC+11 against Mexico City at UTC-06),
which is more than enough for two events to appear in the wrong order if the offsets are
dropped. Both of those figures are asserted by a test that derives them from the tzdata
on the host rather than trusting this comment.
"""

from __future__ import annotations

from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict


class RegionError(Exception):
    """An unknown or unusable region code."""


class Region(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    code: str
    name: str
    timezone: str

    def zone(self) -> ZoneInfo:
        try:
            return ZoneInfo(self.timezone)
        except ZoneInfoNotFoundError as exc:  # pragma: no cover - depends on the host tzdata
            raise RegionError(
                f"region {self.code}: timezone {self.timezone!r} is not available on this system. "
                "On a slim Linux image, install tzdata (pip install tzdata)."
            ) from exc


#: The canonical set. Ordered west to east by standard offset, which is also the order
#: charts and reports use, so a consolidated timeline reads left to right by clock.
REGIONS: tuple[Region, ...] = (
    Region(code="mx", name="Mexico", timezone="America/Mexico_City"),
    Region(code="us", name="United States", timezone="America/New_York"),
    Region(code="ca", name="Canada", timezone="America/Toronto"),
    Region(code="br", name="Brazil", timezone="America/Sao_Paulo"),
    Region(code="uk", name="United Kingdom", timezone="Europe/London"),
    Region(code="ie", name="Ireland", timezone="Europe/Dublin"),
    Region(code="es", name="Spain", timezone="Europe/Madrid"),
    Region(code="fr", name="France", timezone="Europe/Paris"),
    Region(code="de", name="Germany", timezone="Europe/Berlin"),
    Region(code="se", name="Sweden", timezone="Europe/Stockholm"),
    Region(code="pl", name="Poland", timezone="Europe/Warsaw"),
    Region(code="ae", name="United Arab Emirates", timezone="Asia/Dubai"),
    Region(code="in", name="India", timezone="Asia/Kolkata"),
    Region(code="sg", name="Singapore", timezone="Asia/Singapore"),
    Region(code="jp", name="Japan", timezone="Asia/Tokyo"),
    Region(code="au", name="Australia", timezone="Australia/Sydney"),
)

_BY_CODE = {region.code: region for region in REGIONS}

#: Codes that older feeds still emit. Kept as data rather than being "fixed" upstream,
#: because a historical export cannot be rewritten and dropping its rows loses events.
#: Mapping them is not cosmetic: an unmapped code means those events silently vanish.
ALIASES: dict[str, str] = {
    "usa": "us",
    "gb": "uk",
    "uae": "ae",
    "eu-central": "de",
    "eu-west": "ie",
    "na-east": "us",
    "apac-south": "in",
    "emea": "de",
}


def canonical_code(code: str) -> str:
    """Normalise a region code, resolving aliases.

    Case- and whitespace-insensitive, because feeds disagree about both. Raises rather
    than returning a default: guessing a region means guessing a timezone, and a wrong
    timezone silently shifts an event by up to 17 hours.
    """
    cleaned = code.strip().casefold().replace("_", "-")
    if cleaned in _BY_CODE:
        return cleaned
    if cleaned in ALIASES:
        return ALIASES[cleaned]
    known = ", ".join(sorted(_BY_CODE))
    raise RegionError(
        f"unknown region {code!r}. Known: {known}. Aliases: {', '.join(sorted(ALIASES))}"
    )


def region(code: str) -> Region:
    """The Region for a code, resolving aliases."""
    return _BY_CODE[canonical_code(code)]


def all_codes() -> list[str]:
    """Canonical codes in the declared west-to-east order."""
    return [item.code for item in REGIONS]


def is_alias(code: str) -> bool:
    cleaned = code.strip().casefold().replace("_", "-")
    return cleaned in ALIASES
