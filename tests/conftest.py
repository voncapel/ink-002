"""Import-time environment for the tests.

``app.py`` reads its configuration, wipes transient previews, and starts the
print worker at import time, so the data directory and the printing kill switch
have to be set before that import happens. Without ``S002_PRINTING=0`` a test
run would push paper through the real printer over Bluetooth.
"""

import os
import tempfile

_TEMP_DIR = tempfile.mkdtemp(prefix="s002-tests-")
os.environ["S002_DATA_DIR"] = _TEMP_DIR
os.environ["S002_ALLOWED_PATHS"] = os.path.join(_TEMP_DIR, "inbox")
os.environ["S002_PRINTING"] = "0"
os.environ.setdefault("S002_WEB_USER", "")
os.environ.setdefault("S002_WEB_PASSWORD", "")
os.environ.setdefault("S002_API_TOKEN", "")
