# Kitchen payouts

**Payouts** in the admin sidebar shows how each paid order's money splits
between its kitchen partner and Shero. Code: `app/services/payouts.py`,
`app/admin/routes/payouts.py`.

## The split

At the top of the page:

* **Kitchen partner gets ... %** and **Shero gets ... %**. Type either one;
  the other fills itself in, so the two always make 100. **Save split**.
* **Same split for every kitchen** (on by default): one split for all
  kitchens. Turn it off to give each kitchen its own percentage in the table
  that opens. A kitchen left empty uses the shared percentage.

The kitchen gets its percentage of the **food** (the dishes). Shero gets
what the customer paid, less the kitchen's amount and the delivery, Uber
fees and tax it passes on, so a coupon or loyalty points come out of
Shero's part:

```
$100 of food, $8 delivery, kitchen 65%:
    the customer pays $108 = kitchen $65 + delivery $8 + Shero $35
```

A cancelled or refunded order gives the kitchen nothing.

Every order is worked out with today's percentages: changing them changes
the amounts shown for every order, past ones too.

## The orders

Choose the **dates** (this month, last month, last 30 days, this year, all,
or your own) and a **kitchen** (or all). The page lists every order the
customer paid for in those dates - by the date they paid - with its dishes,
what the customer paid, the food, the delivery, fees and tax, what the
kitchen gets and what Shero gets, each with its percentage, and the order's
status. A total row closes the list; with all kitchens, a "By kitchen" table
gives each kitchen's totals. **Download CSV** gives the same orders as a
spreadsheet. Each paid order on the Orders page shows its two amounts too.

The split is stored as one setting (`PARTNER_SPLIT`). The first version of
this page (29 Oct 2026) also recorded payments to kitchens and copied the
split onto each order at payment (`partner_payouts`, `orders.payout_split`,
`orders.payout_id`); that was removed on 30 Oct 2026 for this simpler page,
and those columns are no longer used.
