# Put Rectifi online (and keep it private)

This guide moves Rectifi from your laptop to a small free server that is always on, so you can open it from your phone any time. It is written for someone who has never run a server. Go in order. Each step says what you should see.

**Be honest about what this is.** It is a hobby setup on a free cloud account. It is good enough for one person's expenses. It is not a bank. Read "Honest limits" at the end before you put real statements on it.

## What you are building

```
your phone / laptop  --(Tailscale, encrypted)-->  the server (Oracle VM)
                                                   |- app      (Rectifi web app, port 8501, this machine only)
                                                   |- db       (Postgres, no port open)
                                                   |- ollama   (the local model, no port open)
                                                   `- backup   (a database copy every day)
```

- The only way in is **Tailscale**, which is installed on the server. No port is open to the internet.
- **Tailscale Serve** (recommended): only devices signed in to *your* Tailscale account can reach Rectifi.
- **Tailscale Funnel** (optional): anyone on the internet can reach the sign-in page. Only do this if you must open Rectifi from a device that cannot run Tailscale.
- Either way, Rectifi has its own **password**. The app refuses to start without one when it runs on the server.

## Before you start

You need:

- an email address and a phone number for sign-ups;
- a debit or credit card for Oracle's identity check. Oracle says it does not charge the card for Always Free. **I could not confirm whether Indian debit or RuPay cards are accepted.** If yours is refused, try another card, or use the laptop-only setup (see "If you can't get Oracle");
- your Rectifi code in a Git repository you can clone from the server, or a way to copy the folder (`scp`, below);
- about 90 minutes the first time. Most of it is waiting for downloads.

Money: everything here is meant to cost 0 rupees. Oracle keeps pushing "Upgrade to a paid account". Do not. Stay on Always Free resources only.

## Step 1. Tailscale account

1. Make a free account at tailscale.com (the Personal plan).
2. Install Tailscale on your phone and laptop and sign in with the same account.
3. In the Tailscale admin page, open **DNS**. Make sure **MagicDNS** is on, and click **Enable HTTPS** (Serve and Funnel need it). You will see your tailnet name, like `tail1a2b3c.ts.net`.

## Step 2. Oracle account and the server

1. Sign up at oracle.com/cloud/free. Choose your **home region** carefully: you cannot change it later, and Always Free servers must live there. Pick the one closest to you that you can select (for example Mumbai or Hyderabad).
2. In the console: **Compute > Instances > Create instance**.
   - **Image:** Canonical Ubuntu 24.04 (the `aarch64` build).
   - **Shape:** change shape > **Ampere** > `VM.Standard.A1.Flex`, **2 OCPU, 12 GB memory**. As of the plan date (June 2026) that is the Always Free limit. Look for the "Always Free-eligible" label. If the console shows a price, you picked the wrong thing.
   - **Networking:** the default network is fine. Keep "assign a public IPv4 address" on (the server needs it to reach the internet, and so you can log in the first time).
   - **SSH keys:** choose "Generate a key pair" and download both files, or paste your own public key. **Keep the private key file safe.** Without it you cannot log in.
   - **Boot volume:** you can raise it to 100 GB or more (Always Free gives 200 GB in total).
   - **Advanced options > Management > Cloud-init script:** paste the whole of `deploy/cloud-init.yaml` from this repository. This installs Docker and Tailscale, adds swap and turns on automatic security updates. It opens no ports.
3. Click **Create**.

**If you see "Out of host capacity":** Oracle has no free Arm servers in your region right now. This is common. Try again later (morning in India time often works), or choose another availability domain in the same region. There is no trick that makes it work.

## Step 3. Log in and wait for setup

On your laptop, in a terminal (the key file name will differ):

```
chmod 600 ~/Downloads/ssh-key.key
ssh -i ~/Downloads/ssh-key.key ubuntu@<the public IP from the console>
```

Then on the server:

```
cloud-init status --wait
```

You should see `status: done`. This takes 3 to 8 minutes. If it says `error`, run `sudo less /var/log/cloud-init-output.log` and read the last lines.

Check the tools are there:

```
docker --version && docker compose version && tailscale version
```

If `docker` says "permission denied", log out and back in once (the setup added you to the `docker` group).

## Step 4. Sign in to Tailscale

```
sudo tailscale up --hostname=rectifi
```

It prints a link. Open it on your laptop and approve the new machine. Then get the machine's full name:

```
tailscale status --json | python3 -c "import sys,json; print(json.load(sys.stdin)['Self']['DNSName'].rstrip('.'))"
```

You will get something like `rectifi.tail1a2b3c.ts.net`. Write it down. That is `RECTIFI_HOSTNAME`.

Optional but good: `sudo tailscale set --ssh` lets you log in over Tailscale. Once that works you can delete the SSH rule in the Oracle network settings, so the server has no open ports at all.

## Step 5. Get the code onto the server

If the repository is on GitHub: `git clone <your repository URL> rectifi && cd rectifi`. A private repository needs a read-only access token as the password.

If you would rather copy it from your laptop, run this on the laptop (not the server):

```
rsync -av --exclude node_modules --exclude .venv --exclude .git --exclude .env ./ ubuntu@<ip>:rectifi/
```

The folder `web/dist` must be in it. The server has no Node, so it uses the build that is committed.

## Step 6. Fill in your settings

On the server, inside the `rectifi` folder:

```
cp .env.deploy.example .env.deploy
chmod 600 .env.deploy
python3 scripts/dev.py set-password --print
nano .env.deploy
```

- (If `dev.py` says "unrecognized arguments" for `--print`, run `python3 scripts/devtools/deploy.py set-password --print` instead.)
- `set-password --print` asks for your password twice, then prints two lines. The password must be at least 12 characters. Three or four random words works. It never shows or saves the password itself, only a scrambled copy.
- In `nano`, set `RECTIFI_HOSTNAME`, set `POSTGRES_PASSWORD` to the output of `openssl rand -hex 24`, and paste the two lines over the two empty ones. Keep the single quotes around the hash. Save with Ctrl+O, Enter, Ctrl+X.

Now check everything:

```
python3 scripts/dev.py deploy-check
```

Fix every line that says `FAIL`. Do not go on until it says `Ready to deploy.`

## Step 7. Start Rectifi

To avoid typing a long command every time:

```
echo "alias rc='docker compose --env-file ~/rectifi/.env.deploy -f ~/rectifi/deploy/compose.oracle.yml'" >> ~/.bashrc && source ~/.bashrc
rc up -d --build
```

The first run builds the app image and downloads the models (about 4 GB). Expect 10 to 25 minutes on 2 slow cores. Watch it:

```
rc ps
rc logs -f ollama-pull
```

`ollama-pull` ends by itself with `Exited (0)`. That is correct. `app`, `db`, `ollama` and `backup` should say `Up` (and `healthy` for the first three). Test from the server:

```
curl -s http://127.0.0.1:8501/api/health
```

You should see `{"ok":true}`.

If `app` keeps restarting, run `rc logs app`. The usual cause is a missing setting: the message says which one and how to fix it.

## Step 8. Open it from your devices

**Private (recommended):**

```
sudo tailscale serve --bg 8501
tailscale serve status
```

Open `https://rectifi.tail1a2b3c.ts.net` (your name) on your phone or laptop with Tailscale switched on. You see the sign-in page. Type your password.

