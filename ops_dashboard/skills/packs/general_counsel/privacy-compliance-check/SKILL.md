---
name: "privacy-compliance-check"
description: "Use when a product collects personal data or a policy is published or changed, to reconcile what is actually collected against what is disclosed and record the lawful basis, retention and processor terms."
---

# Privacy Compliance Check

Reconcile what the product actually collects, what the policy says, and what the law requires for the markets served. The output is a gap list with owners, not a policy rewrite.

## When to use

- Before launching anything that collects new data (analytics, SDKs, crash reports, location).
- Before publishing or changing a privacy policy, or adding a country to the markets served.
- Before a processor or sub-processor is added, or an existing one changes its terms.
- Quarterly as a routine check, after a data incident, and when a data subject request arrives.

Not for: a consent banner copy, or an internal analytics review with no personal data involved.

## Step 1 - Build the actual data inventory

The inventory comes from the system, not from the policy: read the client code, the SDK list, the database schema and the network calls. For each surface, list every category below with its
source and purpose, then record where it is collected, whether it is optional, where it is stored, who can access it, and whether it leaves the product.

| Category | Typical fields |
|---|---|
| Account | email, name, phone, password hash, profile fields |
| Device and technical | IP address, device identifiers, user agent, OS version, location, language |
| Usage | pages visited, features used, actions and timestamps, session identifiers, referrer |
| Content | anything uploaded, written, stored or generated, including prompts and outputs |
| Support | tickets, chat transcripts, attachments, screen recordings |
| Financial and marketing | payment references, card brand and last 4 only, invoices, tax IDs, campaign IDs |
| Special categories | health, biometrics, political or religious views, sexual orientation, inferred traits |

## Step 2 - Reconcile collection against the published policy

One row per data category: collected (yes/no), disclosed (yes/no), and the exact fix if either is wrong.

- Collected but not disclosed is a blocking defect: fix the policy, or stop collecting it. Disclosed but not collected means removing the disclosure, not leaving a promise nobody keeps.
- Optional must be optional in practice: no pre-ticked consent, no collection before consent, and no analytics or marketing tag firing before consent where consent is required.
- Third-party SDKs count as collection, so a crash reporter is a processor, not a detail; if data builds a profile, say so in the policy and decide whether it needs a separate opt-in.

## Step 3 - Record the lawful basis per purpose

One row per purpose, not per data field: purpose, data involved, lawful basis, legitimate interest (if used), retention, recipients.

- **Consent**: optional analytics, marketing email, non-essential cookies. Freely given, specific, granular, as easy to withdraw as to give, recorded with a timestamp and the wording shown.
- **Contract**: what is needed to deliver the service the user asked for, including billing and support.
- **Legitimate interest**: security logging, fraud prevention, non-identifying analytics, and improvement. Record the interest, why it outweighs the person's rights, and the balancing test.
- **Legal obligation**: tax, accounting and statutory retention. Vital interests: rare, and they need a reason.

A processor (payment, hosting, email, CRM, support desk, analytics, model API) is not a lawful basis. A legitimate interest is not a fallback for marketing consent. One purpose with two
bases is acceptable only with the more protective basis applied in practice.

## Step 4 - Retention and deletion

Set a retention period per category, with a reason. Defaults to start from, adjusted to the product:

| Data | Default retention | Trigger to delete |
|---|---|---|
| Account data while active | account lifetime | account closure request |
| Account data after closure | 30 days | then delete or irreversibly anonymise |
| Security and access logs | 90 days | automatic |
| Support tickets and chat | 24 months | automatic |
| Marketing engagement data | 24 months | automatic, reset on opt-out |
| Inactive accounts | 24 months, then delete | automatic, warning email at 18 months |
| Backups | 35 days | rolling expiry |
| Billing records | the statutory period in the operating country | automatic |

Deletion must be real: removal from the primary store, caches, search indexes and processors, with backups expiring on the rolling cycle. A request that only hides a row is not deletion.
Every request gets a written confirmation of what was deleted, retained and anonymised.

## Step 5 - Check the processor contracts

For every processor touching personal data, confirm in writing:

- A data processing agreement naming the controller, processor, data, purpose, duration and instructions.
- Disclosed sub-processors, with a change-notice period and a right to object.
- The transfer mechanism for transfers outside the region, plus an impact assessment where needed.
- Security commitments and incident notification within a defined number of hours, where 24 to 72 is normal.
- The right to request deletion at the end of the contract, and return or delete on instruction.
- The right to audit at least annually, and liability for the processor's own breaches.

Also check services where personal data passes through unintentionally: crash reports with user content, support tools capturing screenshots, and model providers where prompts contain
personal data. Confirm the provider does not train on submitted content unless the subject opted in.

## Step 6 - Scope the regimes and output the gap list

Mark which regimes apply and record the extra obligations: GDPR (EU/EEA) needs a lawful basis, records of processing, the full set of data subject rights, breach notification within 72
hours, a data protection contact, and an EU representative where there is no EU presence. CCPA/CPRA (California) needs notice at collection, the rights to know, delete and correct, opt-out
of sale or sharing, limits on sensitive data, and no dark patterns. LGPD (Brazil) needs a designated contact, explicit consent for certain uses, incident notification within 3 business days,
and a data subject agent for international transfers. UK GDPR needs the UK transfer mechanism. A regime in scope with one obligation missing is an escalation item with a name and a date.

Then produce a table, one row per finding: area (inventory, disclosure, basis, retention, processors, transfers, rights, incidents), severity (blocker, high, medium, low), what is wrong in
one line, the fix in one line, owner, and due date. End with a one-line verdict: ship, ship with the blockers fixed by a named date, or stop. Blockers cannot ship: collecting data not
disclosed, processing without a basis, no deletion path, an unsigned processor agreement, or a missing required notice.

## What a good answer looks like

- An inventory built from the code and the database, not from the policy.
- A reconciliation table: collected versus disclosed, for every category.
- A lawful-basis table with one row per purpose, and the balancing note where used.
- A retention table with real triggers, and delete covering caches, indexes and backups.
- A processor checklist with agreement status per processor, and the incident window in hours.
- A gap list with severity, owner and due date, ending in a ship or stop verdict.

## Common traps

- Trusting the policy as the inventory, so undisclosed collection goes unnoticed.
- Forgetting that an SDK or a crash reporter is a processor holding personal data.
- Choosing one lawful basis for the whole product instead of one per purpose.
- Treating legitimate interest as consent for anything marketing-shaped.
- Writing retention periods that no automated job actually enforces.
- Deleting a row and calling it deletion, leaving copies in the search index and in backups.
- Publishing the policy and forgetting the mobile store privacy labels, SDK disclosures and cookie banner.
