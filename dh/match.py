"""Rules-based product matcher: listing title (PL, DE, CZ, HU, EN) -> canonical key, plus bait/wanted/for-parts flags.

Design rules (see tests/test_match.py):
- The KEY comes from the title only. A description can add defects (for-parts) but never creates a product.
- When in doubt the key is None (certainty 0.0). A wrong key can create a fake deal; a missing key only costs recall,
  and the LLM stage (method="llm") can still pick it up.
- Wanted ads ("kupię", "WTB"...) and box-only listings never get a key. Comparisons ("jak RTX 4090", "like", "vs")
  never match the chip they mention.
- A whole gaming PC ("Komputer gamingowy", "Gaming PC", "Herní PC", "Gamer PC") maps to key `pc:bundle` (0.5) whose
  bundle_items list the detected parts; dh.partout values it part by part.
- Certainty: 0.95 = chip/model and memory unambiguous (stated, or the chip has a single variant);
  0.7 = the variant was inferred from the taxonomy default; 0.0 = no key. A bundle (PC, extra RAM/SSD in a GPU
  title) is capped at 0.5.
- `lang` is accepted for the interface but unused: all multilingual patterns are applied to every title.
"""
from __future__ import annotations

import re
import unicodedata
from functools import lru_cache

from . import taxonomy
from .models import Extraction

# ---------------------------------------------------------------------------------------- normalisation


_PRAISE = re.compile(r"(?P<pre>(?P<num>\d{4})?(?P<ti>\s*-?\s*ti)?\s*-?\s*)?super\s+"
                     r"(?:stan\w*|cen\w*|okazj\w*|ofert\w*|stav\w*|kondic\w*|oferta)")


@lru_cache(maxsize=None)
def _super_models() -> set[str]:
    out = set()
    for cat in ("gpu", "gpu-mobile"):
        for e in taxonomy.gpu_entries(cat):
            if e["id"].endswith("-super"):
                out.add(e["id"].split("-", 1)[1])  # e.g. "4070-ti-super"
    return out


def _praise_repl(m: re.Match) -> str:
    """"super stan" is praise, unless "<number> [ti] super" is a real chip (4070 Ti Super, 4080 Super...)."""
    pre = m.group("pre") or ""
    if m.group("num"):
        name = m.group("num") + ("-ti" if m.group("ti") else "") + "-super"
        if name in _super_models():
            return pre + "super "
    return " " if not pre.strip() or not m.group("num") else pre


def norm(text: str) -> str:
    """Lowercase, strip diacritics (ł->l, ß->ss, ő->o ...), unify x/dash variants."""
    t = unicodedata.normalize("NFKC", text or "").lower()
    t = t.replace("ł", "l").replace("ß", "ss").replace("×", "x").replace("–", "-").replace("—", "-")
    t = "".join(c for c in unicodedata.normalize("NFKD", t) if not unicodedata.combining(c))
    # "super stan" / "super cena" is Polish/Czech praise, not the Super suffix
    t = _PRAISE.sub(_praise_repl, t)
    return t


# ---------------------------------------------------------------------------------------- flags

_WANTED = re.compile(
    r"(?<![a-z])(?:kupie|kupuje|szukam|poszukuje|odkupie|zakupie|skupuje|skup|suche|gesucht|koupim|hledam|"
    r"shanim|shanime|veszek|vennek|keresek|wanted|wtb|want to buy|looking for|looking to buy|in search of|"
    r"ankauf|wir kaufen|ich kaufe|kaufe)(?![a-z])")
_ISO = re.compile(r"(?<![A-Za-z])ISO(?![A-Za-z])")

_BOX_WORDS = ("pudelko", "karton", "krabice", "doboz", "opakowanie", "verpackung")
_BOX_STRONG = re.compile(
    r"box only|only box|empty box|(?:just|only) (?:the )?box|nur ovp|ovp nur|nur karton|karton nur|"
    r"nur (?:die )?(?:original ?)?verpackung|leere\w*\s+(?:verpackung|ovp|karton|box)|"
    r"(?:samo|sam|tylko|puste|pusty|jen|csak|ures|prazdna|prazdny|leer)\s+(?:pudelko|karton|krabice|doboz|box|opakowanie|verpackung|ovp)|"
    r"ovp\s+leer|leer\s+ovp|"
    r"(?:ohne|bez|without|nincs)\s+(?:die\s+)?(?:karte|karty|card|gpu|kartya|grafikkarte)|"
    r"(?:pudelko|karton|krabice|doboz|box)\s+(?:po|od|ohne|bez)(?![a-z])")
