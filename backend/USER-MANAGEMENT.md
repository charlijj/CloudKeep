# CloudKeep — User management guide (admins + end users)

How accounts come to exist in CloudKeep, why the process is designed the way it
is, and step-by-step instructions for both audiences:

- **Admins** — create users directly, or review and approve self-service
  requests, on the KVM host.
- **End users** — request an account from the public landing page.

Companion docs: `BACKEND-SETUP.md` (bringing controld up — the prerequisite for
any of this), `../sys/HOSTSETUP.md` (the host), and `../frontend/EDGE-SETUP.md`
(the public edge that proxies the request form).

> **Two machines, two roles.** Everything an *admin* does runs on the **KVM
> host** in the controld venv (`seed_user.py`, `review_requests.py`). Everything
> an *end user* does happens in their **browser** on the public site. The two
> only ever meet through the database queue described below.

---

## 1. How it's designed

### 1.1 Two ways an account is created

| Path | Who drives it | When to use it |
|---|---|---|
| **Direct seeding** (`seed_user.py`) | Admin, on the host | You already know who the user is — bootstrapping the first admin, internal staff, scripted setup. |
| **Self-service request** (`review_requests.py`) | End user requests; admin approves | You want people to ask for access themselves, but you still vet and create every account by hand. |

Both paths converge on **one** function — `useradmin.upsert_user` — so there is
exactly one audited place in the whole system that hashes a password and writes
a user row. The request path does not get a second, weaker way in; it just feeds
the same front door.

### 1.2 The core principle — a request grants nothing

The most important design decision: **creating a user is a privileged,
host-only operation, and it stays that way.** The public form does *not* create
a user. Submitting it only inserts an **inert** `pending` row into an
`account_requests` queue. That row:

- holds no password and no role,
- grants no access and consumes no quota,
- can only be turned into a real account by an admin running a CLI **on the
  host**, who sets the password and quotas at approval time.

So adding self-service signup did **not** move the trust boundary onto the
internet. The internet can only *suggest* an account; only a host-local admin
can *create* one. This is what lets us offer easy onboarding without weakening
the security posture.

### 1.3 What is stored, and where

Two tables in the controld SQLite DB (`/var/lib/cloudkeep/db/cloudkeep.db`):

- **`users`** — `username` (unique), `pw_hash` (bcrypt), `role` (`user` /
  `admin`), the four quotas, and `email` / `first_name` / `last_name`
  (populated at approval; `NULL` for directly-seeded users). The password is
  **only ever stored hashed**; the plaintext is read interactively and never
  written to disk or logged.
- **`account_requests`** — the signup queue: `username`, `email`, `first_name`,
  `last_name`, `status` (`pending` / `approved` / `denied`), `src_ip` (for abuse
  forensics), timestamps, and `decided_by` (which host user actioned it).

Every meaningful action lands in the **`audit`** table: `account.request`,
`account.approve`, `account.deny` (and the `vm.*` actions for machine
lifecycle).

### 1.4 The end-to-end flow

```
 End user (browser)            Edge (nginx)            controld (host)              Admin (host CLI)
┌──────────────────┐  POST    ┌─────────────┐  /ck    ┌────────────────────┐
│ Request access ▸ │ ───────► │ strict rate │ ──────► │ POST /account-     │  INSERT pending row
│  user/email/name │  (form)  │ limit + 1k  │         │  requests          │ ───────────────────► account_requests
│  + invite code   │          │ body cap    │         │ gate+validate+cap  │
└──────────────────┘ ◄─────── └─────────────┘ ◄────── │ 303 -> #account-*  │
        ▲  pure-CSS result modal ("pending review")    └────────────────────┘
        │                                                review_requests.py list
        │                                                review_requests.py approve <id>
        │   ◄────────── password delivered out-of-band ──── (admin sets password -> creates user)
        └─────────────────────────────────────────────────────────────────────────────┘
                          then: sign in at /app/
```

### 1.5 Security controls on the public surface (defense in depth)