On your phone: in Chrome, menu, **Add to Home screen**.

**Public (only if you must):**

```
sudo tailscale funnel --bg 8501
```

Tailscale may print a link to allow Funnel for your account; open it and approve. The same address now works from any device, with no Tailscale. What changes:

- Anyone on the internet can see the sign-in page and try passwords. Use a long password. Rectifi allows 5 wrong tries a minute, then slows down, then **locks sign-in for 15 minutes after 10 wrong tries**. The lock is global, so a stranger can also lock *you* out for 15 minutes. It cannot get them in.
- I could not confirm from Tailscale's plan pages that Funnel is free on the Personal plan. The command tells you if it is not.

To go back to private: `sudo tailscale serve reset`, then run the `serve` command again.

**Funnel from your laptop (not the server).** On the server the password is always on. On your laptop it is not: the laptop app has no login unless you set `RECTIFI_PUBLIC=1`. So `tailscale funnel 8501` on the laptop would put your bank data on the internet for anyone with the link. Rectifi checks for this. If Funnel is on for the app and there is no login, `python scripts/dev.py up` refuses to start and `python scripts/dev.py doctor` fails with "Funnel is on: set RECTIFI_PUBLIC=1 and run `python scripts/dev.py set-password` first". Do that (put the two lines it prints, plus `RECTIFI_PUBLIC=1`, in `.env`), or switch Funnel off with `tailscale funnel reset`. The check reads `tailscale serve status --json`; if Tailscale is not installed or its answer looks different from what Rectifi expects, it stays quiet and does nothing. It has not been run against a real Tailscale install.

## Using it

- The first time, the database is empty. Add accounts, then import a statement or add entries as on your laptop.
- To move your laptop data to the server, see "Move your laptop data".
- The **phone SMS gateway stays off** on the server. The Streamlit app and the MCP server are not exposed either.
- Model replies are slow on this server (see "Honest limits"). Everything that does not use the model is fast.

### Move your laptop data

On the laptop (Postgres in Docker or native):

```
pg_dump -Fc "postgresql://rectifi:rectifi_dev@localhost:5432/rectifi" > rectifi-laptop.dump
scp rectifi-laptop.dump ubuntu@<ip>:
```

On the server, follow "Restore" below using that file. Do this once, early.

## Backups

The `backup` container writes `/var/backups/rectifi/rectifi-YYYY-MM-DD.dump` every 24 hours (first one right after start) and keeps the newest 7.

