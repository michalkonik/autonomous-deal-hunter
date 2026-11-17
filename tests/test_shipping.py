"""Easy access to Poland: Polish offers and listings that ship here rank ahead of the rest."""
from types import SimpleNamespace

from dh.shipping import domestic_classified, easy_poland, inbound_pln, ship_pl


def test_overseas_inbound_is_not_the_european_envelope():
    table = {"PL": "20", "EU": "60", "non_EU": "200"}
    over = {"US": "500", "CA": "500", "BR": "550"}
    assert inbound_pln("PL", table, over) == 20
    assert inbound_pln("DE", table, over) == 60
    assert inbound_pln("CH", table, over) == 200
    assert inbound_pln("US", table, over) == 500
    assert inbound_pln("CA", table, over) == 500
    assert inbound_pln("BR", table, over) == 550


def test_easy_poland_is_a_polish_offer_or_a_shipment_to_poland():
    assert easy_poland("olx", "PL", 0) is True  # pickup in Poland is easy even when ship_pl is 0
    assert easy_poland("olx", None, 0) is True
    assert easy_poland("allegro_lokalnie", "PL", 1) is True
    assert easy_poland("ebay_de", "DE", 1) is True
    assert easy_poland("refurbed", "PL", 1) is True
    assert easy_poland("kleinanzeigen", "DE", 0) is False
    assert easy_poland("kleinanzeigen", "DE", None) is False
    assert easy_poland("kleinanzeigen", "PL", 0) is True


def test_ship_pl_still_marks_pickup_as_not_shipped():
    assert ship_pl(SimpleNamespace(source="olx", pickup_only=True)) == 0
    assert ship_pl(SimpleNamespace(source="ebay_de", pickup_only=False)) == 1
    assert ship_pl(SimpleNamespace(source="refurbed", pickup_only=False, country="PL")) == 1
    assert ship_pl(SimpleNamespace(source="taniekomputery", pickup_only=False, country="PL")) == 1
    assert ship_pl(SimpleNamespace(source="asgoodasnew", pickup_only=False, country="DE")) == 1
    assert ship_pl(SimpleNamespace(source="amso", pickup_only=False, country="PL")) == 1
    assert ship_pl(SimpleNamespace(source="luxtrade", pickup_only=False, country="PL")) == 1
    assert ship_pl(SimpleNamespace(source="devicestore", pickup_only=False, country="PL")) == 1
    assert ship_pl(SimpleNamespace(source="gebrauchtetechnik", pickup_only=False, country="DE")) == 1
    assert easy_poland("asgoodasnew", "DE", 1) is True
    assert easy_poland("luxtrade", "PL", 1) is True
    assert easy_poland("gebrauchtetechnik", "DE", 1) is True
    assert easy_poland("olx", "PL", ship_pl(SimpleNamespace(source="olx", pickup_only=True))) is True
    assert domestic_classified("tori") and domestic_classified("kleinanzeigen")
    assert domestic_classified("asgoodasnew") is False and domestic_classified("ebay_de") is False