The request endpoint is the only unauthenticated *write* surface besides login,
so it is gated at every layer:

| Layer | Control |
|---|---|
| Edge | TLS, a dedicated `location = /ck/account-requests` with a strict `limit_req`, the 1 KB body cap, method allow-list. Public reaches controld only here. |
| Gate | **Invite code required**, **fail-closed** — if `SIGNUP_INVITE_CODES` is empty, every request is rejected. Constant-time (`hmac`) comparison; supports a set of codes you can issue/revoke per cohort. |
| Anti-bot | Hidden **honeypot** field — bots fill it and are silently dropped (no JS, no captcha). |
| Anti-CSRF | `Origin` header is checked against the allowed origin; cross-site form posts are dropped. |
| Validation | Strict allow-list: `username` `[a-z0-9_-]{3,32}`, email shape + ≤254 chars, names length/charset-bounded. Generic error response → no field-level oracle, no user enumeration. |
| Caps | `MAX_PENDING_PER_IP` (default **1**) and `MAX_PENDING_REQUESTS` (default **10**) bound the queue; controld also rate-limits per IP (`SIGNUP_RATE_LIMIT`). |
| Privilege | The row is inert. Only the host-side CLI mints a user. Every decision is audited. |

---

## 2. For admins

> All commands run **on the KVM host**, as the controld service user, using the
> controld venv. The examples below use the `app` / `/opt/cloudkeep/backend/src`
> / `/opt/cloudkeep/.venv` layout from `BACKEND-SETUP.md`; if you deployed with
> the dedicated `cloudkeep` / `/opt/cloudkeep/src` / `/opt/cloudkeep/venv`
> layout from `HOSTSETUP.md`, change the user and paths accordingly. To keep the
> examples short:
>
> ```bash
> CK="sudo -u app /opt/cloudkeep/.venv/bin/python /opt/cloudkeep/backend/src"
> ```

### 2.1 Create a user directly (`seed_user.py`)

The most direct path. Use it for the first admin and any account you already
know you want.

```bash
# Minimal: username + interactive password, default quotas.
$CK/seed_user.py alice

# Full: an admin with custom quotas.
$CK/seed_user.py alice --admin \
    --max-vms 4 --max-vcpus 8 --max-mem-mb 16384 --max-disk-gb 200
```

- The password is read interactively (twice, to confirm) — it is **never** taken
  on the command line, so it can't leak into shell history or the process list.
- Re-running for an existing username **updates** that user's password, role,
  and quotas (it does not create a duplicate). This is how you reset a password
  or change a quota.
- Omitted quota flags fall back to the defaults in `config.py`
  (currently **2** VMs, **4** vCPU, **8192** MB RAM, **100** GB disk).

> **Bootstrap tip:** the very first account should be an admin —
> `$CK/seed_user.py <you> --admin` — created during host setup
> (`HOSTSETUP.md` §7 / `BACKEND-SETUP.md` §5).

### 2.2 Enable self-service requests

Self-service is **off until you configure an invite code** (fail-closed). To
turn it on:

```bash
# 1. Put one or more invite codes in .env (comma-separated for several).
echo 'SIGNUP_INVITE_CODES=clinic-2026' | sudo -u app tee -a \
    /opt/cloudkeep/backend/src/.env

# 2. Restart controld so it picks up the new setting.
sudo systemctl restart cloudkeep-controld
```

Then hand the code to the people you want to let in (one code can cover a whole
cohort; issue several and revoke them independently by editing `.env`). Optional
tuning knobs — all have safe defaults — are listed in §4.

> The edge must also proxy `POST /ck/account-requests`. That `location` block is
> in `sys/cloudkeep` and `frontend/EDGE-SETUP.md`; if you deployed the edge
> before this feature, redeploy the vhost (`nginx -t && systemctl reload nginx`)
> and the static frontend so the landing form appears and submits.

### 2.3 Review and approve requests (`review_requests.py`)

