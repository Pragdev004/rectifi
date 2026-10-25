# Rectifi Android: architecture, design and build plan

## 0. Can the Android app be built in this container? No.

I checked this directly:

- **Present:** OpenJDK 21, Gradle 8.14.3 (`/opt/gradle`), Node 22, Python 3.13 with all Rectifi dependencies, and PostgreSQL 16 (installed, currently stopped). The machine has 2 CPUs and 7 GB RAM.
- **Missing:** the Android SDK (no `ANDROID_HOME`, no `sdkmanager`, no `adb`).
- **Blocked by the proxy (403 on CONNECT):** `dl.google.com`, `maven.google.com`, `services.gradle.org`, `repo.maven.apache.org`, `repo1.maven.org`, `plugins.gradle.org`, `huggingface.co` and `storage.googleapis.com`. Policy denials should not be retried.
- **Reachable:** `api.github.com` (200), but the session's GitHub token is invalid. A repo would have to be attached with `add_repo` before CI or pushing is possible.

So nothing that needs AGP, Compose, SQLDelight, LiteRT-LM or PdfBox can compile here.

**What does work here:** Gradle's own lib folder ships `kotlin-compiler-embeddable-2.0.21.jar`, `kotlin-stdlib-2.0.21`, `kotlinx-serialization-json-jvm-1.6.2` (the runtime only, not the compiler plugin), `junit-4.13.2` and `hamcrest-core-1.3`. I ran `java -cp <those jars> org.jetbrains.kotlin.cli.jvm.K2JVMCompiler -version` and it printed `kotlinc-jvm 2.0.21`. I did not compile a real file, because this session was read-only. That is the first thing Phase 0 must confirm.

This means **a pure-Kotlin `:core` module can be compiled and unit-tested here**, and so can the Python reference with Postgres. `:core` holds money, SMS parsing, the parser registry, masking, importer logic, insights, budgets, reconciliation, weekly review and the AI harness.

**What this means for the workflow:**
- **In the container:** all `:core` logic plus its JUnit tests, the shared fixtures, Python golden exports, design specs, and Android-module source written without compiling.
- **On the laptop (Android Studio on Windows) or in GitHub Actions:** every build of `:data` and `:app`, emulator or phone tests, and the APK.
- **Rule for the engineer:** keep the Android modules thin, and push logic into `:core` so it is tested here. Every Android-module change is "unverified" until a laptop or CI build passes. Record that honestly in PROGRESS.md.
- **CI:** add `.github/workflows/android.yml` in Phase 1. GitHub-hosted `ubuntu-latest` images are commonly documented to include the Android SDK, but that is UNSURE: verify on the first run, and fall back to `android-actions/setup-android` if needed. Once the repo is attached to the session, a container session can push and read CI results, which closes the compile loop without the laptop.

---

## A. Architecture

### A1. Repository layout: a monorepo inside the existing `rectifi/`

The shared fixtures must be read by pytest, vitest and Kotlin from the same paths, so keep everything in one repo (`web/` already lives there).

```
rectifi/
  shared/
    registry/sms_banks.json                 # data-driven SMS parser registry (product data, read by Python AND Kotlin)
    registry/category_synonyms.json         # "food"->"Food & Dining", "petrol"->"Transport" (fast-path)
    fixtures/
      sms/corpus.jsonl                      # golden SMS corpus (masked real + synthetic)
      money/parse_amount.json, amounts_in_text.json   # fmt_inr cases stay in web/src/lib/money.cases.json (now 3 consumers)
      privacy/mask.json
      narration/narration_details.json
      pdf/join_wrapped.json, decide_directions.json
      pdf/words/<fixture>.json              # pdfplumber word boxes of tests/fixtures/*.pdf (input for Kotlin table rebuild)
      statements/expected/<file>.json       # rows + assess result for each tests/fixtures statement
      scenarios/<name>.json + scenarios/expected/<name>.json   # ledger states -> summary/budgets/reconcile/review/insights
      ask/eval.jsonl                        # harness eval corpus (Kotlin-only for now)
  scripts/export_shared_fixtures.py         # Python = oracle: writes goldens; --check fails on drift
  tests/test_shared_fixtures.py             # Python checks itself against shared/ (Postgres _test db)
  rectifi/sms_registry.py                   # Python interpreter for sms_banks.json (additive; generic rules unchanged)
  android/
    settings.gradle.kts  gradle/libs.versions.toml  gradle.properties
    core/   (Kotlin JVM library, no Android imports)
    data/   (Android library: SQLDelight + SQLCipher, repositories = the "tools layer")
    app/    (Compose UI, SMS capture, workers, PDF/XLSX adapters, model runtime adapter, lock, backup)
    eval/   (JVM CLI: runs the harness eval on the laptop with litertlm-jvm)
    core/scripts/test-in-container.sh       # kotlinc + JUnit runner using /opt/gradle-8.14.3/lib jars
  .github/workflows/android.yml, release.yml
```

Additive Python changes, keeping the 822 tests passing:
- `rectifi/sms_registry.py`. `classify_sms` tries registry bank rules first, then the existing generic rules. The registry starts with sender data only and **zero** bank rules, so behaviour is unchanged on day 1.
- The exporter, `tests/test_shared_fixtures.py`, and a new rule in CLAUDE.md: "shared/ is the contract. Change Python, regenerate goldens with `--write`, and Kotlin must follow."

### A2. Stack choices

