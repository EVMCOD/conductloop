---
name: app-store-submission
description: Use when an iOS or Android build is ready to upload for review, to run the pre-submission checklist in rejection-frequency order and clear every blocker before submitting.
---

# App Store Submission

Review is a queue, not a gate. The goal of this procedure is to spend zero
review cycles on preventable rejections: the ones that come from metadata,
privacy declarations and broken demo flows.

## Fix in this order (most rejections first)

| # | Cause | What to check |
|---|---|---|
| 1 | Crash on launch or on the main flow | The exact uploaded build, clean install, on a real device and on the oldest supported OS |
| 2 | Placeholder content | "Lorem ipsum", "Test App", placeholder icon, tester names in the title, "TODO" strings |
| 3 | Broken or missing links | Support, privacy policy and marketing URLs resolve to a public page, not a 404 and not behind a login |
| 4 | Privacy declarations not matching reality | The nutrition label and the data-safety form list exactly the analytics, crash reporting, ads and data that leave the device |
| 5 | Login wall | Account creation is available in-app; the review notes carry working demo credentials and what to look at |
| 6 | Screenshots out of date | They show the shipped build, on the required sizes, with no prices or claims that are not live |
| 7 | Age rating and content declarations | Every questionnaire answered; an incomplete one blocks submission |
| 8 | Payment and subscription disclosure | Auto-renew terms, trial length, price and cancellation path visible before purchase |
| 9 | Permission without a use case | Location, camera or contacts are explained in-app before the prompt |

## Pre-submission checklist

**Metadata**

- App name: 30 characters max, no keyword stuffing, no competitor names, no
  "free" unless it is free.
- Subtitle (iOS): 30 characters. Keywords: 100 characters total, no spaces
  after commas, no duplicates of the name.
- Description: what it does, in the first two lines. No "coming soon".
- Category and age rating chosen correctly.
- Release notes for an update describe what changed, not "bug fixes".
- Support URL, marketing URL, privacy policy URL: all public and live.

**Build**

- Version and build number strictly increasing; the store rejects a
  duplicate build.
- Release notes in the store record, not only in the changelog.
- Uploads signed with the right distribution profile; no development
  certificate.
- Minimum OS version set to something you actually test.
- Debug logging stripped from release builds, no verbose error screens, no
  internal URLs or staging hostnames visible.

**Privacy and compliance**

- Privacy policy page exists and covers what the app collects.
- Data-safety / privacy nutrition label matches the SDKs in the build.
- Account deletion is offered in-app if accounts exist (both stores require
  it for account-based apps).
- ATT prompt requested only if tracking is real; if not tracking, no prompt.
- Encryption export compliance answered accurately.
- Third-party SDKs disclosed and each one's own data use accounted for.

**Screenshots**

- Required device sizes, current UI, no device frames if the store does not
  want them.
- First screenshot shows the app's core value, not a splash screen.
- Text legible at thumbnail size; captions explain each shot.
- Store listing images match the app: no features that were cut.

**Monetisation (if any)**

- Price and trial terms match the store configuration.
- Restore purchases works and is reachable from within the app.
- External purchase methods comply with the current store rules for the
  platform; if the app is not eligible, no external purchase links appear.

## The submission loop

1. Run the checklist above and record each item as pass / fail / n/a.
2. Upload the build and complete every metadata field in the same session.
3. Submit for review, then answer reviewer questions within 24 hours. A
   reviewer waiting on a reply is a review cycle you paid for.
4. If rejected, read the rejection reason literally, fix the cited item only
   plus anything adjacent, and resubmit. Do not resubmit the same build.
5. Track the reason code of every rejection in one place. Two rejections for
   the same reason means a process gap, not bad luck.

## What a good answer looks like

```
PLATFORM: iOS 1.4.2 (build 142) | Android 1.4.2 (code 142)
CHECKLIST: 22/22 pass, 0 n/a blocking
CRASH TEST: clean install on iPhone SE + iPhone 15, iOS <min> — no crash
PRIVACY: label matches SDKs: analytics yes, ads no, account delete yes
DEMO: reviewer notes include working account + what to review
VERDICT: submit | hold on <item>
```

## Traps

- Submitting from a build that differs from what was tested. Upload the
  artifact you tested, by its build number.
- Assuming a rejection means the app is bad. 80% of rejections are metadata,
  privacy or a placeholder.
- Writing "beta" or "test" anywhere in the name, description or screenshots.
- A privacy policy on a page that requires a login or is not linked from the
  store listing.
- Adding a new SDK right before submission; it can change the privacy label
  and trip review.
- Reviewing on a simulator only: reviewer devices are old, and push,
  biometrics and camera behave differently.