_BOX_WORD = re.compile(r"(?<![a-z])(?:%s)(?![a-z])" % "|".join(_BOX_WORDS))
# a box word right after one of these is "with/and a box", not a box-only ad
_BOX_CONNECTORS = {"+", "&", "/", "z", "mit", "with", "s", "i", "a", "and", "und", "oraz", "es", "w", "in", "im",
                   "ve", "ohne", "bez", "without", "plus", "inkl", "incl"}

_NEGATED_DEFECT = re.compile(
    r"nie\s?uszkodzon\w*|"
    r"bez\s+(?:zadnych\s+)?(?:uszkodzen|defektow|wad|artefaktow|usterek)\w*|"
    r"brak\s+(?:uszkodzen|defektow|artefaktow|wad|usterek)\w*|"
    r"nie\s+ma\s+(?:zadnych\s+)?(?:artefaktow|uszkodzen|defektow|wad|usterek)\w*|"
    r"ohne\s+(?:jegliche\s+)?(?:defekt|artefakt|mangel|schaden)\w*|"
    r"(?:kein|keine|keinen)\s+(?:defekt|artefakt|schaden)\w*|"
    r"(?:no|without|zero)\s+(?:defects?|artifacts?|artefacts?|damage|faults?)|"
    r"(?:non|not)[\s-]+(?:defective|damaged)|"
    r"nie\s+(?:jest|byla|byl|bylo)\s+(?:\w+\s+)?(?:uszkodzon|zepsut|wadliw|niesprawn)\w*|"
    r"nicht\s+(?:defekt|kaputt|beschadigt)\w*|"
    r"(?:not|isn't|isnt|never)\s+(?:broken|damaged|defective|faulty|dead)|"
    r"neni\s+(?:poskozen|vadn|rozbit)\w*|nem\s+(?:serult|elromlott)\w*|"
    r"nem\s+hibas\w*|hibatlan\w*|"
    r"bez\s+(?:zavad|zavady|vad|vady|poskozeni|artefaktu)\w*|neposkozen\w*")

# label -> pattern; any hit means for_parts
_DEFECTS = {
    "damaged": re.compile(r"uszkodzon|poskozen|porouchan|beschadigt|schaden|spalon|zalan|vypalen|kurzschluss"),
    "faulty": re.compile(r"defekt|defective|faulty|kaputt|hibas|nefunkcn|nefunguje|vadna|wadliw|niesprawn|"
                         r"do naprawy|broken|nem mukodik|nie dziala|not working|bastler|for repair|"
                         r"spares or repair|elromlott|(?<![a-z])dead(?![a-z])"),
    "no_display": re.compile(r"brak obrazu|bez obrazu|kein bild|kein signal|bildfehler|no display|no signal|"
                             r"no video|no post|nincs kep|nem ad kepet"),
    "artifacts": re.compile(r"artefakt|artifact"),
    "for_parts": re.compile(r"na czesci|na czesc|for parts|ersatzteil|na dily|pro dily|alkatresz"),
}

_LIKE_NEW = re.compile(r"jak nowa|jak nowy|jak nowe|like[\s-]new|wie neu|neuwertig|stan idealny|idealny stan|"
                       r"jako nova|jako novy|mint condition|mint az uj|ujszeru")
_NEW = re.compile(r"(?<![a-z])(?:nowa|nowy|nowe|nowka|new|neu|sealed|zaplombowan\w*|nova|novy|uj)(?![a-z])")
_USED = re.compile(r"uzywan\w*|(?<![a-z])used(?![a-z])|gebraucht|pouzit\w*|hasznalt|second[\s-]?hand|bazar")

# ---------------------------------------------------------------------------------------- bundles / context

_CPU_WORDS = (r"ryzen|core[\s-]*i[3579]|(?<![a-z])i[3579][\s-]\d{4,5}|threadripper|xeon|epyc|athlon|celeron|"
              r"pentium|(?<![a-z])cpu(?![a-z])|procesor|processor")
