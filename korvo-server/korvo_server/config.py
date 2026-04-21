from pathlib import Path

PACKAGE_DIR = Path(__file__).resolve().parent
SERVER_ROOT = PACKAGE_DIR.parent
REPO_ROOT = SERVER_ROOT.parent
KORVO_IDF_SH = REPO_ROOT / "scripts" / "korvo-idf.sh"
SETUP_SH = REPO_ROOT / "setup.sh"
KORVO_APP = REPO_ROOT / "korvo-app"
DB_PATH = SERVER_ROOT / "korvo.db"
BIN_DIR = SERVER_ROOT / "bin"
RECORDINGS_DIR = SERVER_ROOT / "recordings"
DOWNLOADS_DIR = SERVER_ROOT / "downloads"
KOKORO_DIR = DOWNLOADS_DIR / "kokoro"
LOGS_DIR = SERVER_ROOT / "logs"
PORT = 3333
