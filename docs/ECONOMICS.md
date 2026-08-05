# Economics engine

`dh/economics.py` computes the net profit of one resale deal, for one exit channel, for a Polish
VAT-registered sole trader (JDG). It is part of the **frozen layer**: the agent never changes it, its
config (`config/economics.yaml`) or its tests. A change is a human code change, re-checked against
the 10 worked examples below.

The defaults are conservative and are waiting for the owner's accountant (see spec §7 and
`ACCOUNTANT-QUESTIONS.md`).

## Inputs and outputs

`net(NetInputs, cfg, fx, customs) -> NetResult`

- **NetInputs**: buy price and currency, origin country, whether the seller is a business, whether
  there is a VAT invoice, the exit channel, the conservative (P25) and expected (median) exit price in
  PLN, inbound and outbound shipping in PLN, pickup distance in km (or none), number of items, date,
  category (`gpu|ram|ssd|pc` or a 10-digit CN code), scam probability.
- **NetResult**: one list of lines per scenario (conservative, expected), the two nets, the capital
  tied up, and flags.

The two scenarios differ **only** in the exit price. Every other line is the same.

## Rounding

All money is PLN `Decimal`. Every line is rounded to 0.01 PLN (ROUND_HALF_UP) by itself, then the lines
are added. Cost lines are negative. The lines of a scenario always add up exactly to its net, so a
deal card can be re-added by hand.

## Formula

Notation: `r()` rounds to 0.01 PLN. `E` = exit price (P25 or median), gross, in PLN.
`B` = buy price in PLN = r(buy price × NBP mid rate of the day). `items` = units bought; each is
resold as a separate sale.

| Line | Formula | Applies when |
|---|---|---|
| exit price | `E` | always |
| channel fees | `r(pct × E) + r(fixed × items × rate(fixed_ccy))` from `data/fees.yaml` | always |
| buy price | `−B` | always |
| inbound shipping | `−inbound_ship_pln` | always |
| pickup | `−r(pln_per_km × 2 × km + pln_per_trip)` (config/pickup.yaml: 1.2 PLN/km, 40 PLN/trip) | pickup listing |
| customs duty | `−r(duty_rate × CIF)`, `CIF = B + inbound`, rate from ISZTAR by CN code | origin outside the EU |
| EU import fees | `−r(EUR fees × EUR rate)`: EUR 3/item if goods value ≤ EUR 150 (from 2026-07-01), plus EUR 2/item handling (from 2026-11-01) | origin outside the EU |
| import VAT | `−r(23% × (CIF + duty + import fees))` | origin outside the EU |
| clearance fee | `−max(69 PLN, r(2.5% × (duty + import fees + import VAT)))` | origin outside the EU |
| VAT due | see the VAT table below; a refund is positive | always |
| outbound shipping | `−outbound_ship_pln` | always |
| returns reserve | `−r(5% × E)` | B2C channel |
| fiscal cost | `−r(fiscal_cost_per_b2c_sale_pln × items)` (default 0) | B2C channel |
| FX spread (buy) | `−r(1% × B)` | buy currency is not PLN |
| FX spread (exit) | `−r(1% × E)` | channel pays out in a currency other than PLN (eBay.de, Kleinanzeigen) |
| risk haircut | `−r(p_scam × B)` | always |
| **pre-tax profit** | sum of the lines above | |
| income tax | see the tax table below | always |
| **net** | pre-tax profit − income tax | |

**Capital** (cash out before the sale) = buy price + FX spread (buy) + inbound shipping + pickup + duty +
import fees + clearance + import VAT. With a VAT invoice the buy price already includes the
seller's VAT.

### VAT

`23/123` is the VAT inside a gross price at 23%. `output = r(E × 23/123)`.

| Purchase | Regime | VAT due |
|---|---|---|
| Private person, in Poland or another EU state, no invoice | margin scheme | `r(23/123 × max(0, E − B))`. The base is the buy price only: shipping and pickup are not in it |
| Polish seller with a VAT invoice | standard | `output − r(B × 23/123)` |
| Polish business, no VAT invoice | standard, nothing to deduct | `output` |
| Business in another EU state | standard, nothing deductible in PL; flag `needs_vat_ue` while `vat_ue_registered: false` | `output` |
| Any seller outside the EU (import) | standard | `output − import VAT` |

VAT due can be negative (a refund). The sign is kept.

### Income tax

