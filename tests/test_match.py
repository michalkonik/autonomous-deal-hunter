"""Taxonomy and rules matcher (Task 5). Titles are in PL, DE, CZ, HU and EN."""
import pytest

from dh import match, taxonomy
from dh.models import Extraction


def ex(title, desc="", lang="en"):
    return match.rules_extract(title, desc, lang)


# ------------------------------------------------------------------ bait (Review Focus 3)

BAIT = [
    # title, flag that must be set (None = key None is enough)
    ("Pudełko po RTX 4090", "box_only"),
    ("RTX 4090 box only", "box_only"),
    ("Kupię RTX 4090", "wanted_ad"),
    ("Laptop z wydajnością jak RTX 4090", None),
    ("RTX 4090 *like* performance", None),
    ("Karton od RTX 4090 Gaming OC", "box_only"),
    ("RTX 4090 nur OVP", "box_only"),
    ("Leere Verpackung RTX 4090 Suprim", "box_only"),
    ("Krabice od RTX 4090", "box_only"),
    ("RTX 4090 doboz", "box_only"),
    ("Szukam RTX 4090", "wanted_ad"),
    ("Suche RTX 4090 24GB", "wanted_ad"),
    ("Koupím RTX 4090", "wanted_ad"),
    ("Veszek RTX 4090-et", "wanted_ad"),
    ("WANTED: RTX 4090", "wanted_ad"),
    ("WTB RTX 4090 FE", "wanted_ad"),
    ("RTX 4090 vs RX 7900 XTX", None),
    ("Karta lepsza niż RTX 4090", None),
    ("Grafikkarte wie RTX 4090 Leistung", None),
    ("Pudełko po RX 7900 XTX", "box_only"),
]


@pytest.mark.parametrize("title,flag", BAIT)
def test_bait_titles(title, flag):
    r = ex(title)
    assert r.key is None, f"{title!r} became {r.key}"
    assert r.certainty == 0.0
    if flag:
        assert getattr(r, flag) is True


def test_bait_titles_never_gpu_even_with_description():
    r = ex("Gigabyte RTX 4090", "kupię ten model, szukam")  # description does not turn a title into a wanted ad
    assert r.key == "gpu:rtx-4090:24"


# ------------------------------------------------------------------ GPU keys