_CPU = re.compile(_CPU_WORDS)
_SYSTEM_CORE = (r"(?<!do\s)(?<!for\s)(?<!fur\s)(?<!pro\s)(?<!na\s)(?<![a-z])pc(?![a-z0-9])|"
                r"(?<![a-z])(?:komputer|computer|desktop|rechner|pocitac|szamitogep|workstation|barebone|"
                r"plyta glowna|motherboard|mainboard|imac|macbook\w*|mac|mac\s?mini|mac\s?pro)(?![a-z])|mini[\s-]?pc")
_LAPTOP_WORD = r"(?<!do\s)(?<!for\s)(?<!fur\s)(?<!pro\s)(?<!pre\s)(?<![a-z])(?:laptop|notebook|macbook)\w*"
_SYSTEM = re.compile(_SYSTEM_CORE + "|" + _CPU_WORDS)
_SYSTEM_LAPTOP = re.compile(_SYSTEM_CORE + "|" + _CPU_WORDS + "|" + _LAPTOP_WORD)
# "2x RTX 3090", "RTX 3090 x2", "2 szt": a lot of several cards, the price is not per card
_MULTI = re.compile(r"(?<![a-z0-9])[2-9]\s*(?:x|szt\.?|pcs|stk|stuck)\s*(?:nvidia|geforce|radeon|amd|rtx|rx|arc|a100|h100)|"
                    r"(?<![a-z0-9])(?:[2-9]|dwie|dwa|dwoch|trzy|cztery|zwei|drei|vier|dve|dva|tri|ctyri|ket|harom|negy|"
                    r"two|three|four)\s+(?:[a-z0-9+.-]+\s+){0,4}?"
                    r"(?:szt\.?|ks|db|pcs|stk|[a-z]*kart(?:y|en|e|ach|ami)|[a-z]*karet|[a-z]*kartya[a-z]*|"
                    r"cards|gpus)(?![a-z])|"
                    # a spelled number (or digit) straight before the chip, no noun: "zwei RTX 3080"
                    r"(?<![a-z0-9])(?:[2-9]|dwie|dwa|dwoch|trzy|cztery|zwei|drei|vier|dve|dva|tri|ctyri|ket|harom|negy|"
                    r"two|three|four)\s+(?:(?:nvidia|geforce|amd|radeon|intel)\s+)*(?:rtx|rx|arc|gtx)(?![a-z])|"
                    r"(?<![a-z0-9])[x*]\s*[2-9](?![a-z0-9])|(?<![a-z0-9])[2-9]\s*(?:szt\.?|pcs|stk)(?![a-z])")
_RAM_EVIDENCE = re.compile(r"(?<![a-z])ddr[\s-]?[345](?![0-9])|\d\s*(?:gb|go)\s*ram|(?<![a-z])ram\s*\d")
_SSD_EVIDENCE = re.compile(r"(?<![a-z])(?:ssd|nvme)(?![a-z])")

_MOBILE_MARKER = re.compile(
    r"(?<![a-z])(?:laptop\w*|notebook\w*|mobile|mobilni|max-?q|ntb|hordozhato\w*)(?![a-z])|"
    r"(?<![a-z])(?:zephyrus|legion|helios|razer blade|raider|stealth|katana|cyborg|alienware m\d+|nitro 5|victus|loq)"
    r"(?![a-z])")

# ---------------------------------------------------------------------------------------- not a product

# Merchandise that is never a card, wherever it appears in the title.
_MERCH = re.compile(
    r"(?<![a-z])(?:naklejk\w*|sticker\w*|plakat\w*|poster\w*|lego|mousepad|podkladk\w*|koszulk\w*|t-?shirt|etui|"
    r"anleitung)(?![a-z])")
# Accessories: they block the key only when they come BEFORE the chip (the accessory is the item sold:
# "Waterblock RTX 4090", "Kabel do RTX 4090"). After the chip they are an included extra ("RTX 4090 + kabel",
# "z chłodzeniem wodnym", "with original cooler", "Cooler Master edition") and the card keeps its key.
_ACCESSORY = re.compile(
    r"(?<![a-z])(?:waterblock|water[\s-]?block|wasserkuhl\w*|waku|wakue|backplate|kabel\w*|cable\w*|przewod\w*|"
    r"adapter\w*|cooler\w*|chlodzeni\w*|radiator\w*|uchwyt\w*|podstawk\w*|holder|bracket|stojak\w*|"
    r"zasilacz\w*|psu|netzteil\w*|manual|riser\w*)(?![a-z])")
