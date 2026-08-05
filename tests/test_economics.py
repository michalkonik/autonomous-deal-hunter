"""Frozen economics engine: 10 hand-computed worked examples plus rule tests.

Every expected number below was computed by hand (see the comment block above each example and
docs/ECONOMICS.md) before the engine existed. Notation: r(x) = round to 0.01 PLN, ROUND_HALF_UP.
All lines are rounded separately, then added. Fixed fake FX (PLN per unit):
    EUR 4.3951   USD 3.6500   CZK 0.1750   JPY 0.0245
Defaults (config/economics.yaml, data/fees.yaml): liniowy 19% + 4.9% health = 23.9% of max(0, pre-tax);
VAT 23% (23/123 of a gross price); returns reserve 5% of exit; FX spread 1% of foreign-currency amounts;
fiscal cost per B2C sale 0; clearance max(69 PLN, 2.5% of duties+taxes); allegro 8% + 1 PLN/item;
olx 0%; ebay_de 11% + 0.35 EUR/item and paid out in EUR.
"""
from dataclasses import replace
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

import pytest
import yaml

from dh import config, customs, economics
from dh.economics import Line, NetInputs, NetResult

REPO = Path(__file__).resolve().parent.parent
RATES = {"PLN": Decimal("1"), "EUR": Decimal("4.3951"), "USD": Decimal("3.6500"),
         "CZK": Decimal("0.1750"), "JPY": Decimal("0.0245")}
D0 = date(2026, 10, 29)   # before the EUR 2 handling fee
D1 = date(2026, 11, 2)    # EUR 2 handling fee applies (from 2026-11-01)


def q2(x: Decimal) -> Decimal:
    return x.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


class FakeFx:
    """Same interface and rounding as dh.money.Fx, fixed rates, no network or DB."""

    async def rate(self, ccy, day):
        return RATES[ccy.upper()]

    async def to_pln(self, amount, ccy, day):
        return q2(Decimal(amount) * RATES[ccy.upper()])

    async def pln_to_usd(self, amount_pln, day):
        return q2(Decimal(amount_pln) / RATES["USD"])


class FakeCustoms:
    """Duty lookup: fraction per (cn_code, origin); records calls."""

    def __init__(self, rates=None):
        self.rates, self.calls = rates or {}, []

    async def duty_rate(self, cn_code, origin, day):
        self.calls.append((cn_code, origin, day))
        assert origin not in customs.EU, "engine must not look up duty for EU origins"
        return Decimal(self.rates.get((cn_code, origin), "0"))


def make_cfg(**economics_over):
    data = {f.stem: yaml.safe_load(f.read_text()) or {} for f in sorted((REPO / "config").glob("*.yaml"))}
    data["economics"] = config.deep_merge(data["economics"], economics_over)
    return config.Config(data)


def inp(**kw) -> NetInputs:
    base = dict(buy_price=Decimal("0"), buy_ccy="PLN", origin_country="PL", seller_is_business=False,
                has_vat_invoice=False, exit_channel="allegro", exit_price_cons_pln=Decimal("0"),
                exit_price_exp_pln=Decimal("0"), inbound_ship_pln=Decimal("0"),
                outbound_ship_pln=Decimal("0"), pickup_km=None, items=1, day=D0, category="gpu", p_scam=0.0)
    base.update(kw)
    return NetInputs(**base)


P = Decimal