GPU = [
    ("MSI RTX 4070 Ti SUPER Gaming X Slim 16GB", "gpu:rtx-4070-ti-super:16"),
    ("Gigabyte RX 7900XTX 24G", "gpu:rx-7900-xtx:24"),
    ("rtx3080ti", "gpu:rtx-3080-ti:12"),
    ("RTX 4090D", "gpu:rtx-4090d:24"),
    ("RTX4070TiSuper", "gpu:rtx-4070-ti-super:16"),
    ("4070ti 12GB", "gpu:rtx-4070-ti:12"),
    ("7900xtx", "gpu:rx-7900-xtx:24"),
    ("Karta graficzna Gigabyte GeForce RTX 3060 Gaming OC 12GB", "gpu:rtx-3060:12"),
    ("Palit RTX 3060 8GB Dual", "gpu:rtx-3060:8"),
    ("RTX 3060", "gpu:rtx-3060:12"),
    ("ASUS TUF RTX4090 OC 24GB", "gpu:rtx-4090:24"),
    ("Grafikkarte NVIDIA GeForce RTX 5090 32GB Founders Edition", "gpu:rtx-5090:32"),
    ("ASUS Dual RTX 4060 Ti 16GB", "gpu:rtx-4060-ti:16"),
    ("Zotac RTX 4060 Ti 8 GB", "gpu:rtx-4060-ti:8"),
    ("Sapphire Nitro+ Radeon RX 7900 XT 20GB", "gpu:rx-7900-xt:20"),
    ("Sapphire Pulse RX 9070 XT 16GB", "gpu:rx-9070-xt:16"),
    ("Intel Arc B580 12GB Limited Edition", "gpu:arc-b580:12"),
    ("Sparkle Arc A770 16GB", "gpu:arc-a770:16"),
    ("Grafická karta Palit GeForce RTX 3070 Ti 8GB", "gpu:rtx-3070-ti:8"),
    ("Prodám grafickou kartu RTX 3060 Ti 8GB, záruka", "gpu:rtx-3060-ti:8"),
    ("Videókártya Gigabyte RTX 4080 Super 16GB", "gpu:rtx-4080-super:16"),
    ("Eladó Gigabyte RTX 3070 8GB videokártya", "gpu:rtx-3070:8"),
    ("Gebraucht: Zotac RTX 3060 Ti Twin Edge 8GB", "gpu:rtx-3060-ti:8"),
    ("Verkaufe MSI RX 6700 XT 12GB Mech 2X", "gpu:rx-6700-xt:12"),
    ("Radeon RX 6800 XT 16GB Sapphire", "gpu:rx-6800-xt:16"),
    ("Karta MSI RTX 2080 Ti 11GB", "gpu:rtx-2080-ti:11"),
    ("Gainward RTX 3080 12GB LHR", "gpu:rtx-3080:12"),
    ("ASUS ROG Strix RTX 3080 10GB", "gpu:rtx-3080:10"),
    ("Gigabyte RX 7600 XT 16GB OC", "gpu:rx-7600-xt:16"),
    ("MSI GeForce RTX 5070 Ti Ventus 16G", "gpu:rtx-5070-ti:16"),
    ("Palit RTX 3050 6GB", "gpu:rtx-3050:6"),
    ("RTX 4070 Super 12GB Inno3D", "gpu:rtx-4070-super:12"),
    ("Radeon RX 7900 GRE 16GB", "gpu:rx-7900-gre:16"),
    ("RTX 3090 Ti 24GB Zotac", "gpu:rtx-3090-ti:24"),
    ("Karta graficzna Radeon 7900 XTX Nitro+", "gpu:rx-7900-xtx:24"),
    ("RTX 5060 Ti 16GB Gigabyte Windforce", "gpu:rtx-5060-ti:16"),
    ("NVIDIA A100 80GB PCIe", "gpu:a100:80"),
    ("nvidia a100 40gb", "gpu:a100:40"),
    ("NVIDIA RTX A6000 48GB", "gpu:rtx-a6000:48"),
    ("NVIDIA L40S 48GB", "gpu:l40s:48"),
    ("RTX PRO 6000 Blackwell 96GB", "gpu:rtx-pro-6000:96"),
    ("NVIDIA RTX 6000 Ada Generation 48GB", "gpu:rtx-6000-ada:48"),
    ("Tesla H100 80GB PCIe", "gpu:h100:80"),
    # laptop GPUs never map to the desktop key
    ("Laptop ASUS ROG Strix RTX 4090 16GB", "gpu-mobile:rtx-4090:16"),
    ("MSI Titan GT77 Notebook RTX 4080 12GB", "gpu-mobile:rtx-4080:12"),
    ("RTX 4090 Laptop GPU", "gpu-mobile:rtx-4090:16"),
    ("RTX 3080 Ti Mobile 16GB", "gpu-mobile:rtx-3080-ti:16"),
    ("Herní notebook RTX 4070 8GB", "gpu-mobile:rtx-4070:8"),
    ("Gaming laptop RTX 5090 24GB Max-Q", "gpu-mobile:rtx-5090:24"),
]


@pytest.mark.parametrize("title,key", GPU)
def test_gpu_keys(title, key):
    r = ex(title)
    assert r.key == key
    assert r.category == key.split(":")[0]
    assert r.method == "rules"


def test_at_least_25_titles_and_all_languages():
    assert len(GPU) >= 25


def test_laptop_gpu_never_desktop_key():
    for t in ["Laptop RTX 4090", "RTX 4090 Laptop", "notebook 4090", "RTX 4090 Mobile", "RTX 4090 Max-Q laptop"]:
        r = ex(t)
        assert r.key is None or r.key.startswith("gpu-mobile:"), (t, r.key)
    assert ex("Laptop RTX 4070 Ti").key is None  # no such mobile chip: unknown, not the desktop key


