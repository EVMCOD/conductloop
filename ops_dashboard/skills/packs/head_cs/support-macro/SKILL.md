---
name: "support-macro"
description: "Use when a support question repeats across customers and needs a written reply, to write a macro that answers the real question, sets the escalation boundary, and stays short enough to be read."
---

# Support Macro

A macro is a reusable reply for a question that keeps arriving. It must answer the question behind the question, set one clear expectation,
and be short enough that a customer reads it once.

## When to use

- The same question has arrived at least 3 times in 7 days.
- A reply takes more than 5 minutes to write by hand, every time.
- A policy needs to be stated the same way every time (billing, refunds, limits, data export, access).
- You are about to answer the 10th copy of a message you have already written.

Not for: a bug report, a security incident, a complaint needing compensation, or anything with a personal history behind it. Those get a
human, not a macro.

## Step 1 - Find the real question

Before writing a word, write down the question the customer is actually asking, in one line. It is usually one of four:

- "Can I do X?" (a capability question).
- "Why did X happen?" (an explanation, often about a bill or a limit).
- "How do I do X?" (an instruction request).
- "Will you do X for me?" (a request for work or an exception).

The wording the customer used is rarely the wording of the real question. "How do I export to CSV" is usually "can I get my data out" or "my
accountant needs the numbers in a spreadsheet". Answer the real one and still include the instruction, because they asked for it.

If the real question needs a fact you do not have (their plan, their invoice, what their account shows), the macro is the wrong tool. Write
the reply with a placeholder for that fact and check it before sending.

## Step 2 - The structure (5 blocks, in this order)

1. **The answer**, in the first sentence. Not the greeting, not the context. If they have to read three paragraphs to find the answer, they will not read the fourth.
2. **The one thing they need to do**, as numbered steps, 1 to 4, each starting with a verb. Each step names the exact screen, button or menu
   path.
3. **What happens next**, with a number in it: "your export is ready within 10 minutes", "we answer within 1 business day", "the limit
   resets on the 1st".
4. **The edge case**, in one line, only if it is likely: what to do if the step does not apply, or what to do if it fails.
5. **The closing**: one short line and the reply-to address. No fake warmth, no restating the whole problem.

Optional, above the answer: one sentence acknowledging the situation, at most. Skip it when the answer is a fact.

## Step 3 - Tone rules

- Write in plain language, at the level of the customer's message.
- Second person, active voice, short sentences. Average under 15 words.
- No blame, ever: not "you failed to", not "you should have", not "as mentioned in our previous email".
- No passive hostility and no cheerful filler: "Just checking in!" on a billing question reads as sarcasm.
- No legal or technical hedging: "in most cases" without saying what the other cases are.
- State facts with numbers. Vague reassurance is what generates the follow-up ticket.
- Do not apologise for something that is not a fault, and do apologise once when something is.
- Match the customer vocabulary for the product, but never their assumption about the cause.

## Step 4 - Length

- Under 120 words for the body, 6 sentences maximum, one screen on a phone.
- 4 to 6 steps maximum in the instruction block.
- No attachment, no link to another page unless the step is genuinely multi-screen; the instruction must be complete in the message.
- No duplicate of the same paragraph across macros. If two macros share three sentences, one of them is wrong.

Rule of thumb: if a customer would have to scroll, the macro has grown a second question. Split it.

## Step 5 - Escalation boundaries

Send to a human, and stop using the macro, when the message contains:

- **Security or access**: a suspected breach, a credential problem, an unauthorised login, or data that should not be visible.
- **Money**: a refund, a chargeback, a credit above the standing limit, or a pricing dispute.
- **Legal or regulatory**: a legal notice, a regulator, a copyright or privacy complaint, a data subject request.
- **Loss**: data loss, an import that destroyed something, a service outage, or an incident.
- **Health**: any account affecting a safety-critical workflow.
- **The relationship**: the customer says they are leaving, mentions a competitor by name, or the account is above the value where a human
  should always answer.
- **Anything the macro would promise**: a roadmap date, an exception to a policy, custom work, a feature. A macro that promises is a
  liability.

The macro may state the escalation path in one line ("I have asked our billing team and someone will reply by Thursday") but must not
pre-commit to an outcome, a date it cannot control, or an exception nobody approved.

## Step 6 - Write it and maintain it

- Save with a name that describes the question, not the situation: "how-to-export-csv" beats "reply-to-john-2".
- Record in the macro header: the question it answers, when it was last verified, and who owns it.
- Review every macro every quarter. If the product changed, the macro is stale, and a stale macro is worse than no macro.
- Track usage and outcome per macro: times sent, times it produced a follow-up question. A macro with a follow-up rate above 20% needs
  rewriting or a product fix, not a new version of itself.
- When a bug is behind the repeated question, the macro explains the workaround and the macro's owner also has a ticket with the fix. The
  macro is never the fix.

## What a good answer looks like

- One line stating the real question the customer was asking.
- The answer in the first sentence.
- 1 to 4 numbered steps, each with a verb and an exact path in the product.
- A number for what happens next, and a number for the timeframe.
- Under 120 words, one screen.
- A written escalation list, so the next agent knows when to stop using it.
- A review date and an owner.

## Common traps

- Opening with three sentences of preamble before the answer.
- Explaining the internal mechanism instead of giving the steps.
- Hiding the answer in a bulleted list that mixes context and instructions.
- Sending a macro to a customer who has already escalated.
- Copying a support answer from another product, with its menu names.
- Promising a timeline the product cannot control.
- Never revisiting the macro after the feature changed.
- Using one macro for two different questions, so it satisfies neither.
