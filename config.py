import os
from dotenv import load_dotenv

from remote_control.paths import resolve_config_path, resolve_state_path

load_dotenv()

FEISHU_APP_ID = os.environ["FEISHU_APP_ID"]
FEISHU_APP_SECRET = os.environ["FEISHU_APP_SECRET"]
FAR_CONFIG = str(resolve_config_path())
FAR_STATE = str(resolve_state_path())