# ------------------------------------------------------------------ the 10 worked examples
# Each entry: inputs, economics overrides, expected cons net, exp net, capital, flags.
EXAMPLES = {
    # 1. PL private -> Allegro, margin scheme. buy 2000 PLN, inbound 20, outbound 25, p_scam 0.02.
    #   cons E=2600: fees r(.08*2600)+1 = 208.00+1 = 209.00
    #     VAT margin r(23/123*(2600-2000)) = 13800/123 = 112.195 -> 112.20
    #     returns r(.05*2600) = 130.00; risk r(.02*2000) = 40.00; fiscal 0.00
    #     pre-tax = 2600-209-2000-20-112.20-25-130-0-40 = 63.80
    #     tax r(.239*63.80) = 15.2482 -> 15.25; net = 48.55
    #   exp E=2800: fees 224+1 = 225.00; VAT 18400/123 = 149.5935 -> 149.59; returns 140.00
    #     pre-tax = 2800-225-2000-20-149.59-25-140-40 = 200.41
    #     tax r(.239*200.41) = 47.89799 -> 47.90; net = 152.51
    #   capital = 2000 + 20 = 2020.00; flags none
    1: (inp(buy_price=P("2000"), exit_price_cons_pln=P("2600"), exit_price_exp_pln=P("2800"),
            inbound_ship_pln=P("20"), outbound_ship_pln=P("25"), p_scam=0.02),
        {}, P("48.55"), P("152.51"), P("2020.00"), set()),

    # 2. PL business with VAT invoice -> Allegro. buy 3075 PLN gross, inbound 30, outbound 30, p_scam 0.01.
    #   input VAT r(3075*23/123) = 70725/123 = 575.00 exactly
    #   cons E=4200: fees 336+1 = 337.00; output VAT 96600/123 = 785.3659 -> 785.37; VAT due 785.37-575.00 = 210.37
    #     returns 210.00; risk r(.01*3075) = 30.75
    #     pre-tax = 4200-337-3075-30-210.37-30-210-0-30.75 = 276.88
    #     tax r(.239*276.88) = 66.17432 -> 66.17; net = 210.71
    #   exp E=4500: fees 360+1 = 361.00; output VAT 103500/123 = 841.4634 -> 841.46; due 266.46; returns 225.00
    #     pre-tax = 4500-361-3075-30-266.46-30-225-30.75 = 481.79
    #     tax r(.239*481.79) = 115.14781 -> 115.15; net = 366.64
    #   capital = 3075 + 30 = 3105.00; flags none
    2: (inp(buy_price=P("3075"), seller_is_business=True, has_vat_invoice=True,
            exit_price_cons_pln=P("4200"), exit_price_exp_pln=P("4500"),
            inbound_ship_pln=P("30"), outbound_ship_pln=P("30"), p_scam=0.01),
        {}, P("210.71"), P("366.64"), P("3105.00"), set()),

    # 3. CZ private -> OLX, FX in CZK. buy 14000 CZK = r(14000*0.1750) = 2450.00 PLN; inbound 45, outbound 15,
    #    p_scam 0.03. OLX fee 0%, PLN payout. FX spread on the CZK buy: r(.01*2450) = 24.50.
    #   cons E=3000: fees 0.00; VAT margin r(23*550/123) = 12650/123 = 102.8455 -> 102.85; returns 150.00
    #     risk r(.03*2450) = 73.50
    #     pre-tax = 3000-0-2450-45-102.85-15-150-0-24.50-73.50 = 139.15
    #     tax r(.239*139.15) = 33.25685 -> 33.26; net = 105.89
    #   exp E=3200: VAT 17250/123 = 140.2439 -> 140.24; returns 160.00
    #     pre-tax = 3200-2450-45-140.24-15-160-24.50-73.50 = 291.76
    #     tax r(.239*291.76) = 69.73064 -> 69.73; net = 222.03
    #   capital = 2450 + 24.50 (spread) + 45 = 2519.50; flags bdo_check (origin not PL)
    3: (inp(buy_price=P("14000"), buy_ccy="CZK", origin_country="CZ", exit_channel="olx",
            exit_price_cons_pln=P("3000"), exit_price_exp_pln=P("3200"),
            inbound_ship_pln=P("45"), outbound_ship_pln=P("15"), p_scam=0.03),
        {}, P("105.89"), P("222.03"), P("2519.50"), {"bdo_check"}),

    # 4. DE private -> eBay.de. buy 450 EUR = r(450*4.3951) = r(1977.795) = 1977.80; inbound 60, outbound 80,
    #    p_scam 0.02. ebay_de: 11% + r(0.35*4.3951) = r(1.538285) = 1.54; payout in EUR -> exit spread too.
    #   buy spread r(19.778) = 19.78; risk r(.02*1977.80) = r(39.556) = 39.56
    #   cons E=2900: fees 319.00+1.54 = 320.54; VAT margin r(23*922.20/123) = 21210.6/123 = 172.4439 -> 172.44
    #     returns 145.00; exit spread 29.00
    #     pre-tax = 2900-320.54-1977.80-60-172.44-80-145-0-19.78-29.00-39.56 = 55.88
    #     tax r(.239*55.88) = 13.35532 -> 13.36; net = 42.52
    #   exp E=3100: fees 341.00+1.54 = 342.54; VAT 25810.6/123 = 209.8423 -> 209.84; returns 155.00; exit spread 31.00
    #     pre-tax = 3100-342.54-1977.80-60-209.84-80-155-19.78-31.00-39.56 = 184.48
    #     tax r(.239*184.48) = 44.09072 -> 44.09; net = 140.39
    #   capital = 1977.80 + 19.78 + 60 = 2057.58; flags bdo_check
    4: (inp(buy_price=P("450"), buy_ccy="EUR", origin_country="DE", exit_channel="ebay_de",
            exit_price_cons_pln=P("2900"), exit_price_exp_pln=P("3100"),
            inbound_ship_pln=P("60"), outbound_ship_pln=P("80"), p_scam=0.02),
        {}, P("42.52"), P("140.39"), P("2057.58"), {"bdo_check"}),

    # 5. US business -> Allegro, import on 2026-11-02. buy 1000 USD = 3650.00; inbound 200; duty rate 0 (fake);
    #    outbound 30, p_scam 0.02, category gpu (CN 8473302000).
    #   CIF = 3650+200 = 3850.00; duty r(0*3850) = 0.00
    #   value 3650/4.3951 = EUR 830 > 150 -> only the EUR 2 handling fee: r(2*4.3951) = r(8.7902) = 8.79
    #   import VAT r(.23*(3850+0+8.79)) = r(.23*3858.79) = r(887.5217) = 887.52
    #   clearance max(69, r(.025*(0+8.79+887.52))) = max(69, 22.41) = 69.00
    #   buy spread 36.50; risk r(.02*3650) = 73.00
    #   cons E=6400: fees 512+1 = 513.00; output VAT 147200/123 = 1196.7480 -> 1196.75; due 1196.75-887.52 = 309.23
    #     returns 320.00
    #     pre-tax = 6400-513-3650-200-0-8.79-887.52-69-309.23-30-320-0-36.50-73 = 302.96
    #     tax r(.239*302.96) = 72.40744 -> 72.41; net = 230.55
    #   exp E=6900: fees 552+1 = 553.00; output VAT 158700/123 = 1290.2439 -> 1290.24; due 402.72; returns 345.00
    #     pre-tax = 6900-553-3650-200-0-8.79-887.52-69-402.72-30-345-36.50-73 = 644.47
    #     tax r(.239*644.47) = 154.02833 -> 154.03; net = 490.44
    #   capital = 3650+36.50+200+0+8.79+69+887.52 = 4851.81; flags bdo_check
    5: (inp(buy_price=P("1000"), buy_ccy="USD", origin_country="US", seller_is_business=True,
            exit_price_cons_pln=P("6400"), exit_price_exp_pln=P("6900"),
            inbound_ship_pln=P("200"), outbound_ship_pln=P("30"), p_scam=0.02, day=D1),
        {}, P("230.55"), P("490.44"), P("4851.81"), {"bdo_check"}),

    # 6. JP private via forwarder -> Allegro, import on 2026-11-02, low-value consignment.
    #    buy 20000 JPY = r(20000*0.0245) = 490.00; inbound (forwarder + shipping) 120; duty 0 (fake);
    #    category ram; outbound 15; p_scam 0.04.
    #   value 490/4.3951 = EUR 111.49 <= 150 -> EUR 3 + EUR 2 = EUR 5 = r(21.9755) = 21.98
    #   CIF 610.00; duty 0.00; import VAT r(.23*631.98) = r(145.3554) = 145.36
    #   clearance max(69, r(.025*167.34)=4.18) = 69.00; buy spread 4.90; risk r(.04*490) = 19.60
    #   cons E=1100: fees 88+1 = 89.00; output VAT 25300/123 = 205.6911 -> 205.69; due 60.33; returns 55.00
    #     pre-tax = 1100-89-490-120-0-21.98-145.36-69-60.33-15-55-4.90-19.60 = 9.83
    #     tax r(.239*9.83) = 2.34937 -> 2.35; net = 7.48
    #   exp E=1250: fees 100+1 = 101.00; output VAT 28750/123 = 233.7398 -> 233.74; due 88.38; returns 62.50
    #     pre-tax = 1250-101-490-120-21.98-145.36-69-88.38-15-62.50-4.90-19.60 = 112.28
    #     tax r(.239*112.28) = 26.83492 -> 26.83; net = 85.45
    #   capital = 490+4.90+120+0+21.98+69+145.36 = 851.24; flags bdo_check
    6: (inp(buy_price=P("20000"), buy_ccy="JPY", origin_country="JP", category="ram",
            exit_price_cons_pln=P("1100"), exit_price_exp_pln=P("1250"),
            inbound_ship_pln=P("120"), outbound_ship_pln=P("15"), p_scam=0.04, day=D1),
        {}, P("7.48"), P("85.45"), P("851.24"), {"bdo_check"}),

    # 7. Loss: PL private -> OLX, overpaid. buy 3000, inbound 20, outbound 0, p_scam 0.05.
    #   cons E=2900: fees 0.00; VAT margin max(0, 2900-3000) = 0.00; returns 145.00; risk 150.00
    #     pre-tax = 2900-0-3000-20-0-0-145-0-150 = -415.00; tax max(0, ...) = 0.00; net = -415.00
    #   exp E=3100: VAT r(23*100/123) = 18.699 -> 18.70; returns 155.00
    #     pre-tax = 3100-3000-20-18.70-0-155-150 = -243.70; tax 0.00; net = -243.70
    #   capital = 3020.00; flags none
    7: (inp(buy_price=P("3000"), exit_channel="olx", exit_price_cons_pln=P("2900"),
            exit_price_exp_pln=P("3100"), inbound_ship_pln=P("20"), p_scam=0.05),
        {}, P("-415.00"), P("-243.70"), P("3020.00"), set()),

    # 8. Pickup 40 km: PL private -> Allegro. buy 1800, pickup r(1.2*2*40 + 40) = 136.00, inbound 0,
    #    outbound 25, p_scam 0.01. Margin base = buy price only (pickup not in it).
    #   cons E=2600: fees 209.00; VAT r(23*800/123) = 149.59; returns 130.00; risk 18.00
    #     pre-tax = 2600-209-1800-0-136-149.59-25-130-0-18 = 132.41
    #     tax r(.239*132.41) = 31.64599 -> 31.65; net = 100.76
    #   exp E=2750: fees 220+1 = 221.00; VAT 21850/123 = 177.6423 -> 177.64; returns 137.50
    #     pre-tax = 2750-221-1800-136-177.64-25-137.50-18 = 234.86
    #     tax r(.239*234.86) = 56.13154 -> 56.13; net = 178.73
    #   capital = 1800 + 0 + 136 = 1936.00; flags none
    8: (inp(buy_price=P("1800"), exit_price_cons_pln=P("2600"), exit_price_exp_pln=P("2750"),
            outbound_ship_pln=P("25"), pickup_km=40.0, p_scam=0.01),
        {}, P("100.76"), P("178.73"), P("1936.00"), set()),

    # 9. Ryczalt: the deal of example 1 with tax_mode ryczalt and ryczalt_health_pln_per_sale 5.
    #   pre-tax as in example 1: cons 63.80, exp 200.41
    #   cons tax = r(.03*2600) + 5 = 78.00+5 = 83.00; net = 63.80-83.00 = -19.20
    #   exp tax = r(.03*2800) + 5 = 84.00+5 = 89.00; net = 200.41-89.00 = 111.41
    #   capital 2020.00; flags none
    9: (inp(buy_price=P("2000"), exit_price_cons_pln=P("2600"), exit_price_exp_pln=P("2800"),
            inbound_ship_pln=P("20"), outbound_ship_pln=P("25"), p_scam=0.02),
        {"tax": {"tax_mode": "ryczalt", "ryczalt_health_pln_per_sale": 5}},
        P("-19.20"), P("111.41"), P("2020.00"), set()),

    # 10. Capital over the 30000 USD cap: 30 GPUs from a PL business with invoice -> Allegro.
    #    buy 120000 PLN gross, items 30, inbound 300, outbound 750, p_scam 0.01.
    #   input VAT r(120000*23/123) = 2760000/123 = 22439.0244 -> 22439.02; allegro fixed fee 1 PLN x 30 = 30
    #   risk 1200.00
    #   cons E=150000: fees 12000+30 = 12030.00; output VAT 3450000/123 = 28048.7805 -> 28048.78
    #     due 28048.78-22439.02 = 5609.76; returns 7500.00
    #     pre-tax = 150000-12030-120000-300-5609.76-750-7500-0-1200 = 2610.24
    #     tax r(.239*2610.24) = 623.84736 -> 623.85; net = 1986.39
    #   exp E=160000: fees 12800+30 = 12830.00; output VAT 3680000/123 = 29918.6992 -> 29918.70
    #     due 7479.68; returns 8000.00
    #     pre-tax = 160000-12830-120000-300-7479.68-750-8000-1200 = 9440.32
    #     tax r(.239*9440.32) = 2256.23648 -> 2256.24; net = 7184.08
    #   capital = 120000 + 300 = 120300.00 = r(120300/3.65) = 32958.90 USD > 30000 -> flag over_capital_cap
    10: (inp(buy_price=P("120000"), seller_is_business=True, has_vat_invoice=True, items=30,
             exit_price_cons_pln=P("150000"), exit_price_exp_pln=P("160000"),
             inbound_ship_pln=P("300"), outbound_ship_pln=P("750"), p_scam=0.01),
         {}, P("1986.39"), P("7184.08"), P("120300.00"), {"over_capital_cap"}),
}