_INCLUSION = {"+", "&", "z", "ze", "with", "w", "mit", "inkl", "incl", "plus", "oraz", "i", "a", "und", "and", "s",
              "es", "ses"}


def _accessory_after(after: str) -> bool:
    """An accessory noun in the first two tokens after the product, with no inclusion marker, is the item sold
    ("RTX 4090 Waterblock EK"). Further away, or after + / z / with / mit ..., it is an included extra."""
    if after.lstrip()[:1] in (",", ";", "(", "/", "+"):
        return False
    for tok in after.split()[:2]:
        bare = tok.strip(",;:.()")
        if tok.startswith("+") or bare in _INCLUSION:
            return False
        if bare != "manual" and _ACCESSORY.search(" " + bare + " "):
            return not (bare.startswith("cooler") and after.split()[after.split().index(tok) + 1: after.split().index(tok) + 2] == ["master"])
    return False


def _accessory_blocks(t: str, start: int, end: int) -> bool:
    return _ACCESSORY.search(t[:start]) is not None or _accessory_after(t[end:])


# "for <chip>": an accessory made for the card, or a swap ("na RTX 4090")
_FOR_CHIP = re.compile(r"(?<![a-z])(?:do|fur|for|pro|pod|k)\s+(?:(?:nvidia|geforce|amd|radeon|intel)\s+)?$")

# ---------------------------------------------------------------------------------------- GPU

_PREFIX = {
    "rtx": r"(?:(?:nvidia[\s-]*)?geforce[\s-]*(?:rtx[\s-]*)?|(?:nvidia[\s-]*)?rtx[\s-]*)",
    "rx": r"(?:(?:amd[\s-]*)?radeon[\s-]*(?:rx[\s-]*)?|(?:amd[\s-]*)?rx[\s-]*)",
    "arc": r"(?:(?:intel[\s-]*)?arc[\s-]*(?:graphics[\s-]*)?|intel[\s-]*)",
}
# after the model: no further suffix word may follow (so "4070" does not match "4070 Ti")
_EXT = r"(?![\s-]*(?:ti|super|xtx|xt|gre|d)(?![a-z0-9]))"
_GPU_CONTEXT = re.compile(r"pcie|sxm|hbm|(?<![a-z])(?:karta|karte|card|kartya)(?![a-z])|geforce|radeon|nvidia|tesla|quadro|(?<![a-z])amd(?![a-z])|(?<![a-z])gpu(?![a-z])|"
                          r"graphic|grafi[ck]|(?<![a-z])vga(?![a-z])|vram|(?<!l)gddr|videok|video\s?card")

# comparison words directly before a chip (up to 2 words in between) or right after it
_PRE_CMP = re.compile(
    r"(?:(?<![a-z])jak(?!\s+now)|(?<![a-z])like(?![\s-]*new)|(?<![a-z])wie(?!\s+neu)|(?<![a-z])vs\.?|"
    r"(?<![a-z])versus|lepsza niz|lepsze niz|lepszy niz|szybsza niz|szybsze niz|better than|faster than|"
    r"as fast as|as good as|equal to|equivalent to|gleich wie|besser als|schneller als|rowna|"
    r"porownywalna z|na poziomie|lepsi nez|rychlejsi nez|vykon jako|olyan mint|jobb mint|gyorsabb mint|"
    r"ahnlich|similar to|statt)\s+(?:[^\s,;:.()]+\s+){0,2}$")
_POST_CMP = re.compile(r"^[\s*-]*(?:like(?![\s-]*new)|level\b|class\b|killer\b|style\b|equivalent\b)")
_VERSUS = re.compile(r"(?<![a-z])(?:vs\.?|versus)(?![a-z])")


@lru_cache(maxsize=None)
def _compiled_gpu(category: str):
    """[(entry, regex, needs_context)] for every entry of a GPU category."""
    mobile = category == "gpu-mobile"
    out = []
    for e in taxonomy.gpu_entries(category):
        toks = e["id"].split("-")
        series = toks[0] if toks[0] in _PREFIX else None
        body_toks = toks[1:] if series else toks
        if body_toks[-2:] == ["ti", "super"]:
            head = r"[\s-]*".join(re.escape(t) for t in body_toks[:-2])
            body = head + r"[\s-]*(?:ti[\s-]*super|super[\s-]*ti)"
        else:
            body = r"[\s-]*".join(re.escape(t) for t in body_toks)
        pat = r"(?<![a-z0-9])"
        if series:
            pat += r"(?P<pfx>" + _PREFIX[series] + ")?"
        pat += body
        if mobile:
            pat += r"(?P<m>m)?"
        pat += r"(?![a-z0-9])" + _EXT
        # a bare match (no brand prefix) is trusted when the name is distinctive or has a suffix ("4070ti")
        bare_ok = bool(e.get("bare_ok")) or (series is not None and len(body_toks) > 1)
        out.append((e, re.compile(pat), not bare_ok))
    return out


