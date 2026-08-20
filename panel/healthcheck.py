#!/usr/bin/env python3
"""Docker healthcheck for ROLE=panel: is the panel answering HTTP at all?

Any status code counts as healthy, including 401 -- ADMIN_PASSWORD is set and we
deliberately have no credentials here. Only a refused or timed-out connection
means the process is actually broken, which is the same "alive and doing its job"
rule the snapclient/snapserver healthcheck uses.
"""
import os
import sys
import urllib.error
import urllib.request

url = "http://127.0.0.1:%s/api/players" % os.environ.get("PORT", "8080")

try:
    urllib.request.urlopen(url, timeout=5)
except urllib.error.HTTPError:
    pass  # 401/500 still means the server is up and answering
except Exception as exc:  # URLError, socket.timeout, ...
    print("unhealthy: %s" % exc, file=sys.stderr)
    sys.exit(1)
