# Billing — retired

ConductLoop is **free** (since v1.1.0, 2026-07-16). On 2026-10-21 billing was
removed from the app:

- No Stripe keys, no Stripe Checkout, no Stripe webhook in ops-dashboard.
  `POST /api/billing/{checkout-lifetime,checkout-subscription,cancel-subscription,stripe-webhook}`
  return `410 {"free_edition": true}`; `/billing/success` redirects to `/`.
- `/billing` is a static "Free edition" page. Founders from the pre-free beta
  keep their **Founding member** badge (no price shown). The founders roster
  (`db_billing.py`) is kept as historical data.
- No prices anywhere in the UI or i18n; `/api/pricing-tier` was removed.
- No license gate and no calls to the license worker (`licensing.py`).
- The Stripe webhook that pointed at the license worker must be disabled in
  the Stripe Dashboard (see the R7 steps kept outside the repo).

If paid, cloud-only features ship later, billing will be designed per feature
from scratch; the previous implementation is in git history (before 2026-10-21).
