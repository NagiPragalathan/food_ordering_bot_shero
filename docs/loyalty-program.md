# Loyalty rewards: setup, operation and customization

Loyalty is implemented for new orders placed through the ordering website and WhatsApp chat checkout. The admin dashboard has a **Loyalty** page at `/admin/loyalty`. It holds program settings, customer balances, transaction history and manual adjustments.

## Activate after deployment

1. Back up the production database. Deploy the code and run `alembic upgrade head` (migration `0011_loyalty`). Docker production startup already applies migrations. Restart the app/worker.
2. Open **Admin > Loyalty**. Review the currency, rates and limits, then check **Enable the loyalty program** and save. It starts disabled so deploying the schema does not silently create a new financial benefit.
3. Keep **Allow earning points** and **Allow spending points** checked, or independently pause either capability.
4. Place new test orders using your established staging/test payment flow. Existing historical orders are not awarded points retroactively.

Settings are read from the database on each loyalty operation; no restart is required after saving them. Existing payment links keep their agreed redemption value and earning rules. Pausing or changing the program applies to new quotes/orders, while previously issued payment links are honoured.

## The concept, with the starting settings

A point is a customer reward balance, associated with their customer/WhatsApp account. It is not cash or a bank transfer. Customers choose to use it for a food discount on a later order.

| Starting rule | Value |
|---|---|
| Program name / currency | Shero Rewards / USD |
| Earn rate | 1 point per $1 of eligible food spend |
| First eligible program order | +25 points |
| Repeat bonus | 0 extra by default; can be changed |
| Milestone | +50 points every fifth eligible order |
| Silver | 1.5x spend points starting with eligible order 5 |
| Gold | 2x spend points starting with eligible order 10 |
| Point value | 1 point = $0.01; 100 points = $1 |
| Minimum eligible food subtotal | $10 for earning; $10 for redemption |
| Minimum points used | 100, in increments of 1 |
| Discount cap | 25% of remaining eligible food, and at most $10 per order |
| Coupon stacking | Off; earning on coupon orders is allowed |
| Expiry | Points do not expire |

For example, a first eligible $50 food purchase earns **50 + 25 = 75 points** after payment. A second $50 food purchase earns **50**, bringing the balance to **125 points**. Redeeming 100 points on a later qualifying order takes **$1 off the food** and leaves 25 points before any new earnings. Food tax is recalculated from the reduced taxable food amount using the app's existing tax setting. Delivery, external fees and tax do not earn points and are not covered by the loyalty discount.

The fifth eligible $50 food purchase earns 75 spend points at the Silver multiplier, plus the 50-point milestone: **125 points**. Fixed bonuses are added after the multiplier. Spend points are rounded down to whole points; caps apply after all bonuses.

## Customer experience

**Website:** after choosing an address and delivery slot, the summary shows the program name, available balance, points allowed on this order, point value and redemption rules. The customer can enter a number, choose **Use maximum**, **Apply points**, or **Remove**. **My points history** shows recent transactions and current tier. Every total is calculated on the server. A changed balance or policy asks the customer to review the updated total before payment.

**WhatsApp:** type `points`, `rewards` or `loyalty` outside a free-text data-entry step to check balance/tier. In chat ordering, type `redeem 100` before selecting the delivery slot; `redeem 0` removes the request. Final eligibility is checked in the summary. To change points after a payment link exists, use **Change menu** first. On the website, enter the points in checkout; a chat redemption request belongs to chat checkout only. With **Allow points and a coupon on the same order** off, the latest choice wins in chat: typing a coupon code after `redeem` keeps the coupon and tells the customer their points stay in their balance, and `redeem` after a coupon asks them to type `remove coupon` first.

Points earned and loyalty savings appear in the existing WhatsApp payment/summary messages when applicable. Stripe charges the agreed discounted total. PDF receipts show loyalty separately from coupons, and Zoho records the combined discount with the breakdown in the order description. Admin Orders shows redeemed points, reservation state and points earned.

## Every available customization