def _gpu_hits(t: str, category: str) -> list[tuple[dict, int, int, bool]]:
    """(entry, start, end, glued_m) for each entry that matches, bare-match rules applied."""
    ctx = _GPU_CONTEXT.search(t) is not None
    cpu = _CPU.search(t) is not None
    hits = []
    for e, rx, needs_ctx in _compiled_gpu(category):
        for m in rx.finditer(t):
            gd = m.groupdict()
            has_pfx = gd.get("pfx") is not None
            if not has_pfx:
                if needs_ctx and not ctx:
                    continue
                if cpu and (needs_ctx or e["id"].split("-")[0] in _PREFIX):
                    continue
            hits.append((e, m.start(), m.end(), bool(gd.get("m"))))
    return hits


_SECOND_CHIP = re.compile(r"(?:\s*(?:ti|super|xt|xtx|d)\b)?(?:\s*\d{1,3}\s*(?:gb|g)\b)?\s*(?:/|\+|,|&|-|oraz|und|and|i|lub|albo|oder|or|nebo|a|es|"
                          r"vagy|plus)\s*(?:rtx\s*|rx\s*|gtx\s*)?(\d{4})(?![0-9])")


@lru_cache(maxsize=None)
def _model_numbers() -> set[str]:
    return {t for cat in ("gpu", "gpu-mobile") for e in taxonomy.gpu_entries(cat)
            for t in e["id"].split("-") if re.fullmatch(r"\d{4}", t)}


_GB = re.compile(r"(?<![\d.,x*])(\d{1,3})\s*-?\s*(?:gb|gib|go|g)(?![a-z0-9])")
_NOT_VRAM_AFTER = re.compile(r"^\s*(?:ddr\d?|ram|ssd|hdd|nvme|dysk|pamiec)")
_NOT_VRAM_BEFORE = re.compile(r"(?:ram|ddr\d?|ssd|nvme|hdd)\s*[:\-]?\s*$")


def _vram_candidates(t: str) -> list[int]:
    out = []
    for m in _GB.finditer(t):
        if _NOT_VRAM_AFTER.match(t[m.end():]) or _NOT_VRAM_BEFORE.search(t[: m.start()]):
            continue
        out.append(int(m.group(1)))
    return out


def _gpu(t: str, bundle: list[str]) -> tuple[str, str, float] | None:
    """(key, category, certainty), or None when no chip is named. A named but unresolvable chip returns key None."""
    if _VERSUS.search(t):
        return ("", "", 0.0) if (_gpu_hits(t, "gpu") or _gpu_hits(t, "gpu-mobile")) else None
    desktop, mobile = _gpu_hits(t, "gpu"), _gpu_hits(t, "gpu-mobile")
    is_mobile = _MOBILE_MARKER.search(t) is not None or any(h[3] for h in mobile)
    cat, hits = ("gpu-mobile", mobile) if is_mobile else ("gpu", desktop)
    if not hits and not (desktop or mobile):
        return None
    kept = []
    for e, s, en, _m in hits:
        if _PRE_CMP.search(t[:s]) or _POST_CMP.match(t[en:]):
            continue
        if _FOR_CHIP.search(t[:s]) or _accessory_blocks(t, s, en):
            return ("", "", 0.0)  # accessory "for" the chip, or an accessory noun before it
        if _SECOND_CHIP.match(t[en:]) and _SECOND_CHIP.match(t[en:]).group(1) in _model_numbers():
            return ("", "", 0.0)  # "RTX 4090/4080", "4090 + 3090": two cards
        kept.append(e)
    if not hits:  # chip only exists in the other table (e.g. "laptop RTX 4070 Ti"): unknown, never cross-mapped
        return ("", "", 0.0)
    if not kept:
        return None  # every mention is a comparison
    ids = {e["id"] for e in kept}
    if len(ids) != 1:
        return ("", "", 0.0)
    ent = kept[0]
    cands = _vram_candidates(t)
    valid = {v for v in cands if v in ent["vram"]}
    invalid = [v for v in cands if v not in ent["vram"]]
    if len(valid) > 1:
        return ("", "", 0.0)
    if len(valid) == 1:
        vram, cert = next(iter(valid)), (0.95 if not invalid else 0.7)
    elif invalid:
        return ("", "", 0.0)  # a memory size no such card has: wrong chip or not a card
    else:
        vram = ent["default_vram"]
        cert = 0.95 if len(ent["vram"]) == 1 else 0.7
    if bundle:
        cert = min(cert, 0.5)
    return taxonomy.canonical(cat, {"model": ent["id"], "vram": vram}), cat, cert