| `tax_mode` | Income tax |
|---|---|
| `liniowy` (default) | `r((19% + 4.9%) × max(0, pre-tax))` = 23.9% of profit |
| `skala` | `r((12% + 9%) × max(0, pre-tax))` = 21% of profit |
| `ryczalt` | `r(3% × E) + r(ryczalt_health_pln_per_sale × items)`, due even on a loss |

### Flags

| Flag | When |
|---|---|
| `bdo_check` | origin is not PL (possible BDO/WEEE "wprowadzający" duty) |
| `needs_vat_ue` | business seller in another EU state while `vat_ue_registered: false` |
| `over_capital_cap` | capital in USD (NBP mid) > `rules.capital_cap_usd` (30000) |
| `pickup_out_of_range` | pickup farther than `pickup.max_km` (60) or outside `pickup.countries` |
| `channel_disabled` | the channel is not in `exit_channels.enabled`; `best_exit` skips it |

`best_exit(results)` returns the channel with the highest **conservative** net among enabled channels.

### Simplifications (documented, conservative unless stated)

- **Health contribution** under liniowy is a flat 4.9% of the deal's pre-tax profit. Minimums, caps
  and its partial deductibility are ignored. Under ryczałt the real health contribution is a fixed
  monthly amount; `ryczalt_health_pln_per_sale` (default 0) lets the owner spread it per sale.
- **Ryczałt revenue** is the gross exit price. By statute a VAT payer's revenue excludes VAT, so this
  overstates the tax a little (conservative).
- **Income tax** is applied to pre-tax profit after the returns reserve and risk haircut, as in spec §7.
  These are expected-value reserves, not booked costs, so in a deal with no return and no scam the real
  tax is slightly higher than shown.
- **Business without a VAT invoice in Poland** (e.g. a VAT-exempt shop): no margin scheme, output VAT
  in full. The margin scheme may be allowed there; the accountant should confirm.
- **Intra-EU business purchase**: the seller's price is treated as gross with no deductible VAT. If the
  owner registers for VAT-UE, the purchase becomes an intra-Community acquisition (self-assessed and
  deducted, net zero), so the VAT due is the same; only the flag goes away. A foreign dealer selling
  under its own margin scheme is not modelled.
- **eBay.de sales** are taxed at Polish VAT 23% (margin scheme: always taxed in the seller's state;
  standard VAT: below the EUR 10,000 EU distance-selling threshold). Above that threshold OSS and German
  VAT would apply.
- **Import**: the customs value is the buy price plus inbound shipping at the NBP mid rate (customs
  uses its own monthly rate). The EUR 150 low-value test uses the goods value without shipping. The EU
  import fees are part of the import-VAT base (they are charges due on importation). The VAT inside
  the carrier's clearance fee is not deducted.
- **FX spread** is 1% of the buy price when it is paid in a foreign currency, and 1% of the gross exit
  price when the channel pays out in EUR (slightly more than the real payout after fees). Fixed fees in
  EUR, duty, import fees and import VAT are converted at the NBP mid rate with no spread.
- **Returns reserve** is 5% of the exit price for every B2C channel. All enabled channels are B2C: the
  2-year warranty applies to used goods too, and it cannot be shortened.
- **Pickup** is a buying cost only (driving to the seller). It is not part of the margin-scheme base.

## Worked examples

Fixed FX rates used (PLN per unit): **EUR 4.3951, USD 3.6500, CZK 0.1750, JPY 0.0245**. Duty rates come
from a fake lookup (0% for the CN codes used, which is what ISZTAR returns for 8473302000).
Default config unless stated. Examples 1 to 4 and 7 to 10 are dated 2026-10-29; 5 and 6 are dated
2026-11-02 (the EUR 2 handling fee applies).

### Summary

| # | Case | Buy | Exit (cons / exp) | Cons net | Exp net | Capital | Flags |
|---|---|---|---|---|---|---|---|
| 1 | PL private → Allegro (margin scheme) | 2000 PLN | 2600 / 2800 | **48.55** | **152.51** | 2020.00 | — |
| 2 | PL business, VAT invoice → Allegro | 3075 PLN gross | 4200 / 4500 | **210.71** | **366.64** | 3105.00 | — |
| 3 | CZ private → OLX (CZK) | 14000 CZK | 3000 / 3200 | **105.89** | **222.03** | 2519.50 | bdo_check |
| 4 | DE private → eBay.de | 450 EUR | 2900 / 3100 | **42.52** | **140.39** | 2057.58 | bdo_check |
| 5 | US business → Allegro (import, duty 0) | 1000 USD | 6400 / 6900 | **230.55** | **490.44** | 4851.81 | bdo_check |
| 6 | JP private via forwarder → Allegro (low-value import) | 20000 JPY | 1100 / 1250 | **7.48** | **85.45** | 851.24 | bdo_check |
| 7 | Loss: PL private → OLX, overpaid | 3000 PLN | 2900 / 3100 | **−415.00** | **−243.70** | 3020.00 | — |
| 8 | Pickup 40 km → Allegro | 1800 PLN | 2600 / 2750 | **100.76** | **178.73** | 1936.00 | — |
| 9 | Example 1 under ryczałt (+5 PLN health/sale) | 2000 PLN | 2600 / 2800 | **−19.20** | **111.41** | 2020.00 | — |
| 10 | 30 GPUs, PL invoice, capital over the cap | 120000 PLN gross | 150000 / 160000 | **1986.39** | **7184.08** | 120300.00 | over_capital_cap |

