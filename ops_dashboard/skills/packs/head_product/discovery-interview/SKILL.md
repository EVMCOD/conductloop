---
name: "discovery-interview"
description: "Use when a problem, segment or willingness to pay is assumed rather than known, to run a short user interview that produces behavioural evidence and a decision."
---

# Discovery Interview

A 25-minute interview with a stranger, done 5 times, that produces quotes and a decision. The goal is not to be liked;
it is to find out what people actually did last time, and what it cost them.

## When to use

- Before committing a roadmap quarter to a problem that is assumed.
- A churn reason, a conversion drop, or a pricing question needs a cause.
- A founder idea has no user contact behind it yet.
- Two teams disagree on what users want.

Not for: validating a decision already made (that is confirmation bias with a transcript), or a support ticket that
already contains the answer.

## Step 1 - Prepare (10 minutes)

1. Write the hypothesis as one falsifiable sentence: "Teams with more than 20 people cannot find a report because the
   menu is flat." If the interview cannot falsify it, it is not ready.
2. Recruit 5 participants who match the target segment and who did the behaviour in the last 30 days. Recruit from usage
   data, a community, or a referral, not from a convenience list of friends.
3. Send the questions in advance. No pitch, no product names.
4. Prepare a template with the fields in step 3 and a recorder.

Budget: 5 interviews before a roadmap quarter is committed, 3 to 5 per hypothesis after that. Fewer than 5 is a
conversation, not discovery.

## Step 2 - Run the interview (25 minutes)

Structure, in this order, with these questions:

- **Context (3 min)**: "Tell me about the last time you [did the thing]." Then: "What happened just before that?" "What were you trying to accomplish?"
- **The problem (8 min)**: "What did you do next?" "What was the hardest part?" "How long did that take?" "What else did
  you try?"
- **The workaround (6 min)**: "What did you end up doing instead?" "What did that cost you, in time or money?" "Who else
  at the company does this?"
- **The stakes (5 min)**: "What happens if this stays the same for another six months?" "Who gets blamed when it goes
  wrong?"
- **The close (3 min)**: "Is there anything I did not ask that I should have?" "Can I contact you again when this is
  built?"

Rules while talking:

- Ask about the last time, never about what they would do. "Would you use a feature that..." produces nothing usable.
- Ask one question at a time and wait at least 3 seconds after the answer.
- Never say the solution out loud. If the participant asks what the product does, answer in one line and return to the
  last real event.
- Silence is fine. Fill it with "what happened next?", not with a hint.
- Do not defend, explain, or sell.

## Step 3 - Record, at the time

Fill these fields inside 10 minutes of the call, while it is fresh:

- Participant profile: segment, role, size, and how they were recruited.
- The trigger event: what happened that made them look for a solution.
- The timeline of what they actually did, in order, with the tools used.
- The workaround, and its cost in hours, money or delay.
- The decision criteria they used, unprompted.
- At least 3 verbatim quotes, each under 25 words, marked [quote].
- The strongest moment: where the voice or the hesitation changed.
- At least 1 thing that contradicts the hypothesis.
- Non-verbal notes: hesitation, laughter, silence, uncertainty.

Never paraphrase a quote. Paraphrase loses the words that reveal the mechanism.

## Step 4 - Know when the answer is a lie

Treat the answer as unreliable, and go back to behaviour, when:

- It is about the future: "I would definitely use that", "I would pay for that". Stated intent about the future is
  worthless; only past behaviour counts.
- It is politeness or agreement: "yes, that sounds great", "sure, I would do that" after a leading question. Re-ask the
  same point without the leading framing, or move to a different participant.
- It is a hypothetical purchase: "how much would you pay?" prices a stranger's imagination, not a budget. Only a real
  payment, a purchase order, or an existing spend is evidence.
- It is about a secondary problem. Users describe the problem they can explain, not the one that costs them most.
- It comes from a user segment that does not buy. A free user's enthusiasm is a different currency from a paying user's.
- It is about a recent failure they cannot reconstruct. If they cannot recall a specific instance, the behaviour is not
  frequent enough to design for.
- It is second-hand: "my colleague said". Ask for the instance in their own words, or for their own account of the same
  event.

A reliable answer has three anchors: a concrete past event, a workaround, and a cost.

## Step 5 - Turn interviews into a decision

Code the notes into the hypotheses they support or contradict, then apply the count rule:

- 5 of 5 participants show the same behaviour: strong. A PRD can be written.
- 3 to 4 of 5: promising. Run one more round of 3 to confirm, then write it.
- 2 of 5 or fewer: weak. Do not commit a quarter. Either narrow the hypothesis (maybe only a sub-segment has this) or
  drop it.

The output is a one-page discovery memo: the hypothesis and its verdict (confirmed, refuted, unresolved); the count and
the quote that best shows the mechanism; the workaround and its cost, with the number; the sub-segment with the sharpest
pain, if one appeared; the one thing still unknown and the cheapest way to learn it; and the decision: build, spike or
stop.

## Common traps

- Asking "would you use", "how much would you pay", or "do you like this idea".
- Pitching, or letting the participant pitch their own solution to you.
- Running one interview and treating it as a finding.
- Recruiting only enthusiasts, or only power users of another tool.
- Writing notes in prose without quotes, dates or numbers.
- Counting agreement instead of counting behaviour.
- Letting a confident participant define the segment for all 5 slots.
- Stopping at "interesting" and leaving without a decision.
