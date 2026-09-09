# Running the Personal Content Tracker locally

Step-by-step instructions for **Windows**, **Linux** and **macOS**.

Everything below was verified on Python 3.13. The app has no Node build step, no
database server to install and no compile step — you need Python and about two minutes.

---

## Contents

- [What you need](#what-you-need)
- [Windows](#windows)
- [Linux](#linux)
- [macOS](#macos)
- [First launch](#first-launch)
- [Everyday commands](#everyday-commands)
- [Running the tests](#running-the-tests)
- [Troubleshooting](#troubleshooting)

---

## What you need

| Requirement | Notes |
| --- | --- |
| **Python 3.9 or newer** | Verified on 3.13 (test suite) and 3.14 (all dependencies publish cp314 Windows wheels). Check with `python --version` / `python3 --version`. |
| **pip** | Bundled with Python. |
| **~150 MB free disk** | Dependencies plus the thumbnail cache. |
| **No database server** | SQLite is built into Python. The file is created for you. |
| **No API keys** | Every external integration is optional. |

The project folder looks like this once unzipped:

```
content-tracker/
├── app/            ← the application
├── templates/      ← HTML (Jinja2)
├── static/         ← CSS + JS
├── tests/          ← pytest suite
├── run.py          ← development launcher
├── requirements.txt
├── .env.example    ← copy this to .env
└── README.md       ← full documentation
```

> `data/` is **not** included. It is created automatically on first run, holding
> `tracker.db` (your data), `cache/` and `thumbs/` (cover images).

---

## Windows

Works on Windows 10 and 11. These instructions use **Command Prompt (cmd)**,
which has no script-policy restrictions — the simplest path. A PowerShell
version follows at the end of the section.

### 1. Open Command Prompt

Press `Win + R`, type `cmd`, press Enter. (Or search "cmd" in the Start menu.)

### 2. Check Python

```bat
py -3 --version
```

If that fails, Python isn't installed. Get it from <https://www.python.org/downloads/windows/>
and **tick "Add python.exe to PATH"** during setup.

### 3. Open the project folder

```bat
cd C:\Users\YourName\Downloads\content-tracker
```

### 4. Create and activate a virtual environment

```bat
py -3 -m venv .venv
.venv\Scripts\activate.bat
```

Your prompt should now start with `(venv)` — that means the environment is
active. (If `.venv` already exists from a previous attempt, just run the
`activate.bat` line.)

### 5. Install dependencies

```bat
pip install -r requirements.txt
```

### 6. Create your config file

```bat
copy .env.example .env
```

Optional, but recommended — set a real secret key:

```bat
py -3 -c "import secrets; print(secrets.token_urlsafe(48))"
```

Open `.env` in Notepad and paste that value after `SECRET_KEY=`.

### 7. Start the app

```bat
python run.py
```

Open **<http://localhost:8000>** in your browser.

### The same steps in PowerShell

If you'd rather use PowerShell, two lines change: activate with

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy RemoteSigned
.venv\Scripts\Activate.ps1
```

and run the app with `python run.py` as usual. The `Set-ExecutionPolicy` line
is needed once per window on a fresh install — PowerShell blocks `.ps1`
scripts by default — and `-Scope Process` only affects the current window.
If you prefer not to touch the policy, `.venv\Scripts\activate.bat` also
works inside PowerShell.

---

## Linux

Tested on Debian/Ubuntu-style systems. Fedora, Arch and others differ only in the
package-manager line.

### 1. Check Python

```bash
python3 --version
```

Install it if missing:

```bash
sudo apt update && sudo apt install -y python3 python3-venv python3-pip   # Debian/Ubuntu
sudo dnf install -y python3 python3-pip                                   # Fedora
sudo pacman -S --needed python python-pip                                 # Arch
```

> On Debian/Ubuntu, `python3-venv` is a separate package. Without it,
> `python3 -m venv` fails with *"ensurepip is not available"*.

### 2. Open the project folder

```bash
cd ~/Downloads/content-tracker
```

### 3. Create and activate a virtual environment

```bash
python3 -m venv .venv
source .venv/bin/activate
```

Your prompt should now start with `(.venv)`.

### 4. Install dependencies

```bash
pip install -r requirements.txt
```

### 5. Create your config file

```bash
cp .env.example .env
```

Optional, but recommended — set a real secret key:

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(48))"
```

Then edit `.env` and paste that value after `SECRET_KEY=`.

### 6. Start the app

```bash
python3 run.py
```

Open **<http://localhost:8000>** in your browser.

---

## macOS

Tested on the system Python 3 and on Homebrew Python.

### 1. Check Python

```bash
python3 --version
```

If you see *"command not found"*, install the Command Line Tools first:

```bash
xcode-select --install
```

Or install a newer Python with Homebrew (<https://brew.sh>):

```bash
brew install python@3.12
```

### 2. Open the project folder

```bash
cd ~/Downloads/content-tracker
```

### 3. Create and activate a virtual environment

```bash
python3 -m venv .venv
source .venv/bin/activate
```

Your prompt should now start with `(.venv)`.

### 4. Install dependencies

```bash
pip install -r requirements.txt
```

> Apple's system Python may warn *"running pip as the root user"*. Inside an
> activated `.venv` that warning is harmless — you are not installing system-wide.

### 5. Create your config file

```bash
cp .env.example .env
```

Optional, but recommended — set a real secret key:

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(48))"
```

Then edit `.env` and paste that value after `SECRET_KEY=`.

### 6. Start the app

```bash
python3 run.py
```

Open **<http://localhost:8000>** in your browser.

---

## First launch

1. Visit <http://localhost:8000>. You'll be redirected to **/register**.
2. Create an account. **The first account becomes the admin.**
3. You land on the dashboard with an empty library — no demo data is pre-loaded.
4. Click **+ Add content**, paste any URL, pick a category, and save.

What you should see in the terminal:

```
  📚 Personal Content Tracker
     http://localhost:8000   (env: development)
     database: sqlite:///data/tracker.db
     Ctrl+C to stop

INFO:  Uvicorn running on http://0.0.0.0:8000 (Press CTRL+C to quit)
INFO:  Application startup complete.
```

To stop the server, press **Ctrl+C** in the terminal.

---

## Everyday commands

From inside the project folder:

| Task | Windows (cmd) | Linux / macOS |
| --- | --- | --- |
| Activate the venv | `.venv\Scripts\activate.bat` | `source .venv/bin/activate` |
| Start the app | `python run.py` | `python3 run.py` |
| Stop the app | `Ctrl+C` | `Ctrl+C` |
| Deactivate the venv | `deactivate` | `deactivate` |
| Run tests | `python -m pytest -q` | `python3 -m pytest -q` |
| Use another port | set `PORT=8001` in `.env` | set `PORT=8001` in `.env` |
| Start over with an empty library | `del data\tracker.db*` | `rm -f data/tracker.db*` |
| Back up your data | copy the `data\` folder | `cp -r data ~/tracker-backup` |

Your data lives entirely in the **`data/`** folder. Copy it anywhere to move the app
to another machine — no export needed, though `/backup` in the UI produces a
secret-free JSON archive if you prefer.

---

## Running the tests

```bash
python -m pytest -q              # Windows (venv active)
python3 -m pytest -q             # Linux / macOS (venv active)
```

Expected result: **217 passed**.

Useful variations:

```bash
python -m pytest tests/ -v                     # list every test by name
python -m pytest tests/test_security.py -q     # one module
python -m pytest -q -k "csrf"                  # by keyword
```

The tests never touch the network and never use your real database — they create a
temporary one and stub every external call.

---

## Troubleshooting

### `python` / `python3` is not recognised

- **Windows** — reinstall Python and tick *Add python.exe to PATH*, or use the
  launcher: `py -3 run.py`.
- **macOS** — run `xcode-select --install`, or `brew install python@3.12`.
- **Linux** — `sudo apt install python3 python3-venv`.

### PowerShell: *"running scripts is disabled on this system"*

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy RemoteSigned
```

That applies to the current window only, so nothing on your machine is permanently
changed. Or just use `.venv\Scripts\activate.bat` in Command Prompt.

### A "Select an app to open this .ps1 file" dialog appears

You ran `Activate.ps1` inside **Command Prompt**. cmd cannot execute PowerShell
scripts, so Windows offers to open the file in Notepad instead. Cancel the
dialog and use the cmd activator:

```bat
.venv\Scripts\activate.bat
```

Rule of thumb: in cmd, always `activate.bat`; in PowerShell, `Activate.ps1`.

### `ensurepip is not available` / `venv` fails on Linux

```bash
sudo apt install python3-venv
```

### `[Errno 98] Address already in use` / port 8000 is taken

Open `.env` and change:

```ini
PORT=8001
```

Then start again and visit <http://localhost:8001>.

### The browser shows nothing at localhost:8000

Check the terminal for `Uvicorn running on http://0.0.0.0:8000`. If you changed
`HOST` in `.env`, set it back to `0.0.0.0` or `127.0.0.1`.

### `ModuleNotFoundError: No module named 'fastapi'`

The virtual environment isn't active — your prompt should start with `(.venv)`.
Activate it, then run `pip install -r requirements.txt` again.

### `pip install` fails on Pillow

Pillow ships prebuilt wheels for Windows, macOS and most Linux distributions. If yours
fails, install the build tools:

```bash
sudo apt install build-essential python3-dev libjpeg-dev zlib1g-dev   # Debian/Ubuntu
xcode-select --install                                                # macOS
```

Pillow is optional at runtime — the app still runs without it, using the original
cover URL instead of a cached thumbnail.

### Settings save but nothing changes

Make sure you edited `.env` and not `.env.example`, then restart the app — the
environment is read once at startup.

### Forgot your password

No email server is configured. With `APP_ENV=development` (the default), the reset
link is printed **on the page itself** after you request a reset. Just click it.

### I want a completely clean slate

Stop the app, then delete the `data/` folder. It is recreated, empty, on next start.
Your account and library are removed with it.

---

## Running it as a real deployment

`run.py` is for development. For a server, see **README.md §10** — it covers systemd,
nginx with TLS, Docker, the `SECRET_KEY` requirement, and why you should keep to a
single worker (the scheduler and rate limiter are in-process).