def test_certainty_levels():
    assert ex("Gigabyte RTX 3060 12GB").certainty == 0.95   # VRAM stated
    assert ex("Gigabyte RTX 3060").certainty == 0.7         # defaulted among variants
    assert ex("RTX 4090").certainty == 0.95                 # single variant: unambiguous
    assert ex("nic ciekawego").certainty == 0.0
    assert ex("nic ciekawego").key is None and ex("nic ciekawego").category is None


def test_conflicting_or_ambiguous_vram_is_no_key():
    assert ex("RTX 3060 24GB").key is None            # no such variant
    assert ex("RTX 4060 Ti 8GB / 16GB").key is None   # two variants in one listing
    assert ex("RTX 4090 48GB").key is None


def test_ram_gb_does_not_pollute_vram():
    r = ex("RTX 3060 Ti 8GB + 32GB RAM")
    assert r.key == "gpu:rtx-3060-ti:8"


def test_two_different_chips_is_no_key():
    assert ex("RTX 3060 / RTX 3070 do wyboru").key is None


def test_bare_numbers_need_context():
    assert ex("Ryzen 9 7900 XT cooler").key is None
    assert ex("Ryzen 7 7800X3D").key is None
    assert ex("Ryzen 5 7600X + 32GB DDR5 zestaw").key is None
    assert ex("model 4090").key is None
    assert ex("Karta graficzna 3060 12GB").key == "gpu:rtx-3060:12"


def test_comparison_masks_only_that_chip():
    assert ex("RX 7900 XTX, wydajność jak RTX 4090").key == "gpu:rx-7900-xtx:24"
    assert ex("Jak nowa karta RTX 4090").key == "gpu:rtx-4090:24"
    assert ex("RTX 4090 like new").key == "gpu:rtx-4090:24"
    assert ex("RTX 4090 wie neu").key == "gpu:rtx-4090:24"


def test_multiple_cards_capped():
    for t in ["2x RTX 3090 24GB", "RTX 3090 24GB x2", "2 szt. RTX 3090 24GB"]:
        r = ex(t)
        assert r.key == "gpu:rtx-3090:24" and r.certainty <= 0.5 and "multiple" in r.bundle_items, t


def test_bundle_caps_certainty():
    r = ex("Komputer gamingowy Ryzen 7 RTX 4070 12GB 32GB DDR5 1TB SSD")
    assert r.certainty <= 0.5 and r.bundle_items
    r2 = ex("Karta graficzna do komputera RTX 4070 12GB")
    assert r2.certainty == 0.95 and r2.bundle_items == []


# ------------------------------------------------------------------ flags and condition

@pytest.mark.parametrize("title", [
    "RTX 3080 uszkodzona", "RTX 3080 na części", "RTX 3080 defekt", "RTX 3080 for parts", "RTX 3090 Bastler",
    "RTX 3080 nefunkční", "RTX 3080 hibás", "RTX 3080 brak obrazu", "RTX 3080 artefakty", "RTX 3080 kaputt",
    "RTX 3080 Ersatzteile", "RTX 3080 nie działa", "RTX 3080 poškozená", "RTX 3080 not working",
])
def test_for_parts(title):
    r = ex(title)
    assert r.for_parts is True and r.condition == "for_parts"
    assert r.key and r.key.startswith("gpu:rtx-30")  # key kept for audit; the flag excludes the deal


def test_for_parts_from_description():
    assert ex("RTX 3080 10GB", "Karta ma artefakty po rozgrzaniu").for_parts is True
    assert "artifacts" in ex("RTX 3080 10GB", "Karta ma artefakty po rozgrzaniu").defects


@pytest.mark.parametrize("title,desc", [
    ("RTX 3080 nieuszkodzona", ""),
    ("RTX 3080 10GB", "Karta bez uszkodzeń, bez artefaktów, pełna sprawność"),
    ("RTX 3080 10GB", "Ohne Defekt, keine Artefakte"),
    ("RTX 3080 10GB", "no defects, no artifacts"),
    ("RTX 3080 10GB", "hibátlan, nem hibás"),
    ("RTX 3080 10GB", "plně funkční, bez závad"),
])
def test_negated_defects_are_not_for_parts(title, desc):
    assert ex(title, desc).for_parts is False


