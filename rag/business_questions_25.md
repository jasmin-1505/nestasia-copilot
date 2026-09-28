# 25 Business Questions — Nestasia Marketing Team Gold-Set (Synthetic Demo Data)

These are the questions the "full data flow" demo is built to answer, using the synthetic sales/inventory/channel/margin/social data loaded into the fixture database. Every answer to these should carry a citation, a disclaimer, and an accuracy label (Illustrative only / Verified / Partially illustrative) per the retrieve.py/generate.py extension work.

## PRODUCT
1. Which of our products should we prioritize promoting this quarter based on sales momentum and stock health?
2. Are we sitting on slow-moving inventory that's tying up cash right now?
3. Which product category is our strongest performer relative to competitors?
4. Do we have bestsellers at risk of going out of stock soon?
5. Which recent launches are underperforming and might need repositioning?

## PRICE
6. Are we leaving margin on the table by underpricing versus competitors anywhere?
7. Which products have the best margin-to-volume ratio right now?
8. Should we run a promotion on a specific category to clear inventory?
9. How does our profitability in Cookware compare to Bakeware?
10. Are any SKUs priced in a way that's hurting conversion?

## PLACE
11. Which sales channel is driving the most revenue for us right now?
12. Are we out of stock on a high-demand product on any specific channel?
13. Should we expand our qCommerce assortment based on how it's performing?
14. Which channel has our highest return/complaint rate?
15. Where are competitors outperforming us in availability?

## PROMOTION
16. Which ad campaign is actually driving sales, not just impressions?
17. Should we increase ad spend on a specific product given current trends?
18. Are festive/discount campaigns outperforming everyday pricing?
19. What does social engagement tell us about what to promote next?
20. Which competitor promotional approach looks most effective right now?

## CROSS-CUTTING / OUTLIER
21. Which single product should we prioritize across marketing, inventory, and pricing this month?
22. If we could fix only one thing in Kitchen this quarter, what and why?
23. Which product is quietly becoming a problem before it's obvious?
24. Where are we most vulnerable to a competitor taking share right now?
25. If we had to cut 10% of our SKUs, which should go?

## Notes for whoever builds against this file
- Nearly all 25 depend on sales/inventory/margin/channel data, which today only exists as synthetic fixture data. Every real answer to these should carry a prominent disclaimer, not a footnote.
- Questions 16-18 naturally blend real data (paid_ad_creative) with synthetic data (sales_data) in one answer. These need a "Partially illustrative" accuracy label, not a flat "Illustrative only", so the real portion isn't understated either.
- Questions 21-25 are the hardest. They may require joining across most or all of the new synthetic tables plus real catalogue data in a single query.