# ---------------------------------------------------------------------------------------- RAM

_DDR = re.compile(r"(?<![a-z])ddr[\s-]?([2345])(?![0-9])|(?<![a-z])pc([45])-\d{4,5}")
_KIT1 = re.compile(r"(?<![a-z0-9.])(\d)\s*[x*]\s*(\d{1,3})\s*(?:gb|gib|go|g)?(?![a-z0-9])")
_KIT2 = re.compile(r"(?<![a-z0-9.])(\d{1,3})\s*(?:gb|gib|go|g)\s*[x*]\s*(\d)(?![a-z0-9])")
_TOTAL = re.compile(r"(?<![a-z0-9.,])(\d{1,3})\s*(?:gb|gib|go|g)(?![a-z0-9])")
_NOT_RAM_NEAR = re.compile(r"^\s*(?:ssd|hdd|nvme|vram|gddr|dysk)|(?:ssd|hdd|nvme|vram|gddr|dysk)\s*[:\-]?\s*$")
_SERVER_RAM = re.compile(r"(?<![a-z])(?:ecc|reg|registered|rdimm|lrdimm|fb-?dimm|serwerow\w*|server)(?![a-z])")
_SODIMM = re.compile(r"so[\s-]?dimm|(?:do|for|fur|pro|na)\s+(?:laptop\w*|notebook\w*)|"
                     r"(?:laptop\w*|notebook\w*)\s+(?:ram|memory|pamiec|speicher)|"
                     r"(?:ram|pamiec|memory|speicher)\s+(?:laptop\w*|notebook\w*)")
_KIT_WORDS = re.compile(r"(?<![a-z])(?:kit|zestaw|dual(?:\s*channel)?|set|para|pair|komplet|quad)(?![a-z])")


def _ram(t: str, bundle: list[str]) -> tuple[str, str, float] | None:
    ddr = {a or b for a, b in _DDR.findall(t)}
    if not ddr:
        return None
    if ddr - {"4", "5"} or len(ddr) != 1:
        return ("", "", 0.0)  # DDR3 and older are not traded; DDR4+DDR5 in one title is a mess
    typ = f"ddr{next(iter(ddr))}"
    if _SERVER_RAM.search(t):
        return ("", "", 0.0)
    sodimm = _SODIMM.search(t) is not None
    if _SYSTEM_LAPTOP.search(t) and not sodimm:
        bundle.append("system")
        return ("", "", 0.0)
    if _SSD_EVIDENCE.search(t) or re.search(r"(?<![a-z])hdd(?![a-z])", t):
        bundle.append("ssd")
        return ("", "", 0.0)
    cfg = taxonomy.ram_config()
    kits = set()
    spans = []
    for rx, order in ((_KIT1, (0, 1)), (_KIT2, (1, 0))):
        for m in rx.finditer(t):
            count, size = int(m.group(1 + order[0])), int(m.group(1 + order[1]))
            if count in cfg["module_counts"] and size in cfg["module_gb"]:
                kits.add((count, size))
                spans.append(m.span())
    rest = t
    for s, e in sorted(spans, reverse=True):
        rest = rest[:s] + " " * (e - s) + rest[e:]
    totals = set()
    for m in _TOTAL.finditer(rest):
        if _NOT_RAM_NEAR.search(rest[m.end(): m.end() + 8]) or _NOT_RAM_NEAR.search(rest[max(0, m.start() - 8): m.start()]):
            continue
        if int(m.group(1)) in cfg["module_gb"]:
            totals.add(int(m.group(1)))
    cat = "ram-sodimm" if sodimm else "ram"
    if len(kits) > 1:
        return ("", "", 0.0)
    if kits:
        count, size = next(iter(kits))
        total = count * size
        if totals and totals != {total}:
            return ("", "", 0.0)
        cert = 0.95
    else:
        if len(totals) != 1 or _KIT_WORDS.search(t):
            return ("", "", 0.0)  # no memory size, several sizes, or "kit" without a module layout
        total, count, size = next(iter(totals)), 1, next(iter(totals))
        cert = 0.7
    return taxonomy.canonical(cat, {"type": typ, "total_gb": total, "kit": f"{count}x{size}"}), cat, cert