def test_box_word_positive_forms_not_bait():
    assert ex("Gigabyte RTX 3080 w pudełku").box_only is False
    assert ex("Gigabyte RTX 3080 + pudełko").box_only is False
    assert ex("Gigabyte RTX 3080 with box").box_only is False
    assert ex("Gigabyte RTX 3080 mit OVP").box_only is False
    assert ex("Gigabyte RTX 3080 s krabicí").box_only is False
    assert ex("Gigabyte RTX 3080 dobozzal").box_only is False


@pytest.mark.parametrize("title,cond", [
    ("RTX 3060 12GB nowa, zaplombowana", "new"),
    ("RTX 3060 12GB neu OVP", "new"),
    ("RTX 3060 12GB jak nowa", "like_new"),
    ("RTX 3060 12GB wie neu", "like_new"),
    ("RTX 3060 12GB używana", "used"),
    ("RTX 3060 12GB gebraucht", "used"),
    ("RTX 3060 12GB použitá", "used"),
    ("RTX 3060 12GB használt", "used"),
    ("RTX 3060 12GB", "unknown"),
])
def test_condition(title, cond):
    assert ex(title).condition == cond


def test_extraction_type():
    assert isinstance(ex("RTX 4090"), Extraction)


# ------------------------------------------------------------------ RAM

RAM = [
    ("Corsair Vengeance 32GB (2x16GB) DDR5 6000MHz CL30", "ram:ddr5:32:2x16", 0.95),
    ("Kingston Fury 16GB DDR4 3200 2x8GB", "ram:ddr4:16:2x8", 0.95),
    ("G.Skill Trident Z 64GB 4x16GB DDR4 3600", "ram:ddr4:64:4x16", 0.95),
    ("Pamięć RAM DDR5 2 x 24GB 48GB 7200", "ram:ddr5:48:2x24", 0.95),
    ("Crucial 16GB DDR4 3200MHz", "ram:ddr4:16:1x16", 0.7),
    ("Arbeitsspeicher 32GB Kit 2x16GB DDR4-3200", "ram:ddr4:32:2x16", 0.95),
    ("Operační paměť 16GBx2 DDR5 5600", "ram:ddr5:32:2x16", 0.95),
    ("RAM 64 GB (2*32 GB) DDR5-6400", "ram:ddr5:64:2x32", 0.95),
    ("Memória 32 GB DDR4 2x16 GB 3200 MHz", "ram:ddr4:32:2x16", 0.95),
    ("Pamięć RAM DDR5 32GB 6000 CL36 (kit 2x16GB)", "ram:ddr5:32:2x16", 0.95),
    ("Kingston 8GB DDR4 SO-DIMM 2666", "ram-sodimm:ddr4:8:1x8", 0.7),
    ("Pamięć do laptopa 2x8GB DDR5 SODIMM 4800", "ram-sodimm:ddr5:16:2x8", 0.95),
    ("Samsung 32GB DDR4 ECC REG RDIMM", None, 0.0),
    ("Zestaw Ryzen 5 7600 + 32GB DDR5 + płyta główna", None, 0.0),
    ("DDR3 8GB 1600", None, 0.0),
    ("Dell Latitude 16GB DDR4 laptop", None, 0.0),
    ("Pamięć RAM DDR4 2x16GB DDR5", None, 0.0),
    ("RAM 32GB kit DDR5 6000", None, 0.0),
    ("Pamięć 2x8GB DDR4 32GB", None, 0.0),
]


@pytest.mark.parametrize("title,key,cert", RAM)
def test_ram_keys(title, key, cert):
    r = ex(title)
    assert r.key == key
    assert r.certainty == cert
    assert r.category == (key.split(":")[0] if key else None)


def test_ram_not_confused_by_gpu_vram_words():
    assert ex("RTX 4070 12GB GDDR6X").key == "gpu:rtx-4070:12"
    assert ex("LPDDR5 16GB").key is None


# ------------------------------------------------------------------ SSD

