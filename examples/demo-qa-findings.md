# E2E QA — Castaway Studio — Saturday, June 21st

Full end-to-end pass on staging (studio.castaway.example) after the v0.9.4 deploy.
Tested on Chrome + Safari, desktop and mobile widths.

Setup & example accounts (do NOT file these as tasks — reference only):
- Owner: demo.owner@castaway.example / DemoPass1! (plan: Studio)
- Producer: demo.producer@castaway.example / DemoPass1!
- Guest editor: demo.guest@castaway.example / DemoPass1! (invited, never logged in)
- Test show: "Static & Signal" — 42 episodes, 2 scheduled, 1 draft

## Test 1 — Episode upload & publishing

### Feature: Audio upload

🔴 Bug 1.1.1 — Uploading an episode over 200MB fails silently: the progress bar
reaches 100%, then the episode never appears in the drafts list. No error toast,
nothing in the activity feed. Repro 3/3 with a 212MB WAV.
image 1.1.1 — the upload modal after completion: progress bar stuck at 100% with no draft created, empty drafts list visible behind it

🟡 Bug 1.1.2 — The upload dropzone says "Drop you're file here". Typo: should be
"your". Cosmetic only.

✅ Bug 1.1.3 — (Fixed) Chapters imported from the MP3 were off by one — this was
fixed in PR #47 and verified working on staging today. Not reproducible anymore.

### Feature: Scheduling

🔴 Bug 1.2.1 — Scheduling an episode for 9:00 AM in the show's timezone (Berlin)
publishes it at 9:00 AM UTC instead. The RSS feed timestamp confirms the wrong
zone. This makes scheduled publishing unusable for non-UTC shows.
image 1.2.1 — episode set for 09:00 CET showing pubDate 09:00Z in the feed

🟠 Feature 1.2.2 — There's no way to unschedule an episode without deleting it.
Producers expect a "revert to draft" action on scheduled episodes.

## Test 2 — Notifications

### Feature: Email notifications

🟠 Feature 2.1.1 — Producers get no notification when a scheduled episode
actually goes live. They want a "published" confirmation email.

🟠 Feature 2.1.2 — When a collaborator leaves a comment on a draft episode,
nobody is notified. The comment just sits there until someone opens the draft.

🟠 Feature 2.1.3 — Failed uploads (see 1.1.1) should notify the uploader by
email, since big uploads are often left running unattended.

## Test 3 — Team & permissions

### Feature: Roles

🔴 Bug 3.1.1 — A Guest editor can open the show's Billing page by navigating
directly to /settings/billing. The nav item is hidden but the route is not
protected — they can see the card's last four digits and the invoice history.
image 3.1.1 — billing page rendered while logged in as demo.guest, showing the owner's invoice list

🟡 Bug 3.1.2 — The "Invite teammate" modal lets you submit an empty role
dropdown; the invite email then says "joined as undefined".

## Test 4 — Billing & plans

### Feature: Plan changes

🔴 Bug 4.1.1 — Downgrading from Studio to Indie while a bulk upload is running
orphans the in-flight episodes: they finish uploading but are invisible on the
Indie plan's episode list, and storage is still billed for them.

🟠 Feature 4.1.2 — Deleting a show should warn that its RSS feed URL dies
immediately and existing subscribers lose the feed. Right now it's a single
confirm with no explanation of consequences.

## Test 5 — Analytics

### Feature: Listener stats

🟢 Test 5.1.1 — (DONE) Per-episode listener graph — shipped in PR #52, verified
on staging: numbers match the raw log export.

⚪ Note 5.1.2 — Do NOT fix yet, investigate only: the "unique listeners" count
for Static & Signal dropped ~40% on June 12th with no matching drop in
downloads. Find out why before anyone touches the aggregation code — it may be
a bot-filter change, not a bug.

🔴 Bug 5.1.3 — Analytics page intermittently shows a blank chart on Safari.
Could not find reliable repro steps (maybe 1 in 5 loads). The Apple embed
partner said their widget review is still pending on their side, so the
fallback chart may be the only path for now — blocked on their approval either
way.

## Wrap-up

Overall much better than the May pass. Priorities from the team: the scariest
two are the scheduling timezone (1.2.1) and the guest billing access (3.1.1).
The notification asks (2.1.x) keep coming up in every session — probably worth
doing as one piece of work rather than three.