# ---------------------------------------------------------------------------------------- SSD

_SSD_SIGNAL = re.compile(r"(?<![a-z])(?:ssd|nvme|m\.?2)(?![a-z0-9])")
_SSD_EXCLUDE = re.compile(
    r"external|portable|przenosn\w*|zewnetrzn\w*|(?<![a-z])extern\w*|kulso|(?<![a-z])usb|enclosure|obudow\w*|"
    r"kieszen|adapter|caddy|(?<![a-z])u\.?2(?![a-z0-9])|(?<![a-z])sas(?![a-z])|msata|sshd")
_NVME = re.compile(r"nvme|pcie|pci-e|pci express|(?<![a-z])gen\s?[345](?![a-z0-9])")
_SATA = re.compile(r"(?<![a-z])sata")
_M2 = re.compile(r"(?<![a-z])m\.?2(?![a-z0-9])")
_INCH25 = re.compile(r"(?<![\d.])2\.5(?!\s*(?:tb|gb))(?![\d])")
_CAP = re.compile(r"(?<![a-z0-9.,])(\d{1,5}(?:[.,]\d{1,2})?)\s*(tb|gb)(?![a-z0-9])")
_RAM_NEAR = re.compile(r"^\s*(?:ram|ddr\d?)|(?:ram|ddr\d?|vram)\s*[:\-]?\s*$")


def _ssd(t: str, bundle: list[str]) -> tuple[str, str, float] | None:
    if not _SSD_SIGNAL.search(t):
        return None
    if _SSD_EXCLUDE.search(t):
        return ("", "", 0.0)
    if _SYSTEM_LAPTOP.search(t):
        bundle.append("system")
        return ("", "", 0.0)
    if _RAM_EVIDENCE.search(t):
        bundle.append("ram")
        return ("", "", 0.0)
    nvme, sata = _NVME.search(t) is not None, _SATA.search(t) is not None
    if nvme and sata:
        return ("", "", 0.0)
    if nvme:
        iface, cert = "nvme", 0.95
    elif sata:
        iface, cert = "sata", 0.95
    elif _M2.search(t):
        iface, cert = "nvme", 0.7
    elif _INCH25.search(t):
        iface, cert = "sata", 0.7
    else:
        return ("", "", 0.0)
    caps = set()
    for m in _CAP.finditer(t):
        if _RAM_NEAR.search(t[m.end(): m.end() + 6]) or _RAM_NEAR.search(t[max(0, m.start() - 6): m.start()]):
            continue
        try:
            caps.add(taxonomy.capacity_str(f"{m.group(1)}{m.group(2)}"))
        except ValueError:
            continue
    if len(caps) != 1:
        return ("", "", 0.0)
    return taxonomy.canonical("ssd", {"iface": iface, "capacity": next(iter(caps))}), "ssd", cert


# ---------------------------------------------------------------------------------------- whole PCs

_PC_NOUN = r"(?:komputer\w*|pc|computer|rechner|pocitac\w*|desktop|stacjonarn\w*)"
_PC_GAMING = r"(?:gamingow\w*|gaming|gamer|herni|do\s+gier|fur\s+gamer|zocker)"
_PC_TITLE = re.compile(
    r"(?<!do\s)(?<!for\s)(?<!fur\s)(?<!pro\s)(?<!na\s)(?<![a-z0-9])"
    r"(?:" + _PC_NOUN + r"[\s-]+" + _PC_GAMING + r"|" + _PC_GAMING + r"[\s-]*" + _PC_NOUN + r")(?![a-z0-9])")
_PC_ACCESSORY = re.compile(
    r"(?<![a-z])(?:case|gehause|obudow\w*|skrzyn\w*|chassis|tower|stolik|biurko|desk|fotel|krzeslo|chair|mysz|maus|"
    r"mouse|klawiatur\w*|keyboard|monitor|zasilacz\w*|netzteil|kabel\w*|cooler|chlodzeni\w*|naklejk\w*)(?![a-z])")