@pytest.mark.parametrize("n", sorted(EXAMPLES))
async def test_worked_example(n):
    i, over, cons, exp, capital, flags = EXAMPLES[n]
    res = await economics.net(i, make_cfg(**over), FakeFx(), FakeCustoms())
    assert isinstance(res, NetResult)
    assert res.cons_net_pln == cons
    assert res.exp_net_pln == exp
    assert res.capital_pln == capital
    assert set(res.flags) == flags
    # the breakdown is the whole story: lines add up to the net, every line is 2 dp
    assert sum(l.pln for l in res.lines_cons) == res.cons_net_pln
    assert sum(l.pln for l in res.lines_exp) == res.exp_net_pln
    for line in res.lines_cons + res.lines_exp:
        assert isinstance(line, Line) and line.pln == q2(line.pln) and line.label


async def test_example_5_line_breakdown_and_duty_lookup():
    fc = FakeCustoms()
    res = await economics.net(EXAMPLES[5][0], make_cfg(), FakeFx(), fc)
    assert fc.calls == [("8473302000", "US", D1)]
    lines = {l.label: l.pln for l in res.lines_cons}
    assert lines["exit price"] == P("6400.00")
    assert lines["channel fees"] == P("-513.00")
    assert lines["buy price"] == P("-3650.00")
    assert lines["inbound shipping"] == P("-200.00")
    assert lines["customs duty"] == P("0.00")
    assert lines["EU import fees"] == P("-8.79")
    assert lines["import VAT"] == P("-887.52")
    assert lines["clearance fee"] == P("-69.00")
    assert lines["VAT due"] == P("-309.23")
    assert lines["outbound shipping"] == P("-30.00")
    assert lines["returns reserve"] == P("-320.00")
    assert lines["fiscal cost"] == P("0.00")
    assert lines["FX spread (buy)"] == P("-36.50")
    assert lines["risk haircut"] == P("-73.00")
    assert lines["income tax"] == P("-72.41")