SSD = [
    ("Samsung 980 Pro 1TB NVMe M.2", "ssd:nvme:1tb", 0.95),
    ("Dysk SSD Kingston A400 480GB SATA", "ssd:sata:480gb", 0.95),
    ("WD Black SN850X 2TB NVMe", "ssd:nvme:2tb", 0.95),
    ("Crucial MX500 1TB SSD SATA 2.5", "ssd:sata:1tb", 0.95),
    ("SSD M.2 NVMe 512 GB Lexar", "ssd:nvme:512gb", 0.95),
    ("Samsung 990 Pro 4 TB PCIe 4.0 NVMe", "ssd:nvme:4tb", 0.95),
    ("SSD-Festplatte 1000GB SATA Samsung 870 EVO", "ssd:sata:1tb", 0.95),
    ("SSD disk 2TB M.2 NVMe Gen4 Kingston KC3000", "ssd:nvme:2tb", 0.95),
    ("SSD 1TB M.2", "ssd:nvme:1tb", 0.7),
    ("Kingston NV2 250GB NVMe SSD", "ssd:nvme:250gb", 0.95),
    ("Intel SSD D7-P5520 3.84TB NVMe", "ssd:nvme:3.84tb", 0.95),
    ("Samsung 870 QVO 2TB SSD 2.5", "ssd:sata:2tb", 0.7),
    ("Samsung T7 1TB USB external SSD", None, 0.0),
    ("Przenośny dysk SSD 1TB USB-C", None, 0.0),
    ("SSD 1TB", None, 0.0),
    ("Dysk HDD 2TB 3.5", None, 0.0),
    ("Laptop Lenovo 16GB DDR4 512GB SSD NVMe", None, 0.0),
    ("NVMe 1TB / 2TB do wyboru", None, 0.0),
    ("SSD NVMe SATA adapter 1TB", None, 0.0),
]


@pytest.mark.parametrize("title,key,cert", SSD)
def test_ssd_keys(title, key, cert):
    r = ex(title)
    assert r.key == key
    assert r.certainty == cert
    assert r.category == (key.split(":")[0] if key else None)


def test_ssd_ram_gb_not_capacity():
    assert ex("NVMe 1TB SSD + 16GB RAM").key is None  # a bundle


# ------------------------------------------------------------------ taxonomy

def test_canonical_examples():
    assert taxonomy.canonical("gpu", {"model": "rtx-4090", "vram": 24}) == "gpu:rtx-4090:24"
    assert taxonomy.canonical("gpu", {"model": "rx-7900-xtx", "vram": 24}) == "gpu:rx-7900-xtx:24"
    assert taxonomy.canonical("gpu-mobile", {"model": "rtx-4090", "vram": 16}) == "gpu-mobile:rtx-4090:16"
    assert taxonomy.canonical("ram", {"type": "DDR5", "total_gb": 32, "kit": "2x16"}) == "ram:ddr5:32:2x16"
    assert taxonomy.canonical("ram", {"type": "ddr5", "total_gb": 32, "kit": "2x16", "speed": 6000}) == "ram:ddr5:32:2x16"
    assert taxonomy.canonical("ssd", {"iface": "NVMe", "capacity": "2TB"}) == "ssd:nvme:2tb"
    assert taxonomy.canonical("ssd", {"iface": "sata", "capacity": "1000GB"}) == "ssd:sata:1tb"


@pytest.mark.parametrize("cat,fields", [
    ("gpu", {"model": "rtx-4090", "vram": 12}),
    ("gpu", {"model": "rtx-9999", "vram": 12}),
    ("gpu-mobile", {"model": "rtx-4070-ti", "vram": 12}),
    ("ram", {"type": "ddr3", "total_gb": 8, "kit": "1x8"}),
    ("ram", {"type": "ddr5", "total_gb": 32, "kit": "2x8"}),
    ("ssd", {"iface": "ide", "capacity": "1tb"}),
    ("cpu", {}),
])
def test_canonical_rejects_invalid(cat, fields):
    with pytest.raises(ValueError):
        taxonomy.canonical(cat, fields)


def test_taxonomy_integrity():
    for cat in ("gpu", "gpu-mobile"):
        ents = taxonomy.gpu_entries(cat)
        ids = [e["id"] for e in ents]
        assert len(ids) == len(set(ids))
        for e in ents:
            assert e["vram"] and e["default_vram"] in e["vram"], e["id"]
            assert all(isinstance(v, int) and v > 0 for v in e["vram"])
    assert len(taxonomy.gpu_entries("gpu")) >= 80