### 1. PL private → Allegro (margin scheme)

Buy 2000 PLN from a private person in Poland. Inbound 20, outbound 25, p_scam 0.02.

| Line | Cons | Exp | How |
|---|---|---|---|
| exit price | 2600.00 | 2800.00 | |
| channel fees | −209.00 | −225.00 | 8% × E + 1 |
| buy price | −2000.00 | −2000.00 | |
| inbound shipping | −20.00 | −20.00 | |
| VAT due | −112.20 | −149.59 | 23 × 600 / 123 = 112.195; 23 × 800 / 123 = 149.5935 |
| outbound shipping | −25.00 | −25.00 | |
| returns reserve | −130.00 | −140.00 | 5% × E |
| fiscal cost | 0.00 | 0.00 | |
| risk haircut | −40.00 | −40.00 | 0.02 × 2000 |
| *pre-tax* | *63.80* | *200.41* | |
| income tax | −15.25 | −47.90 | 0.239 × 63.80 = 15.2482; 0.239 × 200.41 = 47.89799 |
| **net** | **48.55** | **152.51** | |

Capital 2000 + 20 = **2020.00**.

### 2. PL business with a VAT invoice → Allegro

Buy 3075 PLN gross (input VAT 3075 × 23 / 123 = 575.00 exactly). Inbound 30, outbound 30, p_scam 0.01.

| Line | Cons | Exp | How |
|---|---|---|---|
| exit price | 4200.00 | 4500.00 | |
| channel fees | −337.00 | −361.00 | 8% × E + 1 |
| buy price | −3075.00 | −3075.00 | |
| inbound shipping | −30.00 | −30.00 | |
| VAT due | −210.37 | −266.46 | output 96600/123 = 785.37 (exp 103500/123 = 841.46) − 575.00 |
| outbound shipping | −30.00 | −30.00 | |
| returns reserve | −210.00 | −225.00 | |
| fiscal cost | 0.00 | 0.00 | |
| risk haircut | −30.75 | −30.75 | 0.01 × 3075 |
| *pre-tax* | *276.88* | *481.79* | |
| income tax | −66.17 | −115.15 | 66.17432; 115.14781 |
| **net** | **210.71** | **366.64** | |

Capital 3075 + 30 = **3105.00**.

### 3. CZ private → OLX (FX in CZK)

Buy 14000 CZK × 0.1750 = 2450.00 PLN. Inbound 45, outbound 15, p_scam 0.03. OLX: no fee, PLN payout.

| Line | Cons | Exp | How |
|---|---|---|---|
| exit price | 3000.00 | 3200.00 | |
| channel fees | 0.00 | 0.00 | |
| buy price | −2450.00 | −2450.00 | |
| inbound shipping | −45.00 | −45.00 | |
| VAT due | −102.85 | −140.24 | margin: 23 × 550/123 = 102.8455; 23 × 750/123 = 140.2439 |
| outbound shipping | −15.00 | −15.00 | |
| returns reserve | −150.00 | −160.00 | |
| fiscal cost | 0.00 | 0.00 | |
| FX spread (buy) | −24.50 | −24.50 | 1% × 2450 |
| risk haircut | −73.50 | −73.50 | 0.03 × 2450 |
| *pre-tax* | *139.15* | *291.76* | |
| income tax | −33.26 | −69.73 | 33.25685; 69.73064 |
| **net** | **105.89** | **222.03** | |

Capital 2450 + 24.50 + 45 = **2519.50**. Flags: `bdo_check`.

### 4. DE private → eBay.de

Buy 450 EUR × 4.3951 = 1977.795 → 1977.80 PLN. Inbound 60, outbound 80, p_scam 0.02. eBay.de: 11% + 0.35 EUR
(0.35 × 4.3951 = 1.538285 → 1.54), paid out in EUR, so the exit price also carries the 1% spread.