async def test_cons_and_exp_differ_only_by_exit_price():
    res = await economics.net(EXAMPLES[1][0], make_cfg(), FakeFx(), FakeCustoms())
    same = lambda i: replace(i, exit_price_exp_pln=i.exit_price_cons_pln)  # noqa: E731
    res2 = await economics.net(same(EXAMPLES[1][0]), make_cfg(), FakeFx(), FakeCustoms())
    assert res2.lines_cons == res2.lines_exp
    assert res.lines_cons == res2.lines_cons


async def test_import_with_duty_and_clearance_percentage():
    # US business, buy 20000 USD = 73000.00, inbound 500, duty 6.5% (fake), 2026-11-02, items 1.
    #   CIF 73500.00; duty r(.065*73500) = 4777.50; EUR 2 handling = 8.79
    #   import VAT r(.23*(73500+4777.50+8.79)) = r(.23*78286.29) = r(18005.8467) = 18005.85
    #   clearance max(69, r(.025*(4777.50+8.79+18005.85))) = r(.025*22792.14) = r(569.8035) = 569.80
    #   capital = 73000+730.00+500+4777.50+8.79+569.80+18005.85 = 97591.94
    fc = FakeCustoms({("8473302000", "US"): "0.065"})
    i = inp(buy_price=P("20000"), buy_ccy="USD", origin_country="US", seller_is_business=True,
            exit_price_cons_pln=P("100000"), exit_price_exp_pln=P("100000"),
            inbound_ship_pln=P("500"), day=D1)
    res = await economics.net(i, make_cfg(), FakeFx(), fc)
    lines = {l.label: l.pln for l in res.lines_cons}
    assert lines["customs duty"] == P("-4777.50")
    assert lines["import VAT"] == P("-18005.85")
    assert lines["clearance fee"] == P("-569.80")
    assert res.capital_pln == P("97591.94")