Check they exist:

```
ls -lh /var/backups/rectifi
```

**A copy on the same server is not a real backup.** If the server is taken back by Oracle you lose both. Once a week, copy the newest file to your laptop (run on the laptop):

```
scp ubuntu@rectifi:/var/backups/rectifi/rectifi-2026-10-25.dump .
```

(`ubuntu@rectifi` works when SSH over Tailscale is on; otherwise use the IP.) Oracle's block-volume backups (5 free) are a second safety net: in the console, open the boot volume and create a backup before big changes.

## Restore

Replace the file name with yours. This **replaces everything in the database** with the backup.

```
cd ~/rectifi
rc stop app
rc exec -T db pg_restore -U rectifi -d rectifi --clean --if-exists --no-owner < /var/backups/rectifi/rectifi-2026-10-25.dump
rc start app
```

You may see a few "does not exist, skipping" notes on a fresh database. Those are fine. Open Rectifi and check your latest entries.

**Test a restore once, now, while nothing depends on it.** An untested backup is a hope, not a backup. Easiest test: restore into a scratch database instead of the real one:

```
rc exec db createdb -U rectifi scratch
rc exec -T db pg_restore -U rectifi -d scratch --no-owner < /var/backups/rectifi/<a-file>.dump
rc exec db psql -U rectifi -d scratch -c "select count(*) from transactions"
rc exec db dropdb -U rectifi scratch
```

## Updates

Your app:

```
cd ~/rectifi
git pull
python3 scripts/dev.py deploy-check
rc up -d --build
```

If the new version misbehaves, go back: `git log --oneline`, then `git checkout <the older commit>` and `rc up -d --build`. The database is not changed back, so take a backup first before big updates (`rc exec -T db pg_dump -U rectifi -Fc rectifi > before-update.dump`).

The operating system installs security updates by itself. Some need a restart now and then: `sudo reboot`. Everything comes back on its own (the containers restart, and Tailscale remembers `serve`).

Docker images: Postgres, Ollama and Python are pinned to versions. To move to a newer Ollama, change `OLLAMA_VERSION` in `.env.deploy` (look at hub.docker.com/r/ollama/ollama/tags first) and run `rc up -d`.

## Change the password, sign everyone out, or unlock

- **Change the password:** `python3 scripts/dev.py set-password --print`, paste the two new lines into `.env.deploy`, then `rc up -d app`. The new session secret signs every device out.
- **Locked out for 15 minutes:** wait. To clear it now: `rc exec db psql -U rectifi -c "delete from app_settings where key='login_lock'"` and `rc restart app`.
- **A phone was lost:** change the password as above.

Note: `rc restart app` does not re-read `.env.deploy`. Use `rc up -d app` after editing it.

## If you can't get Oracle

- **Your laptop, same files:** you can run the same Docker setup on your own computer, or just `python scripts/dev.py up` and `python scripts/dev.py tailscale`. It is free and the data never leaves your machine. It only works while the laptop is on.
- **Always-on without a model:** a free web host plus a free hosted Postgres can run the app, but both sleep after a few minutes, are tiny, and cannot run Ollama. I do not recommend it for bank data.
- Hugging Face Docker Spaces now need a paid plan, and the other free hosts I checked (Koyeb, Fly, Railway) are no longer free for new users.

## Honest limits

- **Oracle may take the server back.** Free servers that look idle for 7 days (low CPU, network and memory) can be reclaimed. The model stays loaded (`OLLAMA_KEEP_ALIVE=-1`), which should keep memory use up. That is my reading of Oracle's rule, and I have not tested it. Keep your off-server backups.
- **Capacity.** "Out of host capacity" can stop you creating the server at all.
- **One server is one point of failure.** If it dies, you restore from a backup.
- **It is slow.** My estimates, not measurements: a 3B model on 2 Arm cores writes about 5 to 8 words a second. A chart attempt can take a minute or more. A full "look it up" answer can take 1 to 3 minutes. Bigger models (7B) fit in memory but are 2 to 3 times slower. Classification by rules and past choices is instant.
- **Small models are less accurate.** The checks that protect your numbers (the amount guard, reconciliation) do not depend on the model. Wrong categories are fixable, and Rectifi learns from your fixes.
- **Not tested here.** The files in `deploy/` were checked with `docker compose config`, a Dockerfile linter and Python tests. They were **not** built or run on a real Docker server, an Oracle VM or Funnel. Expect to fix small things on the first run, such as a package that has no Arm build. If you do, tell me what the error said.
- **Your data is on a machine you don't control.** The disk is not encrypted by you, and Oracle staff could in principle read it. If that bothers you, do not use a hosted setup; use your laptop.
- **The password is the only lock behind Funnel.** There is no two-step sign-in. Use Serve if you can.
- **No password reset email.** If you forget it, run `set-password` on the server as above.
- **Uploads are limited to 10 MB**, as on your laptop. Run only one app worker (the compose file does).