| Line | Cons | Exp | How |
|---|---|---|---|
| exit price | 2900.00 | 3100.00 | |
| channel fees | −320.54 | −342.54 | 319.00 + 1.54; 341.00 + 1.54 |
| buy price | −1977.80 | −1977.80 | |
| inbound shipping | −60.00 | −60.00 | |
| VAT due | −172.44 | −209.84 | margin: 23 × 922.20/123 = 172.4439; 23 × 1122.20/123 = 209.8423 |
| outbound shipping | −80.00 | −80.00 | |
| returns reserve | −145.00 | −155.00 | |
| fiscal cost | 0.00 | 0.00 | |
| FX spread (buy) | −19.78 | −19.78 | 1% × 1977.80 = 19.778 |
| FX spread (exit) | −29.00 | −31.00 | 1% × E |
| risk haircut | −39.56 | −39.56 | 0.02 × 1977.80 = 39.556 |
| *pre-tax* | *55.88* | *184.48* | |
| income tax | −13.36 | −44.09 | 13.35532; 44.09072 |
| **net** | **42.52** | **140.39** | |

Capital 1977.80 + 19.78 + 60 = **2057.58**. Flags: `bdo_check`.

### 5. US business → Allegro (import, duty 0, VAT 23%, clearance)

Dated 2026-11-02. Buy 1000 USD × 3.65 = 3650.00 PLN. Inbound 200, outbound 30, p_scam 0.02, GPU (CN 8473302000, duty 0%).
CIF = 3850.00. Goods value 3650 / 4.3951 = EUR 830 > 150, so only the EUR 2 handling fee: 2 × 4.3951 = 8.7902 → 8.79.
Import VAT 23% × (3850 + 0 + 8.79) = 887.5217 → 887.52. Clearance max(69, 2.5% × (0 + 8.79 + 887.52) = 22.41) = 69.00.

| Line | Cons | Exp | How |
|---|---|---|---|
| exit price | 6400.00 | 6900.00 | |
| channel fees | −513.00 | −553.00 | 8% × E + 1 |
| buy price | −3650.00 | −3650.00 | |
| inbound shipping | −200.00 | −200.00 | |
| customs duty | 0.00 | 0.00 | 0% × 3850 |
| EU import fees | −8.79 | −8.79 | EUR 2 |
| import VAT | −887.52 | −887.52 | |
| clearance fee | −69.00 | −69.00 | |
| VAT due | −309.23 | −402.72 | output 147200/123 = 1196.75 (exp 158700/123 = 1290.24) − 887.52 |
| outbound shipping | −30.00 | −30.00 | |
| returns reserve | −320.00 | −345.00 | |
| fiscal cost | 0.00 | 0.00 | |
| FX spread (buy) | −36.50 | −36.50 | 1% × 3650 |
| risk haircut | −73.00 | −73.00 | 0.02 × 3650 |
| *pre-tax* | *302.96* | *644.47* | |
| income tax | −72.41 | −154.03 | 72.40744; 154.02833 |
| **net** | **230.55** | **490.44** | |

Capital 3650 + 36.50 + 200 + 0 + 8.79 + 69 + 887.52 = **4851.81**. Flags: `bdo_check`.

### 6. JP private via forwarder → Allegro (import, low-value fee)

Dated 2026-11-02. Buy 20000 JPY × 0.0245 = 490.00 PLN (EUR 111.49 ≤ 150). Inbound (forwarder and shipping) 120,
outbound 15, p_scam 0.04, RAM (CN 8473302000, duty 0%). Import fees EUR 3 + EUR 2 = EUR 5 × 4.3951 = 21.9755 → 21.98.
CIF = 610.00. Import VAT 23% × 631.98 = 145.3554 → 145.36. Clearance max(69, 2.5% × 167.34 = 4.18) = 69.00.

| Line | Cons | Exp | How |
|---|---|---|---|
| exit price | 1100.00 | 1250.00 | |
| channel fees | −89.00 | −101.00 | 8% × E + 1 |
| buy price | −490.00 | −490.00 | |
| inbound shipping | −120.00 | −120.00 | |
| customs duty | 0.00 | 0.00 | |
| EU import fees | −21.98 | −21.98 | EUR 5 |
| import VAT | −145.36 | −145.36 | |
| clearance fee | −69.00 | −69.00 | |
| VAT due | −60.33 | −88.38 | output 25300/123 = 205.69 (exp 28750/123 = 233.74) − 145.36 |
| outbound shipping | −15.00 | −15.00 | |
| returns reserve | −55.00 | −62.50 | |
| fiscal cost | 0.00 | 0.00 | |
| FX spread (buy) | −4.90 | −4.90 | |
| risk haircut | −19.60 | −19.60 | 0.04 × 490 |
| *pre-tax* | *9.83* | *112.28* | |
| income tax | −2.35 | −26.83 | 2.34937; 26.83492 |
| **net** | **7.48** | **85.45** | |