async def test_intra_eu_business_purchase_needs_vat_ue():
    # DE business, 500 EUR = r(2197.55) = 2197.55; seller's invoice gross, no deductible PL VAT:
    # VAT due = output VAT = r(3000*23/123) = 69000/123 = 560.9756 -> 560.98
    i = inp(buy_price=P("500"), buy_ccy="EUR", origin_country="DE", seller_is_business=True,
            has_vat_invoice=True, exit_price_cons_pln=P("3000"), exit_price_exp_pln=P("3000"))
    res = await economics.net(i, make_cfg(), FakeFx(), FakeCustoms())
    assert {l.label: l.pln for l in res.lines_cons}["VAT due"] == P("-560.98")
    assert set(res.flags) == {"needs_vat_ue", "bdo_check"}
    registered = await economics.net(i, make_cfg(tax={"vat_ue_registered": True}), FakeFx(), FakeCustoms())
    assert set(registered.flags) == {"bdo_check"}
    assert registered.cons_net_pln == res.cons_net_pln


async def test_pl_business_without_invoice_has_no_input_vat():
    # Not a private person, so no margin scheme; no invoice, so nothing to deduct.
    # VAT due = r(2600*23/123) = 59800/123 = 486.1789 -> 486.18
    i = inp(buy_price=P("2000"), seller_is_business=True, exit_price_cons_pln=P("2600"),
            exit_price_exp_pln=P("2600"))
    res = await economics.net(i, make_cfg(), FakeFx(), FakeCustoms())
    assert {l.label: l.pln for l in res.lines_cons}["VAT due"] == P("-486.18")