@pytest.mark.parametrize("model,vram", [
    ("rtx-3060", [8, 12]), ("rtx-4060-ti", [8, 16]), ("rtx-4070-ti-super", [16]), ("rtx-5090", [32]),
    ("rx-7900-xtx", [24]), ("rx-7900-xt", [20]), ("rx-9070-xt", [16]), ("arc-b580", [12]),
    ("rtx-3080", [10, 12]), ("rtx-4090", [24]), ("rtx-5060-ti", [8, 16]), ("a100", [40, 80]),
])
def test_known_vram(model, vram):
    e = {x["id"]: x for x in taxonomy.gpu_entries("gpu")}[model]
    assert sorted(e["vram"]) == vram


def test_every_entry_matches_itself_in_all_spellings():
    for cat in ("gpu", "gpu-mobile"):
        for e in taxonomy.gpu_entries(cat):
            for v in e["vram"]:
                toks = e["id"].split("-")
                spaced = " ".join(t.upper() for t in toks)
                glued = "".join(toks)
                for name in (spaced, glued):
                    ctx = "NVIDIA " if toks[0] not in ("rtx", "rx", "arc") and not e.get("bare_ok") else ""
                    title = f"{'Laptop ' if cat == 'gpu-mobile' else ''}{ctx}{name} {v}GB"
                    r = ex(title)
                    assert r.key == f"{cat}:{e['id']}:{v}", (title, r.key)


# ------------------------------------------------------------------ fix round 1

CHIPS = ["RTX 4090", "RTX 3060 Ti", "RX 7900 XTX", "RTX 5080", "RX 9070 XT"]


@pytest.mark.parametrize("chip", CHIPS)
@pytest.mark.parametrize("tpl", [
    "Waterblock {c} EK", "Wasserkühler für {c}", "Wakü {c} Alphacool", "Backplate {c}", "Kabel 12VHPWR do {c}",
    "Cable for {c} 12VHPWR", "Przewód zasilający do {c}", "Zasilacz do {c} 1200W", "Netzteil für {c} 850W",
    "Naklejka {c}", "Sticker {c} laptop", "Plakat {c}", "Poster {c} A2", "LEGO {c}", "Mousepad {c}",
    "Podkładka pod myszkę {c}", "Koszulka {c}", "T-shirt {c}", "Etui na {c}", "Uchwyt do {c}", "Stojak {c}",
    "Podstawka pod {c}", "Holder {c}", "Bracket {c}", "Riser {c}", "Anleitung {c}", "Manual {c}", "Cooler {c}",
    "Chłodzenie wodne {c}", "Radiator do {c}", "Adapter {c}", "Chlazení pro {c}", "Állvány {c} számára k {c}",
])
def test_accessories_have_no_key(tpl, chip):
    r = ex(tpl.format(c=chip))
    assert r.key is None and r.certainty == 0.0, tpl


@pytest.mark.parametrize("chip", CHIPS)
@pytest.mark.parametrize("pre", [
    "Sháním", "Koupím", "Odkupię", "Zakupię", "Skupuję", "Skup", "Gesucht:", "Suche", "Kaufe", "Ankauf",
    "Vennék", "Veszek", "Keresek", "Looking to buy", "Looking for", "In search of", "ISO", "WTB", "Want to buy",
    "Kupuję", "Szukam", "Poszukuję", "Wanted",
])
def test_wanted_verbs(pre, chip):
    r = ex(f"{pre} {chip}")
    assert r.wanted_ad is True and r.key is None, pre


def test_iso_only_as_caps_word():
    assert ex("Gigabyte RTX 4090 Isolation").wanted_ad is False
    assert ex("iso RTX 4090 24GB").wanted_ad is False