```bash
# See what's waiting (pending only; add --all for every status).
$CK/review_requests.py list
#  #1   2026-06-30T14:02:11Z  dr_smith   smith@clinic.org   Sam Smith  [pending]  ip=203.0.113.5

# Inspect one in full before deciding.
$CK/review_requests.py show 1

# Approve -> prompts for the user's initial password, then CREATES the user
# with the requested name/email and the quotas you pass (or defaults).
$CK/review_requests.py approve 1 --max-vms 3
# (add --admin to grant the admin role)

# Deny, optionally recording why.
$CK/review_requests.py deny 2 --reason "unrecognised requester"

# Housekeeping: drop decided requests older than 30 days.
$CK/review_requests.py purge --days 30
```

What `approve` does, in order:

1. Re-validates the stored username and **refuses to overwrite an existing
   user** (deny instead, or have them request a different name).
2. Prompts you for the initial password (twice — never on the command line).
3. Creates the account via the same `upsert_user` path as `seed_user.py`,
   carrying the request's email and name onto the user row.
4. Marks the request `approved` (recording your host username and the time) and
   writes an `account.approve` audit entry.
5. Calls the (currently inert) email notifier — see §5.

> **Deliver the password out-of-band.** The requester never sets their own
> password; you set it at approval and tell them through your own trusted
> channel (in person, a secure message, etc.). Email delivery is scaffolded but
> off by default (§5).

### 2.4 Roles and quotas

| Field | Flag | Default | Meaning |
|---|---|---|---|
| role | `--admin` | `user` | `admin` sees all VMs (vs. only your own); both are subject to quotas. |
| max VMs | `--max-vms` | 2 | Concurrent machines the user may hold. |
| max vCPUs | `--max-vcpus` | 4 | Total vCPUs across the user's running VMs. |
| max memory | `--max-mem-mb` | 8192 | Total RAM (MB) across the user's running VMs. |
| max disk | `--max-disk-gb` | 100 | Total disk (GB) across the user's VM overlays. |

Quotas are enforced live by the `ResourceTracker` at build/start time against
*both* the user's remaining quota and the host's free pool. Change a quota any
time by re-running `seed_user.py <name> --max-… ` (it updates in place).

### 2.5 Auditing

Every account action is recorded in the `audit` table. To review:

```bash
sudo -u app sqlite3 /var/lib/cloudkeep/db/cloudkeep.db \
  "SELECT ts,action,detail FROM audit WHERE action LIKE 'account.%' ORDER BY id DESC LIMIT 20;"
```

Relevant actions: `account.request` (a public submission, with username/email/IP),
`account.approve`, `account.deny`.

---

## 3. For end users

### 3.1 Requesting an account

1. Go to the CloudKeep site (e.g. `https://cloudkeep-auth.duckdns.org/`).
2. Click **Request access**.
3. Fill in the form:
   - **username** — 3–32 characters: letters, digits, `_` or `-` (it's stored
     lowercase).
   - **first name** and **last name**.
   - **email** — where your administrator can reach you.
   - **invite code** — the code your administrator gave you. Without a valid
     code the request is rejected.
4. Submit. You'll see a **"Request received"** confirmation.

> You do **not** choose a password here. Your administrator sets your initial
> password when they approve the request and shares it with you directly.

If you see **"Couldn't submit that"**, re-check your details and the invite code.
Common causes: a mistyped or expired invite code, an invalid username/email, or
you already have a request pending review (only one pending request per network
is allowed at a time). If you see **"Requests are closed"**, the operator hasn't
enabled signups — contact them directly.

### 3.2 What happens next

Your request waits in a queue for a human to review it. An administrator
approves it (creating your account) or denies it. On approval, they'll contact
you with your username and initial password. Nothing is automatic — there is no
email step today, by design.

### 3.3 Signing in

Once your administrator confirms your account is ready:

1. Open the site and click **Enter Launch Portal** (or go to `/app/`).
2. Sign in with your username and the password you were given.
3. Build a machine with **+ build**, then **Connect** when it reports **ready**.

Consider changing nothing about your password storage — CloudKeep keeps your
session token in memory only, so closing the tab signs you out.