async def test_vat_refund_keeps_sign():
    # Invoice buy 3075 (input 575.00), sale 3000: output 69000/123 = 560.98 -> due -14.02 (a refund, +14.02)
    i = inp(buy_price=P("3075"), seller_is_business=True, has_vat_invoice=True,
            exit_price_cons_pln=P("3000"), exit_price_exp_pln=P("3000"))
    res = await economics.net(i, make_cfg(), FakeFx(), FakeCustoms())
    assert {l.label: l.pln for l in res.lines_cons}["VAT due"] == P("14.02")


async def test_skala_mode():
    # Example 1 under skala: 12% + 9% = 21% of pre-tax.
    #   cons r(.21*63.80) = 13.398 -> 13.40; net 50.40.  exp r(.21*200.41) = 42.0861 -> 42.09; net 158.32
    res = await economics.net(EXAMPLES[1][0], make_cfg(tax={"tax_mode": "skala"}), FakeFx(), FakeCustoms())
    assert (res.cons_net_pln, res.exp_net_pln) == (P("50.40"), P("158.32"))


async def test_fiscal_cost_per_b2c_sale():
    # Example 1 with a 2.50 PLN fiscal cost: pre-tax 63.80-2.50 = 61.30; tax r(.239*61.30) = 14.6507 -> 14.65
    # net = 46.65
    res = await economics.net(EXAMPLES[1][0], make_cfg(fiscal_cost_per_b2c_sale_pln=2.5), FakeFx(), FakeCustoms())
    assert res.cons_net_pln == P("46.65")


