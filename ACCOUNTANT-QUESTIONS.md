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
   gross amount): Allegro and OLX fees, domestic courier shipping (inbound only when a Polish business seller issued a VAT invoice, and
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

## Open points behind the carry-over fixes

10. **Inbound shipping reimbursed to a private seller.** When we repay a private person in Poland for
    the courier they paid, there is no VAT invoice in our name. The engine deducts nothing for that
    inbound shipping (it deducts inbound VAT only for a Polish business seller with a VAT invoice). Is
    that right, or can the shipping be documented (own courier account, invoice issued to the JDG) so
    that its VAT becomes deductible?
11. **Clearance or agency fee on imports.** Does the customs agency's fee belong in the import-VAT base
    (the engine puts duty and EU import fees there, but not the clearance fee)? What VAT rate does the
    agency fee itself carry (the engine assumes Polish 23% and deducts it)?
12. **Margin scheme and service VAT.** For goods sold under the margin scheme (art. 120), is the input
    VAT on services tied to the sale (fees, courier, clearance) deductible at all, or does the scheme
    exclude it for those goods? The engine deducts it (see question 6).
13. **Income tax after reserves.** The engine taxes profit after the returns reserve (5% of the sale)
    and the risk haircut, both of which are expected values, not booked costs. Which base is right per
    deal, given that tax is settled monthly or quarterly on actual figures?
14. **Ryczałt base.** Is the 3% ryczałt base the gross sale price, the net of VAT, or net of VAT and the
    margin-scheme VAT? And how should the lump-sum health contribution be spread over sales (see
    question 8)?