| Concern | Choice | Why |
|---|---|---|
| Language/UI | Kotlin, Jetpack Compose, Material 3 used only as plumbing (theme, ripple, text fields, sheets). Visible components are custom. | Your decision. Compose makes the Passbook look possible without fighting View XML. |
| minSdk / target | minSdk 26, target and compile on the latest stable | API 26 gives `Matcher.group(name)` (named regex groups), `java.time`, adaptive icons and Compose `FontVariation`. |
| DI / arch | Manual `AppContainer`, ViewModel + `StateFlow<UiState>` (Loading / Empty / Content / Error) | No Hilt or kapt. Fewer build failures for a solo student. |
| Database | **SQLDelight 2.4.0** (docs version) with `android-driver` + `coroutines-extensions`, on **SQLCipher** `net.zetetic:sqlcipher-android:4.19.0` + `androidx.sqlite:sqlite:2.7.0` (README versions) | See A3. |
| Money | `Long` paise in SQLite (`INTEGER`, exact, `SUM` stays exact) and an `@JvmInline value class Money(val paise: Long)` in `:core`. `java.math.BigDecimal` only at parse, format and percent edges. Never `Double`. | |
| Background | WorkManager (inbox backfill, daily auto-export, optional Drive backup, weekly-review reminder, model download) | |
| SMS | Manifest `BroadcastReceiver` for `SMS_RECEIVED` (exempt from implicit-broadcast limits) + `ContentResolver` inbox backfill (`Telephony.Sms.Inbox.CONTENT_URI`) | |
| PDF | **PdfBox-Android** `com.tom-roush:pdfbox-android:2.0.27.0`, Apache-2.0, based on PDFBox 2.0.27 | Only in `:app`. Text plus word positions through a `PDFTextStripper` subclass. Avoid iText and MuPDF (both AGPL). |
| XLSX | Hand-written reader in `:core`: `java.util.zip` + `javax.xml.parsers` SAX (present on Android and the JVM) for `sharedStrings.xml`, `sheet1.xml`, and Excel serial dates | Apache POI is heavy and awkward on Android. About 200 lines, tested here. `.xls` is refused with Rectifi's message. |
| CSV | Hand-written RFC-4180 reader in `:core` with delimiter sniffing (`,` `;` `\t` `|`, most consistent column count over the first 50 lines) and UTF-8 BOM strip | Mirrors pandas `sep=None`. |
| On-device LLM | **LiteRT-LM** (`com.google.ai.edge.litertlm:litertlm-android`; Google's page says `latest.release`, so pin after the first sync), Apache-2.0, v0.16.0 on GitHub | MediaPipe LLM Inference is "maintenance-only". LiteRT-LM has a Kotlin API, CPU/GPU/NPU, tool calling "with constrained decoding", and a `litertlm-jvm` artifact for laptop evals. Hidden behind an interface; see A6. |
| Lock | `androidx.biometric` BiometricPrompt (`BIOMETRIC_STRONG or DEVICE_CREDENTIAL`), `FLAG_SECURE` | |
| Backup | Encrypted export file (SAF) → Auto Backup + Block Store → optional Drive `appDataFolder` | See A7. |
| Fonts | Bundled TTFs in `res/font`: Fraunces (variable), IBM Plex Sans 400/500/600, IBM Plex Mono 400/500, OFL licences in `assets/licenses` | Android `res/font` cannot use the web's woff2 files. |

### A3. Database: SQLDelight + SQLCipher, not Room

**Why SQLDelight:**
- It is SQL-first like `schema.sql` and Rectifi's "raw SQL, no ORM" rule.
- Partial unique indexes (`WHERE upi_ref IS NOT NULL`) are plain SQL in `.sq` files. Room's `@Index` cannot express `WHERE`; you would have to create them by hand and fight Room's schema validation.
- `AndroidSqliteDriver(schema, context, name, factory: SupportSQLiteOpenHelper.Factory, callback, ...)` takes a factory parameter. I confirmed this in the driver source, so SQLCipher's `SupportOpenHelperFactory(passphraseBytes)` plugs straight in.
- Room 2 (and a Room 3 path) is supported by SQLCipher too, so Room is viable. It just fits the rules worse.

**Why SQLCipher at all (honest view):**
- Android file-based encryption already protects data while the phone is locked.
- SQLCipher adds protection when the phone is unlocked and the file is pulled out (root, forensic tools, a careless backup).
- It is defense in depth for a file holding bank SMS. Its cost is APK size (UNSURE, a few MB per ABI) and backup complexity.
- I recommend it **from Phase 1**, because encrypting an existing plain database later needs a migration path.
- It needs `System.loadLibrary("sqlcipher")` in `Application.onCreate` (per the README) and API 23+.

**Key handling (`data/.../db/DbKeyManager.kt`):**
- At first launch, generate a random 32-byte key.
- Wrap it with an Android Keystore AES-256-GCM key that is **not** user-authentication-bound, and store the wrapped key in `noBackupFilesDir`.
- Why not authentication-bound: the SMS receiver must write while the app is locked. The app lock is a UI gate. Encryption at rest protects the file.

**Dialect and SQL rules:**
- Use `INSERT ... ON CONFLICT DO NOTHING`, **never** `INSERT OR IGNORE`. `OR IGNORE` also silently swallows CHECK and NOT NULL violations, such as `amount > 0`.
- SQLCipher bundles its own SQLite, so upsert syntax works at runtime. But SQLDelight picks its dialect from minSdk (26 means SQLite 3.18, which has no upsert), so set `dialect("app.cash.sqldelight:sqlite-3-38-dialect:<ver>")` explicitly. UNSURE: verify this in Phase 1.
- Fallback if the dialect override fails: `INSERT` + catch `SQLiteConstraintException` on the unique index. The database is still the guard.
- Detect duplicates with `SELECT changes()` after the insert. Do not use `RETURNING`.
- **`PRAGMA foreign_keys = ON` in the open callback.** SQLite leaves foreign keys off by default; Postgres enforces them.

**Schema (`data/src/main/sqldelight/app/rectifi/data/*.sq`), mirroring `rectifi/schema.sql`:**

```sql
CREATE TABLE accounts (id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE, last4 TEXT, created_at INTEGER NOT NULL);
CREATE TABLE categories (id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE,
  kind TEXT NOT NULL CHECK (kind IN ('expense','income','transfer')));
CREATE TABLE import_batches (id INTEGER PRIMARY KEY, account_id INTEGER REFERENCES accounts(id) ON DELETE SET NULL,
  filename TEXT, created_at INTEGER NOT NULL, added INTEGER NOT NULL DEFAULT 0, merged INTEGER NOT NULL DEFAULT 0,
  duplicates INTEGER NOT NULL DEFAULT 0, needs_review INTEGER NOT NULL DEFAULT 0,
  snapshot_changes TEXT NOT NULL DEFAULT '[]', undone_at INTEGER);
CREATE TABLE transactions (
  id INTEGER PRIMARY KEY,
  account_id INTEGER REFERENCES accounts(id) ON DELETE SET NULL,
  amount_paise INTEGER NOT NULL CHECK (amount_paise > 0),
  direction TEXT NOT NULL CHECK (direction IN ('debit','credit')),
  txn_date TEXT NOT NULL CHECK (length(txn_date) = 10),          -- ISO yyyy-MM-dd, sorts correctly
  payee TEXT, description TEXT, upi_ref TEXT, fingerprint TEXT,
  category_id INTEGER REFERENCES categories(id),
  source TEXT NOT NULL CHECK (source IN ('sms','statement','chat','manual')),
  raw_text TEXT, note TEXT, created_at INTEGER NOT NULL,
  import_batch_id INTEGER REFERENCES import_batches(id) ON DELETE SET NULL,
  sms_rule_id TEXT                                                  -- NEW: which parser rule made it (corpus loop)
);
CREATE UNIQUE INDEX uq_txn_upi_ref     ON transactions(upi_ref, direction) WHERE upi_ref IS NOT NULL;
CREATE UNIQUE INDEX uq_txn_fingerprint ON transactions(fingerprint)        WHERE fingerprint IS NOT NULL;
CREATE INDEX idx_txn_date ON transactions(txn_date);
CREATE INDEX idx_txn_account ON transactions(account_id, txn_date);
CREATE INDEX idx_txn_batch ON transactions(import_batch_id) WHERE import_batch_id IS NOT NULL;
CREATE TABLE budgets (category_id INTEGER NOT NULL REFERENCES categories(id) ON DELETE CASCADE,
  month TEXT NOT NULL CHECK (substr(month,9,2) = '01'), limit_paise INTEGER NOT NULL CHECK (limit_paise > 0),
  PRIMARY KEY (category_id, month));
CREATE TABLE balance_snapshots (account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
  as_of TEXT NOT NULL, balance_paise INTEGER NOT NULL, source TEXT NOT NULL DEFAULT 'manual', PRIMARY KEY (account_id, as_of));
CREATE TABLE merchant_rules (id INTEGER PRIMARY KEY, pattern TEXT NOT NULL UNIQUE,
  category_id INTEGER NOT NULL REFERENCES categories(id) ON DELETE CASCADE);
CREATE TABLE weekly_reviews (week_start TEXT PRIMARY KEY CHECK (strftime('%w', week_start) = '1'),
  commitment TEXT NOT NULL, created_at INTEGER NOT NULL);
CREATE TABLE sms_inbox (id INTEGER PRIMARY KEY, sender TEXT, raw_text TEXT NOT NULL, sent_at INTEGER,
  received_at INTEGER NOT NULL, status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','added','dismissed')),
  reason TEXT, text_hash TEXT NOT NULL UNIQUE,
  origin TEXT NOT NULL CHECK (origin IN ('receiver','backfill','share','paste')));
CREATE TABLE sms_seen (text_hash TEXT PRIMARY KEY, seen_at INTEGER NOT NULL);
CREATE TABLE app_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);   -- last_backfill_ms, schema notes, etc.
```

`phone_devices` is dropped: there is no gateway on the phone. Seed `DEFAULT_CATEGORIES` and `DEFAULT_RULES` exactly as in `rectifi/db.py`.

Postgres features with no SQLite equivalent, and their replacements:
- `strpos` in `_categorize`: done in Kotlin over the small `merchant_rules` list (longest pattern first, kind filter).
- `generate_series` in `daily_spending`: done in Kotlin.
- `ILIKE`: SQLite `LIKE`, which is case-insensitive for ASCII.

### A4. Modules and the "tools layer" rule

**`:core`** (`app.rectifi.core.*`, pure Kotlin, must compile with Kotlin 2.0.21):
- No Android imports.
- No kotlinx-serialization plugin. Read JSON with `Json.parseToJsonElement`.
- No 2.1+ language features (guard conditions in `when`, non-local break/continue, multi-dollar strings).

Packages and the Python they port:
- `money`: `Money`, `MoneyParse` (`parse_amount`, ROUND_HALF_UP to paise), `MoneyFormat` (`fmt_inr` exactly, including truncation when `paise=false`), `AmountsInText` (the guard's `amounts_in_text`).
- `sms`: `SenderId`, `GenericRules` (parsing.py `AMOUNT`, `DEBIT_WORDS`, `CREDIT_WORDS`, `ACCOUNT`, `REF`, `VPA`, `TO_PAYEE`, `FROM_PAYEE`, `DATE_PATTERNS`), `BankRegistry`, `SmsClassifier` (`classify_sms` + registry), `BestEffort` (`best_effort_sms`), `SmsDigest` (`_sms_digest`, same string so hashes match Python).
- `privacy`: `Mask` (`mask_sensitive`).
- `importer`:
  - `CsvReader`, `XlsxReader`, `HeaderFinder` (`_looks_like_header`), `ColumnDetect` (`detect_columns`, `KEYWORDS`), `Normalise`, `AssessStatement` (thresholds copied exactly), `StatementAccount` (`_last4_from_lines`), `Fingerprint` (`_fingerprint` string format), `NarrationDetails`.
  - `pdf/`: `JoinWrapped`, `TextLineParser`, `DecideDirections`, `HeaderAnchoredTable`. The last one is new: rebuild rows from word boxes using the header words' x-ranges and merge wrapped lines. It replaces pdfplumber `extract_tables`, which has no Android equivalent.
- `ledger`: the pure calculators fed with rows from `:data`.
  - `SpendingSummary`, `BudgetStatus`, `SpendingPace`, `Reconcile`, `WeeklyReview` (+ `weeklyReviewText`), `AccountChoice` (`_sms_account`, `account_for_statement`, `default_account`, `only_account_id`), `Categorize`, `DateWords` (`resolve_date`, `parse_month`, `week_bounds`).
- `insights`: `InsightsEngine` (all 8 rules and the tunables block copied verbatim), `Insight(kind, severity, title, detail, numbers, actionLabel, actionPage, key, apply)`.
- `harness`: see A6.
- `Clock` interface: never call `LocalDate.now()` directly.

**Compile-time "no money maths outside the tools layer":**
- `Money`'s `plus`, `minus` and `compareTo` are `internal` to `:core`.
- `:data` only runs SQL (`SUM` over integer paise is exact) and hands rows to `:core` calculators.
- `:app` can only format (`Money.format(paise, sign)`) and parse user input (`Money.parse`). It physically cannot add rupees.

**Numeric parity traps the engineer must copy exactly** (the shared fixtures will catch any miss):
- Python's bare `.quantize(Decimal("0.1"))` uses **ROUND_HALF_EVEN**. This covers savings rate, share, percent, pace and change %. Kotlin: `x.divide(y, MathContext(28, HALF_EVEN)).multiply(100).setScale(1, HALF_EVEN)`.
- `parse_amount` and insights `_whole` use **HALF_UP**.
- Two-digit years: Python `%y` maps 69–99 to the 1900s and 00–68 to the 2000s. Kotlin: `DateTimeFormatterBuilder().appendValueReduced(YEAR, 2, 2, LocalDate.of(1969,1,1))`, with `parseCaseInsensitive()` and `Locale.ENGLISH` for month names.
- Java `\d` is ASCII but Python's is Unicode. Registry patterns use `[0-9]`.
- `statistics.median` of an even count averages the two middle values. Use BigDecimal with `MathContext(28, HALF_EVEN)`.

**`:data`** (Android library):
- `db/DbFactory.kt`, `DbKeyManager.kt`, `Seed.kt`.
- Repositories that compose SQL with `:core`: `LedgerRepository` (`add_transaction` semantics: `Added` / `Duplicate(existing)`, category-kind check, `set_category(remember)`, `set_account` refusing statement rows, delete), `AccountsRepository` (+ `recordBalance`, `reconcile`), `BudgetsRepository` (+ `copyBudgets`), `ReviewRepository`, `InsightsRepository`, `SmsRepository` (`ingest`, inbox list/dismiss/added, prefill), `ImportRepository` (`importRows` with batch, merge window ±2 days, snapshots, `undoImport`), `BackupRepository`.
- Read screens subscribe to SQLDelight `Flow`s, so Overview refreshes the moment an SMS lands.

**`:app`:**
- `RectifiApp.kt` (`loadLibrary`, `AppContainer`), `MainActivity.kt` (lock gate, edge-to-edge).
- `ui/theme/*`, `ui/components/*`, `ui/<screen>/*Screen.kt + *ViewModel.kt`.
- `capture/` (`full` flavor only): `SmsReceiver.kt`, `InboxBackfill.kt`, `BackfillWorker.kt`, `CaptureNotifier.kt`.
- `share/ShareTargetActivity.kt`: receives `text/plain` from the share sheet (both flavors).
- `importing/PdfBoxReader.kt` (PdfBox to `core.importer.pdf.PdfContent` word boxes), `SafFiles.kt`.
- `ai/LiteRtLmModel.kt` (implements `core.harness.LocalModel`), `ModelDownloader.kt`, `ModelStore.kt`.
- `lock/AppLock.kt`, `backup/*`, `settings/*`.
- `src/debug/.../SimulateSmsScreen.kt`: calls the same ingest path. Debug builds only.

**Flavors:**
- `full`: `src/full/AndroidManifest.xml` adds RECEIVE_SMS, READ_SMS and the receiver.
- `lite`: no SMS permission at all. Capture is by share sheet, paste, quick entry and statements.

Both use the **same `applicationId` and the same signing key**, so switching flavors keeps the data (versionCode must not go down).

**Source-scan tests in `:core`, ported from Rectifi's import checks:**
- `sms`, `importer`, `insights` and `ledger` must not import `harness` or `ai`.
- `harness` must not import `sms` parsing internals. It gets only the user's Ask text.

### A5. SMS capture, the parser registry and the golden corpus

**Pipeline (`core.sms.SmsClassifier.classify(sender, body, sentAtMs)`):**

1. **Normalise the sender id.** Headers look like `AD-HDFCBK-S`: a 2-letter prefix, the header, and a TRAI suffix (`-P` promotional, `-S` service, `-T` transactional, `-G` government; mandatory since 6 May 2025).
   - A `-P` suffix is ignored ("promotional sender").
   - A plain phone number is ignored (as Rectifi does).
2. **Bank-specific rules.** The `BankProfile` is found by header. Its rules are tried in order. Each rule is a regex with named groups `(?<amount>…)`, `(?<account>…)`, `(?<payee>…)`, `(?<ref>…)`, `(?<date>…)`, a fixed `direction`, `date_formats`, and `kind` (`transaction` | `ignore` | `review`). Explicit `ignore` rules cover failed or declined transactions, mandate created, bill due and card statement notices.
3. **Generic rules.** No bank rule matched: today's `parsing.py` logic, including every safety rule in `classify_sms` (OTP goes to review or ignore, promo words, the past-tense verb requirement).
4. Result: `SmsVerdict(kind, parsed, reason, ruleId)`.

**`shared/registry/sms_banks.json` shape:**

```json
{"version": 1,
 "banks": [{"id": "hdfc", "name": "HDFC Bank", "headers": ["HDFCBK", "HDFCBN"],
   "rules": [{"id": "hdfc.upi_debit.v1", "kind": "transaction", "direction": "debit",
              "pattern": "(?i)Sent Rs\\.?(?<amount>[0-9,]+(?:\\.[0-9]{1,2})?) From HDFC Bank A/C \\*(?<account>[0-9]{4}) To (?<payee>.+?) On (?<date>[0-9]{2}/[0-9]{2}/[0-9]{2}) Ref (?<ref>[0-9]{9,16})",
              "date_formats": ["dd/MM/yy"], "corpus_ids": ["hdfc-001","hdfc-002"]}]}]}
```

- **Common regex subset:** only `(?i)` at the start, ASCII classes, no possessive or atomic groups, no `\p{}`.
- Python's loader rewrites `(?<name>` to `(?P<name>` (Python's `re` reads `(?<` as lookbehind).
- Both sides have a test that compiles every pattern and that every `corpus_ids` entry matches its rule.
- **Precision first:** a rule must reproduce its corpus lines exactly.

**`shared/fixtures/sms/corpus.jsonl`, one line each:**

```json
{"id":"hdfc-001","bank":"hdfc","sender":"AD-HDFCBK-S","sent_at_ms":1792573200000,"origin":"real-masked",
 "text":"Sent Rs.1240.50 From HDFC Bank A/C *4417 To UBER INDIA On 05/10/26 Ref 429900112233 Not You? Call 18002586161",
 "expect":{"kind":"transaction","amount":"1240.50","direction":"debit","account_last4":"4417","payee":"UBER INDIA",
           "upi_ref":"429900112233","txn_date":"2026-10-05","rule":"hdfc.upi_debit.v1"},"notes":""}
```

- Seed it in Phase 0 from every SMS string in Rectifi's tests: `SMS_SAMPLES` in `test_money_and_parsing.py`, `BANK_SMS`, `NO_REF_SMS`, `OTP_SMS`, `PROMO_SMS` and `UNREADABLE_SMS` in `test_sms_gateway.py`, and the review/OTP cases in `test_phone_page.py`.
- **Corpus metrics, reported by both test suites:**
  - auto-add precision: must be 100%; a wrong amount or direction is a failed build,
  - review rate,
  - ignore precision: no real transaction may be ignored.

**Masking guide for corpus lines:**
- Keep wording, punctuation and spacing exact.
- Replace account and card digits with fake ones of the same shape (`XX1234` becomes `XX4417`).
- Replace refs with random digits of the same length.
- Replace person names with `RAVI`-style placeholders and VPA local parts with fakes. Keep merchant names.
- Amounts may change but must stay consistent within the message.

An in-app **"Report a wrong reading"** action on an SMS-sourced entry or inbox item:
- builds a masked corpus line (`core.privacy.Mask` + name and VPA replacement),
- shows it for review,
- then shares it as text.

This is the loop for adding banks over time.

**Android capture (`full` flavor):**
- **`SmsReceiver`:** `Telephony.Sms.Intents.getMessagesFromIntent` → join multipart bodies per originating address → `goAsync()` → ingest on `Dispatchers.IO` → `finish()`. If the database throws, enqueue a `OneTimeWorkRequest` with the body (WorkManager `Data` is 10 KB; Rectifi caps SMS at 4000 chars).
- **`InboxBackfill`:** queries `content://sms/inbox` for `date >= last_backfill_ms - 1 day` (columns `address, body, date, date_sent`). It runs on app open, every 6 h via `BackfillWorker`, and once at onboarding with a user-chosen window (30 / 90 / 180 days).
- **Dedupe across both paths:**
  - `sms_seen` digest of `(sender, sent_at_ms, text)`. Use `date_sent` from the provider and `SmsMessage.getTimestampMillis()` in the receiver. UNSURE whether OEM SMS apps always store the same value; verify on a real phone. If they differ, also treat the same `(header, text)` within ±120 s as a duplicate.
  - `uq_txn_upi_ref`, `uq_sms_inbox_hash`.
- **After ingest, a notification:** "Saved ₹250.00 · swiggy@icici · Food & Dining" with actions **Change category** and **Undo**; or "Needs a look: a bank SMS Rectifi couldn't read" with **Review**. POST_NOTIFICATIONS is requested on Android 13+.

**Permission flow:**
1. **Onboarding, step "Capture".** A prominent-disclosure screen: what is read (bank SMS), what is kept (transaction and needs-review SMS only), that OTPs, offers and chats are ignored and not stored, and that nothing leaves the phone. Buttons: **Allow SMS access** / **Not now**.
2. `RequestMultiplePermissions(RECEIVE_SMS, READ_SMS)`.
3. **On denial, always show the sideload help.** Android 15 treats SMS as a restricted setting for sideloaded apps. The path is App info → ⋮ → **Allow restricted settings** → Permissions → SMS → Allow; on Xiaomi, "Install unknown apps" may need enabling first. Add a button opening `ACTION_APPLICATION_DETAILS_SETTINGS`. Denial cannot be reliably told apart from "restricted", so show the help after any denial.
4. **Backfill offer:** "Read bank SMS from the last 90 days?" Show the result slip: "Added 143 · Needs review 9 · Ignored 1,204 (OTPs, offers, chats)".
5. **Capture screen:** permission state, "Last bank SMS captured 2 h ago", recognised banks, the Needs review list, a battery-optimisation note (OEM killers; link to dontkillmyapp.com), and a backfill button.

**Play policy (plan the declaration, don't depend on it):**
- "SMS-based money management" ("apps that track and manage budget") is a listed temporary exception for `READ_SMS, RECEIVE_MMS, RECEIVE_SMS, RECEIVE_WAP_PUSH`.
- It needs the Permissions Declaration Form in Play Console, core functionality that depends on SMS, and "no alternative method". Budgeting apps "may not exfiltrate or share non-financial or personal SMS history". It is subject to review.
- Keep the disclosure screen, the store-nothing-non-financial design and a privacy policy page ready.
- Play Console costs (UNSURE: $25 one-time; new personal accounts reportedly need a 12-tester, 14-day closed test). Plan nothing on Play until the sideloaded app is stable.

### A6. AI harness

**Principle: the model is optional and small, and the harness does the work.** Inputs flow like this:

```
user text
  -> FastPathParser (deterministic)        hit  -> Command (EXACT) -> write at once + receipt with Undo
                                            miss -> (model installed & RAM ok?) no -> GuidedFallback (chips + prefilled Add form via BestEffort)
  -> ModelRouter: LocalModel.complete(tools=CLOSED SET, constrained tool call, temp 0, maxTokens 64, 6 s deadline)
  -> SlotValidator + AmountGuard  invalid -> Repair (1 retry with the error stated) -> still invalid -> GuidedFallback
  -> Command (MODEL) -> ConfirmCard ("Save ₹250 · Swiggy · Food & Dining · today?" [Save] [Edit]) -> Executor
  -> Repositories (:data) -> core result -> AnswerTemplates.render(result) -> Answer (every ₹ from the result)
```

**Interfaces (`core/src/main/kotlin/app/rectifi/core/harness/`):**

```kotlin
sealed interface Command {
  data class LogTxn(val amount: Money, val direction: Direction, val payee: String?, val category: String?,
                    val date: LocalDate, val note: String?) : Command
  data class SetBudget(val category: String, val limit: Money, val month: YearMonth) : Command
  data class AskSpending(val period: PeriodRef, val category: String?) : Command
  data class AskBudget(val category: String?) : Command
  data class Find(val search: String, val period: PeriodRef?) : Command
  data object WeeklyReview : Command; data object Suggestions : Command; data object UndoLast : Command
}
enum class Origin { FAST_PATH, MODEL }
data class Routed(val command: Command, val origin: Origin, val trace: List<TraceStep>)
sealed interface RouteResult { data class Ok(val routed: Routed) : RouteResult
  data class NeedSlot(val partial: Command, val missing: Slot, val question: String) : RouteResult
  data class Fallback(val prefill: Prefill, val reason: String) : RouteResult }

interface FastPathParser { fun parse(text: String, ctx: ParseContext): FastPathResult }   // Matched | NoMatch
interface LocalModel : AutoCloseable { val id: String; val ramClassMb: Int
  suspend fun load(); suspend fun complete(req: ModelRequest): ModelResponse }
data class ToolSpec(val name: String, val description: String, val params: List<ParamSpec>)   // ParamSpec has enum values
data class ModelRequest(val system: String, val user: String, val tools: List<ToolSpec>, val maxTokens: Int, val temperature: Float)
data class ModelResponse(val toolName: String?, val args: Map<String, String>, val raw: String, val timings: Timings)
class SlotValidator(categories: List<Category>, synonyms: Map<String,String>, clock: Clock)   // -> Valid(Command) | Invalid(reasons)
class AmountGuard(lookback: Int = 3) { fun allowed(userTexts: List<String>): Set<Money>
  fun check(amount: Money, userTexts: List<String>): GuardResult }                            // port of graph._guard
class IntentRouter(fast: FastPathParser, model: LocalModel?, validator: SlotValidator, guard: AmountGuard, clock: Clock)
object AnswerTemplates { fun render(r: ToolResult): Answer }   // Answer(text, numbers: Set<Money>) — test: every ₹ in text ∈ numbers
```

**Fast-path grammar** (target: ≥80% of everyday inputs, **100% precision**):
- **Logging:** `<amount> <payee words ≤4>`, `<payee> <amount>`, `spent|paid <amount> on|at|to <payee> [today|yesterday|on <date>]`, `+<amount> <word>` or `got|received <amount> from <payee>`.
- **Budgets:** `<category> budget <amount>`, `budget <category> <amount>`, `set <category> budget to <amount>`.
- **Questions:** `how much [did i spend] [on <category>] (this|last) (week|month)`, `<category> this month`, `review`, `tips|suggestions`.
- **Undo:** `undo` (last chat or manual entry, ≤10 min old).
- **Amounts:** the `amounts_in_text` port (`2.5k`, `1 lakh`, `₹1,250`).
- **Categories:** `_category_id` semantics (exact, then a unique prefix) + `category_synonyms.json`.
- It **refuses** (defers to the model) when there are two amounts, leftover verbs, or a negation ("didn't").

**Model tool set (closed, 9 tools):**
- `log_expense(amount_text, payee?, category? enum, when enum[today,yesterday,date_in_text])`
- `log_income(...)`
- `set_budget(category enum, amount_text, month enum[this_month,next_month])`
- `ask_spending(period enum, category? enum)`
- `ask_budget(category? enum)`
- `find_payments(search, period? enum)`
- `weekly_review()`
- `suggestions()`
- `not_supported(reason)`

The model **never** outputs a number we use: `amount_text` is parsed by `MoneyParse` and must be in `AmountGuard.allowed(last 3 user messages)`. Dates come from enums. Categories must validate.

**Safety and robustness:**
- Model-path writes always need a tap.
- Answers are templates, so a model-written ₹ figure can never reach the screen.
- Keep a port of `unverified_amounts` as a test oracle anyway.
- Repair: one retry that appends "Your answer was invalid: <reasons>. Reply with exactly one tool call." Then fall back.
- Missing amount → `NeedSlot` ("How much?"); the next message's amount is merged deterministically.

**Runtime decision:**
- **Primary: LiteRT-LM.**
  - Google Maven AAR, Kotlin API `Engine(EngineConfig(modelPath, backend))` → `initialize()` (up to about 10 s, off the main thread) → `createConversation(ConversationConfig(tools = …, automaticToolCalling = false))` → `sendMessage`.
  - Tools via `OpenApiTool` JSON descriptions (enums go in the JSON).
  - Google says tool calling uses constrained decoding. UNSURE: whether that holds for non-Gemma models and whether enums are enforced. The validator does not rely on it.
  - GPU needs `<uses-native-library android:name="libOpenCL.so" android:required="false"/>`.
- **Secondary, only if the eval shows LiteRT-LM output is unreliable: llama.cpp** (MIT) built from source with NDK + CMake behind the same `LocalModel`. Its GBNF / JSON-schema grammar gives a hard output constraint. Documented limits: the schema is not injected into the prompt; nested `$ref`, `prefixItems` and others are broken. Cost: a JNI layer and native build time on the laptop.
- **Optional third: ML Kit GenAI Prompt API (Gemini Nano).** Beta; structured output is alpha; flagship-only device list (UNSURE). Never the default.

**Candidate models (download in-app on demand; never bundled in the APK):**

| Candidate | File (format) | Size | RAM (published) | Licence | Notes |
|---|---|---|---|---|---|
| None (fast path + guided fallback) | – | 0 | 0 | – | Always the baseline row; must be fully usable |
| Qwen3-0.6B no-think int4 | `qwen3_0.6b_nothink_q4_block32_ekv1280.litertlm` | ~332 MB | not published (UNSURE) | Apache-2.0 | Ungated; no-think variant about 2x faster on CPU |
| FunctionGemma 270M | litert-community builds, int8 | 288 MB | ~550 MB peak RSS (S25 Ultra) | Gemma Terms | Google says fine-tune first: Mobile Actions 58% base → 85% fine-tuned; BFCL Simple 61.6 |
| Gemma3-1B-IT int4 QAT | `gemma3-1b-it-int4.litertlm` | 529 MB | ~1,009 MB CPU RSS (S24 Ultra) | Gemma Terms | HF repo is gated (licence click needed), so self-host |
| Qwen2.5-0.5B-Instruct | `.task` q8 only on litert-community (no `.litertlm` listed) | 547 MB | UNSURE | Apache-2.0 | Needs the MediaPipe runtime or your own conversion: deprioritise |
| Gemma 4 E2B | `.litertlm` | 2.58 GB | 1,733 MB CPU / 676 MB GPU (S26 Ultra) | Apache-2.0 | "Ceiling" reference only; too big for 4 GB phones |

**Hosting and gating:**
- Host the chosen file as a **GitHub Release asset with a pinned SHA-256** in the app, plus the LICENSE/NOTICE. This avoids HF gating and URL drift. UNSURE: the per-asset size limit (believed 2 GB).
- Download only on user action, Wi-Fi by default, as a foreground worker with progress. Store in `noBackupFilesDir`, with a Delete button.
- RAM gate: enable model features only if `!isLowRamDevice` and total RAM ≥ the model's `ramClassMb × 3`. Load lazily on the first model-needed input. Unload after 2 min idle or on `onTrimMemory(RUNNING_LOW)`.
- **Honest expectation:** published speeds are flagship numbers. On a 4 GB mid-range phone, a 0.6–1B model may take several seconds per request (UNSURE until measured). That is why the fast path must carry most inputs.

**Eval design (`shared/fixtures/ask/eval.jsonl` + `android/eval` + an in-app "Model lab" in debug builds):**
- **Item:** `{"id","text","tags":["en","hinglish","typo","multi-amount","no-amount","negation"],"split":"dev|test","expect":{"route":"fast|model|fallback","intent":"log_expense","slots":{"amount":"250.00","payee":"swiggy","category":"Food & Dining","date":"today"},"must_not_write":false}}`. About 300 items: ~150 fast-path and ~150 model-needed, including adversarial ones ("spent around 300", "split 900 three ways", "2 coffees 120 each"). The test split is frozen.
- **Metrics per candidate:**
  - fast-path coverage, and fast-path precision (must be 100%),
  - intent accuracy and per-slot exact match,
  - end-to-end exact (intent and all slots) on model-needed items,
  - invalid-output rate before and after repair,
  - guard catches (model proposed an amount not in the text). Count it; a write is impossible by construction, and a unit test proves the `Executor` only accepts guarded commands,
  - p50/p95 latency (load, prefill, total), peak RSS, file size.
- **Where it runs:**
  - Accuracy on the laptop with `litertlm-jvm` (the same `.litertlm` weights, CPU): `./gradlew :eval:run --args="--model <path> --split test --out report.json"`.
  - Latency and RSS on each family phone through the Model lab screen, which runs the same corpus and writes `report.json` to Downloads.
  - `scripts/eval_report.py` merges reports into a markdown table in PROGRESS.md.
- **Ship rule:** a model ships only if end-to-end ≥ 90% on test, p95 ≤ 3 s on Prag's phone, and it beats "None" on usefulness (fallback rate). Otherwise ship no model; the app is still complete.
- **Optional later:** fine-tune FunctionGemma on synthetic Rectifi tool calls (template-generated; train only on the dev split's distribution). It is only worth it if it beats Qwen3-0.6B in the eval.

### A7. Backup: free, three tiers

1. **Export file (no sign-in, both flavors, Phase 5).**
   - `.rfbk` = zip(`manifest.json` {format "rectifi-backup-v1", schema_version, app_version, created_at, row counts, sha256} + `data.json` with all tables, money as integer paise).
   - Encrypted: `"RFBK1"` + salt(16) + iv(12) + AES-256-GCM(ciphertext). Key = PBKDF2WithHmacSHA256(passphrase, salt, 600,000 iterations; measure on a low-end phone and record the number in the manifest).
   - Saved through Storage Access Framework (`ACTION_CREATE_DOCUMENT`) anywhere, **including Google Drive via the Drive app's document provider: no OAuth, no Cloud project**.
   - Restore validates the manifest, runs migrations, and replaces the data in one transaction.
2. **Automatic: Android Auto Backup + Block Store (no in-app sign-in).**
   - Facts: up to 25 MB per app, stored in the user's Google account, does **not** count toward Drive quota, only the latest backup is kept, runs about once a day (idle, Wi-Fi, 24 h since the last one), end-to-end encrypted on Android 9+ when a screen lock is set, restored when the app is installed (including `adb install`).
   - **The SQLCipher database must be excluded.** Its Keystore wrapping key is hardware-bound and won't come back; the Android docs say to use Block Store for credentials. So:
     - `data_extraction_rules.xml` and `full_backup_content.xml` exclude `database/` and include only `file/backup/auto.rfbk`;
     - a daily `AutoExportWorker` writes `auto.rfbk` encrypted with a random key;
     - that key goes in **Block Store** (16 entries × 4 KB) with `setShouldBackupToCloud(true)`, only if `isEndToEndEncryptionAvailable()`. Target devices: Pixel on Android 9+, others Android 12+.
   - On first launch with an empty database and `auto.rfbk` present, ask "Restore your data from 24 Oct?"
   - UNSURE: whether a sideloaded app's backup is restored on a new device when the APK is installed later. Expected to work (restore happens "whenever the app is installed"), but verify.
3. **Opt-in "Back up to my Google Drive" (Phase 5b).**
   - Drive `appDataFolder` with scope `https://www.googleapis.com/auth/drive.appdata`, which is **non-sensitive** (basic verification only).
   - Auth: `Identity.getAuthorizationClient(ctx).authorize(AuthorizationRequest(scopes))` → `accessToken`, valid about 1 h. Credential Manager sign-in is optional; it only picks the default account.
   - REST with plain HTTPS:
     - `POST https://www.googleapis.com/upload/drive/v3/files?uploadType=multipart` with `{"name":"rectifi-<date>.rfbk","parents":["appDataFolder"]}`,
     - `GET /drive/v3/files?spaces=appDataFolder&fields=files(id,name,modifiedTime,size)`,
     - `GET /files/{id}?alt=media`,
     - keep the last 5.
   - Counts against the user's 15 GB Drive quota. Free.
   - Needs a Google Cloud project, an Android OAuth client (package + release SHA-1) and a consent screen. In "Testing" status, only listed test users (≤100) can authorise, which suits family.
   - UNSURE: whether Testing-status grants expire weekly on Android, and whether `authorize()` returns a token silently from a WorkManager context without an Activity. If it needs UI, post a "Tap to back up" notification.
   - Requires Google Play services. The `lite` flavor works without it; tier 1 does too.

---

## B. Design: "The Passbook" on Android

This applies the master design standard: a brief, a token system, designed states, restrained motion, accessibility, and a self-critique.

**Brief.**
- **Product:** a pocket passbook that proves itself.
- **Loop:** capture (automatic or in 10 s) → "am I on track?" in 3 s → trust (BALANCED slip) → one weekly change.
- **Philosophy:** proof is the hero; every mark earns its place. Hierarchy comes from type, rules and whitespace. No card grid, no tonal elevation, no FAB, no pill chips.

**Material 3 policy.** Use `MaterialTheme` for ripple, text selection, text-field internals and sheets. Set `tonalElevation = 0` everywhere. Build all signature components yourself. **Do not** use M3 `NavigationBar` (pill indicator), `Card`, `FilterChip`, `ExtendedFloatingActionButton` or dynamic colour. The brand palette is fixed; Material You would erase the identity.

**Tokens (`app/ui/theme/Tokens.kt`):**
- `RectifiColors(light, dark)` uses **exactly** the hex values and contrast-tested pairs from WEB_PLAN B2: bg #F6F2E9 / #17150F, raised, sunk, text, muted, subtle, rule, rule-strong (#8C826F / #7A7260), accent #1F4E5F / #7FBACB, on-accent, accent-tint, ok/near/over + bg, overlay. The dark rule-strong value is the raised one from PROGRESS (#7a7260).
- **Spacing:** 4, 8, 12, 16, 24, 32, 48, 64 dp.
- **Radius:** 2 dp (stamps), 4 dp (inputs, buttons, slips), 10 dp (sheet top corners and dialogs).
- **Borders:** 1 dp rule, 1 dp rule-strong for controls, 1 dp ink for the total rule, and a double rule (two 1 dp lines 2 dp apart) under final totals.
- **Touch targets: 48 dp minimum** (the Android guideline, up from the web's 44).

**Type (`Type.kt`, bundled fonts):**
- **Fraunces variable:** `Font(R.font.fraunces, variationSettings = FontVariation.Settings(FontVariation.weight(600), FontVariation.Setting("opsz", 72f)))` for hero and display, weight 500 for section titles.
- **IBM Plex Sans:** 400/500/600 for body.
- **IBM Plex Mono:** 400/500 for figures, with `fontFeatureSettings = "tnum, lnum"`.
- **Scale (sp, size/line height):** xs 12/16 (eyebrow: Plex 600, uppercase, letterSpacing 0.12 em), sm 14/20, md 16/24, lg 20/28 (Fraunces 500), xl 25/32, 2xl 32/38, hero 56/56 on phone (Fraunces 600, `tnum lnum`). Mono: 15/22 ledger, 22/28 hero cells, 14/20 slips.
- Text fields are never under 16 sp.
- Subset the TTFs with fonttools (pip works here) to Latin + ₹ (U+20B9) + − (U+2212). **Check cmap coverage of ₹ in each font.** UNSURE that IBM Plex has it; fall back to the system font for that glyph if not.

**Navigation:**
- **Custom bottom bar:** 64 dp + `WindowInsets.navigationBars`, five slots: **Home, Ledger, Add** (centre: a 48 dp accent square with "+" and the word "Add"), **Ask, More**.
  - Active slot: accent-tint background rectangle + 3 dp accent top bar + `selected` semantics.
  - The bar hides while the IME is visible (`WindowInsets.isImeVisible`) so Save isn't stolen. That fixes the web bug from PROGRESS, item 1.
- **More** opens a sheet: Review, Insights, Budgets, Accounts, Capture, Backup, Settings (theme System/Light/Dark, app lock, AI model, licences).
- **Top bar:** 56 dp, Fraunces 20 title + the page control (month or week stepper), bottom hairline, no shadow.
- Navigation Compose. Predictive back for sheets (`PredictiveBackHandler`). Edge-to-edge (enforced when targeting API 35+).
- Static app shortcut "Add entry". Adaptive icon: Fraunces "R" over a double rule, plus a monochrome layer for themed icons.

**Signature components (`app/ui/components/`):**
- `Stamp`: 11.5 sp uppercase, 0.08 em tracking, 1 dp border in the state colour, tint background, 2 dp radius. Always a word: BALANCED, GAP, ACT, WATCH, NOTE, OVER, NEAR, ON TRACK, ALL CLEAR, IMPORTED, NEEDS REVIEW.
- `Money`: ₹ / integer / `.paise` at 0.85 em muted in ledgers. A credit shows "+" plus an "IN" label, never colour alone. U+2212 for negatives.
- `Slip`: raised surface, 1 dp rule, 4 dp radius, mono 14. Variants: `ReconSlip` (opening, + credits, − debits (n entries), ink rule, = expected, bank's closing, double rule, verdict stamp), `ImportSlip`, `CompareSlip`, `ParsedSmsSlip`.
- `LedgerRow`: two lines, payee 600 with the amount right-aligned, then "category · account or Not linked". Under sticky `DayHeader`s ("WED 21 OCT").
- `BudgetBar`: 10 dp track, pace tick 2 dp ink at 0.55 alpha.
- `CategoryBars`.
- `PaceChart`: Canvas; solid 2 dp accent "You" and dashed muted "Budget pace" lines; labels at the line ends; touch crosshair; "Show as table" disclosure.
- `AmountInput`: "₹" prefix, mono 32 sp, `KeyboardType.Decimal`.
- `SegmentedControl`: radio semantics, words "Money out / Money in".
- `MonthStepper` / `WeekStepper`.
- `Callout` (note / near / over, icon + words), `EmptyState`, `Receipt` (with a 10 s Undo), `Disclosure`, `CodeValue` (copy).

**Screens (phone order follows WEB_PLAN B5; Android differences listed):**
- **Lock:** paper background, wordmark, "Unlock Rectifi" opens BiometricPrompt. Locks 60 s after backgrounding (choice: immediately / 1 min / 5 min). `FLAG_SECURE` on.
- **Onboarding (3 steps):**
  1. What Rectifi does, honestly.
  2. Add your bank account: name + last 4 digits + today's balance (this seeds reconciliation).
  3. Capture: the disclosure screen, permission, restricted-settings help, backfill.
  Plus "Restore from backup" as a link on step 1.
- **Home (Overview):**
  - Hero: SAVINGS RATE numeral + three passbook cells (Money in / Money out / Saved).
  - Capture-health line ("Capturing from HDFC, SBI · last SMS 2 h ago").
  - Worth a look (≤3 insights) → Budgets → Proof of completeness (ReconSlip per account; empty: "Not proven complete yet" + Record a balance / Import a statement) → Latest entries (8) → Where it went → Pace chart.
  - Empty month: "Nothing recorded for this month yet."
- **Ledger:** "Filters (n)" button opens a sheet; sticky day headers. A tap opens the Entry sheet (category and account selects, note, Delete with a confirm dialog). Search with 250 ms debounce. Count line + "teaches Rectifi that payee". A statement row's account is locked with the explanation.
- **Add:**
  - 3-segment control: Quick · Paste SMS · Import.
  - **Quick:** amount, direction, date (Today / Yesterday / picker with max today), payee, category ("Auto (from payee)"), account (default preselected, "No account" last with a hint), note, full-width Save. Receipt line + Undo. Duplicate gives the info line.
  - **Paste SMS / shared SMS:** text field, live ParsedSmsSlip (300 ms debounce), Save.
  - **Import:** SAF picker (csv/xlsx/pdf). PDF password field (cleared from state right after the call). Result slip with Undo, or the preview mode (column selects in a 2-column grid, totals strip, first 12 rows, Import N rows).
- **Ask:**
  - A transcript with no bubbles: "YOU" / "RECTIFI" eyebrows; user text on sunk with a 3 dp left rule.
  - Confirm card for model writes.
  - "What Rectifi did" disclosure (fast path / model id / tool / validator notes).
  - A composer above the IME.
  - Chips when empty ("250 swiggy", "food budget 3000", "how much this week?", "review", "tips").
  - Header line: "Quick mode · works offline" or "Model: Qwen3-0.6B · amounts must be in your words".
- **Review:** week stepper, hero SPENT THIS WEEK, CompareSlip, Needs attention checklist (ALL CLEAR only when `all_clear`), Where it went, Biggest payments, Worth acting on, One commitment (280 chars, count shown after 240). Optional Sunday 7 pm reminder notification.
- **Insights:** groups FIX THESE FIRST / ACT NOW / KEEP AN EYE ON / GOOD TO KNOW. Apply buttons ("Set a ₹3,000 budget"). The "<28 days" note. Empty states as on the web. No "Explain this" in v1.
- **Budgets, Accounts:** as WEB_PLAN B5, with an inline limit editor and the record-balance form. The stamp animation plays once when a slip turns BALANCED after the user's own action.
- **Capture:** as A5, plus the Needs review list (Add pre-fills Add, Dismiss) and "Report a wrong reading".
- **Backup:** last export, auto-backup status ("Encrypted backup in your Google account: last 24 Oct" / "Off: turn on backup in Android settings"), Export file, Restore, opt-in Drive.

**States per screen:** loading skeleton only after 200 ms; empty; partial/warning; success; error inline at the action. "Database can't open" gets a full-screen recovery page: retry, restore from backup, export a diagnostic with no data.

**Motion:**
- **Tokens:** fast 120 ms, base 180, slow 240, exit 140; ease-out `CubicBezierEasing(.2f,.8f,.2f,1f)`, ease-in `(.4f,0f,1f,1f)`.
- **Used only for:** press scale 0.98; sheet slide (slow) with backdrop fade; dialog fade + scale 0.98→1; toast; the saved-row tint fade (600 ms); budget bar width after an edit only; the BALANCED stamp once (scale 1.06→1).
- Haptic confirm on save and stamp (`HapticFeedbackConstants.CONFIRM` on API 30+).
- No page transitions, no animated counters.
- **Reduced motion:** if `Settings.Global.ANIMATOR_DURATION_SCALE == 0` ("Remove animations"), skip the transforms and the stamp. The state is still carried by words.

**Accessibility:**
- TalkBack: `Money` merges descendants and speaks "Money out, 250 rupees". Stamps are announced in their row. BudgetBar description: "Food: 78.0% of budget used, 68.0% of month gone". The chart's table alternative.
- Font scale 200%: hero numerals wrap to their own line and passbook cells stack. Test at 1.0, 1.3 and 2.0.
- 48 dp targets. Colour never alone. Visible focus for keyboard and D-pad.
- **Dark mode:** follows the system with a manual override (DataStore). Status and navigation bar icons switch. Check the dark contrast pairs that WEB_PLAN computed.

**Self-critique gate (Phase 7):** screenshots of every screen × {light, dark} × {font 1.0, 2.0} × {empty, full} via `adb exec-out screencap`. Run the screenshot test and a 5-minute critique, fix at least 5 issues, and log them in PROGRESS like the web's 19.

---

## C. Phased build plan

Sizes are in sessions of about 3–4 h with a Sonnet engineer. "C" means it can be done in the container, "L/CI" means laptop or CI.

**Phase 0: shared contract + `:core` bootstrap (1 session, all C)**
- Files:
  - `shared/` tree; `scripts/export_shared_fixtures.py` (`--write` / `--check`; patches `rectifi.tools.today`; dumps pdfplumber words for `tests/fixtures/*.pdf`; seeds the corpus from existing test SMS);
  - `tests/test_shared_fixtures.py`; `rectifi/sms_registry.py` (no bank rules yet);
  - `android/core/` with `money/*`, `privacy/Mask.kt`, `sms/*` generic port + registry loader + `SmsDigest`;
  - `android/core/scripts/test-in-container.sh`: kotlinc from `/opt/gradle-8.14.3/lib` with `-no-stdlib -no-reflect -jvm-target 17`, classpath of stdlib + serialization-core/json 1.6.2 + junit 4.13.2 + hamcrest 1.3, then `org.junit.runner.JUnitCore` over the found test classes;
  - CLAUDE.md rule 14 (shared fixtures are the contract), PROGRESS.md Android section.
- Acceptance:
  1. The script compiles and runs `:core` tests here (first proof the embedded kotlinc works).
  2. All 67 cases in `web/src/lib/money.cases.json` pass in Kotlin (pytest and vitest still pass).
  3. `parse_amount`, `amounts_in_text` and `mask` fixtures pass in both languages.
  4. Every seeded corpus SMS gives the same verdict, amount, direction, last4, ref, payee and date in Python and Kotlin, and the same `_sms_digest` hex.
  5. The full Python suite still reports 822 passed, 1 skipped (start Postgres: `pg_ctlcluster 16 main start`).
  6. The no-model-imports source scan passes.

**Phase 1: smallest end-to-end slice. A real bank SMS lands on Overview (3 sessions: 1 C writing, 2 L/CI)**
- Laptop setup (Prag): Android Studio stable, the "Empty Activity (Compose)" wizard into `android/` (use its AGP / Kotlin / Compose BOM and pin them in `libs.versions.toml`), include `:core`.
- Files:
  - `:data`: `Schema.sq` (tables above), `Transactions.sq`, `Accounts.sq`, `Sms.sq`, `Seed.kt`, `DbFactory.kt` + `DbKeyManager.kt` (SQLCipher, `PRAGMA foreign_keys=ON`, dialect override), `LedgerRepository.addTransaction`, `SmsRepository.ingest` (port of `ingest_sms`: `sms_seen` + insert in one transaction, `_sms_account`, a future date goes to the inbox), `OverviewRepository` (month income / spend / saved + latest 8).
  - `:app`: `RectifiApp`, `MainActivity`, `ui/theme/*` (tokens and fonts; system fonts allowed until Prag supplies the TTFs), `Money`, `Stamp`, `LedgerRow`, `OverviewScreen` (hero + latest entries), `CapturePermissionScreen`, `SmsReceiver`, `InboxBackfill` + `BackfillWorker`, `CaptureNotifier`, debug `SimulateSmsScreen`, `ShareTargetActivity`.
  - Flavors `full` / `lite`.
  - `.github/workflows/android.yml` (`:core:test`, `:data:testDebugUnitTest`, `:app:assembleFullDebug`, `assembleLiteDebug`) and a `python-fixtures` job with a Postgres service.
- Acceptance:
  1. CI or the laptop builds both flavors.
  2. On Prag's phone (`adb install`), onboarding gets SMS permission; the restricted-settings help shows if it is denied.
  3. Make a real ₹1 UPI payment: within 5 s a notification "Saved ₹1.00 · …"; Overview's Latest entries and Money out update live.
  4. Force-stop the app, make a second payment, reopen: backfill adds it exactly once; backfill again adds 0.
  5. An OTP SMS stores nothing.
  6. The Simulate screen with `UNREADABLE_SMS` puts it in the inbox (count 1); sending it twice still gives count 1.
  7. `adb exec-out run-as <pkg> cat databases/rectifi.db | head -c 16` is **not** "SQLite format 3".
  8. The `lite` APK's merged manifest has no SMS permission (`aapt dump permissions`).

**Phase 2: daily capture and the ledger (3 sessions: about 60% C)**
- Quick entry with Receipt + Undo, Paste SMS with a live slip, the share target filling Paste, Ledger (filters, day headers, Entry sheet, recategorise with "remember", set account, delete), Accounts (add, record balance, ReconSlip), `default_account` / `only_account_id`, category-kind check, merge semantics.
- Bundle the real fonts (Prag supplies TTFs; subset in C).
- Acceptance:
  1. Scenario goldens for `spending_summary`, `reconcile` and `budget_status` match Python for 6 scenarios: empty, one account, two accounts, gap, income-only month, transfers.
  2. The DB tests are instrumented, on the laptop or a CI emulator: a duplicate UPI ref returns `Duplicate`; a debit under an income category gives an error; a statement-row account change is refused.
  3. The BALANCED stamp appears after recording two balances that add up.

**Phase 3: budgets, weekly review, insights (3 sessions: about 70% C)**
- `BudgetStatus`, `SpendingPace`, `WeeklyReview` + commitment, `InsightsEngine` (8 rules, tunables verbatim), and the screens.
- Acceptance:
  1. At least 12 insight scenarios (one or more per rule, plus "needs a month" and "nothing to flag"): Kotlin output equals Python's for kind, severity, key, title, detail, `numbers`, apply.
  2. A Kotlin test proves every ₹ in title or detail is in `numbers`.
  3. Weekly review `all_clear` parity (no account → never all clear).
  4. Apply "Set a ₹3,000 budget" creates the budget.

**Phase 4: statement import (4 sessions: about 60% C)**
- `CsvReader`, `XlsxReader` (add `tests/fixtures/statement.xlsx` via openpyxl, dev-only), `HeaderFinder`, `ColumnDetect`, `Normalise`, `AssessStatement`, `HeaderAnchoredTable`, the text-line parser, `decide_directions`, `ImportRepository` (batch, merge ±2 days, snapshots, undo), `PdfBoxReader` (password through `PDDocument.load(stream, password)`).
- Acceptance:
  1. Kotlin rows and assess results equal Python's goldens for `hdfc_style_statement.csv`, the new xlsx, and the pdfplumber word dumps of `statement_ruled.pdf` and `statement_text_only.pdf`.
  2. On a device or emulator, PdfBox gives the **same final rows** for those PDFs.
  3. `statement_encrypted.pdf` (AES-128) asks for a password; a wrong password gives the wrong-password message; the right one imports.
  4. `statement_image_only.pdf` gives the scan message.
  5. Importing twice adds 0. Undo removes exactly the added rows and restores snapshots.
  6. A review row never imports.
  7. UNSURE item to test: an AES-256 PDF made with pypdf. PDFBox 2.x is believed to need BouncyCastle only for certificate (public-key) encryption, not passwords.

**Phase 5: lock, backups (2–3 sessions: about 40% C)**
- `AppLock`, `FLAG_SECURE`, `BackupFormat` (core: zip, manifest, AES-GCM, PBKDF2; tested here), export and restore through SAF, `AutoExportWorker` + Block Store key + backup rules XML. Phase 5b (optional): Drive `appDataFolder`.
- Acceptance:
  1. A round trip export → wipe app data → restore gives identical row counts and an identical reconciliation.
  2. A wrong passphrase gives a friendly error and no partial restore.
  3. `adb shell bmgr backupnow <pkg>` → uninstall → reinstall: the restore prompt appears and the data comes back. Uses the documented Auto Backup test flow; Block Store behaviour on Prag's device is UNSURE.
  4. Lock after 60 s works. Recents shows a blank thumbnail.

**Phase 6: Ask harness, then the model (4 sessions: harness C, runtime L/CI)**
- 6a, no model:
  - `FastPathParser`, `SlotValidator`, `AmountGuard`, `IntentRouter`, `Executor`, `AnswerTemplates`, `GuidedFallback`, the Ask screen;
  - `eval.jsonl` (~300 items); `:eval` CLI.
  - Acceptance: fast-path precision 100% and coverage ≥80% on the fast-tagged items; the guard refuses every planted invented amount; the `Executor` rejects unguarded commands (test); every template answer's ₹ figures are in `Answer.numbers`.
- 6b, model:
  - `LiteRtLmModel`, `ModelDownloader` (SHA-256 check), RAM gate, Model lab (debug).
  - Run the eval for None, Qwen3-0.6B no-think, FunctionGemma base and Gemma3-1B; put the table in PROGRESS.
  - Acceptance: the ship rule in A6, or ship no model. Turning on airplane mode with the model deleted leaves Ask working in quick mode.

**Phase 7: polish and release (2–3 sessions: L/CI)**
- The design critique gate (fix at least 5), TalkBack pass, font scale 200%, dark mode, a baseline profile (optional), release signing.
- `release.yml` on tag: keystore from secrets → `assembleFullRelease` + `assembleLiteRelease` → GitHub Release with SHA-256 sums and install notes (adb path, restricted-settings steps, Play Protect note).
- Acceptance: clean install on 2 family phones (one ≤4 GB RAM). A 1-week dogfood with every bank SMS either captured or in review (the reconciliation gap explains the rest).

**Phase 8 (optional):** Play readiness (privacy policy, Data safety form, SMS declaration), FunctionGemma fine-tune, desktop interop (`scripts/import_android_export.py` reads `.rfbk`).

**Total:** about 23–26 sessions, roughly 6–10 weeks part-time. Phases 0, 3, 6a and much of 4 are container-heavy. Phases 1 and 7 need the laptop or CI.

---

## D. What Prag must provide, risks, licences, UNSURE

**Prag must provide:**
1. **Masked SMS: 30–50 per bank** from his and his family's banks.
   - Tell me which banks. Likely candidates for a Chennai family: SBI, HDFC, ICICI, Axis, Indian Bank, IOB, Canara, KVB, City Union, TMB, and any credit cards.
   - Per bank, cover: UPI debit and credit, card POS and online, ATM, NEFT/IMPS/RTGS in and out, salary, NACH/autopay, refund or reversal, **failed or declined**, OTP, promo, balance-only alert, card bill due, card payment received, mandate created.
   - Keep the exact sender ids (e.g. `AD-HDFCBK-S`) and the received time. Mask per A5.
2. **Device list:** model, Android version and RAM for each phone, for model gating and the test matrix.
3. **Laptop:** Android Studio, phone USB debugging, and about 15–20 GB free disk (UNSURE).
4. **GitHub:** a private repo, attached to sessions with push access for CI.
5. **Release keystore:** made once and backed up in two places. Losing it means no updates and no flavor switching.
6. **Font TTFs:** Fraunces variable and IBM Plex Sans/Mono, downloaded on the laptop (blocked here).
7. **Real statements:** kept off-repo. Report failures as structure-only fixtures (real header + 3 fake rows).
8. **Optional:** a Google Cloud project for the Drive tier, and a decision to self-host the model file as a release asset.

**Risks (blunt):**
- **Distribution is the biggest risk, not code.**
  - Since a pilot announced for November 2024, Play Protect in India **blocks installing internet-sideloaded apps that request RECEIVE_SMS / READ_SMS**, affecting browsers, messaging apps and file managers. Install by `adb` is not mentioned (UNSURE).
  - Android 15 makes SMS a restricted setting for sideloaded apps.
  - Developer verification: Brazil, Indonesia, Singapore and Thailand from 30 Sep 2026; global on certified devices in 2027; India not named yet. A free student/hobbyist account allows up to 20 devices. Unregistered apps need ADB or an "advanced flow", reportedly with a 24 h wait.
  - Plan: `adb install` the `full` flavor for Prag and family (from his laptop); `lite` for anyone else; Play later.
- **On-device model value is unproven.** Small models (0.27–1B) are weak at slot filling without fine-tuning; Google itself says FunctionGemma should be fine-tuned. Latency on budget phones is unknown. The fast path + templates + guided fallback **is** the product; the model is a bonus decided by the eval.
- **PDF parity.** pdfplumber's ruled-table extraction has no Android twin. The header-anchored rebuild may fail on real statements (merged cells, Hindi, split headers). Mitigations: preview mode, review rows, the balance check, Undo. Also, PdfBox-Android's maintenance status and last release date are UNSURE (it is still on PDFBox 2.0.27).
- **OEM background killers** (MIUI, ColorOS, FuntouchOS) can delay the receiver or workers. The backfill on app open is the safety net; Capture shows "last captured".
- **Parity drift** between Python and Kotlin. Mitigated by `shared/` goldens, `--check` in CI, and the HALF_EVEN / two-digit-year / `\d` rules above.
- **SQLCipher + backup interplay.** Solved by excluding the database and backing up encrypted exports, but restore is the least-tested path. Make it a Phase 5 acceptance test.
- **Play policy:** the SMS exception is "temporary" and review-based. Never make the app depend on Play.
- **iPhone: out of scope.** iOS gives apps no access to the SMS inbox, and this is a native Android app. iPhone users keep the Python/web Rectifi (statement import on the laptop).

**Licences:**

| Component | Licence | Status |
|---|---|---|
| Kotlin, AndroidX, Compose, WorkManager, biometric | Apache-2.0 | |
| SQLDelight | Apache-2.0 | |
| SQLCipher Community (`net.zetetic:sqlcipher-android`) | BSD-style per Zetetic | UNSURE: the README doesn't state it; read the LICENSE file and ship attribution |
| PdfBox-Android | Apache-2.0 | Verified |
| LiteRT-LM | Apache-2.0 | Verified on the repo page |
| llama.cpp | MIT | Widely documented; not fetched this session |
| Qwen3-0.6B, Qwen2.5 0.5B/1.5B | Apache-2.0 | |
| Gemma 4 | Apache-2.0 | Verified |
| Gemma 3, FunctionGemma | Gemma Terms of Use | Allowed; pass the terms through if redistributing; HF repo gated |
| Fraunces, IBM Plex | OFL | Already in the repo |
| play-services-auth / blockstore | Google APIs terms, proprietary but free | Needs Play services; the `lite` flavor and tiers 0–1 don't |

- **Avoid:** PennyWise (AGPL; do not read its code, so the parsers stay clean-room), iText and MuPDF (AGPL).
- The SMS corpus is Prag's own masked data. Keep it in the private repo.

**UNSURE (verify, don't assume):**
- The embedded kotlinc actually compiling `:core` here (only `-version` was run).
- SQLDelight dialect override syntax and whether its grammar accepts partial indexes. Fallback: raw `execute` in a migration.
- Exact versions of AGP, Kotlin, Compose, LiteRT-LM and play-services. Pin after the first laptop sync.
- Whether LiteRT-LM's constrained decoding applies to Qwen and honours enums. Its minSdk.
- Peak RAM of Qwen3-0.6B on a 4 GB phone; real latency on family phones.
- `date_sent` vs `getTimestampMillis()` equality across SMS apps.
- Whether Play Protect's block covers `adb install`.
- Auto Backup restore for a sideloaded app on a new device; Block Store on the family's phones.
- Whether Drive Testing-mode grants expire, and silent `authorize()` from a background worker.
- Play Console fee and tester rule; GitHub Release asset size limit; Android SDK on GitHub `ubuntu-latest`.
- IBM Plex ₹ glyph coverage.
- APK size added by SQLCipher, PdfBox and LiteRT-LM (expect tens of MB; consider ABI splits: arm64-v8a only for family phones).
- PDFBox 2.0.27 opening AES-256 password PDFs without BouncyCastle.
- Android platform `PdfRenderer` text APIs (`PdfPageTextContent`, `LoadParams`) exist in newer Android, but their API level is unconfirmed, so they can't serve minSdk 26.

**Worth saving to memory (main session decides):**
- Rectifi Android decisions:
  - native Kotlin/Compose, local-first, single APK;
  - two flavors, full (SMS) and lite (share/paste);
  - SQLDelight + SQLCipher with Long paise;
  - a shared fixtures monorepo where Python is the oracle;
  - LiteRT-LM primary runtime, with the model optional and gated by an eval;
  - backups via encrypted export + Auto Backup/Block Store, Drive appdata optional.
- Build environment facts:
  - this container cannot build Android (Google Maven, Maven Central and Gradle are blocked);
  - `:core` can be tested here with kotlinc 2.0.21 from `/opt/gradle-8.14.3/lib`;
  - Android builds happen on the Windows laptop or in GitHub Actions.

### Critical Files for Implementation
- /home/claude/rectifi/rectifi/parsing.py (the SMS rules being ported; base of the registry's generic tier)
- /home/claude/rectifi/rectifi/tools.py (add_transaction, ingest_sms, _sms_account, reconcile, budget_status, weekly_review, undo_import: the tools-layer semantics to mirror)
- /home/claude/rectifi/rectifi/schema.sql (the data model and DB-level dedupe the SQLDelight schema mirrors)
- /home/claude/rectifi/rectifi/importer.py and /home/claude/rectifi/rectifi/pdf_statement.py (import pipeline, assess thresholds, fingerprints, decide_directions)
- /home/claude/rectifi/rectifi/insights.py and /home/claude/rectifi/rectifi/money.py (deterministic rules, tunables, rounding and formatting parity; fmt_inr cases at /home/claude/rectifi/web/src/lib/money.cases.json)
- /home/claude/rectifi/rectifi/agent/graph.py (`_guard`, `amounts_in_text` lookback and `unverified_amounts`: the amount-guard contract the harness reimplements)
- /home/claude/rectifi/docs/WEB_PLAN.md section B (tokens, contrast table, screens and states adapted above)

**Sources:**
- [9to5Google: Google blocking sideloaded apps in India](https://9to5google.com/2024/10/03/google-blocking-sideloaded-apps-india/)
- [Android Authority: sideloading changes timeline](https://www.androidauthority.com/android-sideloading-changes-timeline-3679204/)
- [Android Authority: Android 15 restricted settings](https://androidauthority.com/android-15-restricted-settings-sideloading-3481098)
- [textbee: Android 15+ SMS permission steps](https://textbee.dev/blog/android-15-send-sms-permission-guide)
- [Play Console: SMS/Call Log permissions exceptions](https://support.google.com/googleplay/android-developer/answer/10208820)
- [LiteRT-LM Android guide](https://developers.google.com/edge/litert-lm/android)
- [LiteRT-LM overview](https://developers.google.com/edge/litert-lm/overview)
- [LiteRT-LM GitHub](https://github.com/google-ai-edge/LiteRT-LM)
- [Gemma 4 on LiteRT-LM](https://developers.google.com/edge/litert-lm/models/gemma-4)
- [MediaPipe LLM Inference (maintenance-only notice)](https://developers.google.com/edge/mediapipe/solutions/genai/llm_inference/index)
- [FunctionGemma model card](https://ai.google.dev/gemma/docs/functiongemma/model_card?hl=en)
- [litert-community Qwen3-0.6B-int4](https://huggingface.co/litert-community/Qwen3-0.6B-int4)
- [litert-community Gemma3-1B-IT](https://huggingface.co/litert-community/Gemma3-1B-IT)
- [litert-community Qwen2.5-0.5B-Instruct files](https://huggingface.co/litert-community/Qwen2.5-0.5B-Instruct/tree/main)
- [llama.cpp grammars README](https://github.com/ggml-org/llama.cpp/blob/master/grammars/README.md)
- [ML Kit GenAI Prompt API](https://developers.google.com/ml-kit/genai/prompt/android)
- [PdfBox-Android](https://github.com/TomRoush/PdfBox-Android)
- [sqlcipher-android](https://github.com/sqlcipher/sqlcipher-android)
- [SQLDelight Android setup](https://sqldelight.github.io/sqldelight/latest/android_sqlite/)
- [AndroidSqliteDriver source](https://github.com/sqldelight/sqldelight/blob/master/drivers/android-driver/src/main/java/app/cash/sqldelight/driver/android/AndroidSqliteDriver.kt)
- [Android Auto Backup](https://developer.android.com/identity/data/autobackup)
- [Block Store](https://developer.android.com/identity/block-store)
- [Authorizing access to Google user data](https://developer.android.com/identity/authorization)
- [Drive API scopes](https://developers.google.com/workspace/drive/api/guides/api-specific-auth)
- [Storyboard18: TRAI SMS header suffixes](https://www.storyboard18.com/amp/how-it-works/why-your-sms-now-looks-like-a-code-trais-new-message-format-explained-70270.htm)