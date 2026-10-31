---
name: native-integration-review
description: Use when a mobile app touches the device camera, location, push notifications, biometrics, a native webview or Capacitor plugins, to check the native contract and permissions before shipping.
---

# Native Integration Review

Native integrations fail in ways web code cannot: the difference between
"works on my simulator" and "works on a user's phone in a real place". Review
the contract with the OS, not just the API call.

## When to use

- A feature uses camera, photo library, location, contacts, microphone,
  biometrics, NFC, Bluetooth, push notifications or local storage.
- A Capacitor / React Native / Flutter bridge plugin is added or upgraded.
- A webview loads local or remote content that needs native capabilities.
- An app review rejection mentions permissions, privacy, or a device that
  "does not work".

## The permission contract

- **Ask at the moment of need**, with an in-app explanation screen first.
  A permission prompt with no context is the most common cause of a denial
  that you can never ask again.
- **Handle all three outcomes**: granted, denied, and "denied permanently"
  (where only Settings can change it — send the user there with a written
  explanation, do not just fail silently).
- **Declare every permission you actually use** in the manifest and in the
  store listing. An unused declared permission is a review flag.
- **Justify sensitive ones in the review notes**: why location, why
  background location, why photo library rather than camera only.
- **Do not request background location or background refresh** unless the
  feature is useless without it. Both are near-automatic rejections.
- On iOS, add the purpose string; a missing usage description is an instant
  crash, not a rejection.

## Per-capability checks

**Camera / photo library**
- Request read and write separately; iOS 14+ needs an explicit "limited library" path when the user selects fewer than all photos.
- Check the authorisation status before presenting the picker, and handle a cancel.
- Handle a device with no camera (simulator, some tablets) and one whose camera another app is using.

**Location**
- Ask for `whenInUse` first; escalate to `always` only after the user has seen value, and justify it.
- Check accuracy: a 5 km city-level fix is fine for "shops near me" and a privacy problem for a delivery tracker.
- Handle location services off at the OS level, and a denied permission, without an error dialog that blocks the screen.

**Push notifications**
- Token registration can fail, the token rotates, and it is invalid after a reinstall: register every launch, unregister on logout.
- Ask for permission in context, after the user sees a reason. On first launch it produces a permanent refusal.
- Deep links from a notification must land on the right screen with the payload, from a cold start as well as a warm one.
- On Android, declare the notification channel before posting, and check the POST_NOTIFICATIONS runtime permission on API 33+.

**Biometrics**
- Provide a passcode fallback: a biometric prompt with no fallback locks the user out when the sensor fails.
- Treat a cancelled prompt as a normal outcome, not an error.

**Webview / hybrid (Capacitor)**
- The webview and the native shell drift on resume: re-check the session and the stored token on resume, not only at launch.
- A remote webview over a flaky connection shows a white screen: ship an offline fallback inside the bundle.
- Store tokens in native secure storage (keychain / keystore), not `localStorage` in the webview.
- On resume the OS may have killed the app: treat resume as a cold start for state purposes.

## Plugin and bridge hygiene

- Pin the plugin version and read its changelog before upgrading; a plugin
  bump can change a permission request or a callback shape.
- Audit the plugin's own permissions and network activity: a plugin is code
  you did not write, in your app, under your name.
- Check the plugin's minimum platform version against the app's minimum
  supported version.
- Keep a list of every native plugin in the repo, with version and purpose.
  A dependency you cannot name is a dependency you cannot audit.

## Test matrix (minimum, before release)

| Capability | Devices | Checks |
|---|---|---|
| Camera | 1 older + 1 current, one without camera | grant, deny, cancel, no hardware |
| Location | 1 device with location off | grant, deny, services off |
| Push | 1 device + 1 with the app reinstalled | token, rotation, cold start, logout |
| Biometrics | 1 with biometrics, 1 without | success, failure, cancel, fallback |
| Webview | 1 with poor connectivity | offline, slow, resume, session expiry |

## What a good answer looks like

```
FEATURE: <name>   CAPABILITIES: camera, push
PERMISSIONS DECLARED: camera, notifications | JUSTIFIED IN NOTES: yes
DENIAL PATHS: camera -> settings link + explanation | push -> in-app banner
TESTED: iPhone SE (iOS <min>) ok, iPhone 15 ok, no-camera device ok
PLUGINS: <name> v<x.y.z> - permissions <list> - audited
VERDICT: ship | fix <item>
```

## Traps

- Testing permissions only on the simulator, where they often auto-grant or
  are unavailable.
- Requesting everything at first launch.
- Assuming a plugin's permissions are yours; the store lists the union.
- Forgetting to handle the offline path for a webview-based screen.
- Believing a green Play Console pre-launch report means the permissions are
  justified to a human reviewer.
