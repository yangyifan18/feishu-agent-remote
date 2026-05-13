import os
from dotenv import load_dotenv

load_dotenv()

FEISHU_APP_ID = os.environ["FEISHU_APP_ID"]
FEISHU_APP_SECRET = os.environ["FEISHU_APP_SECRET"]
FAR_CONFIG = os.getenv("FAR_CONFIG") or os.getenv("YYF_CODEX_CONFIG", "~/.feishu-agent-remote/config.yaml")
FAR_STATE = os.getenv("FAR_STATE") or os.getenv("YYF_CODEX_STATE", "~/.feishu-agent-remote/state.sqlite")