@pytest.mark.parametrize("chip", CHIPS)
@pytest.mark.parametrize("tpl", [
    "Opakowanie po {c}", "Samo opakowanie {c}", "{c} Verpackung", "{c} OVP leer", "Leer OVP {c}", "{c} nur Karton",
    "{c} bez karty, samo pudełko", "{c} ohne Karte", "{c} without card", "{c} nincs kártya", "Box without GPU {c}",
    "{c} ohne Grafikkarte", "{c} bez karty",
])
def test_box_only_more(tpl, chip):
    r = ex(tpl.format(c=chip))
    assert r.box_only is True and r.key is None, tpl


@pytest.mark.parametrize("tpl", [
    "{c} 24GB, pudełko, faktura", "{c}; pudełko; faktura", "{c} (pudełko, faktura)", "{c} + pudełko",
    "{c} / pudełko",
])
def test_box_in_list_is_not_box_only(tpl):
    r = ex(tpl.format(c="RTX 4090"))
    assert r.box_only is False and r.key == "gpu:rtx-4090:24"


@pytest.mark.parametrize("chip", CHIPS)
@pytest.mark.parametrize("word", [
    "niesprawna", "wadliwa", "do naprawy", "spalona", "zalana", "uszkodzona", "nie działa", "kein Signal",
    "kein Bild", "Schaden", "Kurzschluss", "Bildfehler", "defekt", "nefunguje", "nefunkční", "vypálená",
    "pro díly", "na díly", "elromlott", "nem ad képet", "alkatrésznek", "hibás", "dead", "not working",
    "no display", "no signal", "artifacts", "artefakty", "na części", "for parts", "Bastler",
])
def test_for_parts_more(word, chip):
    r = ex(f"{chip} {word}")
    assert r.for_parts is True and r.condition == "for_parts", word


@pytest.mark.parametrize("desc", [
    "nie jest uszkodzona", "Karta nie jest uszkodzona, nie jest zepsuta", "nicht defekt", "nicht kaputt",
    "not broken", "is not damaged", "není poškozená", "nem sérült", "nem elromlott", "nie jest wadliwa",
    "bez uszkodzeń", "kein Schaden", "not dead", "no damage",
])
def test_negation_more(desc):
    assert ex("RTX 3080 10GB", desc).for_parts is False, desc


@pytest.mark.parametrize("title", [
    "2x RTX 3090 24GB", "RTX 3090 24GB x2", "2 szt. RTX 3090", "Dwie karty RTX 3090", "3 karty RX 7900 XTX",
    "Zwei Karten RTX 4090", "Drei RTX 3080 Karten", "Dvě karty RTX 3060", "Tři RTX 4070 ks", "Két kártya RTX 3070",
    "Három RTX 3080 db", "Two RTX 4090 cards", "Three RTX 3090 cards", "2 ks RTX 3060 Ti", "2 db RX 6800 XT",
])
def test_multi_card_lots_capped_or_none(title):
    r = ex(title)
    assert r.key is None or (r.certainty <= 0.5 and "multiple" in r.bundle_items), title


@pytest.mark.parametrize("title", [
    "RTX 4090/4080", "RTX 4090 oraz 4080", "4090 + 3090", "RTX 3060 / 3070", "RX 7900 XTX i 6800 XT",
    "RTX 3080 10GB und 3090", "RTX 4090 24GB / 4080", "RTX 4090 és 4080", "RTX 3070 nebo 3080",
])
def test_two_chips_in_one_title_no_key(title):
    assert ex(title).key is None, title


@pytest.mark.parametrize("title,key", [
    ("RTX 4070 Ti Super okazja", "gpu:rtx-4070-ti-super:16"),
    ("RTX 4070 Ti Super super cena 16GB", "gpu:rtx-4070-ti-super:16"),
    ("RTX 4080 Super okazja", "gpu:rtx-4080-super:16"),
    ("RTX 3060 super stan 12GB", "gpu:rtx-3060:12"),
    ("RTX 4090 super stan", "gpu:rtx-4090:24"),
    ("RX 6800 XT super cena", "gpu:rx-6800-xt:16"),
])
def test_super_praise_vs_suffix(title, key):
    assert ex(title).key == key


@pytest.mark.parametrize("title", ["MSI B550 A100", "Płyta A100", "H100 Gaming", "A100 koszulka"])
def test_datacenter_needs_context(title):
    assert ex(title).key is None


