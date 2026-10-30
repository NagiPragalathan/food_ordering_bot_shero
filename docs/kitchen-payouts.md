Written for: the Shero team who pay kitchen partners.

# Kitchen partner payouts

When a customer pays for an order, part of the money belongs to the kitchen
partner that cooks it and the rest is Shero's. **Payouts** in the admin
sidebar (`/admin/payouts`) sets how the money is split. It also shows what
each kitchen is owed and keeps a record of what you have paid them.

```
$100 of food + $8 delivery, kitchen share 65%

  Customer pays   $108
  Kitchen gets     $65   (65% of the food)
  Delivery          $8   (passed on to Uber, not split)
  Shero keeps      $35
```

## Setting the split (Split rules tab)

| Setting | Choices | Starts as |
|---|---|---|
| Kitchen's share | **A percentage of the food**, or **each dish's partner price (PPP)** times the quantity | 65% of the food |
| Kitchen's percentage | Any number from 0 to 100, up to 2 decimals. With partner prices, it is used only for a dish that has no PPP | 65 |
| Coupons and loyalty points | **Out of Shero's share only**, or **out of both shares, in proportion** | Shero's share only |
| When a kitchen is owed | **Once the order is delivered**, or **as soon as the customer pays** | Once delivered |
| Kitchens with their own split | Any kitchen can have its own percentage, its own basis, or both | None |

The PPP amounts come from the PPP column of the menu sheet. On today's menu,
PPP is about 65% of the menu price for every dish. To change one dish's PPP,
edit its **Cost** on the Menu page or re-import the sheet.

**Try it**, on the same tab, works out any order with the split as it is
on screen, before you save. **What the saved split gives** shows the
kitchen's and Shero's amounts for $50, $100 and $250 orders under each rule.

### What is split and what is not

- **Split:** the food (the menu price of the dishes).
- **Not split:** delivery, Uber's fees and tax. Shero collects them and
  passes them on, so they are left out of both shares.
- **Coupons and points that take money off the food:** by default Shero
  gives that money up and the kitchen's share is unchanged. With "in
  proportion", both shares shrink. For example, $100 of food with a $10
  coupon at 65% gives the kitchen $58.50 and Shero $31.50.
- **Free-delivery coupons** always come out of Shero's share.

### Changing the split later

Each order keeps the split that was in force when it was paid, so changing
the split does not change what you owe for orders already paid. Orders paid
before this page existed are worked out with today's split until they are
paid out. The blue notice on the Overview can copy today's split onto them,
or onto any orders not paid out yet.

## What is owed (Overview tab)

Pick the dates and, if you like, one kitchen. Dates are counted from the day
the customer paid.

- **Totals:** what customers paid, the kitchens' share, Shero's share,
  delivery and tax passed on, and what is due to kitchens now.
- **By kitchen:** orders, the kitchen's share, Shero's share, what has been
  paid out and what is due now. **Statement** downloads that kitchen's
  orders as a spreadsheet (CSV).
- **Orders:** each order with its split and where it stands:

| Status | Meaning |
|---|---|
| Not delivered yet | Paid, but not delivered yet (when kitchens are owed once an order is delivered) |
| Due to the kitchen | Ready to pay |
| Paid to the kitchen | Included in a payment you recorded |
| Nothing owed (refunded) | Refunded or cancelled, so the kitchen gets nothing |
| Refunded after payout | The kitchen was paid, then the order was refunded. Get that amount back |

**Details** on an order shows each dish's share. It also lets you set the
kitchen's amount for that one order by hand, with a reason, for example when
a refunded order was already cooked. **Use the split** puts the order back
on the rule. An order already paid out cannot be changed until its payout is
undone.

## Paying a kitchen

1. Pay the kitchen however you normally do (Zelle, bank transfer and so on).
2. On the Overview, choose the dates and press **Record payment** on that
   kitchen's row.
3. Enter the date you paid and a reference, then press **Record $X paid**.
   Every order due to that kitchen in those dates is marked paid.

If the amount due changes between loading the page and recording the
payment (an order is delivered or refunded meanwhile), nothing is recorded.
You are asked to check the new amount.

**Payout history** lists every payment recorded, with its statement.
**Undo** removes a payment recorded by mistake: its orders become due again,
and the payment stays in the list marked "Undone".

The Orders page also shows each paid order's kitchen and Shero amounts under
its total.

## For engineers

- Rules: `app/services/payouts.py` (`Rules`), stored in `app_settings` under
  `PARTNER_SPLIT`.
- Each order's split: `orders.payout_split`, copied at payment by
  `payments.handle_payment_success`. A failure is logged and does not block
  the payment. It holds the rule, each dish's share, any hand-set amount and,
  once paid out, the amount paid.
- Payments to kitchens: the `partner_payouts` table. Orders link to it
  through `orders.payout_id`.
- Migration: `0015_partner_payouts`.
- Page: `app/admin/routes/payouts.py`, `app/templates/admin/payouts.html`.
- Tests: `tests/services/test_payouts.py`, `tests/admin/test_payouts_admin.py`.
