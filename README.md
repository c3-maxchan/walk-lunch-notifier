# Walk & Lunch Daily Teams Notifier

Automatically posts a daily message to a Microsoft Teams channel every weekday
morning with:

- **Noon weather forecast** (12 pm – 1 pm) for your walking break at 1400
  Seaport Blvd, Redwood City
- **Today's cafeteria lunch specials** from the Bon Appétit café

Everything runs on GitHub for free — you don't need to install anything on your
computer.

---

## One-Time Setup (Step by Step)

### Step 1 — Create a Teams channel

1. Open Microsoft Teams.
2. In the team where your coworkers are, click **"+"** next to the existing
   channels (or right-click the team name and choose **Add a channel**).
3. Name it something like **Walk & Lunch** and click **Create**.
4. Invite your coworkers to the channel if they aren't already in the team.

### Step 2 — Create a webhook in that channel

A "webhook" is a special URL that lets this script send messages into your
channel. Here's how to set one up using the modern **Workflows** method:

1. Open the **Walk & Lunch** channel you just created.
2. Click the **"…"** (three dots) at the top-right of the channel, then choose
   **Manage channel** (or **Connectors / Workflows**, depending on your Teams
   version).
3. Look for **Workflows** in the list, or click **"More apps"** and search for
   **Workflows**.
4. Select the template called **"Post to a channel when a webhook request is
   received"**.
5. Give the workflow a name like "Walk Lunch Bot" and pick the channel you
   created in Step 1.
6. Click **Save** (or **Add workflow**).
7. Teams will show you a **webhook URL** — it looks like a long link starting
   with `https://`. **Copy this URL** and save it somewhere temporarily (you'll
   need it in Step 4).

> **Can't find Workflows?** Your IT admin may need to enable it. Ask them to
> turn on the Workflows app in the Teams admin center.

### Step 3 — Create a GitHub repository

If you don't have a GitHub account yet, go to <https://github.com> and sign up
(it's free).

1. Go to <https://github.com/new> to create a new repository.
2. Name it something like `walk-lunch-notifier`.
3. Choose **Public** (this gives you unlimited free automation minutes; your
   webhook URL stays private in a separate step).
4. Check **"Add a README file"** (GitHub will replace it when you push this
   code, but it makes the next step easier).
5. Click **Create repository**.

### Step 4 — Add your webhook URL as a secret

A "secret" is a way to store your webhook URL on GitHub without anyone being
able to see it.

1. In your new GitHub repository, click **Settings** (the gear icon tab at the
   top).
2. In the left sidebar, click **Secrets and variables** → **Actions**.
3. Click the green **"New repository secret"** button.
4. Set the **Name** to exactly: `TEAMS_WEBHOOK_URL`
5. Paste the webhook URL you copied in Step 2 into the **Secret** field.
6. Click **Add secret**.

### Step 5 — Push the code to GitHub

Open **Terminal** (on Mac, press `Cmd + Space`, type "Terminal", hit Enter) and
run these commands one at a time. Replace `YOUR_GITHUB_USERNAME` with your
actual GitHub username:

```bash
cd ~/Documents/walk-lunch-notifier

git init

git add .

git commit -m "Initial commit: walk & lunch notifier"

git branch -M main

git remote add origin https://github.com/YOUR_GITHUB_USERNAME/walk-lunch-notifier.git

git push -u origin main
```

> **First time using git?** GitHub may ask you to sign in. Follow the prompts —
> it will open a browser window for you to authorize.

### Step 6 — Test it right now

You don't have to wait until tomorrow morning to see if it works:

1. In your GitHub repository, click the **Actions** tab at the top.
2. On the left, click **"Daily Walk & Lunch Update"**.
3. Click the **"Run workflow"** dropdown button on the right.
4. Click the green **"Run workflow"** button.
5. Wait about 30 seconds, then check your Teams channel — you should see the
   message.

If the run fails, click on it in the Actions tab to see the error log. The most
common issue is a missing or incorrect webhook URL secret.

---

## How It Works

Every weekday at approximately 8:30 AM Pacific Time, GitHub automatically runs
a small Python script that:

1. **Fetches the weather** from the free Open-Meteo API — specifically the
   hourly forecast for noon–1 PM at your office location (no API key needed).
2. **Reads the lunch specials** from the Bon Appétit cafeteria website at
   `c3ai.cafebonappetit.com` by downloading the page and extracting the menu.
3. **Sends a formatted message** to your Teams channel with both pieces of
   information, plus a walk recommendation ("Great day for a walk!" or "Bring
   an umbrella!").

### Costs

- **GitHub Actions**: Free for public repositories. Private repos get 2,000
  free minutes per month; this job uses about 15 seconds per run.
- **Open-Meteo API**: Free for non-commercial use; no signup required.
- **Teams Webhook**: Built into your enterprise Teams; no extra cost.

### Schedule

The script runs at **15:30 UTC** on weekdays (Monday–Friday), which is:
- **8:30 AM PDT** during daylight saving time (March–November)
- **7:30 AM PST** during standard time (November–March)

GitHub Actions scheduled runs can sometimes be delayed by 5–30 minutes, so the
message may arrive between 7:30 and 9:00 AM.

---

## Troubleshooting

| Problem | What to do |
|---|---|
| No message appears in Teams | Check the **Actions** tab in GitHub for errors. Make sure the `TEAMS_WEBHOOK_URL` secret is set correctly. |
| "No lunch specials posted today" | The café website may not have updated yet, or the café is closed. Check <https://c3ai.cafebonappetit.com/#lunch> directly. |
| Weather shows as unavailable | The Open-Meteo API may be temporarily down. The menu portion will still send. |
| Action doesn't run on schedule | GitHub may skip scheduled runs if the repo has had no activity in 60 days. Push a small change or run it manually to re-activate. |
| Webhook URL changed | Go to **Settings → Secrets → Actions** in GitHub, delete the old secret, and create a new one with the same name (`TEAMS_WEBHOOK_URL`). |

---

## Project Files

| File | Purpose |
|---|---|
| `daily_update.py` | The main script — fetches weather, scrapes the menu, sends the Teams message |
| `.github/workflows/daily-update.yml` | Tells GitHub when and how to run the script |
| `requirements.txt` | Lists the Python libraries the script needs |
| `README.md` | This file — setup instructions |