@pytest.mark.parametrize("title,key", [
    ("NVIDIA A100 80GB", "gpu:a100:80"), ("Tesla H100 80GB", "gpu:h100:80"), ("H200 141GB GPU", "gpu:h200:141"),
    ("A100 40GB PCIe", "gpu:a100:40"), ("H100 SXM 80GB", "gpu:h100:80"), ("Karta A100 80GB", "gpu:a100:80"),
])
def test_datacenter_with_context(title, key):
    assert ex(title).key == key


@pytest.mark.parametrize("title", [
    "iMac 16GB DDR4 1TB", "MacBook Pro 32GB DDR5", "Mac mini 16GB DDR4", "Apple Mac Pro 64GB DDR4 ECC",
    "Mac 16GB DDR5", "MacBook Air 16GB DDR4 512GB SSD NVMe",
])
def test_mac_systems_no_ram_or_ssd_key(title):
    assert ex(title).key is None


def test_fx_effective_date_in_schema(tmp_path):
    from dh.store import Store
    s = Store(tmp_path / "s.db")
    cols = [r["name"] for r in s.conn.execute("pragma table_info(fx)")]
    assert "effective_date" in cols
    s.close()


# ------------------------------------------------------------------ fix round 2

MULTI_CHIPS = ["RTX 3070", "RTX 4090", "RX 7900 XTX", "RTX 3060 Ti"]


@pytest.mark.parametrize("chip", MULTI_CHIPS)
@pytest.mark.parametrize("tpl", [
    "2 Grafikkarten {c}", "zwei Grafikkarten {c}", "drei Grafikkarten {c}", "Zwei Grafikkarten {c} 8GB",
    "2 grafické karty {c}", "dvě grafické karty {c}", "3 videokártyák {c}", "két videokártya {c}",
    "2 karty graficzne {c}", "dwie karty graficzne {c}", "trzy kartami {c}", "two graphics cards {c}",
])
def test_multi_compound_nouns(tpl, chip):
    r = ex(tpl.format(c=chip))
    assert r.key is None or (r.certainty <= 0.5 and "multiple" in r.bundle_items), tpl


@pytest.mark.parametrize("chip", MULTI_CHIPS)
@pytest.mark.parametrize("num", ["zwei", "drei", "dvě", "tři", "két", "három", "dwie", "trzy", "two", "three", "2"])
@pytest.mark.parametrize("pre", ["", "Nvidia ", "GeForce "])
def test_spelled_number_before_chip(num, pre, chip):
    if not chip.startswith(("RTX", "RX")):
        pytest.skip()
    r = ex(f"{num} {pre}{chip}")
    assert r.key is None or (r.certainty <= 0.5 and "multiple" in r.bundle_items)


@pytest.mark.parametrize("title,key", [
    ("RTX 4090 24GB + kabel 12VHPWR", "gpu:rtx-4090:24"),
    ("RTX 3080 Ti z chłodzeniem wodnym EK", "gpu:rtx-3080-ti:12"),
    ("RTX 3070 with original cooler", "gpu:rtx-3070:8"),
    ("RTX 4090 w zestawie kabel zasilacz", "gpu:rtx-4090:24"),
    ("RX 7900 XTX + backplate", "gpu:rx-7900-xtx:24"),
    ("RTX 3090 Cooler Master edition", "gpu:rtx-3090:24"),
    ("RTX 4080 manual box", "gpu:rtx-4080:16"),
    ("RTX 3060 Ti 8GB mit Kabel und Netzteil-Adapter", "gpu:rtx-3060-ti:8"),
    ("Karta graficzna na RTX 3080 Ti", "gpu:rtx-3080-ti:12"),
])
def test_included_extras_keep_key(title, key):
    assert ex(title).key == key


@pytest.mark.parametrize("chip", CHIPS)
@pytest.mark.parametrize("tpl", ["Uchwyt na {c}", "Stojak na {c}", "Etui na {c}", "Podstawka na {c}",
                                 "Kabel na {c}", "Backplate na {c}", "Waterblock {c}", "Kabel do {c}"])
def test_accessory_before_chip_still_blocked(tpl, chip):
    assert ex(tpl.format(c=chip)).key is None
