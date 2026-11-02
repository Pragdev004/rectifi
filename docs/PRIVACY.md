# Privacy: what Rectifi protects, and what it does not

This is a hobby project run by one person for a few friends. It tries to keep your money data private from the other
friends and, as far as a program can, from the person who runs the server (the owner). It does not claim more than that.
The live page is `GET /api/privacy`; this file says the same things in sentences. A test checks that every field below
matches the code.

## Who can read what

"Sealed" means the text is encrypted in the database with a key that only your password (or your recovery code) can open.
"Plain" means readable by anyone who can read the database. The two lists are further down.

| Who is looking | Plain data (amounts, dates, categories, account names, last 4 digits, caps, times, well-known shop names) | Sealed data (SMS text, narrations, every other payee, notes) |
|---|---|---|
| Another friend, through the app | No (the app checks, and the database shows each person only their own rows) | No (the same checks, and a different key) |
| Someone with a database dump or a stolen backup | Yes, readable | No, unless they crack your password (scrypt makes each guess slow) |
| The person who runs the server (the owner), browsing the database | Yes, readable | No |
| The owner changing the code, or reading server memory or the login request | Yes | Yes. Passwords and SMS pass through the server in clear |

The last row is the important one. Encryption in the database limits what a person can read by looking at stored data.
It does not stop the person who controls the program from changing the program. If the owner edited the code to write
your password or your SMS to a file, nothing here would stop him. Trust him, or do not join. This is true of almost every
web app; most just do not say it.

## Sealed fields

These columns hold encrypted text (`rx1.` envelopes, AES-256-GCM under your own data key). The database refuses plain
text in them once you are encrypted (a trigger, not only the app).

- `transactions.payee`, `transactions.description`, `transactions.raw_text`, `transactions.note`,
  `transactions.category_reason`, `transactions.upi_ref_sealed`
- `merchant_rules.pattern` (only rules you taught; the built-in rules are public and stay plain)
- `weekly_reviews.commitment`
- `phone_devices.name`
- `sms_inbox.sender`, `sms_inbox.raw_text`, `sms_inbox.reason`
- `import_batches.filename`
- `merchant_patterns.pattern`, `merchant_patterns.merchant`
- `alert_rules.target` (only a cap on a shop that is not on the list below)
- `alerts.title`, `alerts.detail`
- `push_subscriptions.label`

A text that arrives while you are signed out (a bank SMS forwarded by your phone) is sealed to your public key instead,
so the server can store it without being able to read it. It is moved under your data key the next time you sign in.

## Keyed hashes

Some columns have to stay comparable so duplicates can be found, but must not leak the text behind them. They hold a
keyed hash (`b1:` followed by an HMAC), not the text:

- `transactions.upi_ref`, `transactions.fingerprint`
- `merchant_rules.pattern_blind`, `merchant_patterns.pattern_blind`, `alert_rules.target_blind`
- `sms_inbox.text_hash`, `sms_seen.text_hash`

A hash cannot be read back. Someone who already knows a UPI reference could check whether it is in the database; they
cannot list the references.

## Plain on purpose

These stay readable, because alerts and the signed-out SMS capture need them while you are not signed in:

- `amount`, `txn_date`, `direction`, `category`
- `account name`, `account last 4 digits`
- `budget and alert caps`, `times`
- `merchant_key (allow-listed brands only)`

`merchant_key` holds a shop name only when the payment is to one of these well-known shops, so "Swiggy more than
₹1,500 a week" can fire while you are signed out: Swiggy, Instamart, Zomato, Blinkit, Zepto, BigBasket, JioMart,
Domino's, EatSure, Amazon, Flipkart, Myntra, Ajio, Meesho, Nykaa, Uber, Ola, Rapido, IRCTC, BookMyShow, PVR, Netflix,
Spotify, Hotstar. The list is fixed in the code. Nothing that points to health, lending, investing, donations or a person
is on it. The database allows no other value in that column. Known limit: a person whose own name is Ola could be tagged
as the taxi company.

Your phone's push address and keys are also plain, because a push must be sent while you are signed out.

## The owner's own data

The owner's tables for the AI chat and library are not sealed in this phase: `classify_jobs`, `charts`, `conversations`,
`rag_documents`, `rag_chunks`, `trace_spans`. They hold only the owner's data, never a friend's. Friends cannot use the
AI features at all, and a friend's rows are never indexed, charted or sent to any model. The model that helps the owner
runs either on the owner's machine or at a cloud provider (the page says which); your data does not reach it either way.

The owner used Rectifi on a laptop before friends joined, so his old rows were stored in plain text. When he claims the
owner account, a background job seals them in small batches. After that, old plain copies can still sit in backups
(including the one taken before the upgrade), in dead rows and in the database's write-ahead log until they are removed.
Your data never has this gap: it is sealed from your first row.

## Passwords and the recovery code

There is no email and no password reset. Not for you, not for the owner. If you forget your password, the recovery code
you saved at sign-up is the only way back. If you lose both, your sealed text is gone for good, because nothing else can
open your key. This is on purpose: a reset path would be a way in for anyone, including the owner.

## Export and delete

Settings can download everything you have as a ZIP of CSV files (money as exact text; anything a spreadsheet could run
as a formula is defused). Settings can also delete your account: it needs your password and your username, and it
removes every row you own in every table and signs you out everywhere. Old backups keep your encrypted text for up to 7
days (the backup days on the live page). Amounts and dates in those backups are readable, like any plain data.

## What this does not cover

- The encryption is built from standard parts (AES-256-GCM, X25519, scrypt, HKDF, HMAC-SHA256 from the `cryptography`
  library), but the way they are put together is this project's own and has not been reviewed by an outside expert.
- Text and keys sit in the server's memory while it works. Memory, swap and crash dumps can hold decrypted text until it
  is overwritten.
- Holding other people's bank data has legal duties in India (for example the DPDP Act). This project has not assessed
  them. If it grows beyond a few friends, ask someone who knows.
- Real bank SMS wording, Android battery savers and the hosting setup have only been tested with fakes so far.

## If you want more privacy than this

Run your own Rectifi on your own laptop (laptop mode: one person, nothing leaves the machine, no sign-up) or use a
bank's own app. A friend running the server can always, in principle, read what passes through it.