def _pc_parts(t: str) -> list[str]:
    parts = []
    if _gpu_hits(t, "gpu") or _gpu_hits(t, "gpu-mobile"):
        parts.append("gpu")
    if _CPU.search(t):
        parts.append("cpu")
    if _RAM_EVIDENCE.search(t):
        parts.append("ram")
    if _SSD_EVIDENCE.search(t):
        parts.append("ssd")
    return parts


def _pc(t: str) -> tuple[str, str, float, list[str]] | None:
    """`pc:bundle` for a whole gaming PC title (certainty 0.5, the bundle cap); bundle_items = detected parts.
    The key carries no price comparables of its own: `dh.partout` values the parts."""
    if not _PC_TITLE.search(t) or _PC_ACCESSORY.search(t) or _VERSUS.search(t):
        return None
    return "pc:bundle", "pc", 0.5, _pc_parts(t) or ["system"]


# ---------------------------------------------------------------------------------------- public API


def _box_only(t: str) -> bool:
    if _BOX_STRONG.search(t):
        return True
    for m in _BOX_WORD.finditer(t):
        head = t[: m.start()].rstrip()
        if head.endswith((",", ";", "(", "/", "+", "&")):  # one item of a list: "RTX 4090 24GB, pudelko, faktura"
            continue
        prev = head.split()
        if prev and prev[-1].strip(",;:.()") in _BOX_CONNECTORS:
            continue
        return True
    return False


def _flags(title_n: str, full_n: str, title_raw: str | None = None) -> tuple[bool, bool, bool, list[str]]:
    wanted = _WANTED.search(title_n) is not None or (title_raw is not None and _ISO.search(title_raw) is not None)
    box = _box_only(title_n)
    cleaned = _NEGATED_DEFECT.sub(" ", full_n)
    defects = [label for label, rx in _DEFECTS.items() if rx.search(cleaned)]
    return wanted, box, bool(defects), defects


def _condition(title_n: str, for_parts: bool) -> str:
    if for_parts:
        return "for_parts"
    if _LIKE_NEW.search(title_n):
        return "like_new"
    if _NEW.search(title_n):
        return "new"
    if _USED.search(title_n):
        return "used"
    return "unknown"


def rules_extract(title: str, description: str, lang: str) -> Extraction:
    """Extract a canonical product key and flags from a listing. See the module docstring for the rules."""
    t = norm(title)
    full = t + " \n " + norm(description)
    wanted, box_only, for_parts, defects = _flags(t, full, title)
    condition = _condition(t, for_parts)

    gpu_bundle: list[str] = []  # other products named in a GPU title: it is a PC/bundle, not a card
    if _SYSTEM.search(t):
        gpu_bundle.append("system")
    if _RAM_EVIDENCE.search(t):
        gpu_bundle.append("ram")
    if _SSD_EVIDENCE.search(t) and re.search(r"\d\s*(?:tb|gb)", t):
        gpu_bundle.append("ssd")
    if _MULTI.search(t):
        gpu_bundle.append("multiple")
    other_bundle: list[str] = []  # why a RAM/SSD title was rejected as a bundle

    if not (wanted or box_only or _MERCH.search(t)):
        pc = _pc(t)
        if pc:
            return Extraction(key=pc[0], category=pc[1], certainty=pc[2], condition=condition, defects=defects,
                              box_only=box_only, for_parts=for_parts, wanted_ad=wanted, bundle_items=pc[3],
                              method="rules")

    result: tuple[str, str, float] | None = None
    if not (wanted or box_only or _MERCH.search(t)):
        result = _gpu(t, gpu_bundle)
        if result is None:
            result = _ram(t, other_bundle) or _ssd(t, other_bundle)
            if result and result[0]:
                anchor = _DDR.search(t) if result[1].startswith("ram") else _SSD_SIGNAL.search(t)
                if anchor and _accessory_blocks(t, anchor.start(), anchor.end()):
                    result = None
    key, cat, cert = result if result and result[0] else (None, None, 0.0)
    items = gpu_bundle if (key and cat in taxonomy.GPU_CATEGORIES) else other_bundle
    return Extraction(
        key=key, category=cat, certainty=cert, condition=condition, defects=defects,
        box_only=box_only, for_parts=for_parts, wanted_ad=wanted, bundle_items=sorted(set(items)), method="rules")
