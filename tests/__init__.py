import os

# Prevent test suites from altering real Windows Credential Manager state
os.environ["AGYM_DISABLE_WINCRED"] = "1"
