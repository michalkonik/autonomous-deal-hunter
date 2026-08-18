# Questions for the accountant

The economics engine (`dh/economics.py`, formula in `docs/ECONOMICS.md`) uses conservative defaults
until these are answered. Each answer maps to a config value or a documented rule.

## From the design (spec §7)

1. Which activity code (PKD) should the JDG register for trading in used electronics?
2. Is a cash register (kasa fiskalna) needed from the first B2C online sale of computers, computer parts,
   consoles, phones and monitors? Engine: `fiscal_cost_per_b2c_sale_pln` (default 0).
3. How should margin-scheme (procedura marży, art. 120 VAT Act) bookkeeping be kept for used goods
   bought from private persons?
4. Should the JDG register for VAT-UE, and is OSS needed for eBay.de sales above the EUR 10,000
   distance-selling threshold? Engine: `tax.vat_ue_registered` (default false).
5. Does importing GPUs and other computer parts from another EU state or from outside the EU make the
   owner a "wprowadzający" under BDO/WEEE rules? Engine: flag `bdo_check`.

## Raised by the economics engine

6. **Input VAT on service costs.** The engine deducts the Polish VAT inside these costs (23/123 of the
   gross amount): Allegro and OLX fees, domestic courier shipping (inbound from a Polish seller and
   outbound to a Polish buyer), and the customs clearance fee. It does not deduct eBay fees
   (reverse-charged), shipping to or from abroad, or pickup costs. Is that right? In particular, is
   this input VAT deductible for goods sold under the margin scheme? Engine: `vat_deductible` in
   `data/fees.yaml` and `data/shipping.yaml`.
7. Can the margin scheme be used for used goods bought from a Polish business that issues no VAT
   invoice (for example a VAT-exempt shop)? The engine currently charges full output VAT there.
8. Under ryczałt, the engine uses 3% of the gross sale price. Should revenue exclude VAT (and, under
   the margin scheme, the VAT on the margin)?
9. Under liniowy, the engine charges a flat 4.9% health contribution on each deal's profit. Is that a
   fair per-deal approximation?