| Admin control | Effect |
|---|---|
| Enable the loyalty program | Turns the program on for new quotes |
| Program name | Customer-facing name |
| Program currency | Orders must match this currency; one program/wallet currency at a time |
| Allow earning / spending | Independent switches |
| Points per currency unit | Spend-based rate; fractional earn rates are supported |
| Extra points per eligible order | Fixed points in addition to spend points |
| Minimum eligible food subtotal to earn | Qualification threshold, before coupon/loyalty reductions and after excluding dishes |
| Maximum points earned per order | Limits all spend points and bonuses combined; 0 means no configured cap |
| Bonus on first eligible program order | Awarded with the first qualifying payment after the program starts |
| Bonus on each repeat eligible order | Additional points on repeat purchases |
| First eligible order receiving repeat bonus | For example, start repeat bonuses at order 2 or 3 |
| Milestone every N orders | 0 turns milestone bonuses off |
| Points per milestone | Fixed milestone bonus |
| Membership tiers | Custom names, eligible order thresholds and spend-point multipliers; [] disables tiers |
| Discount value of one point | Currency value, to two decimal places |
| Minimum points per redemption | Smallest allowed points transaction |
| Spend points in multiples | For example, 50 means customers can use 100, 150, 200, etc. Minimum must be a multiple |
| Minimum food subtotal to redeem | Separate redemption threshold |
| Maximum food percent covered | Protects the remaining eligible food margin |
| Maximum loyalty discount | Fixed money cap; 0 removes this fixed cap |
| Allow coupon stacking | Whether coupon + points can be used on one order |
| Earn points on coupon orders | Applies to the remaining eligible food after the food coupon |
| Earn when redeeming | If enabled, earns on the remaining eligible food after the loyalty discount |
| Eligible kitchens | Select none for all, or restrict the program to selected partners |
| Excluded dishes | Retailer IDs that earn no points and cannot be discounted with points |
| Manual wallet adjustment | Grant/remove points with a required reason and recorded admin email |

Example tier configuration:

```json
[
  {"name": "Silver", "orders": 5, "multiplier": 1.5},
  {"name": "Gold", "orders": 10, "multiplier": 2},
  {"name": "VIP", "orders": 20, "multiplier": 3}
]
```

The highest reached order threshold wins. Thresholds include the order currently being paid. A full refund removes that order from the eligible order count for future thresholds; past orders are not repriced or recalculated. Changing point value/currency changes the future redemption value of existing balances: review those settings carefully before saving. There is no historical import, referral program, birthday campaign, expiry schedule, cash payout or free-item reward in this version.

## Payment and refund safeguards

- A draft reserves the selected points immediately. Available balance already excludes held points, so two payment links cannot use the same balance.
- Payment confirms the redemption and awards new points once. Duplicate payment callbacks do not duplicate the loyalty entries. Pending/unpaid drafts earn nothing.
- Cancellation, Change menu, expiry and failed payment-link creation release the reservation once. A payment attempt that fails but still has a live payment link retains its points reservation for retry until the link is expired/cancelled.
- If a customer pays an old link after its reservation was released, its discount is still honoured and the points are charged again. If those points were already spent, the wallet goes negative; future earnings repay that balance and redemption is blocked until enough positive points exist.
- Full refunds return the redeemed points and remove the earned points, including bonuses. If earned points were already spent, reversal can also create a negative balance.
- Partial refunds proportionally return redeemed points (rounded down) and reverse earned points (rounded to nearest whole point). Cumulative refund events apply only the difference; repeated or older events are ignored. This applies to refunds from the app and the Stripe dashboard. The app retains its existing one-refund-action guard; further partial refunds can be issued through Stripe, whose cumulative webhook updates are reconciled.
- All money calculations use Decimal. Points never reduce the final charge below the app's existing minimum of 0.50 currency units. Coupon discounts and excluded food are accounted for before the points cap.
- Refunds/late payments use the stored order values, not whatever the admin settings are today. Customer deletion removes their wallet and ledger through the existing privacy flow.

A transaction history is retained for holds, releases, paid-order earnings, late redemptions, refunds and admin adjustments. Adjustments require a reason and duplicate form submissions are idempotent. Customers cannot use the public endpoint to grant themselves points or read another customer's wallet without that customer's signed ordering token.

## Verification

Run the complete suite:

```powershell
.venv/Scripts/python.exe -m pytest -o addopts='' -q
```

Focused tests are `tests/services/test_loyalty.py`, `test_loyalty_checkout.py`, `test_loyalty_concurrency.py`, and `tests/admin/test_loyalty_admin.py`. They cover rates/bonuses/tiers, eligibility, caps, invalid inputs, simultaneous transactions on separate SQLite connections, duplicate callbacks, quote/confirm/Stripe charge integration, reservation cleanup, cumulative refunds, admin validation, authentication and audit records.

Migration upgrade/downgrade/upgrade and checkout JavaScript syntax were checked independently. Browser checks use a separate local preview database and verify applying points, the changed total and points history without live payments or messages. PostgreSQL row locking is implemented but has not been exercised against a live PostgreSQL server in this session.
