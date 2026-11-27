"""Loose room names for the assistant's speaker tools.

A request names rooms ("dining room"), a floor ("upstairs", "outside"), a
quick group ("whole lodge") or all of them, in the guest's words. The request
and every room's label and Sonos name go through the same :func:`words`, so
"the master bedroom ensuite" matches "Master Bedroom En-suite" and "hot tubs"
matches "Hot Tub". Modelled on device_control's matching for lights and
blinds, where a room is a speaker.
"""
import difflib
import re

FILLER = frozenset({
    "the", "a", "an", "and", "in", "on", "of", "at", "to", "for", "my", "our", "this", "that",
    "these", "those", "please", "one", "speaker", "sonos", "music", "audio", "sound", "song",
    "volume", "it", "here", "there", "player", "zone",
})
QUANTIFIERS = frozenset({"all", "every", "everything", "both", "each", "entire", "whole",
                         "house", "lodge", "home", "everywhere"})
FLOOR_ALIASES = {
    "upper": ("upstairs", "upper floor", "top floor", "second floor"),
    "main": ("main floor", "main level", "ground floor"),
    "lower": ("downstairs", "basement", "lower floor", "lower level"),
    "outside_main": ("outside", "outdoors", "outdoor", "exterior"),
    "outside_lower": ("outside", "outdoors", "outdoor", "exterior"),
}
FLOOR_NAMES = {"upper": "upstairs", "main": "the main floor", "lower": "downstairs",
               "outside_main": "outside", "outside_lower": "outside"}
FUZZY_CUTOFF = 0.75
_SPLIT = re.compile(r",|;|&|\+|/|\band\b|\bplus\b|\bwith\b", re.I)
_ENSUITE = re.compile(r"\ben[\s-]+suite", re.I)


class NoMatch(LookupError):
    """Nothing matches a request; the message says which rooms there are."""


class Ambiguous(LookupError):
    """A request could mean more than one room; ask which."""

    def __init__(self, message, matches=()):
        super().__init__(message)
        self.matches = list(matches)


def _stem(word):
    return word[:-1] if len(word) > 3 and word.endswith("s") and not word.endswith("ss") else word


def join(parts):
    """"a", "a and b", "a, b and c"."""
    parts = list(parts)
    if not parts:
        return ""
    return parts[0] if len(parts) == 1 else ", ".join(parts[:-1]) + " and " + parts[-1]


def _raw(text):
    text = _ENSUITE.sub("ensuite", str(text or "").casefold().replace("'", ""))
    return [w for w in re.split(r"[^0-9a-z]+", text) if w]


def words(text):
    """The words that identify rooms in ``text``: lower case, singular, no fillers."""
    return tuple(w for w in (_stem(w) for w in _raw(text)) if w not in FILLER)


def key(text):
    return frozenset(w for w in words(text) if w not in QUANTIFIERS)


def _plural(text):
    """True when a word was plural ("the decks"): it means every room it matches."""
    return any(_stem(w) != w and _stem(w) not in FILLER for w in _raw(text))


class Rooms:
    """The rooms the assistant may play to: dicts with uid, label, name and floor.

    ``presets`` are the quick groups ({label, rooms: [names] or "*"}).
    """

    def __init__(self, rooms, presets=()):
        self.rooms = list(rooms)
        self.presets = list(presets or ())
        for r in self.rooms:
            floor_words = set()
            for alias in FLOOR_ALIASES.get(r.get("floor"), ()):
                floor_words |= set(words(alias))
            r["_exact"] = {key(r["label"]), key(r.get("name") or r["label"])}
            r["_words"] = (set(words(r["label"])) | set(words(r.get("name") or "")) | floor_words) - QUANTIFIERS

    def labels(self):
        return [r["label"] for r in self.rooms]

    def _vocab(self):
        vocab = set()
        for r in self.rooms:
            vocab |= r["_words"]
        for p in self.presets:
            vocab |= key(p.get("label") or "")
        return sorted(vocab)

    def _preset(self, k):
        for p in self.presets:
            if k and k == key(p.get("label") or ""):
                names = p.get("rooms")
                if names == "*":
                    return list(self.rooms)
                return [r for r in self.rooms if r.get("name") in (names or ()) or r["label"] in (names or ())]
        return None

    def _one(self, text, strict):
        found = words(text)
        k = frozenset(w for w in found if w not in QUANTIFIERS)
        every = len(k) < len(set(found)) or _plural(text)
        if not k:
            return list(self.rooms) if every else None
        exact = [r for r in self.rooms if k in r["_exact"]]
        if len(exact) == 1:
            return exact
        preset = self._preset(k)
        if preset:
            return preset
        for floor, aliases in FLOOR_ALIASES.items():
            if any(k == key(a) for a in aliases) or k == key(floor.replace("_", " ")):
                on = [r for r in self.rooms if k in {key(a) for a in FLOOR_ALIASES.get(r.get("floor"), ())}]
                if on:
                    return on
        matches = [r for r in self.rooms if k <= r["_words"]]
        if not matches:
            return None
        if strict and len(matches) > 1 and not every:
            raise Ambiguous(f"{str(text).strip()!r} could mean {join([r['label'] for r in matches])}. "
                            "Ask which one, or say all of them.", matches)
        return matches

    def _fuzzy(self, text):
        """``text`` with each unknown word swapped for the nearest room word (ASR slips)."""
        vocab = self._vocab()
        out, changed = [], False
        for w in words(text):
            if w in QUANTIFIERS or w in vocab:
                out.append(w)
                continue
            close = difflib.get_close_matches(w, vocab, n=1, cutoff=FUZZY_CUTOFF)
            if close:
                out.append(close[0])
                changed = True
            else:
                return None
        return " ".join(out) if changed else None

    def _resolve(self, text, strict):
        found = self._one(text, strict)
        if found is None:
            fixed = self._fuzzy(text)
            if fixed:
                found = self._one(fixed, strict)
        return found

    def resolve(self, text, *, strict=True):
        """The rooms ``text`` names, in page order.

        ``text`` may list several ("kitchen, office" or "dining room and top
        deck"). Raises :class:`NoMatch` or (when ``strict``) :class:`Ambiguous`.
        """
        if not self.rooms:
            raise NoMatch("There are no speakers set up.")
        found = self._resolve(text, strict)
        if found is None:
            parts = [p for p in _SPLIT.split(str(text or "")) if p.strip()]
            if len(parts) < 2:
                raise NoMatch(self.no_match_message(text))
            found = []
            for part in parts:
                got = self._resolve(part, strict)
                if got is None:
                    raise NoMatch(self.no_match_message(part))
                found += got
        ids = {r["uid"] for r in found}
        return [r for r in self.rooms if r["uid"] in ids]

    def no_match_message(self, text):
        text = str(text or "").strip()
        head = f"There's no speaker called {text!r}." if words(text) else "Which room?"
        return f"{head} The rooms with speakers are {join(self.labels())}."
