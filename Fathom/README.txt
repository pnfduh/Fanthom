═══════════════════════════════════════════════════════════════
  Fathom — USB Agent Installer
═══════════════════════════════════════════════════════════════

BEFORE YOU START
────────────────
1. PC1 server must already be running
   Dashboard should be visible at http://PC1-IP:8000

2. Edit agent\config.json — set server_host to PC1's IP:
   "server_host": "192.168.1.50"    ← change this

INSTALL (on PC2)
────────────────
   Right-click install_task.bat
   Select "Run as administrator"
   Click Yes

   The installer will:
   - Detect and remove Windows Store Python stub if present
   - Download and install real Python 3.12 if needed
   - Install all dependencies automatically
   - Deploy the agent to %APPDATA%\FathomAgent
   - Register a scheduled task that starts at every login
   - Start the agent immediately — no reboot needed

   PC2 will appear in the dashboard within seconds.

UNINSTALL
─────────
   Right-click remove_task.bat
   Select "Run as administrator"

SENDING THIS TO SOMEONE REMOTELY
──────────────────────────────────
   1. Edit agent\config.json with your PC1 IP first
   2. Zip this entire folder and send it
   3. They unzip, right-click install_task.bat, Run as admin

LOGS (if something goes wrong)
────────────────────────────────
   %APPDATA%\FathomAgent\watchdog.log
   %APPDATA%\FathomAgent\agent.log

USB FILE LAYOUT
───────────────
  Fathom-USB\
  ├── README.txt
  ├── install_task.bat       ← right-click Run as administrator
  ├── remove_task.bat
  ├── requirements-agent.txt
  ├── autorun.inf
  └── agent\
      ├── config.json        ← edit server_host before sending
      ├── telemetry_agent.py
      ├── activity_monitor.py
      ├── system_monitor.py
      └── watchdog.py

═══════════════════════════════════════════════════════════════
