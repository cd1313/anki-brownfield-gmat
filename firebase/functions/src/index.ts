// SPDX-License-Identifier: AGPL-3.0-or-later
// GMAT AI proxy — a single HTTPS function that lets the desktop/Android apps use
// AI features WITHOUT shipping an OpenAI key. The real key lives only in Cloud
// Secret Manager here. Every request must carry a Firebase (anonymous) Auth ID
// token. Android may additionally pass a Play Integrity App Check token, but that
// is only enforced when GMAT_AI_APPCHECK_ENFORCED=true (Play Integrity can only
// attest apps installed from Google Play, not sideloaded/GitHub-released APKs).
// Usage is bounded by a per-user daily quota plus a global kill-switch.
//
// The request/response body is the OpenAI chat-completions shape, so the clients'
// existing parsing is unchanged; this function only validates + forwards.

import { initializeApp } from "firebase-admin/app";
import { getAppCheck } from "firebase-admin/app-check";
import { getAuth } from "firebase-admin/auth";
import { FieldValue, getFirestore } from "firebase-admin/firestore";
import { defineSecret } from "firebase-functions/params";
import { onRequest } from "firebase-functions/v2/https";
import * as logger from "firebase-functions/logger";

initializeApp();

const OPENAI_API_KEY = defineSecret("OPENAI_API_KEY");
// Per-user daily request cap; override via the GMAT_AI_DAILY_QUOTA env var
// (functions/.env or the deployed environment) without a code change.
const DAILY_QUOTA = Number(process.env.GMAT_AI_DAILY_QUOTA) || 100;
// Enforce Play Integrity App Check on Android requests. OFF by default because a
// sideloaded / GitHub-released APK cannot produce a valid attestation; flip to
// true only once the app is distributed via Google Play. Auth + quota still apply.
const APPCHECK_ENFORCED = process.env.GMAT_AI_APPCHECK_ENFORCED === "true";

const OPENAI_URL = "https://api.openai.com/v1/chat/completions";
const ALLOWED_MODELS = new Set(["gpt-4o-mini"]);
const MAX_TOKENS_CAP = 800;
const MAX_BODY_BYTES = 16 * 1024;

function today(): string {
  return new Date().toISOString().slice(0, 10); // UTC yyyy-mm-dd
}

export const gmatAiChat = onRequest(
  { secrets: [OPENAI_API_KEY], region: "us-central1", cors: false },
  async (req, res): Promise<void> => {
    if (req.method !== "POST") {
      res.status(405).json({ error: "method_not_allowed" });
      return;
    }
    if ((req.rawBody?.length ?? 0) > MAX_BODY_BYTES) {
      res.status(413).json({ error: "payload_too_large" });
      return;
    }

    // 1. Firebase Auth (anonymous) — required on every platform.
    const match = (req.get("Authorization") || "").match(/^Bearer (.+)$/);
    if (!match) {
      res.status(401).json({ error: "missing_auth" });
      return;
    }
    let uid: string;
    try {
      uid = (await getAuth().verifyIdToken(match[1])).uid;
    } catch {
      res.status(401).json({ error: "invalid_auth" });
      return;
    }

    // 2. App Check — only enforced for Android when APPCHECK_ENFORCED is set
    //    (requires Google Play distribution for Play Integrity to attest). Desktop
    //    has no App Check provider and is always exempt; it declares its platform.
    const platform = req.get("X-Gmat-Platform") || "";
    if (platform === "android" && APPCHECK_ENFORCED) {
      const appCheckToken = req.get("X-Firebase-AppCheck");
      if (!appCheckToken) {
        res.status(401).json({ error: "missing_appcheck" });
        return;
      }
      try {
        await getAppCheck().verifyToken(appCheckToken);
      } catch {
        res.status(401).json({ error: "invalid_appcheck" });
        return;
      }
    }

    const db = getFirestore();

    // 3. Global kill-switch (config/ai { enabled: false } disables everyone).
    const cfg = await db.doc("config/ai").get();
    if (cfg.exists && cfg.get("enabled") === false) {
      res.status(503).json({ error: "ai_disabled" });
      return;
    }

    // 4. Validate + sanitize the request (whitelist model, force JSON+temp0, cap
    //    tokens). Done BEFORE the quota bump so a malformed request never costs a
    //    user part of their daily allowance.
    const body = (req.body ?? {}) as Record<string, unknown>;
    const model = String(body.model || "");
    if (!ALLOWED_MODELS.has(model)) {
      res.status(400).json({ error: "model_not_allowed" });
      return;
    }
    if (!Array.isArray(body.messages)) {
      res.status(400).json({ error: "missing_messages" });
      return;
    }

    // 5. Per-user daily quota (atomic) — counts only well-formed requests that are
    //    about to hit OpenAI.
    const quotaRef = db.doc(`quotas/${uid}/days/${today()}`);
    const withinQuota = await db.runTransaction(async (tx) => {
      const snap = await tx.get(quotaRef);
      const used = (snap.exists ? (snap.get("count") as number) : 0) || 0;
      if (used >= DAILY_QUOTA) return false;
      tx.set(
        quotaRef,
        { count: used + 1, updatedAt: FieldValue.serverTimestamp() },
        { merge: true }
      );
      return true;
    });
    if (!withinQuota) {
      res.status(429).json({ error: "quota_exceeded" });
      return;
    }

    const payload = {
      model,
      messages: body.messages,
      temperature: 0,
      response_format: { type: "json_object" },
      max_tokens: Math.min(Number(body.max_tokens) || MAX_TOKENS_CAP, MAX_TOKENS_CAP),
    };

    // 6. Forward to OpenAI with the server-held key; return its response verbatim.
    try {
      const upstream = await fetch(OPENAI_URL, {
        method: "POST",
        headers: {
          Authorization: `Bearer ${OPENAI_API_KEY.value()}`,
          "Content-Type": "application/json",
        },
        body: JSON.stringify(payload),
      });
      const text = await upstream.text();
      res.status(upstream.status).type("application/json").send(text);
    } catch (err) {
      logger.error("openai_forward_failed", err);
      res.status(502).json({ error: "upstream_failed" });
    }
  }
);