---

## 4. Configuration reference (`.env`)

All optional except where noted; sane defaults shown.

| Setting | Default | Purpose |
|---|---|---|
| `ACCOUNT_REQUESTS_ENABLED` | `true` | Master on/off for the public endpoint. |
| `SIGNUP_INVITE_CODES` | *(empty)* | Comma-separated invite codes. **Empty ⇒ all requests rejected** (signups closed). |
| `SIGNUP_RATE_LIMIT` | `5/hour` | Per-IP request rate at controld (the edge throttles too). |
| `MAX_PENDING_REQUESTS` | `10` | Global cap on the pending queue. |
| `MAX_PENDING_PER_IP` | `1` | Pending requests a single IP may hold at once. |
| `DEFAULT_MAX_VMS` / `_VCPUS` / `_MEM_MB` / `_DISK_GB` | 2 / 4 / 8192 / 100 | Quota defaults for new users when no flag is given. |
| `SMTP_ENABLED` | `false` | Master switch for email notifications (see §5). |

---

## 5. Email notifications (groundwork — currently off)

Approval emails are **fully scaffolded but disabled**. With `SMTP_ENABLED=false`
(the default), `notify.py` opens no connection and just logs what it *would*
send; the approval flow already calls it, so the wiring is in place. Failures
can never break an approval — email is best-effort.

To turn it on later (no code change), set these in `.env` and restart controld:

```bash
SMTP_ENABLED=true
SMTP_HOST=smtp.example.com
SMTP_PORT=587
SMTP_USERNAME=apikey-or-user
SMTP_PASSWORD=secret
SMTP_FROM=CloudKeep <no-reply@example.com>
SMTP_USE_TLS=true
PORTAL_URL=https://cloudkeep-auth.duckdns.org   # used for the sign-in link (defaults to ALLOWED_ORIGIN)
```

The approved user then receives a short "your account is ready" email with the
portal link (the password is still delivered out-of-band — it is never emailed).

---

## 6. Troubleshooting

| Symptom | Likely cause / fix |
|---|---|
| Form submit → "Requests are closed" | `ACCOUNT_REQUESTS_ENABLED=false`, or you never set it. Enable + restart controld. |
| Valid details → "Couldn't submit that" | Invalid/empty `SIGNUP_INVITE_CODES` (fail-closed), a per-IP/global cap hit, a bad `Origin`, or invalid field. Check `journalctl -u cloudkeep-controld` for the reason. |
| Landing page has no **Request access** button | Frontend not redeployed. `rsync` the current `frontend/` to the web root. |
| Submitting reloads the SPA / 404s | Edge isn't proxying `POST /ck/account-requests`. Add the `location` block (`EDGE-SETUP.md` §7 / `sys/cloudkeep`), `nginx -t && reload`. |
| controld errors on form posts about "multipart" | The `python-multipart` dependency is missing — re-run the `requirements.txt` pip step (`BACKEND-SETUP.md` §1). |
| `review_requests.py list` shows nothing | No pending requests (try `--all`), or you're pointed at a different `DB_PATH` than controld. |
| Approve says "a user named … already exists" | The username is taken. Deny the request and ask the requester to choose another, or rename. |

---

## 7. Where this lives in the code

| File | Role |
|---|---|
| `src/validation.py` | shared username/email/name validators |
| `src/db.py` | `users` + `account_requests` tables and their methods |
| `src/app.py` | `POST /account-requests` (gated public intake) |
| `src/useradmin.py` | `upsert_user` — the single create/update path |
| `src/seed_user.py` | admin CLI: create/update a user directly |
| `src/review_requests.py` | admin CLI: list / show / approve / deny / purge requests |
| `src/notify.py` | email notification groundwork (inert until `SMTP_ENABLED`) |
| `src/config.py` | all the knobs in §4 |
| `../sys/cloudkeep`, `../frontend/EDGE-SETUP.md` | the edge `location` for the public endpoint |
| `../frontend/index.html` | the landing-page **Request access** form (no JS) |
