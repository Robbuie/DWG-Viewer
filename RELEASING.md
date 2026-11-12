# Packaging and releasing DWG Viewer

## One-time setup

**1. Create the GitHub repo** (public), then point the app at it —
edit `src/version.py`:

```python
GITHUB_OWNER = "your-github-username"
GITHUB_REPO  = "dwg-viewer"
```

**2. Push the code**

```bat
cd "C:\Users\Rokray.ENCORE\Projects\DWG Viewer"
git init
git add .
git commit -m "DWG Viewer"
git branch -M main
git remote add origin https://github.com/<owner>/dwg-viewer.git
git push -u origin main
```

**3. Install the build tools locally** (only needed if you want to build
on your own machine — GitHub Actions does it without them):

- Inno Setup 6 — https://jrsoftware.org/isdl.php
- `pip install -r requirements-dev.txt`

## Cutting a release

```bat
:: 1. bump the version
::    edit src/version.py  ->  __version__ = "1.0.1"
git commit -am "v1.0.1"
git tag v1.0.1
git push && git push --tags
```

That's it. The `Build and release` workflow builds the app with
PyInstaller, wraps it with Inno Setup, and publishes
`DWGViewer-Setup-1.0.1.exe` to the GitHub release. Every installed copy
is offered it the next time it is opened (or within six hours if it is
left open), or immediately via **Help ▸ Check for Updates**. Copies
older than 1.10.1 still use the old once-a-day check, so on those the
quickest way is that menu item (it was a toolbar button then).

The workflow **fails the build** if the tag and `src/version.py`
disagree — that mismatch would otherwise ship an installer whose About
box, filename and update check all claim different versions.

## Building locally instead

```bat
build.bat
```

Output: `dist_installer\DWGViewer-Setup-<version>.exe`

## How updating works

- The installer is **per-user** (`%LOCALAPPDATA%\Programs\DWG Viewer`).
  This is deliberate: the updater runs the new installer silently, and a
  Program Files install would throw a UAC prompt at the user on every
  single update.
- On launch the app checks GitHub in the background and stays quiet
  unless there is something newer. It skips the check only if one
  *succeeded* in the last two hours, and re-checks every six hours while
  left open. A check that could not reach GitHub (VPN down, proxy) does
  not count — before 1.10.1 it did, and the stamp was written before the
  check ran, so a release published after that morning's launch went
  unnoticed for a day.
- The prompt offers **Install now**, **Later** (asks again next launch)
  and **Skip this version** (quiet until a newer one appears).
- **Help ▸ Check for Updates** always reports a result, including when
  GitHub could not be reached — it no longer says "latest version" when
  it never got an answer.
- Installing keeps the same `AppId` GUID, so Windows upgrades in place
  rather than accumulating copies in Add/Remove Programs. **Never change
  that GUID** in `installer/DWGViewer.iss`.

### If you later make the repo private

Set a `DWGVIEWER_GITHUB_TOKEN` environment variable containing a
fine-grained PAT with read access to the repo; `src/updater.py` already
sends it when present. Note that shipping a token inside a distributed
app is not a secret — anyone with the exe can read it.

## Two things worth knowing

**ODA File Converter is still a separate install.** It cannot be
redistributed inside the installer under its licence, so DWG (not DXF)
support still requires each machine to have it from
https://www.opendesign.com/guestfiles/oda_file_converter. If you want
the installer to check for it and prompt, that's a small addition to the
`.iss` — worth doing before you hand this to other people.

**The exe is unsigned.** Windows SmartScreen will warn the first few
users with "Windows protected your PC" until the download builds
reputation. A code-signing certificate removes that; without one, tell
users to click *More info → Run anyway*.
