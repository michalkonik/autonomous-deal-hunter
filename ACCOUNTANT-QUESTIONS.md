# Questions for the accountant

Deal Hunter's economics engine (`dh/economics.py`; the formula is in [`docs/ECONOMICS.md`](docs/ECONOMICS.md))
uses conservative defaults until these are answered. The owner's company is a one-person JDG, VAT-registered,
on podatek liniowy, reselling used electronics (GPUs, RAM, SSDs, whole PCs). Each answer maps to a config value or
a documented rule. After you have the answers, edit `config/economics.yaml` (and `data/fees.yaml` or
`data/shipping.yaml` where noted) while the agent is stopped, because these files are frozen.

Facts the research verified on 2026-10-29 and that the accountant should still confirm: B2C liability for
non-conformity is 2 years, also for used goods; computers, computer parts, consoles, phones and monitors are
excluded from the cash-register exemptions; a EUR 3 duty per tariff line applies to parcels up to EUR 150 since
1 July 2026 and a EUR 2 EU handling fee per item starts on 1 November 2026.

## Registration and compliance

1. **Activity code (PKD).** Which PKD code should the JDG register for trading in used electronics?
   *Context:* the owner will buy and resell used electronics, domestically and across borders.
   *Engine setting:* none.

2. **Cash register (kasa fiskalna).** Is a cash register needed from the first B2C online sale of computers,
   computer parts, consoles, phones and monitors?
   *Context:* the research reads the statute as excluding these goods from every exemption, including the
   mail-order one.
   *Engine setting:* `fiscal_cost_per_b2c_sale_pln` (default `0`) adds a per-sale cost.

3. **VAT-UE and OSS.** Should the JDG register for VAT-UE? Is OSS needed for eBay.de sales above the EUR 10,000
   distance-selling threshold?
   *Context:* intra-EU purchases from businesses need VAT-UE (reverse charge); eBay.de is an enabled exit channel.
   *Engine setting:* `tax.vat_ue_registered` (default `false`). While false, such deals carry `needs_vat_ue` and
   are treated as Heavy.

4. **BDO and WEEE.** Does importing GPUs and other computer parts from another EU state or from outside the EU
   make the owner a "wprowadzajacy" under the BDO and WEEE rules? Is a bare GPU in scope?
   *Context:* domestic resale makes the owner a distributor, with take-back duties but no registration.
   *Engine setting:* flag `bdo_check` on imported deals; with a non-EU origin it makes the deal Heavy.

## VAT treatment

5. **Margin scheme bookkeeping.** How should margin-scheme (procedura marzy, art. 120 VAT Act) bookkeeping be kept
   for used goods bought from private persons?
   *Engine setting:* `tax.vat_margin_scheme_private` (default `true`); VAT = 23/123 x max(0, sale - purchase).

6. **Margin scheme for a Polish business seller without a VAT invoice.** Can the margin scheme be used for used
   goods bought from a Polish business that issues no VAT invoice (for example a VAT-exempt shop)?
   *Engine setting:* currently the engine charges full output VAT on such deals.

7. **Input VAT on service costs, and the margin scheme.** The engine deducts the Polish VAT inside Allegro and OLX
   fees, domestic courier shipping (inbound only when a Polish business seller issued a VAT invoice, and outbound
   to a Polish buyer) and the customs clearance fee, as 23/123 of the gross amount. It does not deduct eBay fees
   (reverse-charged), shipping to or from abroad, or pickup costs. Is that right? In particular, for goods sold
   under the margin scheme, is the input VAT on services tied to the sale deductible at all, or does the scheme
   exclude it?
   *Engine setting:* `vat_deductible` per channel in `data/fees.yaml` and per lane in `data/shipping.yaml`.

8. **Inbound shipping repaid to a private seller.** When the owner repays a private person in Poland for the
   courier they paid, there is no VAT invoice in the company's name. The engine deducts nothing. Is that right, or
   can the shipping be documented (own courier account, invoice to the JDG) so its VAT becomes deductible?
   *Engine setting:* the inbound line deducts VAT only when `seller_is_business` and `has_vat_invoice`.

9. **Customs agency fee on imports.** Does the clearance or agency fee belong in the import-VAT base? The engine
   puts duty and EU import fees there, but not the clearance fee. What VAT rate does the agency fee itself carry
   (the engine assumes Polish 23% and deducts it)?
   *Engine setting:* `import_costs.clearance_fee_pln` (69) and `clearance_pct_of_duties_taxes` (0.025).

## Income tax

10. **Tax base per deal.** The engine taxes profit *after* the returns reserve (5% of the sale) and the scam/DOA
    risk haircut. Both are expected values, not booked costs, while tax is settled monthly or quarterly on actual
    figures. Which base is right per deal?
    *Engine setting:* `returns_reserve` (default `0.05`) and the risk haircut (not configurable).

11. **Health contribution under liniowy.** The engine charges a flat 4.9% on each deal's profit. Is that a fair
    per-deal approximation of the real contribution?
    *Engine setting:* `tax.health_rate` (default `0.049`) with `tax.pit_rate` `0.19`.

12. **Ryczalt base and health contribution.** If the JDG were on ryczalt, the engine uses 3% of the *gross* sale
    price. Should revenue exclude VAT (and, under the margin scheme, the VAT on the margin)? How should the
    lump-sum health contribution be spread over sales?
    *Engine setting:* `tax.ryczalt_rate` (`0.03`) and `tax.ryczalt_health_pln_per_sale` (`0`). Only used when
    `tax.tax_mode: ryczalt`.
