import json, os, re, sys, yaml
from pathlib import Path
repo = Path(sys.argv[1])
env = {}
if (repo / ".env").exists():
    for line in (repo / ".env").read_text().splitlines():
        key, sep, value = line.partition("=")
        if sep: env[key] = value.strip("\"'")
env.update(os.environ)
text = (repo / "docker-compose.yml").read_text()
text = re.sub(r"\$\{(\w+):-([^}]*)\}", lambda m: env.get(m[1]) or m[2], text)
print(json.dumps(yaml.safe_load(text)))