async def test_pickup_out_of_range_flag():
    i = inp(buy_price=P("1000"), exit_price_cons_pln=P("1500"), exit_price_exp_pln=P("1500"), pickup_km=70.0)
    res = await economics.net(i, make_cfg(), FakeFx(), FakeCustoms())
    assert {l.label: l.pln for l in res.lines_cons}["pickup"] == P("-208.00")  # 1.2*2*70 + 40
    assert "pickup_out_of_range" in res.flags


async def test_import_needs_duty_lookup_and_known_cn():
    i = inp(buy_price=P("100"), buy_ccy="USD", origin_country="US", exit_price_cons_pln=P("900"),
            exit_price_exp_pln=P("900"))
    with pytest.raises(ValueError, match="duty"):
        await economics.net(i, make_cfg(), FakeFx())
    with pytest.raises(ValueError, match="CN code"):
        await economics.net(replace(i, category="toaster"), make_cfg(), FakeFx(), FakeCustoms())
    # a 10-digit CN code may be given directly as the category
    fc = FakeCustoms()
    await economics.net(replace(i, category="8471709800"), make_cfg(), FakeFx(), fc)
    assert fc.calls[0][0] == "8471709800"


async def test_unknown_channel_raises():
    with pytest.raises(ValueError, match="channel"):
        await economics.net(inp(exit_channel="vinted", buy_price=P("1")), make_cfg(), FakeFx(), FakeCustoms())


async def test_bad_inputs_raise():
    with pytest.raises(ValueError, match="p_scam"):
        await economics.net(inp(buy_price=P("1"), p_scam=1.5), make_cfg(), FakeFx(), FakeCustoms())
    with pytest.raises(ValueError, match="items"):
        await economics.net(inp(buy_price=P("1"), items=0), make_cfg(), FakeFx(), FakeCustoms())


def test_eu_members():
    assert len(economics.EU) == 27
    assert economics.EU == customs.EU
    assert "GB" not in economics.EU and "CH" not in economics.EU and "NO" not in economics.EU


def test_fee_and_shipping_data_files():
    fees = economics.load_fees()
    assert (fees["allegro"]["pct"], fees["allegro"]["fixed"]) == (P("0.08"), P("1"))
    assert fees["olx"]["pct"] == 0
    assert fees["ebay_pl"]["pct"] == P("0.09")
    assert (fees["ebay_de"]["pct"], fees["ebay_de"]["fixed"], fees["ebay_de"]["fixed_ccy"]) == (P("0.11"), P("0.35"), "EUR")
    assert fees["kleinanzeigen"]["pct"] == 0
    ship = economics.load_shipping()
    assert ship["outbound_pln"]["default"] > 0 and ship["inbound_pln"]["PL"] > 0


async def test_best_exit_ignores_disabled_channels():
    # kleinanzeigen (0% fee) beats allegro on these prices but is not in exit_channels.enabled.
    base = inp(buy_price=P("2000"), exit_price_cons_pln=P("2600"), exit_price_exp_pln=P("2800"),
               inbound_ship_pln=P("20"), outbound_ship_pln=P("25"))
    cfg, fx, cu = make_cfg(), FakeFx(), FakeCustoms()
    results = {ch: await economics.net(replace(base, exit_channel=ch), cfg, fx, cu)
               for ch in ("allegro", "kleinanzeigen", "ebay_pl")}
    assert "channel_disabled" in results["kleinanzeigen"].flags
    assert results["kleinanzeigen"].cons_net_pln > results["allegro"].cons_net_pln
    ch, best = economics.best_exit(results)
    assert ch == "allegro" and best is results["allegro"]
    # an explicit enabled list is honoured too
    assert economics.best_exit(results, enabled=["ebay_pl"])[0] == "ebay_pl"
    with pytest.raises(ValueError):
        economics.best_exit({"kleinanzeigen": results["kleinanzeigen"]})
