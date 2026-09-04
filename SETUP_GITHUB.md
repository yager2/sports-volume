# Run the daily pull on GitHub instead of this laptop

Once this is set up, GitHub's servers run the whole pipeline every day (~15 min),
commit the refreshed data back to the repo, and push the spreadsheet + dashboard
to Google Drive. Your laptop doesn't need to be on. The Windows scheduled task
can stay as a backup for a week, then be removed.

Free-tier note: a private repo gets 2,000 Actions minutes/month; this uses ~450.

---

## 1. Make the repo

1. Go to <https://github.com/new>
2. Name: `sports-volume` (anything is fine) · **Private** · do **not** add a README
3. Click **Create repository**, leave the page open — you'll need the URL it shows.

## 2. Push this folder up

Right-click inside the **Mention folder** → **Open Git Bash here** (or use PowerShell),
then paste these one at a time. Replace `YOU` with your GitHub username:

```bash
git init
git add -A
git commit -m "initial"
git branch -M main
git remote add origin https://github.com/YOU/sports-volume.git
git push -u origin main
```

On `git push` a browser window opens — sign in to GitHub to authorize.
(If it instead asks for a *password* on the command line, that's a Personal
Access Token: github.com → Settings → Developer settings → Personal access
tokens → Tokens (classic) → Generate, scope `repo`, paste it as the password.)

## 3. Let the workflow write back to the repo

On the repo page: **Settings → Actions → General → Workflow permissions** →
select **Read and write permissions** → **Save**.

## 4. (Optional) Google Drive push

Skip this if you're happy opening the dashboard/spreadsheet straight from the
repo. To keep the Google Drive copy updating:

1. Open `C:\Users\yager\AppData\Roaming\rclone\rclone.conf` in Notepad, copy **all** of it.
2. Repo page → **Settings → Secrets and variables → Actions → New repository secret**
   - Name: `RCLONE_CONF`
   - Secret: paste the file contents → **Add secret**

## 5. First run

Repo page → **Actions** tab → click **daily-volume** on the left →
**Run workflow** button → **Run workflow**.

Watch it (~15-20 min). Green check = working. If a step is red, send me the log
from the failed step — a couple of the data sources may need a tweak to work
from GitHub's servers.

## 6. Where the outputs live

- **Spreadsheet / dashboard:** committed to the repo each run — open them from
  the repo's file list, or download `unified_sports_volume.xlsx` raw. If you set
  up step 4, they're also in `Google Drive / SportsVolume/`.
- **History:** every run is a commit, so you can see exactly what changed each day.

## 7. Turn off the laptop task (after a week of clean GitHub runs)

```powershell
Unregister-ScheduledTask -TaskName "SportsVolumeTracker" -Confirm:$false
```

---

### Schedule

`.github/workflows/daily.yml` runs at **20:00 UTC** (3 PM Central / 4 PM Eastern),
after DKeX finalises the prior day. GitHub cron can run 5-30 min late under load
— fine for a daily job. Change the `cron:` line to adjust.
