"""Host-side model probes using the environment passed to the batch driver."""

from __future__ import annotations

import shlex

from opencollab_eval.experiment.batch_spec import HostConfig, runtime_env

_PATH_CHECK = """
import os
from pathlib import Path
import stat

path = Path(os.environ['OPENCOLLAB_CONFIG_FILE'])
if not path.is_absolute():
    path = Path.cwd() / path
print('PATH_FIELD\\t' + str(path))
try:
    entry = path.lstat()
except FileNotFoundError:
    print('STATUS\\tabsent')
    raise SystemExit(0)
if stat.S_ISLNK(entry.st_mode):
    print('STATUS\\tsymlink')
    raise SystemExit(0)
if not stat.S_ISREG(entry.st_mode):
    print('STATUS\\tnot-regular')
    raise SystemExit(0)
"""

_IDENTITY = """
import hashlib
from opencollab import OpenCollab

try:
    config = OpenCollab(Path.cwd()).configuration
    print('MODEL_ENV\\tpresent')
    print('MODEL\\t' + config['model'])
    print('PROVIDER\\t' + config['provider'])
    print('BASE_URL_SHA\\t' + (config['base_url_sha256'] or hashlib.sha256(b'').hexdigest()))
except Exception as error:
    print('MODEL_ENV\\tinvalid')
    print('MODEL_CONFIG_ERROR\\t' + type(error).__name__)
"""

_ENDPOINT = """
import asyncio
from opencollab import OpenCollab

async def check(app):
    async with app.create_model_client() as client:
        await client.complete([{'role': 'user', 'content': 'ok'}], max_output_tokens=1)

try:
    app = OpenCollab(Path.cwd(), config={
        'llm_max_retries': 0,
        'llm_timeout': 90,
        'llm_first_event_timeout': 90,
        'llm_stream_idle_timeout': 90,
        'provider_error_time_budget': 0,
    })
    asyncio.run(check(app))
    print('ENDPOINT\\t200')
except Exception as error:
    status = getattr(error, 'status_code', None)
    if status is None:
        status = getattr(getattr(error, 'response', None), 'status_code', None)
    print('ENDPOINT\\t' + (str(status) if isinstance(status, int) else '000'))
    print('ENDPOINT_BODY\\t' + type(error).__name__)
"""


def probe_script(host: HostConfig, model_env: str, overrides: dict[str, str] | None, *, endpoint: bool) -> str:
    """Run the public OpenCollab configuration and model-client path on the host."""
    environment = runtime_env(host, model_env, overrides)
    assignments = " ".join(f"{key}={shlex.quote(value)}" for key, value in environment.items())
    status = "ENDPOINT" if endpoint else "MODEL_ENV"
    path_field = "ENDPOINT_ENV_PATH" if endpoint else "MODEL_ENV_PATH"
    code = _PATH_CHECK.replace("STATUS", status).replace("PATH_FIELD", path_field) + (
        _ENDPOINT if endpoint else _IDENTITY
    )
    return f"cd {shlex.quote(host.workdir)} && env {assignments} {shlex.quote(host.python)} -c {shlex.quote(code)}"
