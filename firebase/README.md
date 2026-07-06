# GMAT AI proxy (Firebase)

Lets the desktop and Android apps use the AI features **without shipping an OpenAI
key**. The apps authenticate as an anonymous Firebase user (Android additionally
attests with App Check / Play Integrity) and call the `gmatAiChat` Cloud Function,
which holds the real OpenAI key in Secret Manager and forwards to OpenAI. Usage is
bounded by a per-user daily quota and a global kill-switch.

A user who sets their own `OPENAI_API_KEY` (desktop) or pastes a key on the GMAT
dashboard (Android) bypasses the proxy and calls OpenAI directly (BYOK).

## Contents

- `functions/src/index.ts` — the `gmatAiChat` HTTPS function (auth + App Check +
  quota/kill-switch + model whitelist + OpenAI forward).
- `firestore.rules` — clients get no direct Firestore access; only the function
  (Admin SDK) writes `quotas/*` and reads `config/ai`.
- `firebase.json`, `.firebaserc` — project + emulator config. `.firebaserc`
  defaults to `demo-gmat` (emulator only) — set your real project id before deploy.

## One-time setup (console / CLI)

1. Create or reuse a Firebase project; register a **Web app** (for desktop/anon
   auth) and the **Android app** (`com.ichi2.anki` / `.debug`).
2. Enable **Anonymous** sign-in (Auth → Sign-in method).
3. Enable **App Check** with the **Play Integrity** provider for the Android app
   (needs the app in Play Console + its SHA-256).
4. Set the OpenAI secret:
   `firebase functions:secrets:set OPENAI_API_KEY`
5. (optional) `firebase functions:config` / env `GMAT_AI_DAILY_QUOTA` to tune the
   daily per-user cap (default 100).

## Deploy

```
cd firebase
firebase use <your-project-id>
firebase deploy --only functions,firestore:rules
```

Note the deployed function URL, the Web **API key**, the Android **App ID**, and
the **project id** — these are public client identifiers (not secrets) that go
into the apps.

## Wire the apps

- **Desktop** — bake into `qt/aqt/gmat_ai.py` (or pass as env for testing):
  `_FIREBASE_API_KEY` (Web API key) and `_PROXY_URL` (function URL). Env
  overrides: `GMAT_FIREBASE_API_KEY`, `GMAT_AI_PROXY_URL`. Until both are set the
  desktop stays BYOK-only.
- **Android** — set gradle properties (e.g. in `~/.gradle/gradle.properties` or
  `-P` flags), consumed by `AnkiDroid/build.gradle` BuildConfig fields:
  `gmatAiProxyUrl`, `gmatFirebaseApiKey`, `gmatFirebaseAppId`,
  `gmatFirebaseProjectId`. Until all are set the app stays BYOK-only. (No
  `google-services.json` is required — Firebase is initialized manually from these
  values in `gmat/GmatFirebase.kt`.)

## Local testing (emulator)

```
cd firebase/functions && npm install
# provide the OpenAI key to the emulator only (gitignored):
echo "OPENAI_API_KEY=sk-..." > .secret.local
cd .. && firebase emulators:start --only functions,auth,firestore --project demo-gmat
```

Then point a client at the emulator, e.g. desktop:
`GMAT_FIREBASE_API_KEY=fake GMAT_AI_PROXY_URL=http://127.0.0.1:5001/demo-gmat/us-central1/gmatAiChat
GMAT_IDENTITY_URL=http://127.0.0.1:9099/identitytoolkit.googleapis.com/v1
GMAT_SECURETOKEN_URL=http://127.0.0.1:9099/securetoken.googleapis.com/v1`.

## Cost / safety

- OpenAI key lives **only** in Secret Manager. Never in any client or in git.
- Controls: per-user daily quota, `config/ai { enabled: false }` global
  kill-switch, model whitelist (`gpt-4o-mini`), token/size caps. Set a billing
  budget alert on the OpenAI + GCP projects.
