import json
import os
from dataclasses import dataclass, field
from pathlib import Path

PKG_DIR = Path(__file__).resolve().parent
DEFAULT_AGENTS_FILE = PKG_DIR / "agents.json"

CAPABILITIES = ("planning", "design", "copy", "code", "debug")

# Capabilities to borrow from when no agent with the exact one has quota left.
DEFAULT_FALLBACKS = {"planning": ["code"], "design": ["code"], "copy": ["code"], "debug": ["code"]}


def load_dotenv(path):
    """Minimal .env reader; existing environment variables win."""
    path = Path(path)
    if not path.is_file():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


@dataclass
class Agent:
    name: str
    type: str
    model: str
    capabilities: list
    priority: int = 50
    base_url: str = None
    api_key_env: str = None
    local: bool = False
    enabled: bool = True
    max_output_tokens: int = 4096
    limits: dict = field(default_factory=dict)
    extra_body: dict = field(default_factory=dict)

    @property
    def api_key(self):
        return os.environ.get(self.api_key_env) if self.api_key_env else None

    def is_configured(self):
        if not self.enabled:
            return False
        if self.type == "mock" or not self.api_key_env:
            return True
        return bool(self.api_key)

    def limit(self, key):
        return self.limits.get(key)


def load_agents(path=DEFAULT_AGENTS_FILE):
    data = json.loads(Path(path).read_text())
    agents = []
    for raw in data["agents"]:
        raw = {k: v for k, v in raw.items() if not k.startswith("_")}
        unknown = set(raw["capabilities"]) - set(CAPABILITIES)
        if unknown:
            raise ValueError(f"agent {raw['name']}: unknown capabilities {sorted(unknown)}")
        agents.append(Agent(**raw))
    names = [a.name for a in agents]
    if len(names) != len(set(names)):
        raise ValueError("agent names must be unique")
    return agents, data.get("capability_fallbacks", DEFAULT_FALLBACKS)