Capital 490 + 4.90 + 120 + 0 + 21.98 + 69 + 145.36 = **851.24**. Flags: `bdo_check`.

### 7. A loss: PL private → OLX, overpaid

Buy 3000 PLN, inbound 20, no outbound cost, p_scam 0.05.

| Line | Cons | Exp | How |
|---|---|---|---|
| exit price | 2900.00 | 3100.00 | |
| channel fees | 0.00 | 0.00 | |
| buy price | −3000.00 | −3000.00 | |
| inbound shipping | −20.00 | −20.00 | |
| VAT due | 0.00 | −18.70 | margin max(0, −100) = 0; 23 × 100/123 = 18.699 |
| outbound shipping | 0.00 | 0.00 | |
| returns reserve | −145.00 | −155.00 | |
| fiscal cost | 0.00 | 0.00 | |
| risk haircut | −150.00 | −150.00 | 0.05 × 3000 |
| *pre-tax* | *−415.00* | *−243.70* | |
| income tax | 0.00 | 0.00 | no tax on a loss |
| **net** | **−415.00** | **−243.70** | |

Capital **3020.00**.

### 8. Pickup 40 km → Allegro

Buy 1800 PLN from a private person 40 km from Kraków. Pickup 1.2 × 2 × 40 + 40 = 136.00. No inbound shipping,
outbound 25, p_scam 0.01.

| Line | Cons | Exp | How |
|---|---|---|---|
| exit price | 2600.00 | 2750.00 | |
| channel fees | −209.00 | −221.00 | |
| buy price | −1800.00 | −1800.00 | |
| inbound shipping | 0.00 | 0.00 | |
| pickup | −136.00 | −136.00 | |
| VAT due | −149.59 | −177.64 | margin on 800 and 950 (pickup is not in the base) |
| outbound shipping | −25.00 | −25.00 | |
| returns reserve | −130.00 | −137.50 | |
| fiscal cost | 0.00 | 0.00 | |
| risk haircut | −18.00 | −18.00 | |
| *pre-tax* | *132.41* | *234.86* | |
| income tax | −31.65 | −56.13 | 31.64599; 56.13154 |
| **net** | **100.76** | **178.73** | |

Capital 1800 + 0 + 136 = **1936.00**.

### 9. Ryczałt

The deal from example 1 with `tax_mode: ryczalt` and `ryczalt_health_pln_per_sale: 5`. All lines up to
pre-tax are as in example 1.

| Line | Cons | Exp | How |
|---|---|---|---|
| *pre-tax* | *63.80* | *200.41* | as example 1 |
| income tax | −83.00 | −89.00 | 3% × 2600 + 5 = 78 + 5; 3% × 2800 + 5 = 84 + 5 |
| **net** | **−19.20** | **111.41** | |

Ryczałt is charged on revenue, so a thin deal can turn into a loss.

### 10. Capital over the cap

30 GPUs from a Polish business with a VAT invoice: 120000 PLN gross (input VAT 2760000/123 = 22439.0244 →
22439.02). Inbound 300, outbound 750, p_scam 0.01. The Allegro fixed fee is 1 PLN × 30 items.

| Line | Cons | Exp | How |
|---|---|---|---|
| exit price | 150000.00 | 160000.00 | |
| channel fees | −12030.00 | −12830.00 | 8% × E + 30 |
| buy price | −120000.00 | −120000.00 | |
| inbound shipping | −300.00 | −300.00 | |
| VAT due | −5609.76 | −7479.68 | output 28048.78 (exp 29918.70) − 22439.02 |
| outbound shipping | −750.00 | −750.00 | |
| returns reserve | −7500.00 | −8000.00 | |
| fiscal cost | 0.00 | 0.00 | |
| risk haircut | −1200.00 | −1200.00 | |
| *pre-tax* | *2610.24* | *9440.32* | |
| income tax | −623.85 | −2256.24 | 623.84736; 2256.23648 |
| **net** | **1986.39** | **7184.08** | |

Capital 120000 + 300 = **120300.00** PLN = 120300 / 3.65 = **32958.90 USD** > 30000. Flags: `over_capital_cap`
(tiering then skips the deal).
