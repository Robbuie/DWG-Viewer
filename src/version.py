"""
version.py — single source of truth for the application version.

The release workflow refuses to build if the pushed git tag does not
match this string, so the number in the About box, the number baked
into the installer, and the number the updater compares against can
never drift apart.

Bump this, commit, then tag: git tag v1.0.1 && git push --tags
"""

__version__ = "1.0.0"

# GitHub repository that publishes releases. Update these two lines
# once, after you create the repo.
GITHUB_OWNER = "Robbuie"
GITHUB_REPO  = "DWG-Viewer"

APP_NAME = "DWG Viewer"
