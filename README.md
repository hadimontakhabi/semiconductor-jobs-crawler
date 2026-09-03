# Semiconductor Startup Job Crawler

A automated job crawler that scans active startups and exited (alumni) companies listed in the [awesome-semiconductor-startups](https://github.com/aolofsson/awesome-semiconductor-startups) database to find senior leadership, director, VP, and architect openings.

## 🚀 Features
* **Automated Sync:** Pulls the latest startup data directly from the upstream `awesome-semiconductor-startups` repository.
* **Alumni Tracking:** Integrates active alumni companies (excluding those that are shut down) by parsing the historical Git logs of the startups database to recover websites and metadata.
* **Fuzzy Naming Resolution:** Handles name variations (e.g., matching `"Astera"` in the alumni list to `"Astera Labs"` in the history) using fuzzy string matching.
* **Concurrent Scraping:** Discovers and scans job boards (Greenhouse, Lever, Ashby, Workday, and generic career pages) concurrently using Python's `ThreadPoolExecutor`.
* **Markdown & Email Reports:** Generates a structured Markdown report of all matching openings and emails a styled HTML copy to a configured address.
* **Systemd Timer Integration:** Ready to run automatically on a weekly schedule (e.g., every Monday morning).

## 🛠️ Setup

1. **Clone & Install Dependencies:**
   Make sure you have Python 3 and `requests` installed:
   ```bash
   pip install requests
   ```

2. **Configure Environment Variables:**
   Create a `.env` file in the root directory with your SMTP email configuration:
   ```ini
   SENDER_EMAIL=your_email@gmail.com
   RECEIVER_EMAIL=recipient_email@gmail.com
   SMTP_SERVER=smtp.gmail.com
   SMTP_PORT=587
   SMTP_USER=your_email@gmail.com
   SMTP_PASSWORD=your_app_password
   ```

3. **Run Manually:**
   ```bash
   python3 run_weekly_analysis.py
   ```

## ⏰ Automated Weekly Scheduling (Systemd)

You can set this script to run automatically every Monday at 9:00 AM using systemd user timers:

1. Create a service file at `~/.config/systemd/user/semiconductor-jobs.service`:
   ```ini
   [Unit]
   Description=Weekly Semiconductor Job Analysis
   After=network.target

   [Service]
   Type=oneshot
   WorkingDirectory=/home/anonymous/code/semiconductor-jobs-crawler
   ExecStart=/bin/bash -c "python3 run_weekly_analysis.py >> /home/anonymous/code/semiconductor-jobs-crawler/weekly-run.log 2>&1"

   [Install]
   WantedBy=default.target
   ```

2. Create a timer file at `~/.config/systemd/user/semiconductor-jobs.timer`:
   ```ini
   [Unit]
   Description=Weekly Semiconductor Job Analysis Timer

   [Timer]
   OnCalendar=Mon *-*-* 09:00:00
   Persistent=true

   [Install]
   WantedBy=timers.target
   ```

3. Enable and start the timer:
   ```bash
   systemctl --user daemon-reload
   systemctl --user enable --now semiconductor-jobs.timer
   ```
